"""The pqk → issue index: which question a DingTalk reply is answering.

DingTalk returns a `processQueryKey` when a card is sent, and a user's
**quote-reply** to that card carries the same key back as
`originalProcessQueryKey`. That is what makes routing exact and parallel-safe
without asking anyone to type a ticket number.

This index is deliberately **only an index**. The question and its answer both
live on the issue; losing this directory degrades a quote-reply to the same
handling as a bare reply — "answers the newest open question" — and loses nothing
durable. Which is why it is a directory of small files rather than anything that
needs to be kept consistent.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".loop-on-issue", "pending")

#: Orphans left by a SIGKILL, collected by `sweep`. Long enough that a genuinely
#: patient question is not swept out from under someone still deciding.
DEFAULT_TTL = 24 * 3600


def slug(pqk: str) -> str:
    """A filename for a routing key.

    A processQueryKey is base64-ish and contains `/` and `=`, so it cannot be a
    filename directly.
    """
    return hashlib.sha256(pqk.encode("utf-8")).hexdigest()[:24]


class Index:
    def __init__(self, directory: Optional[str] = None):
        self.dir = directory or DEFAULT_DIR

    def _path(self, pqk: str) -> str:
        return os.path.join(self.dir, slug(pqk) + ".json")

    def record(self, pqk: str, data: Dict[str, Any], now: Optional[float] = None) -> str:
        os.makedirs(self.dir, exist_ok=True)
        payload = dict(data)
        payload["pqk"] = pqk
        payload["asked_at"] = now if now is not None else time.time()
        path = self._path(pqk)
        # The record names a repository and an issue number; nothing secret, but
        # nothing anyone else on a shared machine needs either.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def lookup(self, pqk: str) -> Optional[Dict[str, Any]]:
        return _read(self._path(pqk))

    def remove(self, pqk: str) -> None:
        try:
            os.unlink(self._path(pqk))
        except OSError:
            # `ask` cleans up in a finally block that may run after something
            # already removed the entry. Absence is the desired end state.
            pass

    def all(self) -> List[Dict[str, Any]]:
        records = []
        for path in glob.glob(os.path.join(self.dir, "*.json")):
            record = _read(path)
            if record:
                records.append(record)
        return sorted(records, key=lambda r: r.get("asked_at", 0), reverse=True)

    def newest(self) -> Optional[Dict[str, Any]]:
        """What a bare reply — no quote — is taken to answer."""
        records = self.all()
        return records[0] if records else None

    def sweep(self, ttl: int = DEFAULT_TTL, now: Optional[float] = None) -> int:
        now = now if now is not None else time.time()
        removed = 0
        for path in glob.glob(os.path.join(self.dir, "*.json")):
            record = _read(path)
            if record is None or now - record.get("asked_at", 0) > ttl:
                try:
                    os.unlink(path)
                    removed += 1
                except OSError:
                    pass
        return removed


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # A kill mid-write leaves a truncated file; one bad record must not make
        # every lookup fail.
        return None
    return data if isinstance(data, dict) else None
