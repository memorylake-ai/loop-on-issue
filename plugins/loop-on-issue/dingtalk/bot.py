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

#: How long a chat-raised job waits on a question before giving up on it.
#: Generous on purpose: the person who raised the requirement is in the
#: conversation the card lands in, and a job that guesses instead of waiting three
#: minutes produces issues nobody agreed to.
CHAT_ASK_WAIT = 300

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from loopkit import config as cfg  # noqa: E402
from loopkit import dingtalk as dt_mod  # noqa: E402
from loopkit import failures as failures_mod  # noqa: E402
from loopkit import intake as intake_mod  # noqa: E402
from loopkit import listener as listener_mod  # noqa: E402
from loopkit import pending, remotes  # noqa: E402
from loopkit import repos as repos_mod  # noqa: E402
from loopkit import state as state_mod  # noqa: E402
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
    """Runs approved work: several jobs at once, one per repository.

    Fully serial was the first design, for a good reason that turned out to be
    narrower than the rule built on it: two agents in the *same checkout* fight
    over git state. Two in different checkouts do not, and a single worker meant
    one slow job stalled every other repository behind it — with a two-hour
    timeout, for two hours.

    So concurrency is bounded globally and serialised per repository. A worker
    that finds a repository busy puts the job back and takes another rather than
    blocking on the lock, or the parallelism would be lost to whichever job
    happened to arrive first.

    Jobs are durable in the intake store, so a listener that dies mid-queue picks
    up where it left off — and a job whose process is gone is not "running" any
    more, however its record reads.
    """

    def __init__(self, store, registry, notify, timeout=1800, workers=2, max_retries=3):
        import threading

        self.store = store
        self.registry = registry
        self.notify = notify
        self.timeout = timeout
        self.workers = max(1, workers)
        self.max_retries = max(0, max_retries)
        self.queue = __import__("queue").Queue()
        self._threads = []
        self._repo_locks = {}
        self._locks_guard = threading.Lock()
        self._processes = {}

    def _repo_lock(self, repo):
        import threading

        with self._locks_guard:
            if repo not in self._repo_locks:
                self._repo_locks[repo] = threading.Lock()
            return self._repo_locks[repo]

    def start(self):
        import threading

        self._recover()
        threading.Thread(target=self._waker, daemon=True, name="loop-waker").start()
        for index in range(self.workers):
            thread = threading.Thread(target=self._worker, daemon=True,
                                      name="loop-worker-{}".format(index + 1))
            thread.start()
            self._threads.append(thread)
        log.info("executor started with %d worker(s), %ds timeout",
                 self.workers, self.timeout)

    def _waker(self):
        """Put deferred jobs back when their backoff elapses.

        A separate thread rather than a sleeping worker: a worker asleep on a
        backoff is a slot nobody else can use, and an outage tends to defer
        several jobs at once.
        """
        import time as _time

        while True:
            try:
                for request in self.store.due():
                    log.info("%s backoff elapsed, retrying (transport fault #%d)",
                             request.id, request.transient_failures)
                    request.status = intake_mod.APPROVED
                    self.store.save(request)
                    self.queue.put(request.id)
            except Exception:  # noqa: BLE001 - never let the waker die
                log.exception("waker failed")
            _time.sleep(15)

    def _recover(self):
        """Requeue work that was approved or interrupted.

        A record left at `running` belonged to a process this listener no longer
        has; whether it died with the last listener or was killed, it is not
        running now. Re-queueing is right, and leaving it labelled `running`
        would make it invisible to every later recovery.
        """
        for request in self.store.by_status(intake_mod.APPROVED, intake_mod.RUNNING,
                                            intake_mod.WAITING):
            if request.status == intake_mod.WAITING and not request.due():
                # Its backoff outlived the listener; the waker will get it.
                continue
            if request.status == intake_mod.RUNNING:
                if _alive(request.pid):
                    log.warning("%s is still running as pid %s elsewhere; leaving it",
                                request.id, request.pid)
                    continue
                log.info("recovering %s (was running, process gone)", request.id)
                request.status = intake_mod.APPROVED
                request.pid = 0
                self.store.save(request)
            else:
                log.info("recovering %s (%s)", request.id, request.status)
            self.queue.put(request.id)

    def submit(self, request):
        self.queue.put(request.id)

    def _notify(self, text, conversation=None):
        try:
            self.notify(text, conversation)
        except TypeError:
            self.notify(text)

    def cancel(self, request_id, by=""):
        """Stop a job and free the worker holding it."""
        request = self.store.get(request_id)
        if request is None:
            return False, "找不到 {}".format(request_id)
        if request.status not in intake_mod.OPEN:
            return False, "{} 已经是 {}，没有在办".format(request_id, request.status)
        killed = _kill(request.pid)
        request.cancel(by=by)
        self.store.save(request)
        return True, ("已停掉 {}{}".format(request_id, "（进程已杀）" if killed else ""))

    def _worker(self):
        while True:
            request_id = self.queue.get()
            try:
                self._run(request_id)
            except Exception:  # noqa: BLE001 - one bad job must not stop the queue
                log.exception("job %s failed", request_id)

    def _run(self, request_id):
        request = self.store.get(request_id)
        if not request or request.status not in (intake_mod.APPROVED, intake_mod.RUNNING,
                                                 intake_mod.WAITING):
            return

        lock = self._repo_lock(request.repo)
        if not lock.acquire(blocking=False):
            # Another job holds this checkout. Put it back and take something
            # else; blocking here would spend a worker waiting.
            log.info("%s waiting for %s", request.id, request.repo)
            time.sleep(2)
            self.queue.put(request_id)
            return
        try:
            self._run_locked(request)
        finally:
            lock.release()

    def _run_locked(self, request):
        entry = self.registry.get(request.repo)
        if not entry or not os.path.isdir(entry.path):
            request.fail("no local checkout registered for {}".format(request.repo))
            self.store.save(request)
            self.notify(self._failure_text(request), request.conversation)
            return

        # Derive the session before starting, so `claude --resume <id>` can get
        # into exactly what this agent saw. Without it a job that goes wrong is
        # only inspectable as a log, and only by guessing.
        resuming = request.resuming
        session = request.session or runner_mod.intake_session_id(request.id)
        request.start(session=session)
        self.store.save(request)
        log.info("running %s (%s) in %s session=%s",
                 request.id, request.kind, entry.path, session)

        prompt = (self._retry_prompt(request) if resuming else self._prompt(request, entry))
        log_path = self.store.log_for(request.id)
        repo_conf = cfg.load(entry.path)

        env = dict(os.environ, LOOP_INTAKE=request.id)
        env.pop("LOOP_ISSUE", None)
        if request.kind == intake_mod.DEVELOP and request.issue:
            # LOOP_INTAKE alone reaches the request; a development job also has an
            # issue, and questions about the code belong on it.
            env["LOOP_ISSUE"] = str(request.issue)
        # Read by the hook *and* by an explicit `loop ask`. Without it the agent is
        # told to confirm its reading with a human, gets an instant "nobody
        # answered", and proceeds on its own guess — the question was asked and
        # never waited for.
        env["LOOP_ASK_WAIT"] = str(repo_conf.ask_wait or CHAT_ASK_WAIT)

        command = (runner_mod.resume_command("claude", session, prompt, model=repo_conf.agent_model)
                   if resuming
                   else runner_mod.start_command("claude", session, prompt, model=repo_conf.agent_model))

        with open(log_path, "ab") as fh:
            fh.write("\n$ {}\n".format(request.kind).encode("utf-8"))
            try:
                # Popen rather than run, so the pid is recorded before the wait: a
                # job nobody can name the process of is a job nobody can stop.
                proc = subprocess.Popen(
                    command, cwd=entry.path, stdout=fh, stderr=subprocess.STDOUT,
                    env=env, start_new_session=True,
                )
            except OSError as exc:
                request.fail(str(exc))
                self.store.save(request)
                self._release_issue(request, entry)
                self.notify(self._failure_text(request), request.conversation)
                return
            request.pid = proc.pid
            self.store.save(request)
            self._processes[request.id] = proc
            try:
                code = proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                _kill(proc.pid)
                request.fail("timed out after {}s and was stopped".format(self.timeout))
                self.store.save(request)
                self._release_issue(request, entry)
                self.notify(self._failure_text(request), request.conversation)
                return
            finally:
                self._processes.pop(request.id, None)

        # Cancelled while it ran: the record already says so, and overwriting it
        # with a result would erase the fact that a human stopped it.
        current = self.store.get(request.id)
        if current and current.status == intake_mod.CANCELLED:
            return
        request = current or request


        result = _read_text(self.store.result_for(request.id))
        # Ask the board what exists, rather than believing the report. An agent
        # that could not run the CLI still writes a confident-sounding summary.
        issues = self._issues_filed(request, entry) or _issue_urls(result)

        if intake_mod.produced_nothing(request.kind, issues, result):
            # Ask what kind of nothing. A busy API and an agent that thought and
            # built nothing look identical from here, and want opposite responses.
            tail = _read_tail(log_path)
            kind = failures_mod.classify(tail, code)
            if failures_mod.should_retry(kind, request.transient_failures, self.max_retries):
                delay = failures_mod.backoff(request.transient_failures + 1)
                request.defer(_first_fault_line(tail) or "transport fault", delay)
                self.store.save(request)
                log.info("%s deferred %ds after a transport fault", request.id, delay)
                return
            reason = (
                "gave up after {} transport fault(s); the last was: {}".format(
                    request.transient_failures, _first_fault_line(tail))
                if request.transient_failures else
                "agent exited {} but produced nothing — no issues on the board and "
                "no report. Inspect with `claude --resume {}`, or read {}".format(
                    code, request.session, log_path)
            )
            request.fail(reason)
            self.store.save(request)
            self._release_issue(request, entry)
            self.notify(self._failure_text(request), request.conversation)
            return

        request.finish(issues=issues)
        self.store.save(request)
        self.notify(self._success_text(request, result), request.conversation)

    def _release_issue(self, request, entry):
        """Hand a failed development job's issue back to a human.

        The session that claimed it is gone, so the issue is left mid-flight with
        nothing to finish it and nothing that will notice. PAUSED rather than
        unclaimed, because a job that failed is a fact somebody should read before
        the next run picks the issue up and repeats it.
        """
        if request.kind != intake_mod.DEVELOP or not request.issue:
            return
        try:
            conf = cfg.load(entry.path)
            forge = for_repo(remotes.detect(cwd=entry.path, forge=conf.forge, repo_path=entry.repo))
            issue = forge.get_issue(request.issue)
            current, base = state_mod.split_state(issue.title)
            if current in (None, "PAUSED", "FINISHED", "SKIP"):
                return
            forge.add_issue_comment(request.issue, state_mod.stamp(
                "The session working this stopped without submitting anything "
                "(`{}`): {}\n\nLeft at PAUSED. Its log is `{}`, and "
                "`claude --resume {}` opens what it saw.".format(
                    request.id, request.error, self.store.log_for(request.id),
                    request.session)))
            forge.set_issue_title(request.issue, state_mod.compose("PAUSED", base))
            log.info("released issue #%s back to PAUSED", request.issue)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not release issue #%s: %s", request.issue, exc)

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

    def _retry_prompt(self, request):
        """Continue an attempt that did not finish, rather than starting over.

        Deliberately short: the session already holds the requirement, whatever
        reading it did, and whatever it drafted. Restating the brief invites it to
        begin again and reach a different answer.
        """
        return (
            "That attempt ended without producing anything, so this is a retry of the "
            "same job in the same session.\n\n"
            "Whatever blocked you before has been addressed: you can run `loop`, `git` "
            "and `gh`.\n\n"
            "Continue from where you stopped. Finish the job — actually create the "
            "issues — and write the report to `{result}`. If you are still blocked, say "
            "exactly what by, and stop rather than describing what you would have done."
        ).format(result=self.store.result_for(request.id))

    def _prompt(self, request, entry):
        if request.kind == intake_mod.DEVELOP:
            return (
                "**You are the development session for issue #{n} of {repo}.** Do the work "
                "yourself, here, in this checkout. Do not spawn another agent, do not run "
                "`claude -p`, and do not background anything and report back — there is nobody "
                "watching for it, and the job ends when you do.\n\n"
                "Read the `loop-issue-swarm` skill for *how a session works* — its "
                "per-issue brief: plan, code, verify with the repository's own command, get "
                "fresh eyes on the diff, submit. Ignore the parts about scanning a queue, "
                "claiming issues and starting sessions; that has already happened and you "
                "are the result of it.\n\n"
                "Its safety boundaries all still hold, in particular: never merge, and "
                "never close the issue.\n\n"
                "If you hit something only a human can settle, use `AskUserQuestion` — it "
                "is intercepted and relayed. If nobody answers, stop and say what you are "
                "waiting on rather than guessing.\n\n"
                "When you are done, write a short report to `{result}` — what you did, the "
                "change request URL, and anything a human still has to decide. That file is "
                "what gets sent back to the person who asked, so make it readable on its "
                "own; an empty file means the job produced nothing."
            ).format(n=request.issue, repo=entry.repo, result=self.store.result_for(request.id))

        return (
            "Use the loop-issue-creator skill to decompose the requirement below into "
            "queue-ready issues in {repo}.\n\n"
            "The requirement, verbatim — this is the source of scope, and nothing outside "
            "it gets built:\n\n{text}\n\n"
            "Raised by: {who}\n{note}"
            "Ground every slice in code you actually read, and draft before you create.\n\n"
            "Nobody is at a keyboard, but you can still ask: `AskUserQuestion` is "
            "intercepted and relayed to a human, so use it for anything where guessing "
            "would decide the shape of the work. Give it real options. If nobody answers, "
            "stop — do not file issues on an assumption you were unsure enough to ask "
            "about.\n\n"
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


def _alive(pid):
    """Is that process still there?

    A record saying `running` proves only what was true when it was written.
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def _kill(pid):
    """Stop a job's process group, politely then not.

    The group, not the process: an agent spawns children — a build, a test run —
    and killing only the parent leaves those holding the checkout.
    """
    if not _alive(pid):
        return False
    import signal

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(int(pid)), sig)
        except OSError:
            try:
                os.kill(int(pid), sig)
            except OSError:
                return True
        time.sleep(1.5)
        if not _alive(pid):
            return True
    return not _alive(pid)


def _read_tail(path, lines=60):
    text = _read_text(path)
    return "\n".join(text.splitlines()[-lines:])


def _first_fault_line(tail):
    """The line that actually says what went wrong, for a human to read."""
    import re as _re

    for line in reversed((tail or "").splitlines()):
        if _re.search(r"error|overloaded|ECONN|ETIMEDOUT|rate.?limit", line, _re.IGNORECASE):
            return line.strip()[:160]
    return ""


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
        conversation_type=str(data.get("conversationType") or ""),
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
        allow_conversation=lambda cid, private: dt_mod.allow_conversation(
            dt_mod.default_env_paths()[0], cid, private),
        deny_conversation=lambda cid: dt_mod.deny_conversation(
            dt_mod.default_env_paths()[0], cid),
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

    def announce(text, conversation=None):
        """Say something unprompted, where the work came from.

        Used when a job finishes: whoever asked stopped watching long ago, and a
        result delivered to a different conversation than the request reaches
        nobody who was waiting for it.
        """
        try:
            client_out.send("loop", text, conversation_id=conversation)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not announce: %s", exc)

    brain, registry, store, conf = make_brain(env, repo_root)
    executor = Executor(store, registry, announce,
                        timeout=conf.job_timeout, workers=conf.max_parallel_jobs,
                        max_retries=conf.max_retries)
    brain.cancel_job = executor.cancel
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
            # Re-read the allow-list per message: it is a small file, and a
            # conversation added with /allow has to work in the next breath rather
            # than after somebody restarts the listener.
            brain.conversations = dt_mod.conversations(dt_mod.load_env())
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
