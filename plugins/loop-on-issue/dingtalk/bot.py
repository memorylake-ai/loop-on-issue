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
import shutil
import subprocess
import time
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from loopkit import config as cfg  # noqa: E402
from loopkit import dingtalk as dt_mod  # noqa: E402
from loopkit import intake as intake_mod  # noqa: E402
from loopkit import listener as listener_mod  # noqa: E402
from loopkit import pending, remotes  # noqa: E402
from loopkit import repos as repos_mod  # noqa: E402
from loopkit import runner as runner_mod  # noqa: E402
from loopkit.forge import for_repo  # noqa: E402
from loopkit.models import Repo  # noqa: E402

log = logging.getLogger("loop-bot")


def load_registry(repo_root: str) -> repos_mod.Registry:
    """Which repositories this bot serves.

    The registry is machine-level, because a bot taking requirements in chat has
    to know about several before it knows which one a request belongs to. With no
    registry at all it falls back to the checkout it was started in, so a
    single-repo setup needs no configuration.
    """
    registry = repos_mod.Registry.load()
    if registry.names():
        return registry
    conf = cfg.load(repo_root)
    repo = remotes.detect(cwd=repo_root, forge=conf.forge, repo_path=conf.repo)
    registry.add(repo.name, repo.path, repo_root)
    registry.set_default(repo.name)
    return registry


def forge_factory(registry: repos_mod.Registry):
    """Resolve a project path to a forge client, caching per repository."""
    cache = {}

    def forge_for(path: str):
        if path not in cache:
            entry = registry.get(path)
            root = entry.path if entry else os.getcwd()
            conf = cfg.load(root)
            cache[path] = for_repo(
                remotes.detect(cwd=root, forge=conf.forge, repo_path=path or conf.repo)
            )
        return cache[path]

    return forge_for


