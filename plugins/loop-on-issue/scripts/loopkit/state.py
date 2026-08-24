"""The issue state machine, encoded as a title prefix.

Both forges model an issue as nothing but `open` or `closed`. That cannot
distinguish an untouched issue from one an agent grabbed three seconds ago, one
mid-refactor, one blocked on a human, and one awaiting review — and conflating
them either duplicates work or strands it.

So state lives in a **title prefix**: `[WORKING] fix drive URI late binding`. The
title is the one field that appears in every issue list, notification email and
board card, so humans and agents read the same state with no extra tooling, and a
human can hand-edit a prefix to redirect the swarm.

    no prefix ──────► [CLAIMED] ──────► [WORKING] ──────► [FINISHED] ────► human
    (the queue)                          ▲   │   ▲            │            closes
                                         │   │   └────────────┘
                          human replied  │   ▼   review feedback
                                       [PAUSED]

    [CLAIMED] or [WORKING] ──────────► [SKIP]   needs no code change; dormant

Everything here is a pure function over strings and `Comment` objects. No network,
no forge knowledge — which is what makes the tricky parts (stacked prefixes,
literal brackets, marker parsing) cheap to test exhaustively.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Comment

STATES = ("CLAIMED", "WORKING", "PAUSED", "FINISHED", "SKIP")

#: States a run does not need to look at again. SKIP is a deliberate "this needs
#: no code change" verdict; FINISHED still gets re-checked for review feedback.
DORMANT = ("SKIP",)

# One leading state prefix. Applied repeatedly so a title that somehow accumulated
# two of them (an interrupted transition) still normalises cleanly instead of
# growing forever. Case-sensitive on purpose: `[working]` in a human-written title
# is a note to another human, not a claim by an agent.
_PREFIX_RE = re.compile(r"^\s*\[(" + "|".join(STATES) + r")\]\s*")

#: Invisible in rendered markdown on both forges, so it labels a note as
#: machine-written without adding visual noise to the thread.
MARKER_NAME = "loop-on-issue:agent"

#: Markers written by the private skills this plugin grew out of. Read but never
#: written: a board mid-flight would otherwise lose the anchor that tells an agent
#: comment from a human reply, and every PAUSED issue on it would stop waking up.
LEGACY_MARKERS = ("<!-- loop-swarm-agent -->",)

_MARKER_RE = re.compile(r"<!--\s*" + re.escape(MARKER_NAME) + r"(?P<attrs>[^>]*?)-->")
_ATTR_RE = re.compile(r"(\w+)=([^\s]+)")


# --------------------------------------------------------------------------- #
# title prefixes
# --------------------------------------------------------------------------- #


def split_state(title: str) -> Tuple[Optional[str], str]:
    """Return `(state, base_title)`, stripping every stacked prefix.

    The first prefix wins, because that is the one an agent wrote before it was
    interrupted; the rest are debris from the interrupted write.
    """
    state: Optional[str] = None
    rest = title
    while True:
        m = _PREFIX_RE.match(rest)
        if not m:
            break
        if state is None:
            state = m.group(1)
        rest = rest[m.end() :]
    return state, rest.strip()


def compose(state: Optional[str], base_title: str) -> str:
    return "[{}] {}".format(state, base_title) if state else base_title


def is_dormant(state: Optional[str]) -> bool:
    return state in DORMANT


# --------------------------------------------------------------------------- #
# the agent marker
# --------------------------------------------------------------------------- #


def stamp(body: str, session: str = None, runner: str = None) -> str:
    """Prefix a comment body with the agent marker.

    The agent authenticates as the *same account* as the human it reports to, so
    note authorship cannot tell them apart. Every note the tooling posts carries
    this marker, and "a human replied" means *an unmarked note newer than our last
    marked one*. Post an agent comment without going through here and it later
    reads as a human reply, waking an issue nobody answered.

    `session` and `runner` ride along as marker attributes. That is the台账 for
    runners like Codex, whose session id cannot be chosen at start and therefore
    has to be recorded somewhere durable after the fact.
    """
    attrs = ""
    if session:
        attrs += " session={}".format(session)
    if runner:
        attrs += " runner={}".format(runner)
    return "<!-- {}{} -->\n{}".format(MARKER_NAME, attrs, body)


def is_agent_note(body: str) -> bool:
    body = body or ""
    if _MARKER_RE.search(body):
        return True
    return any(m in body for m in LEGACY_MARKERS)


def parse_marker(body: str) -> Optional[Dict[str, str]]:
    """Marker attributes as a dict, or `None` when the body carries no marker.

    An empty dict therefore means "written by an agent, no attributes", which is
    a different thing from "not written by an agent".
    """
    m = _MARKER_RE.search(body or "")
    if not m:
        return None
    return dict(_ATTR_RE.findall(m.group("attrs")))


# --------------------------------------------------------------------------- #
# reading a comment thread
# --------------------------------------------------------------------------- #


def unanswered(comments: Sequence[Comment]) -> List[Comment]:
    """Human notes posted after our most recent agent note.

    Anchoring on our own last note rather than a stored timestamp keeps the check
    correct across restarts and needs no state outside the forge. When we have
    never posted, every human note counts — that is a thread we have not read yet,
    not one we are up to date on.
    """
    human = [c for c in comments if not c.system]
    agent_times = [c.created_at for c in human if is_agent_note(c.body)]
    last_agent = max(agent_times) if agent_times else None
    return [
        c
        for c in human
        if not is_agent_note(c.body) and (last_agent is None or c.created_at > last_agent)
    ]


def latest_session(comments: Sequence[Comment]) -> Optional[Dict[str, str]]:
    """The newest session recorded in a marker comment, if any.

    Used by runners that cannot be told their session id up front, so the id has
    to be captured from the first run and read back on the next one.
    """
    best = None
    best_at = None
    for c in comments:
        attrs = parse_marker(c.body)
        if not attrs or "session" not in attrs:
            continue
        if best_at is None or c.created_at > best_at:
            best, best_at = attrs, c.created_at
    return best
