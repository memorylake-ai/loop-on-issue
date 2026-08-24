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

#: Invisible in rendered markdown on both forges, so machinery can label a note
#: without relying on authorship — the agent authenticates as the same account as
#: the human it reports to.
MARKER_NAME = "loop-on-issue:agent"

#: The same fact, said where a person can see it. The hidden marker is correct for
#: the machine and useless for the human, who otherwise reads a thread of notes
#: from themselves that they did not write. Detection never keys on this: a human
#: typing it must not be able to make their own reply invisible to the loop.
AGENT_PREFIX = "**[AGENT]**"

#: Markers written by the private skills this plugin grew out of. Read but never
#: written: a board mid-flight would otherwise lose the anchor that tells an agent
#: comment from a human reply, and every PAUSED issue on it would stop waking up.
LEGACY_MARKERS = ("<!-- loop-swarm-agent -->",)

#: Stamped on a comment the tooling posts *on a human's behalf* — an answer given
#: in DingTalk, mirrored onto the issue. Deliberately a different name from the
#: agent marker: `unanswered` must still see it as a human reply, or an answer
#: relayed from chat would never wake the issue it answers.
RELAY_NAME = "loop-on-issue:relay"

_MARKER_RE = re.compile(r"<!--\s*" + re.escape(MARKER_NAME) + r"(?P<attrs>[^>]*?)-->")
_RELAY_RE = re.compile(r"<!--\s*" + re.escape(RELAY_NAME) + r"(?P<attrs>[^>]*?)-->\s*\n?")
_ATTR_RE = re.compile(r"(\w+)=(\S+)")


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
    return "<!-- {}{} -->\n{} {}".format(MARKER_NAME, attrs, AGENT_PREFIX, body.lstrip())


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


def relay(text: str, by: str = None, via: str = "dingtalk", choice: str = None) -> str:
    """Wrap an answer a human gave somewhere else, for posting onto the issue.

    The visible line names who said it, because six weeks later the issue is the
    only record that a decision was made and by whom — and, when the answer was a
    bare option number, what that number *meant*. The answer itself is relayed
    verbatim below it, so the machine-readable form survives: resolving "2" to
    "right" in the body would stop it parsing as a selection at all.
    """
    attrs = ""
    if by:
        attrs += " by={}".format(_slugish(by))
    if via:
        attrs += " via={}".format(via)
    who = "**{}** 在 {} 回答".format(by, via) if by else "在 {} 收到的回答".format(via)
    if choice:
        who += "（选了：{}）".format(choice)
    return "<!-- {}{} -->\n{}：\n\n{}".format(RELAY_NAME, attrs, who, text.strip())


def parse_relay(body: str) -> Tuple[Optional[Dict[str, str]], str]:
    """`(attributes, the answer text)`, or `(None, body)` when it is not a relay."""
    m = _RELAY_RE.search(body or "")
    if not m:
        return None, body or ""
    attrs = dict(_ATTR_RE.findall(m.group("attrs")))
    rest = (body[: m.start()] + body[m.end():]).strip()
    # Drop the human-facing attribution line the relay adds above the answer.
    lines = rest.split("\n")
    if lines and ("回答" in lines[0]):
        rest = "\n".join(lines[1:]).strip()
    return attrs, rest


def _slugish(value: str) -> str:
    """Attribute values are whitespace-delimited, so a nickname cannot carry one."""
    return re.sub(r"\s+", "_", value.strip())


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


def latest_marker(comments: Sequence[Comment]) -> Optional[Dict[str, str]]:
    """Attributes from the newest agent marker carrying any, if there are any.

    Used to read back what the board says about an issue — which runner holds it,
    and which session — without recomputing anything.
    """
    best = None
    best_at = None
    for c in comments:
        attrs = parse_marker(c.body)
        if not attrs:
            continue
        if best_at is None or c.created_at > best_at:
            best, best_at = attrs, c.created_at
    return best


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
