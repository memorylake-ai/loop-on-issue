#!/usr/bin/env python3
"""DingTalk Stream listener — transport only.

Everything that can be got wrong lives in `loopkit.listener`, which has no
dependencies and is tested exhaustively. This file connects a socket, translates
one payload shape, and sends a reply. Keep it that way: logic that lands here is
logic that stops being tested.

Stream mode rather than a webhook is not a preference. A custom group webhook can
only *send*; receiving requires this long connection, which in exchange needs no
public callback URL, no tunnel and no open port.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from loopkit import config as cfg  # noqa: E402
from loopkit import dingtalk as dt_mod  # noqa: E402
from loopkit import listener as listener_mod  # noqa: E402
from loopkit import pending, remotes  # noqa: E402
from loopkit.forge import for_repo  # noqa: E402

log = logging.getLogger("loop-bot")


def build_brain(env, repo_root, spawn=None):
    conf = cfg.load(repo_root)
    repo = remotes.detect(cwd=repo_root, forge=conf.forge, repo_path=conf.repo)
    forges = {}

    def forge_for(path):
        if path not in forges:
            target = repo if path == repo.path else remotes.Repo(repo.forge, repo.host, path)
            forges[path] = for_repo(target)
        return forges[path]

    return listener_mod.Brain(
        forge_for=forge_for,
        default_repo=repo.path,
        index=pending.Index(),
        conversations=dt_mod.conversations(env),
        approver=env.get("LOOP_DINGTALK_APPROVER") or "",
        approver_nick=env.get("LOOP_DINGTALK_APPROVER_NICK") or "",
        queue_label=conf.queue_label,
        intake_label=conf.intake_label,
        assignee=conf.assignee,
        creator_mode=conf.creator_mode,
        spawn_creator=spawn,
    ), conf, repo


def inbound_from(data: dict) -> listener_mod.Inbound:
    """Translate one DingTalk payload. The field names here are the contract."""
    text = ((data.get("text") or {}).get("content")) or data.get("content") or ""
    return listener_mod.Inbound(
        msg_id=data.get("msgId") or data.get("msgid") or "",
        text=text,
        sender_id=data.get("senderStaffId") or "",
        sender_nick=data.get("senderNick") or "",
        conversation_id=data.get("conversationId") or "",
        # The key that makes a quote-reply route exactly.
        pqk=data.get("originalProcessQueryKey") or None,
        session_webhook=data.get("sessionWebhook") or "",
    )


def reply(session_webhook: str, text: str) -> None:
    """Answer in the thread the message came from.

    The session webhook is short-lived and scoped to that conversation, which is
    why it is preferred over a configured one: it always lands where the person
    is looking.
    """
    if not session_webhook or not text:
        return
    body = json.dumps({"msgtype": "markdown", "markdown": {"title": "loop", "text": text}},
                      ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(session_webhook, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(request, timeout=15).read()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not reply into the conversation: %s", exc)


def make_spawn(repo_root: str, conf):
    """Run the creator on an approved intake issue, in the background.

    Only used when `creator_mode` is "immediate". The default leaves this to the
    next scheduled run, which is what keeps the listener stateless and therefore
    safe to restart at any moment.
    """
    def spawn(number: int) -> str:
        prompt = (
            "Use the loop-issue-creator skill. Decompose the requirement in issue "
            "#{n} of this repository into queue-ready issues, passing --epic {n} so "
            "each slice links back. The requirement text is the issue body; the "
            "approval and any approval note are in its comments and carry the same "
            "weight as the requirement itself. When the draft is ready, use "
            "`loop ask --id {n}` to get it confirmed before creating anything."
        ).format(n=number)
        cmd = ["claude", "-p", "--permission-mode", "acceptEdits", prompt]
        if conf.runner == "codex":
            cmd = ["codex", "exec", "--json", "--sandbox", "workspace-write",
                   "-c", 'approval_policy="never"', prompt]
        log_path = os.path.join(repo_root, ".loop-on-issue", "creator-{}.log".format(number))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "ab") as fh:
            subprocess.Popen(cmd, cwd=repo_root, stdout=fh, stderr=subprocess.STDOUT,
                             start_new_session=True)
        return "已在后台开始拆分（日志 `{}`）。".format(os.path.relpath(log_path, repo_root))

    return spawn


def run(env, repo_root):
    try:
        from dingtalk_stream import AckMessage, ChatbotMessage, Credential, DingTalkStreamClient
    except ImportError:
        print("dingtalk-stream is not installed. Run dingtalk/run-bot.sh, which "
              "manages its own virtualenv.", file=sys.stderr)
        return 1

    brain, conf, repo = build_brain(env, repo_root, spawn=make_spawn(repo_root, cfg.load(repo_root)))
    if conf.creator_mode != "immediate":
        brain.spawn_creator = None
    dedupe = listener_mod.Dedupe()

    if not brain.conversations:
        print("No conversations are allow-listed, so every message will be ignored.\n"
              "Send `@bot /whoami` in the group — it is the one command an unlisted "
              "conversation may run — and paste what it replies into "
              "LOOP_DINGTALK_CONVERSATIONS.", file=sys.stderr)

    class Handler(__import__("dingtalk_stream").ChatbotHandler):
        async def process(self, callback):
            data = callback.data or {}
            inbound = inbound_from(data)
            # staffId is logged because it is the value needed to configure the
            # approver, and the only place it is otherwise visible is a /whoami
            # reply the sender has to think to ask for.
            log.info("inbound conversation=%s sender=%s(%s) pqk=%s text=%r",
                     inbound.conversation_id, inbound.sender_nick, inbound.sender_id,
                     (inbound.pqk or "")[:12], inbound.text[:120])
            # At-least-once delivery: a reconnect redelivers the same id, and
            # acting twice on a bare reply answers the wrong question.
            if inbound.msg_id and dedupe.seen(inbound.msg_id):
                log.info("duplicate delivery %s ignored", inbound.msg_id)
                return AckMessage.STATUS_OK, "duplicate"
            try:
                answer = brain.handle(inbound)
            except Exception as exc:  # noqa: BLE001 - never let one message stop the loop
                log.exception("handler failed")
                answer = "出错了：{}".format(exc)
            if answer:
                reply(inbound.session_webhook, answer)
            return AckMessage.STATUS_OK, "OK"

    client = DingTalkStreamClient(
        Credential(env.get("DINGTALK_CLIENT_ID"), env.get("DINGTALK_CLIENT_SECRET"))
    )
    client.register_callback_handler(ChatbotMessage.TOPIC, Handler())
    log.info("connecting: repo=%s conversations=%s approver=%s creator_mode=%s",
             repo.path, brain.conversations or "(none)",
             brain.approver_nick, conf.creator_mode)
    client.start_forever()
    return 0


def simulate(env, repo_root, text, sender="sim-user", nick="模拟用户", conversation=None) -> int:
    """Run one message through the real pipeline without a group.

    Everything except the DingTalk transport is exercised: dispatch, permissions,
    the forge. Useful for checking a deployment before anyone is watching, and for
    reproducing a misbehaving message afterwards.

    It is not a dry run — a command that writes to the board writes to it.
    """
    brain, _, _ = build_brain(env, repo_root)
    conversations = dt_mod.conversations(env)
    inbound = listener_mod.Inbound(
        msg_id="sim-{}".format(int(time.time() * 1000)),
        text=text,
        sender_id=sender,
        sender_nick=nick,
        conversation_id=conversation or (conversations[0] if conversations else "sim-conversation"),
    )
    answer = brain.handle(inbound)
    print(answer or "(ignored — the conversation is not allow-listed)")
    return 0


def selftest(env, repo_root) -> int:
    def line(ok, label, detail=""):
        print("{} {:<32} {}".format("✓" if ok else "✗", label, detail))
        return 0 if ok else 1

    bad = 0
    client = dt_mod.DingTalk(env)
    bad += line(bool(env.get("DINGTALK_CLIENT_ID")), "DINGTALK_CLIENT_ID",
                env.get("DINGTALK_CLIENT_ID", ""))
    bad += line(bool(env.get("DINGTALK_CLIENT_SECRET")), "DINGTALK_CLIENT_SECRET", "(set)"
                if env.get("DINGTALK_CLIENT_SECRET") else "missing")
    conv = dt_mod.conversations(env)
    bad += line(bool(conv), "conversation allow-list",
                ", ".join(conv) or "empty — every message will be ignored")
    bad += line(bool(env.get("LOOP_DINGTALK_APPROVER")), "approver staffId",
                env.get("LOOP_DINGTALK_APPROVER") or "unset — nobody can approve intake")
    try:
        import dingtalk_stream  # noqa: F401
        bad += line(True, "dingtalk-stream", "importable")
    except ImportError:
        bad += line(False, "dingtalk-stream", "not installed (run-bot.sh installs it)")
    try:
        _, conf, repo = build_brain(env, repo_root)
        bad += line(True, "repository", "{} · {}".format(repo.forge, repo.path))
        bad += line(bool(conf.assignee), "assignee", conf.assignee or "unset")
    except Exception as exc:  # noqa: BLE001
        bad += line(False, "repository", str(exc))
    if client.configured:
        try:
            client.access_token()
            bad += line(True, "DingTalk credentials", "access token obtained")
        except Exception as exc:  # noqa: BLE001
            bad += line(False, "DingTalk credentials", str(exc))
    return 2 if bad else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--simulate", metavar="TEXT",
                        help="run one message through the real pipeline, no group needed")
    parser.add_argument("--as", dest="sender", default="sim-user", help="sender staffId to simulate")
    parser.add_argument("--nick", default="模拟用户")
    parser.add_argument("--conversation", help="conversationId to simulate")
    parser.add_argument("--repo-root", default=os.getcwd())
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env = dt_mod.load_env()
    root = os.path.abspath(args.repo_root)
    if args.selftest:
        return selftest(env, root)
    if args.simulate:
        return simulate(env, root, args.simulate, args.sender, args.nick, args.conversation)
    return run(env, root)


if __name__ == "__main__":
    sys.exit(main())