class Executor:
    """Runs approved work, one job at a time.

    Serial on purpose. Two agents in the same checkout fight over git state, and
    two anywhere compete for the same quota; decomposing a requirement is not
    urgent enough to be worth either. Jobs are durable in the intake store, so a
    listener that dies mid-queue picks up where it left off.
    """

    def __init__(self, store, registry, notify, timeout=7200):
        self.store = store
        self.registry = registry
        self.notify = notify
        self.timeout = timeout
        self.queue = __import__("queue").Queue()
        self._thread = None

    def start(self):
        import threading

        # Anything approved but never run — a crash, a restart — goes back in.
        for request in self.store.by_status(intake_mod.APPROVED, intake_mod.RUNNING):
            log.info("recovering %s (%s)", request.id, request.status)
            self.queue.put(request.id)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def submit(self, request):
        self.queue.put(request.id)

    def _worker(self):
        while True:
            request_id = self.queue.get()
            try:
                self._run(request_id)
            except Exception:  # noqa: BLE001 - one bad job must not stop the queue
                log.exception("job %s failed", request_id)

    def _run(self, request_id):
        request = self.store.get(request_id)
        if not request or request.status not in (intake_mod.APPROVED, intake_mod.RUNNING):
            return
        entry = self.registry.get(request.repo)
        if not entry or not os.path.isdir(entry.path):
            request.fail("no local checkout registered for {}".format(request.repo))
            self.store.save(request)
            self.notify(self._failure_text(request))
            return

        # Derive the session before starting, so `claude --resume <id>` can get
        # into exactly what this agent saw. Without it a job that goes wrong is
        # only inspectable as a log, and only by guessing.
        session = runner_mod.intake_session_id(request.id)
        request.start(session=session)
        self.store.save(request)
        log.info("running %s (%s) in %s session=%s",
                 request.id, request.kind, entry.path, session)

        prompt = self._prompt(request, entry)
        log_path = self.store.log_for(request.id)
        with open(log_path, "ab") as fh:
            fh.write("\n$ {}\n".format(request.kind).encode("utf-8"))
            try:
                proc = subprocess.run(
                    runner_mod.start_command("claude", session, prompt),
                    cwd=entry.path, stdout=fh, stderr=subprocess.STDOUT,
                    timeout=self.timeout,
                )
                code = proc.returncode
            except subprocess.TimeoutExpired:
                request.fail("timed out after {}s".format(self.timeout))
                self.store.save(request)
                self.notify(self._failure_text(request))
                return
            except OSError as exc:
                request.fail(str(exc))
                self.store.save(request)
                self.notify(self._failure_text(request))
                return

        result = _read_text(self.store.result_for(request.id))
        # Ask the board what exists, rather than believing the report. An agent
        # that could not run the CLI still writes a confident-sounding summary.
        issues = self._issues_filed(request, entry) or _issue_urls(result)

        if intake_mod.produced_nothing(request.kind, issues, result):
            request.fail(
                "agent exited {} but produced nothing — no issues on the board and "
                "no report. Inspect with `claude --resume {}`, or read {}".format(
                    code, request.session, log_path))
            self.store.save(request)
            self.notify(self._failure_text(request))
            return

        request.finish(issues=issues)
        self.store.save(request)
        self.notify(self._success_text(request, result))

    def _issues_filed(self, request, entry):
        """Issues carrying the queue label that appeared while this job ran.

        The only trustworthy answer to "did it do the thing", and cheap: one
        listing against the repository it was pointed at.
        """
        if request.kind != intake_mod.REQUIREMENT:
            return []
        try:
            conf = cfg.load(entry.path)
            forge = for_repo(remotes.detect(cwd=entry.path, forge=conf.forge, repo_path=entry.repo))
            issues = forge.list_issues(label=conf.queue_label, state="all")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not verify what %s filed: %s", request.id, exc)
            return []
        started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(request.started_at - 60))
        return [i.url for i in issues if (i.created_at or "") >= started]

    def _prompt(self, request, entry):
        if request.kind == intake_mod.DEVELOP:
            return (
                "Use the loop-issue-swarm skill, but work **only** issue #{n} of {repo} "
                "and nothing else — do not scan or claim anything from the queue.\n\n"
                "Take it through the skill's normal cycle: triage, worktree, plan, code, "
                "review, and submit the change request. Honour every safety boundary the "
                "skill states, in particular: never merge and never close the issue.\n\n"
                "When you are done, write a short report to `{result}` — what you did, the "
                "change request URL, and anything a human still has to decide. That file is "
                "what gets sent back to the person who asked, so make it readable on its "
                "own."
            ).format(n=request.issue, repo=entry.repo, result=self.store.result_for(request.id))

        return (
            "Use the loop-issue-creator skill to decompose the requirement below into "
            "queue-ready issues in {repo}.\n\n"
            "The requirement, verbatim — this is the source of scope, and nothing outside "
            "it gets built:\n\n{text}\n\n"
            "Raised by: {who}\n{note}"
            "Ground every slice in code you actually read, and draft before you create. "
            "Nobody is at a keyboard: confirm the draft with `loop ask` and give it real "
            "options, or if the requirement is unambiguous enough, proceed and say in the "
            "report that you did.\n\n"
            "When you are done, write a short report to `{result}` listing each issue you "
            "created with its URL and one line on what it covers, plus anything you "
            "deliberately did not file. That file is what gets sent back to the person who "
            "asked, so make it readable on its own."
        ).format(
            repo=entry.repo,
            text="\n".join("> " + line for line in request.text.splitlines()),
            who=request.requester or "unknown",
            note=("Approval note, which carries the same weight as the requirement "
                  "itself: {}\n\n".format(request.approval_note)) if request.approval_note else "",
            result=self.store.result_for(request.id),
        )

    def _success_text(self, request, result):
        head = "✅ **{}** 完成（{}）".format(
            request.id, "开发 #{}".format(request.issue) if request.issue else "需求拆分")
        parts = [head]
        if request.issues:
            parts.append(listener_mod.bullets(*request.issues))
        if result.strip():
            parts.append(result.strip()[:1500])
        return listener_mod.md(*parts)

    def _failure_text(self, request):
        return listener_mod.md(
            "❌ **{}** 失败".format(request.id),
            request.error,
            listener_mod.bullets(
                "日志：`{}`".format(self.store.log_for(request.id)),
                "看它当时怎么想的：`claude --resume {}`".format(request.session or "—"),
            ),
        )


