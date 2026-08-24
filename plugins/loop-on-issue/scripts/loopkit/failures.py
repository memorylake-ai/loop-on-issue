"""Was that the server, or was that the job?

From outside they look the same — a non-zero exit and nothing produced — and the
right response is opposite. A 529 wants a quiet retry in a few minutes; an agent
that ran, thought, and built nothing wants a human. Recording the first as the
second dresses an outage up as a problem with somebody's requirement, and a board
that does that stops being believed.

Deliberately conservative: anything not recognisably a fault of the transport is
treated as real. Retrying a genuine failure wastes a slot and postpones the moment
somebody looks at it, which is worse than one manual retry.
"""

from __future__ import annotations

import re

TRANSIENT = "transient"
REAL = "real"

#: First retry soon enough to be worth waiting for, then backing off fast.
_BACKOFF = (60, 300, 900)
MAX_BACKOFF = 900

#: Only the tail is examined. An agent's own prose can contain any of these words
#: — "the worker pool was overloaded, so I split the slice" — and a fault, if there
#: was one, is what stopped it, so it is at the end.
_TAIL_LINES = 40

#: Status codes that mean "come back later". 4xx does not fix itself, so retrying
#: only burns the queue against a request that will fail identically.
_SERVER_STATUS = re.compile(r"\b(?:API\s+)?[Ee]rror:?\s*(5\d{2})\b")

_TRANSPORT = (
    re.compile(r"\b429\b"),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"\boverloaded\b.*\b(?:error|529|server)\b", re.IGNORECASE),
    re.compile(r"\bECONN(?:RESET|REFUSED|ABORTED)\b"),
    re.compile(r"\bETIMEDOUT\b"),
    re.compile(r"socket hang up", re.IGNORECASE),
    re.compile(r"\bnetwork error\b", re.IGNORECASE),
    re.compile(r"\btemporarily unavailable\b", re.IGNORECASE),
    # Our own doing, and it clears on retry because a retry resumes rather than
    # starting a session id that already exists.
    re.compile(r"Session ID .* is already in use", re.IGNORECASE),
)

#: Ran for its whole budget. Handing it the same budget again is not a different
#: experiment, so this is real however transport-shaped it looks.
_TIMED_OUT = re.compile(r"timed out after \d+s", re.IGNORECASE)


def classify(output: str, returncode: int) -> str:
    tail = "\n".join((output or "").splitlines()[-_TAIL_LINES:])
    if _TIMED_OUT.search(tail):
        return REAL
    if _SERVER_STATUS.search(tail):
        return TRANSIENT
    for pattern in _TRANSPORT:
        if pattern.search(tail):
            return TRANSIENT
    return REAL


def backoff(attempt: int) -> int:
    """Seconds to wait before the next try. `attempt` counts from one."""
    index = max(1, attempt) - 1
    return _BACKOFF[min(index, len(_BACKOFF) - 1)]


def should_retry(kind: str, transient_failures: int, limit: int) -> bool:
    """Only transport faults, and only so many times.

    An outage lasting all afternoon must not leave a job hammering it, and a
    request that has burned its budget is one somebody should look at.
    """
    return kind == TRANSIENT and limit > 0 and transient_failures < limit
