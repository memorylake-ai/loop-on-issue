"""Requirements raised in chat, held locally until somebody approves them.

An earlier design filed the requirement straight onto the forge as an unqueued
issue, which made the board its own state machine. That was wrong for one plain
reason: **anybody who can message the bot could then write to the repository.**
An issue tracker that fills with unapproved one-liners stops being readable, and
nothing about approval requires a durable public record *before* the decision.

So an unapproved requirement lives here — outside any repository, outside version
control — together with the agent log and output from decomposing it. Once
approved, a decomposition agent runs and *its* issues are what reach the forge.

Machine-level rather than per-repository, because at the moment a request arrives
nobody has yet decided which repository it belongs to.
"""

from __future__ import annotations

import glob
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".loop-on-issue", "intake")

PENDING = "pending"
APPROVED = "approved"
RUNNING = "running"
DONE = "done"
REJECTED = "rejected"
FAILED = "failed"
EXPIRED = "expired"
CANCELLED = "cancelled"
#: Hit a transport fault and is waiting out a backoff. Still somebody's work —
#: it just has nothing to do this minute.
WAITING = "waiting"

#: States that still hold a claim on somebody's attention.
OPEN = (PENDING, APPROVED, WAITING, RUNNING)

#: What an approved request makes an agent do. Both are "run one `claude -p` in a
#: checkout and report what came of it", which is why they share this store, its
#: log directory and the one serial worker that drains it.
REQUIREMENT = "requirement"   # decompose a requirement into queue-ready issues
DEVELOP = "develop"           # take one existing issue through to a change request
KINDS = (REQUIREMENT, DEVELOP)


class NotPending(Exception):
    """This request has already been decided."""


