"""The decidable half of the chat listener — no SDK, no network, all testable.

The transport (a DingTalk Stream long connection) lives in `dingtalk/bot.py` and
is deliberately thin, because everything that can be got *wrong* is here: which
messages to act on, which to ignore, who is allowed to approve what, and where an
answer belongs.

Three things carried over from the prior art, each of which was learned the hard
way rather than designed:

* **Delivery is at-least-once.** A reconnect redelivers the same `msgId`. Without
  dedupe, a redelivered bare reply is taken as new and answers the *second* newest
  question — one reply silently answering two questions.
* **A quote-reply carries the routing key of the card it quotes.** That is what
  makes answering exact and parallel-safe without anyone typing a ticket number.
* **Fail closed on conversations.** An empty allow-list means no conversations,
  never all of them; a bot that answers any group it is added to is a bot anyone
  can put work into.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import state
from .errors import Precondition

IGNORE = "ignore"
COMMAND = "command"
ANSWER = "answer"
INTAKE = "intake"

#: Commands anyone in an allow-listed conversation may run, versus the ones only
#: the approver may. Approving is the gate between a chat message and unattended
#: code changes, so it is held to a stricter standard than answering a question.
APPROVER_ONLY = ("approve", "reject", "now")

#: The one command an unlisted conversation may run. Without it the allow-list is
#: a bootstrap deadlock: you cannot fill in a conversationId without first being
#: told it, and you cannot be told it from a conversation that is ignored. It
#: reveals only the caller's own identifiers, which they already have.
ALLOWLIST_EXEMPT = ("whoami",)

_ALIASES = {
    "h": "help", "help": "help", "?": "help",
    "whoami": "whoami", "who": "whoami", "id": "whoami",
    "ls": "ls", "list": "ls", "board": "ls",
    "q": "q", "pending": "q", "questions": "q",
    "i": "i", "issue": "i", "show": "i",
    "a": "a", "answer": "a",
    "new": "new", "intake": "new",
    "approve": "approve", "ok": "approve",
    "reject": "reject",
    "report": "report",
    "skip": "skip",
    "requeue": "requeue", "unclaim": "requeue",
    "now": "now",
    "ping": "ping",
}

#: Approval on a phone is typed without a slash, in the language people use.
_BARE_COMMANDS = {
    "同意": "approve", "批准": "approve", "通过": "approve",
    "拒绝": "reject", "不做": "reject",
}

_MENTION_RE = re.compile(r"@[^\s@]+\s*")
_CONFIRM_RE = re.compile(r"(?:^|\s)confirm(?:\s|$)", re.IGNORECASE)


@dataclass
class Inbound:
    msg_id: str
    text: str
    sender_id: str = ""
    sender_nick: str = ""
    conversation_id: str = ""
    pqk: Optional[str] = None
    session_webhook: str = ""


@dataclass
class Action:
    kind: str
    text: str = ""
    command: Optional[str] = None
    rest: str = ""
    pqk: Optional[str] = None


class Dedupe:
    """Remembers recent message ids, and forgets them on a schedule."""

    def __init__(self, window: int = 600):
        self.window = window
        self._seen: Dict[str, float] = {}

    def seen(self, msg_id: str, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        # Prune first, so the map cannot grow without bound on a long-running bot.
        for key, at in list(self._seen.items()):
            if now - at > self.window:
                del self._seen[key]
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False


def strip_mention(text: str) -> str:
    return _MENTION_RE.sub("", text or "", count=1).strip()


def parse_command(text: str) -> Tuple[Optional[str], str]:
    """`(canonical name, the rest)`, or `(None, text)` when it is not a command."""
    text = strip_mention(text)
    if text.startswith("/"):
        head, _, rest = text[1:].strip().partition(" ")
        return _ALIASES.get(head.lower(), head.lower()), rest.strip()
    head, _, rest = text.partition(" ")
    if head in _BARE_COMMANDS:
        return _BARE_COMMANDS[head], rest.strip()
    return None, text


def needs_confirm(rest: str) -> Tuple[bool, str]:
    """Destructive commands are confirmed by repeating them with `confirm`.

    Statelessly: nothing is remembered between the two messages, so a listener
    restart in between changes nothing — which is the whole reason the listener
    can be restarted freely.
    """
    if _CONFIRM_RE.search(rest or ""):
        return False, _CONFIRM_RE.sub(" ", rest, count=1).strip()
    return True, (rest or "").strip()


def dispatch(inbound: Inbound, conversations: List[str], has_pending: bool) -> Action:
    text = strip_mention(inbound.text)
    if not text.strip():
        return Action(IGNORE)

    name, rest = parse_command(inbound.text)
    allowed = bool(conversations) and inbound.conversation_id in conversations
    if not allowed:
        if name in ALLOWLIST_EXEMPT:
            return Action(COMMAND, text=text, command=name, rest=rest)
        return Action(IGNORE)

    if name:
        return Action(COMMAND, text=text, command=name, rest=rest)

    if inbound.pqk:
        # Quoting a card is an unambiguous statement of intent, even if the index
        # entry has since been swept.
        return Action(ANSWER, text=text, pqk=inbound.pqk)
    if has_pending:
        return Action(ANSWER, text=text)
    return Action(INTAKE, text=text)


# --------------------------------------------------------------------------- #
# acting on it
# --------------------------------------------------------------------------- #

HELP = """**loop-on-issue**
`/q` 待答问题 · `/ls [状态]` 看板 · `/i <id>` 看一条
`/a <id> <文本>` 作答 · 引用回复卡片 = 直接作答
`/new <需求>` 提需求 · `同意 <id>` / `拒绝 <id> <理由>`（仅审批人）
`/report` 重发上轮报告 · `/now <id>` 立即拆需求（仅审批人）
`/skip <id> confirm <理由>` 退掉 · `/requeue <id> confirm` 放回队列
`/ping` 存活 · `/whoami` 看自己的 staffId 与会话 ID"""


class Brain:
    """Turns an inbound message into board changes and a reply."""

    def __init__(
        self,
        forge_for: Callable[[str], Any],
        default_repo: str,
        index: Any,
        conversations: List[str],
        approver: str = "",
        approver_nick: str = "",
        queue_label: str = "loop",
        intake_label: str = "intake",
        assignee: Optional[str] = None,
        creator_mode: str = "routine",
        spawn_creator: Optional[Callable[[int], str]] = None,
        last_report: str = "",
    ):
        self.forge_for = forge_for
        self.default_repo = default_repo
        self.index = index
        self.conversations = list(conversations or [])
        self.approver = approver
        self.approver_nick = approver_nick or approver or "审批人"
        self.queue_label = queue_label
        self.intake_label = intake_label
        self.assignee = assignee
        self.creator_mode = creator_mode
        self.spawn_creator = spawn_creator
        self.last_report = last_report

    # -- entry point ---------------------------------------------------------
    def handle(self, inbound: Inbound) -> str:
        action = dispatch(inbound, self.conversations, has_pending=bool(self.index.newest()))
        if action.kind == IGNORE:
            return ""
        try:
            if action.kind == COMMAND:
                return self._command(action, inbound)
            if action.kind == ANSWER:
                return self._answer(action, inbound)
            return self._intake(action.text, inbound)
        except Precondition as exc:
            return "跳过：{}".format(exc)
        except Exception as exc:  # noqa: BLE001 - a bad message must not kill the listener
            return "出错了：{}".format(exc)

    # -- answers -------------------------------------------------------------
    def _answer(self, action: Action, inbound: Inbound, number: Optional[int] = None,
                repo: Optional[str] = None, text: Optional[str] = None) -> str:
        body = text if text is not None else action.text
        options = []
        if number is None:
            record = self.index.lookup(action.pqk) if action.pqk else self.index.newest()
            if not record:
                return ("这条回复找不到对应的问题（卡片可能已过期或已被回答）。"
                        "用 `/q` 看还有哪些在等，或 `/a <id> <文本>` 直接指定。")
            number = record["issue"]
            repo = record.get("repo") or self.default_repo
            options = record.get("options") or []
        repo = repo or self.default_repo

        # Resolve a bare option number for the humans reading the board later; the
        # relayed text stays verbatim so it still parses as a selection.
        from .ask import parse_answer

        parsed = parse_answer(body, options)
        choice = "、".join(parsed.choices) if parsed.choices else None

        forge = self.forge_for(repo)
        forge.add_issue_comment(
            number, state.relay(body, by=inbound.sender_nick, via="dingtalk", choice=choice)
        )
        if action.pqk:
            self.index.remove(action.pqk)
        return "已把你的答复写到 {}#{}。".format(repo, number)

    # -- intake --------------------------------------------------------------
    def _intake(self, text: str, inbound: Inbound) -> str:
        forge = self.forge_for(self.default_repo)
        auto = bool(self.approver) and inbound.sender_id == self.approver
        title = _title_from(text)
        body = _intake_body(text, inbound, self.approver_nick, auto)

        issue = forge.create_issue(
            title, body,
            labels=[self.intake_label],
            assignees=[self.assignee] if self.assignee else None,
        )
        number = issue.number

        if auto:
            _set_state(forge, number, "WORKING")
            forge.add_issue_comment(number, state.stamp(
                "由 **{}** 本人提出，免审批，直接进入拆分。".format(inbound.sender_nick or self.approver_nick)))
            return ("已受理 #{}（审批人本人提交，**免审批**）：{}\n"
                    "撤销：`/reject {} <理由>`".format(number, issue.url, number))

        _set_state(forge, number, "PAUSED")
        forge.add_issue_comment(number, state.stamp(
            "等待 **{}** 批准。批准后才会拆成可执行 issue。".format(self.approver_nick)))
        return ("📥 已受理 #{} 待审批：{}\n"
                "请 **{}** 回复 `同意 {}`（或 `拒绝 {} <理由>`）。".format(
                    number, issue.url, self.approver_nick, number, number))

    # -- commands ------------------------------------------------------------
    def _command(self, action: Action, inbound: Inbound) -> str:
        name, rest = action.command, action.rest
        if name in APPROVER_ONLY and self.approver and inbound.sender_id != self.approver:
            return "只有 **{}** 能执行 `{}`。".format(self.approver_nick, name)

        handler = getattr(self, "_cmd_" + name, None)
        if handler is None:
            return "不认识的命令 `{}`。发 `/h` 看可用命令。".format(name)
        return handler(rest, inbound)

    def _cmd_help(self, rest, inbound):
        return HELP

    def _cmd_whoami(self, rest, inbound):
        """Hand back the identifiers needed to configure this bot.

        Answered from any conversation on purpose — see ALLOWLIST_EXEMPT.
        """
        listed = inbound.conversation_id in self.conversations
        return (
            "你：**{}**\n"
            "```\n"
            'LOOP_DINGTALK_APPROVER="{}"\n'
            'LOOP_DINGTALK_APPROVER_NICK="{}"\n'
            'LOOP_DINGTALK_CONVERSATIONS="{}"\n'
            "```\n"
            "本会话{}在白名单里。".format(
                inbound.sender_nick or "?", inbound.sender_id or "?",
                inbound.sender_nick or "", inbound.conversation_id or "?",
                "已经" if listed else "**还不**",
            )
        )

    def _cmd_ping(self, rest, inbound):
        return "alive · 待答 {} 条".format(len(self.index.all()))

    def _cmd_q(self, rest, inbound):
        records = self.index.all()
        if not records:
            return "没有等待中的问题。"
        lines = ["**待答问题 {} 条**".format(len(records))]
        for record in records[:20]:
            lines.append("- {}#{} {}".format(
                record.get("repo") or self.default_repo, record["issue"], record.get("url") or ""))
        return "\n".join(lines)

    def _cmd_ls(self, rest, inbound):
        forge = self.forge_for(self.default_repo)
        wanted = (rest or "").strip().upper() or None
        buckets: Dict[str, List[str]] = {}
        for issue in forge.list_issues(label=self.queue_label, assignee=self.assignee):
            st, base = state.split_state(issue.title)
            buckets.setdefault(st or "UNCLAIMED", []).append("#{} {}".format(issue.number, base))
        if wanted:
            buckets = {k: v for k, v in buckets.items() if k == wanted}
        if not buckets:
            return "队列是空的。"
        lines = []
        for name, rows in buckets.items():
            lines.append("**{}** ({})".format(name, len(rows)))
            lines.extend("- " + row for row in rows[:10])
        return "\n".join(lines)

    def _cmd_i(self, rest, inbound):
        number = _first_int(rest)
        if number is None:
            return "用法：`/i <id>`"
        forge = self.forge_for(self.default_repo)
        try:
            issue = forge.get_issue(number)
        except Exception:  # noqa: BLE001
            return "找不到 #{}。".format(number)
        st, base = state.split_state(issue.title)
        marker = state.latest_marker(forge.list_issue_comments(number)) or {}
        return "**#{} {}**\n状态：{} · runner：{} · session：{}\n{}".format(
            number, base, st or "UNCLAIMED", marker.get("runner") or "—",
            marker.get("session") or "—", issue.url)

    def _cmd_a(self, rest, inbound):
        number = _first_int(rest)
        text = re.sub(r"^\s*#?\d+\s*", "", rest or "", count=1).strip()
        if number is None or not text:
            return "用法：`/a <id> <你的答复>`"
        return self._answer(Action(ANSWER), inbound, number=number, repo=self.default_repo, text=text)

    def _cmd_new(self, rest, inbound):
        if not (rest or "").strip():
            return "用法：`/new <需求>`"
        return self._intake(rest.strip(), inbound)

    def _cmd_report(self, rest, inbound):
        return self.last_report or "还没有可重发的报告。"

    def _cmd_approve(self, rest, inbound):
        number = _first_int(rest)
        if number is None:
            return "用法：`同意 <id> [备注]`"
        note = re.sub(r"^\s*#?\d+\s*", "", rest or "", count=1).strip()
        forge = self.forge_for(self.default_repo)
        _set_state(forge, number, "WORKING")
        forge.add_issue_comment(number, state.stamp(
            "**已批准** — {} 于此批准。{}".format(
                self.approver_nick, "\n\n审批备注（与需求原文同等效力）：{}".format(note) if note else "")))
        started = ""
        if self.creator_mode == "immediate" and self.spawn_creator:
            started = "\n" + self.spawn_creator(number)
        return "✅ #{} 已批准。{}{}".format(
            number,
            "正在拆分。" if self.creator_mode == "immediate" else "下一轮 routine 会拆成可执行 issue。",
            started)

    def _cmd_reject(self, rest, inbound):
        number = _first_int(rest)
        reason = re.sub(r"^\s*#?\d+\s*", "", rest or "", count=1).strip()
        if number is None:
            return "用法：`拒绝 <id> <理由>`"
        if len(reason) < 4:
            return "拒绝要写理由——提需求的人只会看到这句话。"
        forge = self.forge_for(self.default_repo)
        forge.add_issue_comment(number, state.stamp(
            "**未批准** — {} 拒绝。\n\n{}".format(self.approver_nick, reason)))
        _set_state(forge, number, "SKIP")
        return "🚫 #{} 已拒绝：{}".format(number, reason)

    def _cmd_now(self, rest, inbound):
        number = _first_int(rest)
        if number is None:
            return "用法：`/now <id>`"
        if not self.spawn_creator:
            return "这台机器没有开启立即拆分（`creator_mode`）。"
        return self.spawn_creator(number)

    def _cmd_skip(self, rest, inbound):
        number = _first_int(rest)
        pending_confirm, cleaned = needs_confirm(rest)
        reason = re.sub(r"^\s*#?\d+\s*", "", cleaned, count=1).strip()
        if number is None:
            return "用法：`/skip <id> confirm <理由>`"
        if pending_confirm:
            return "这会把 #{} 永久退出队列。确认请发：`/skip {} confirm {}`".format(number, number, reason)
        if len(reason) < 15:
            return "退掉一条 issue 要写清楚为什么不需要改代码（至少 15 个字）。"
        forge = self.forge_for(self.default_repo)
        forge.add_issue_comment(number, state.stamp(
            "**Skipped — no code change needed.**\n\n{}\n\n"
            "_由 {} 在钉钉操作。移除标题前缀即可放回队列。_".format(reason, inbound.sender_nick)))
        _set_state(forge, number, "SKIP")
        return "已退掉 #{}。".format(number)

    def _cmd_requeue(self, rest, inbound):
        number = _first_int(rest)
        pending_confirm, _ = needs_confirm(rest)
        if number is None:
            return "用法：`/requeue <id> confirm`"
        if pending_confirm:
            return "这会清掉 #{} 的状态前缀，下一轮可能被重新认领。确认请发：`/requeue {} confirm`".format(
                number, number)
        forge = self.forge_for(self.default_repo)
        _set_state(forge, number, None)
        forge.add_issue_comment(number, state.stamp(
            "由 {} 在钉钉放回队列。".format(inbound.sender_nick)))
        return "已把 #{} 放回队列。".format(number)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _set_state(forge, number: int, target: Optional[str]) -> None:
    issue = forge.get_issue(number)
    _, base = state.split_state(issue.title)
    forge.set_issue_title(number, state.compose(target, base))


def _first_int(text: str) -> Optional[int]:
    m = re.search(r"#?(\d+)", text or "")
    return int(m.group(1)) if m else None


def _title_from(text: str, limit: int = 60) -> str:
    line = (text or "").strip().splitlines()[0].strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _intake_body(text: str, inbound: Inbound, approver_nick: str, auto: bool) -> str:
    return (
        "### 需求原文\n\n"
        "> {}\n\n"
        "### 来源\n\n"
        "- 提出人：{}（`{}`）\n"
        "- 来源会话：`{}`\n"
        "- 审批：{}\n\n"
        "_本 issue 由钉钉受理，**不带队列标签**，在获批并拆分成子 issue 之前不会被执行。_\n"
    ).format(
        (text or "").strip().replace("\n", "\n> "),
        inbound.sender_nick or "unknown",
        inbound.sender_id or "unknown",
        inbound.conversation_id or "unknown",
        "本人提交，免审批" if auto else "待 {} 批准".format(approver_nick),
    )