def _read_text(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _issue_urls(text):
    import re as _re

    return _re.findall(r"https?://\S+?/(?:issues|-/issues)/\d+", text or "")


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


def make_brain(env, repo_root, enqueue=None):
    registry = load_registry(repo_root)
    conf = cfg.load(repo_root)
    store = intake_mod.Store()
    brain = listener_mod.Brain(
        forge_for=forge_factory(registry),
        registry=registry,
        index=pending.Index(),
        store=store,
        conversations=dt_mod.conversations(env),
        approver=env.get("LOOP_DINGTALK_APPROVER") or "",
        approver_nick=env.get("LOOP_DINGTALK_APPROVER_NICK") or "",
        queue_label=conf.queue_label,
        assignee=conf.assignee,
        enqueue=enqueue,
    )
    return brain, registry, store, conf


def run(env, repo_root):
    try:
        from dingtalk_stream import AckMessage, ChatbotMessage, Credential, DingTalkStreamClient
    except ImportError:
        print("dingtalk-stream is not installed. Run dingtalk/run-bot.sh, which "
              "manages its own virtualenv.", file=sys.stderr)
        return 1

    client_out = dt_mod.DingTalk(env)

    def announce(text):
        """Say something into the configured conversation, unprompted.

        Used when a job finishes: whoever asked has long since stopped watching
        the thread their request came from.
        """
        try:
            client_out.send("loop", text)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not announce: %s", exc)

    brain, registry, store, conf = make_brain(env, repo_root)
    executor = Executor(store, registry, announce)
    brain.enqueue = executor.submit
    executor.start()

    expired = store.expire_stale(conf.intake_ttl)
    if expired:
        log.info("expired %d request(s) nobody decided on", len(expired))

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
            # acting twice on an approval would start the same job twice.
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

    stream = DingTalkStreamClient(
        Credential(env.get("DINGTALK_CLIENT_ID"), env.get("DINGTALK_CLIENT_SECRET"))
    )
    stream.register_callback_handler(ChatbotMessage.TOPIC, Handler())
    log.info("connecting: repos=%s default=%s conversations=%s approver=%s",
             registry.names(), registry.default and registry.default.repo,
             brain.conversations or "(none)", brain.approver_nick)
    stream.start_forever()
    return 0


def simulate(env, repo_root, text, sender="sim-user", nick="模拟用户", conversation=None) -> int:
    """Run one message through the real pipeline without a group.

    Everything except the DingTalk transport is exercised: dispatch, permissions,
    the forge. Useful for checking a deployment before anyone is watching, and for
    reproducing a misbehaving message afterwards.

    It is not a dry run — a command that writes to the board writes to it.
    """
    brain, _, _, _ = make_brain(env, repo_root)
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
        registry = load_registry(repo_root)
        bad += line(bool(registry.names()), "repositories",
                    ", ".join("{}→{}".format(e.name, e.repo) for e in registry.all()) or "none")
        default = registry.default
        bad += line(default is not None, "default repository",
                    default.repo if default else "unset — a bare requirement cannot be routed")
        for entry in registry.all():
            bad += line(os.path.isdir(entry.path), "checkout {}".format(entry.name), entry.path)
        conf = cfg.load(repo_root)
        bad += line(bool(conf.assignee), "assignee", conf.assignee or "unset")
    except Exception as exc:  # noqa: BLE001
        bad += line(False, "repositories", str(exc))
    store = intake_mod.Store()
    waiting = store.by_status(intake_mod.PENDING)
    print("· {:<32} {}".format("requests awaiting approval", len(waiting)))
    bad += line(bool(shutil.which("claude")), "claude CLI",
                shutil.which("claude") or "not on PATH — approved work cannot run")
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
