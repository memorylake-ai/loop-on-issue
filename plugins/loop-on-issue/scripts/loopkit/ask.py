"""Ask a human, with the issue as the durable channel.

The shape is taken from `loopcue`'s `ask_human` and keeps its four hard
constraints — bounded timeout so a session never hangs, sender authorisation,
a structured answer (a number picks an option, anything else is free text), and
one post per request — but changes where the answer lives.

loopcue routed an answer back to *the session that asked*, through a local file
rendezvous. Here the question is a comment on the issue and so is the answer, so:

* it survives the machine, the process, and the person answering from a laptop
  instead of their phone;
* a human who never opens DingTalk can just reply on the issue;
* and there is no pending/replies/buffer protocol to keep consistent.

Authorisation moved with it. Anything reaching the issue is already authenticated
by the forge; what needs gating is the *chat* side, so the listener enforces the
conversation allow-list before it mirrors anything here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from . import pending, state

#: Only a reply that is *nothing but* numbers selects options. "2 because it is
#: faster" is an explanation, and reading it as a bare selection would throw away
#: everything the human actually said.
_ONLY_NUMBERS = re.compile(r"^\s*\d+(?:\s*[,，、/\s]+\s*\d+)*\s*$")
_NUMBER = re.compile(r"\d+")


@dataclass
class Answer:
    kind: str                       # "option" | "text"
    raw: str
    indexes: List[int] = field(default_factory=list)
    choices: List[str] = field(default_factory=list)
    by: Optional[str] = None
    via: Optional[str] = None

    def as_dict(self):
        return {
            "kind": self.kind, "raw": self.raw, "indexes": self.indexes,
            "choices": self.choices, "by": self.by, "via": self.via,
        }


@dataclass
class Result:
    answered: bool
    issue: int
    url: str = ""
    answer: Optional[Answer] = None
    pqk: Optional[str] = None
    notify_error: str = ""

    def as_dict(self):
        return {
            "answered": self.answered, "id": self.issue, "url": self.url,
            "answer": self.answer.as_dict() if self.answer else None,
            "notify_error": self.notify_error or None,
        }


def parse_answer(body: str, options: Optional[List[str]] = None) -> Answer:
    attrs, text = state.parse_relay(body)
    text = (text or "").strip()
    by = (attrs or {}).get("by")
    if by:
        by = by.replace("_", " ")
    via = (attrs or {}).get("via")

    options = list(options or [])
    if options and _ONLY_NUMBERS.match(text):
        indexes = [int(n) for n in _NUMBER.findall(text)]
        if indexes and all(1 <= i <= len(options) for i in indexes):
            return Answer("option", text, indexes, [options[i - 1] for i in indexes], by, via)
        # An out-of-range number is not silently mapped onto a neighbouring
        # option; it is returned as text so a human sees what was actually said.
    return Answer("text", text, by=by, via=via)


def question_body(question: str, options: Optional[List[str]] = None) -> str:
    lines = ["**需要你拍板 / A decision is needed**", "", question.strip()]
    if options:
        lines.append("")
        for index, option in enumerate(options, 1):
            lines.append("{}. {}".format(index, option))
        lines.append("")
        lines.append("_直接在本 issue 回复编号或你的答复即可；也可以在钉钉里引用回复那条卡片。_")
    else:
        lines.append("")
        lines.append("_直接在本 issue 回复即可；也可以在钉钉里引用回复那条卡片。_")
    return "\n".join(lines)


def ask(
    forge: Any,
    repo: str,
    number: int,
    question: str,
    options: Optional[List[str]] = None,
    wait: int = 0,
    notify: Optional[Callable[[str, str], Optional[str]]] = None,
    index: Optional[pending.Index] = None,
    poll: int = 5,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Post a question, optionally wait a bounded time for the answer.

    `wait=0` — the default — posts and returns immediately. That keeps the swarm's
    standing rule intact: a session that blocks on a human holds a slot, and the
    next scheduled run is the waiting mechanism. Only the interactive hook passes
    a non-zero wait, and only a short one.
    """
    options = list(options or [])
    issue = forge.get_issue(number)
    url = issue.url

    forge.add_issue_comment(number, state.stamp(question_body(question, options)))
    # Everything from here is best-effort on top of a question that is already
    # durably recorded.
    anchor = _latest_comment_key(forge, number)

    idx = index if index is not None else pending.Index()
    pqk = None
    notify_error = ""
    if notify is not None:
        try:
            pqk = notify(
                "#{} 需要你拍板".format(number),
                _card(repo, number, url, question, options),
            )
        except Exception as exc:  # noqa: BLE001 - a chat outage must not lose the question
            notify_error = str(exc)
        if pqk:
            idx.record(pqk, {"repo": repo, "issue": number, "options": options, "url": url})

    try:
        result = _poll(forge, number, options, anchor, wait, poll, clock, sleep)
        result.url = url
        result.pqk = pqk
        result.notify_error = notify_error
        return result
    finally:
        # The index entry only exists to route an answer back to a question that
        # is still being waited on. Leaving it behind makes a later bare reply
        # answer something nobody is listening to.
        if pqk:
            idx.remove(pqk)


def _card(repo, number, url, question, options):
    from . import dingtalk

    return dingtalk.question_card(repo, number, url, question, options)


def _latest_comment_key(forge, number) -> str:
    comments = forge.list_issue_comments(number)
    return max((c.created_at for c in comments), default="")


def _poll(forge, number, options, anchor, wait, poll, clock, sleep) -> Result:
    deadline = clock() + wait
    while True:
        replies = [
            c for c in state.unanswered(forge.list_issue_comments(number))
            if c.created_at > anchor
        ]
        if replies:
            answer = parse_answer(replies[-1].body, options)
            if answer.raw:
                return Result(True, number, answer=answer)
        remaining = deadline - clock()
        if remaining <= 0:
            return Result(False, number)
        sleep(min(poll, remaining))
