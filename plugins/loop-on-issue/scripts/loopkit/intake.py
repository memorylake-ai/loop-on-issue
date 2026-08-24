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

#: States that still hold a claim on somebody's attention.
OPEN = (PENDING, APPROVED, RUNNING)

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

    def start(self, session: str = "") -> "Request":
        self.status = RUNNING
        self.started_at = time.time()
        if session:
            self.session = session
        return self

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