@dataclass
class Request:
    id: str
    text: str
    kind: str = REQUIREMENT
    issue: Optional[int] = None
    requester: str = ""
    requester_id: str = ""
    conversation: str = ""
    repo: str = ""
    status: str = PENDING
    created_at: float = field(default_factory=time.time)
    approved_by: str = ""
    approved_at: str = ""
    approval_note: str = ""
    auto_approved: bool = False
    rejected_reason: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    issues: List[str] = field(default_factory=list)
    error: str = ""
    #: The `claude --resume` id for the agent that ran this, derived from the
    #: request id so it can be recomputed rather than remembered.
    session: str = ""
    #: How many times it has been started. A derived id can only be *created*
    #: once — a second start collides with the session the first one left behind
    #: — so anything past the first has to resume instead.
    attempts: int = 0
    #: The agent process, while one is running. Recorded so a job that stops
    #: making progress can actually be stopped — a status field alone lets you
    #: relabel a stuck job without freeing the worker it is holding.
    pid: int = 0
    #: When a deferred job becomes runnable again, and how many transport faults
    #: it has survived. Counted separately from `attempts`, because a retry after
    #: an outage is not the same kind of event as a retry after a real failure and
    #: should not spend the same budget.
    retry_at: float = 0.0
    transient_failures: int = 0
    #: Questions the agent put to a human while working, with their answers.
    #: A decomposition has no issue to hold them, and it is the job that most
    #: needs to ask: most ambiguity, least to check a reading against.
    questions: List[Dict[str, Any]] = field(default_factory=list)
    session: str = ""

    # -- transitions ---------------------------------------------------------
    def _require_pending(self) -> None:
        if self.status != PENDING:
            raise NotPending("{} is already {}".format(self.id, self.status))

    def approve(self, by: str, note: str = "", repo: Optional[str] = None,
                auto: bool = False, at: Optional[str] = None) -> "Request":
        """Release a request for decomposition.

        The note is kept beside the request because it narrows scope as surely as
        the request itself did — "同意 R… 注意别动定价页" is part of the requirement
        from that moment on, and leaving it in a chat log loses it.
        """
        self._require_pending()
        self.status = APPROVED
        self.approved_by = by
        self.approved_at = at or time.strftime("%Y-%m-%dT%H:%M:%S")
        self.approval_note = note or ""
        self.auto_approved = bool(auto)
        if repo:
            self.repo = repo
        return self

    def reject(self, by: str, reason: str) -> "Request":
        self._require_pending()
        self.status = REJECTED
        self.approved_by = by
        self.rejected_reason = reason
        return self

    def start(self, session: str = "", pid: int = 0) -> "Request":
        self.status = RUNNING
        self.started_at = time.time()
        self.attempts += 1
        self.pid = pid
        if session:
            self.session = session
        return self

    def defer(self, reason: str, seconds: int) -> "Request":
        """Stand down for a while after a transport fault.

        Status, not silence: `/p` shows it as waiting, so a queue that looks idle
        during an outage can be told apart from one that is idle.
        """
        self.status = WAITING
        self.transient_failures += 1
        self.retry_at = time.time() + seconds
        self.error = reason
        self.pid = 0
        return self

    def due(self, now: Optional[float] = None) -> bool:
        return self.status == WAITING and (now if now is not None else time.time()) >= self.retry_at

    def cancel(self, by: str = "") -> "Request":
        self.status = CANCELLED
        self.finished_at = time.time()
        self.error = "cancelled{}".format(" by " + by if by else "")
        self.pid = 0
        return self

    @property
    def running_for(self) -> float:
        return (time.time() - self.started_at) if self.status == RUNNING and self.started_at else 0.0

    @property
    def resuming(self) -> bool:
        """Has this already been attempted, so the session exists to continue?

        Resuming rather than restarting is not only about the id collision: the
        earlier attempt did the reading. Throwing that away makes a retry pay for
        recon twice and invites it to reach a different conclusion.
        """
        return self.attempts > 0 and bool(self.session)

    def finish(self, issues: Optional[List[str]] = None) -> "Request":
        """Record a job that actually produced something.

        Callers must establish that first — see `produced_nothing`. An agent that
        exits cleanly having created nothing is a failure, and recording it as
        success is the same mistake as reporting green from a test run that never
        ran.
        """
        self.status = DONE
        self.finished_at = time.time()
        self.issues = list(issues or [])
        return self

    def fail(self, error: str) -> "Request":
        self.status = FAILED
        self.finished_at = time.time()
        self.error = error
        return self

    # -- asking a human ------------------------------------------------------
    def ask(self, text: str, options: Optional[List[str]] = None) -> str:
        """Record a question. Returns its id, which routes the answer back."""
        question_id = "{}-q{}".format(self.id, len(self.questions) + 1)
        self.questions.append({
            "id": question_id,
            "text": text,
            "options": list(options or []),
            "asked_at": time.time(),
            "answer": None,
            "by": "",
            "answered_at": 0.0,
        })
        return question_id

    def pending_question(self) -> Optional[Dict[str, Any]]:
        for question in reversed(self.questions):
            if question.get("answer") is None:
                return question
        return None

    def answer(self, text: str, by: str = "") -> bool:
        """Attach an answer to the open question, if there is one.

        Refusing when nothing is open matters: a stray reply would otherwise
        attach itself to a question already settled, and the record would show
        two answers to one question with no way to tell which was meant.
        """
        question = self.pending_question()
        if question is None:
            return False
        question["answer"] = text
        question["by"] = by
        question["answered_at"] = time.time()
        return True

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Store:
    def __init__(self, directory: Optional[str] = None):
        self.dir = directory or DEFAULT_DIR

    # -- layout --------------------------------------------------------------
    def dir_for(self, request_id: str) -> str:
        return os.path.join(self.dir, request_id)

    def log_for(self, request_id: str) -> str:
        return os.path.join(self.dir_for(request_id), "agent.log")

    def result_for(self, request_id: str) -> str:
        return os.path.join(self.dir_for(request_id), "result.md")

    def new_id(self, day: Optional[str] = None) -> str:
        """`R<YYYYMMDD>-<NN>`. The date is in the id, so filenames never repeat it."""
        day = day or time.strftime("%Y%m%d")
        prefix = "R{}-".format(day)
        existing = [
            os.path.basename(p) for p in glob.glob(os.path.join(self.dir, prefix + "*"))
        ]
        used = []
        for name in existing:
            tail = name[len(prefix):]
            if tail.isdigit():
                used.append(int(tail))
        return "{}{:02d}".format(prefix, (max(used) + 1) if used else 1)

    # -- reading and writing -------------------------------------------------
    def save(self, request: Request) -> Request:
        directory = self.dir_for(request.id)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "request.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(request.as_dict(), fh, ensure_ascii=False, indent=2)
        return request

    def get(self, request_id: str) -> Optional[Request]:
        return _read(os.path.join(self.dir_for(request_id), "request.json"))

    def all(self) -> List[Request]:
        found = []
        for path in glob.glob(os.path.join(self.dir, "*", "request.json")):
            record = _read(path)
            if record:
                found.append(record)
        # Oldest first, so a queue drains in the order people asked.
        return sorted(found, key=lambda r: (r.created_at, r.id))

    def by_status(self, *statuses: str) -> List[Request]:
        wanted = set(statuses)
        return [r for r in self.all() if r.status in wanted]

    def due(self, now: Optional[float] = None) -> List[Request]:
        """Deferred jobs whose backoff has elapsed."""
        return [r for r in self.by_status(WAITING) if r.due(now)]

    def expire_stale(self, ttl: int, now: Optional[float] = None) -> List[Request]:
        """Retire requests nobody ever decided on.

        Only `pending` ones: an approved request is somebody's committed work and
        must never be expired out from under the runner holding it.
        """
        now = now if now is not None else time.time()
        expired = []
        for request in self.by_status(PENDING):
            if now - request.created_at > ttl:
                request.status = EXPIRED
                self.save(request)
                expired.append(request)
        return expired


def produced_nothing(kind: str, issues: Optional[List[str]], report: str) -> bool:
    """Did this job leave any evidence that it did the thing?

    A clean exit is not evidence. For a decomposition that means issues on the
    board; for developing an issue it means a written report, since the change
    request is reported inside it.
    """
    if issues:
        return False
    if kind == DEVELOP:
        return not (report or "").strip()
    return True


def produced_nothing(kind: str, issues: Optional[List[str]], report: str) -> bool:
    """Did this job leave evidence that it did the thing it was asked to do?

    A clean exit is not evidence, and treating it as such is how the first real
    chat-raised requirement was recorded as `done` with zero issues: the agent
    was denied permission to run the CLI, reasoned its way to three good drafts,
    explained that it could file none of them, and exited 0.

    For a decomposition the evidence is issues on the board. For developing an
    issue it is a written report, since that job creates no issues and names its
    change request inside the report.
    """
    if issues:
        return False
    if kind == DEVELOP:
        return not (report or "").strip()
    return True


def _read(path: str) -> Optional[Request]:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # A kill mid-write leaves a truncated file; one bad record must not make
        # the whole queue unreadable.
        return None
    if not isinstance(data, dict) or "id" not in data:
        return None
    known = {f for f in Request.__dataclass_fields__}  # type: ignore[attr-defined]
    return Request(**{k: v for k, v in data.items() if k in known})
