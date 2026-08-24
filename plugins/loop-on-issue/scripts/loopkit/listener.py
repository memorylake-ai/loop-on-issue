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

from . import intake as intake_mod
from . import state
from .errors import Precondition

IGNORE = "ignore"
COMMAND = "command"
ANSWER = "answer"
INTAKE = "intake"

#: Commands anyone in an allow-listed conversation may run, versus the ones only
#: the approver may. Approving is the gate between a chat message and unattended
#: code changes, so it is held to a stricter standard than answering a question.
#: Approving a requirement and starting a session on an issue are the same class
#: of act — both put an unattended agent to work — so they sit behind the same
#: gate. Everything else, answering included, is open to the conversation.
APPROVER_ONLY = ("approve", "reject", "dev", "repo", "cancel")

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
    "p": "p", "reqs": "p", "requests": "p",
    "r": "r", "req": "r",
    "repos": "repos", "repo": "repo",
    "dev": "dev", "go": "dev", "start": "dev",
    "cancel": "cancel", "stop": "cancel", "kill": "cancel",
    "approve": "approve", "ok": "approve",
    "reject": "reject",
    "report": "report",
    "skip": "skip",
    "requeue": "requeue", "unclaim": "requeue",
    "ping": "ping",
}

#: Approval on a phone is typed without a slash, in the language people use.
_BARE_COMMANDS = {
    "同意": "approve", "批准": "approve", "通过": "approve",
    "拒绝": "reject", "不做": "reject",
}

_MENTION_RE = re.compile(r"@[^\s@]+\s*")
_CONFIRM_RE = re.compile(r"(?:^|\s)confirm(?:\s|$)", re.IGNORECASE)

#: Decoration a command picks up on its way through a rendered message. Replies
#: offer options as pasteable commands in backticks, and copying the rendered
#: line brings the backticks along — the leading one stopped the command being
#: recognised at all, so an approval was filed as a brand new requirement while
#: the thing it approved stayed pending.
_LEADING_JUNK = re.compile(r"^[\s\-*_`>•·]+")
#: A copied option line trails its rendered explanation: `cmd` → `result`.
_TRAILING_RENDER = re.compile(r"\s*[`\s]*(?:→|->|=>)\s*.*$")


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
    """`(canonical name, the rest)`, or `(None, text)` when it is not a command.

    Tolerant of decoration, because the decoration is ours: a message that offers
    `同意 R1 demo-gh` as a tappable option gets copied back with its backticks and
    its rendered `→ owner/name` tail attached.

    Only the *command word* is un-decorated. Prose that merely contains backticks
    stays prose — stripping enough to recognise a command must not be enough to
    turn a requirement into one.
    """
    original = strip_mention(text)
    candidate = _LEADING_JUNK.sub("", original)

    if candidate.startswith("/"):
        head, _, rest = candidate[1:].strip().partition(" ")
        head = head.strip("`*_")
        return _ALIASES.get(head.lower(), head.lower()), _clean_args(rest)

    head, _, rest = candidate.partition(" ")
    if head.strip("`*_") in _BARE_COMMANDS:
        return _BARE_COMMANDS[head.strip("`*_")], _clean_args(rest)
    return None, original


def _clean_args(rest: str) -> str:
    """Drop the rendered tail a copied option line brings with it."""
    rest = _TRAILING_RENDER.sub("", rest or "")
    return rest.strip().strip("`*_").strip()


def needs_confirm(rest: str) -> Tuple[bool, str]:
    """Destructive commands are confirmed by repeating them with `confirm`.

    Statelessly: nothing is remembered between the two messages, so a listener
    restart in between changes nothing — which is the whole reason the listener
    can be restarted freely.
    """
    if _CONFIRM_RE.search(rest or ""):
        return False, _CONFIRM_RE.sub(" ", rest, count=1).strip()
    return True, (rest or "").strip()


def dispatch(inbound: Inbound, conversations: List[str], has_pending: bool = False) -> Action:
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
        # Quoting a card is an unambiguous statement of intent. Only a quote
        # answers a question: reading a bare sentence as an answer whenever one
        # happened to be open meant a new requirement could be swallowed by a
        # question nobody was thinking about.
        return Action(ANSWER, text=text, pqk=inbound.pqk)
    return Action(INTAKE, text=text)


# --------------------------------------------------------------------------- #
# acting on it
# --------------------------------------------------------------------------- #

#: DingTalk markdown is narrower than it looks: a single newline does **not**
#: break a line, and `_underscore italics_` are not rendered at all. So every
#: multi-line reply is built from list items — which do break — with blank lines
#: between blocks. Getting this wrong does not error; it just arrives as one
#: unreadable paragraph.
def md(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


def bullets(*items: str) -> str:
    return "\n".join("- {}".format(item) for item in items if item)


HELP = md(
    "**loop-on-issue**",
    bullets(
        "直接发一句话 = **提需求**（不用加 `/`）",
        "作答请**引用回复**那张问题卡片",
    ),
    "**两种编号，别弄混**",
    bullets(
        "`<R-ID>` — 一条**需求**，形如 `R20260824-01`",
        "`<issue-id>` — 一个 **issue**，形如 `#612` 或 `612`",
    ),
    "**提需求 / 查看**",
    bullets(
        "`/new [仓库] <需求>` — 提需求，与直接发一句话等价",
        "`/p` — 在办的需求（待审批 · 排队中 · 执行中）",
        "`/r <R-ID>` — 某条需求：状态 · 审批 · 产物",
        "`/q` — 待答问题",
        "`/ls [状态]` — issue 队列",
        "`/i <issue-id>` — 某个 issue，跨仓库写 `demo-gh:612`",
        "`/repos` — 本机服务的仓库",
    ),
    "**作答**",
    bullets(
        "引用回复问题卡片（可一次回多个编号）",
        "`/a <issue-id> <文本>` — 直接答某个 issue",
    ),
    "**仅审批人**",
    bullets(
        "`同意 <R-ID> [仓库] [备注]` — 批准，也可写 `/approve`",
        "`拒绝 <R-ID> <理由>` — 驳回，也可写 `/reject`",
        "`/repo <R-ID> <仓库>` — 改归哪个仓库（开跑前有效）",
        "`/cancel <R-ID>` — 停掉卡住的任务，释放 worker",
        "`/dev <issue-id> [仓库]` — 让 agent 现在就去开发这个 issue",
    ),
    "**看板维护**",
    bullets(
        "`/skip <issue-id> confirm <理由>` — 退掉一条 issue",
        "`/requeue <issue-id> confirm` — 放回队列",
        "`/report` — 重发上轮报告",
    ),
    "**其他**",
    bullets(
        "`/ping` — 存活",
        "`/whoami` — 看自己的 staffId 与会话 ID",
        "`/h`（`/help`）— 本帮助",
    ),
)


class Brain:
    """Turns an inbound message into board changes and a reply."""

    def __init__(
        self,
        forge_for: Callable[[str], Any],
        registry: Any,
        index: Any,
        store: Any,
        conversations: List[str],
        approver: str = "",
        approver_nick: str = "",
        queue_label: str = "loop",
        assignee: Optional[str] = None,
        enqueue: Optional[Callable[[Any], None]] = None,
        cancel_job: Optional[Callable[[str, str], Any]] = None,
        last_report: str = "",
    ):
        self.forge_for = forge_for
        self.registry = registry
        self.index = index
        self.store = store
        self.conversations = list(conversations or [])
        self.approver = approver
        self.approver_nick = approver_nick or approver or "审批人"
        self.queue_label = queue_label
        self.assignee = assignee
        # Called with an approved Request. Injected so the Brain never owns a
        # subprocess and stays testable without one.
        self.enqueue = enqueue or (lambda request: None)
        # Injected by whoever owns the processes; without one, cancelling can
        # still mark the record, which is better than nothing but does not free
        # the worker.
        self.cancel_job = cancel_job
        self.last_report = last_report

    @property
    def default_repo(self) -> str:
        entry = self.registry.default
        return entry.repo if entry else ""

    # -- entry point ---------------------------------------------------------
    def handle(self, inbound: Inbound) -> str:
        action = dispatch(inbound, self.conversations)
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
                return md(
                    "这条回复找不到对应的问题（卡片可能已过期或已被回答）。",
                    bullets("`/q` 看还有哪些在等", "`/a <id> <文本>` 直接指定"),
                )
            if record.get("intake"):
                # A decomposition job asked this; it has no issue yet, so the
                # answer belongs on the request the job is working from.
                return self._answer_intake(record, body, inbound, action.pqk)
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

    def _answer_intake(self, record, body, inbound, pqk):
        request = self.store.get(record["intake"])
        if request is None:
            return "找不到需求 `{}`，这条回复没有归处。".format(record["intake"])
        if not request.answer(body, by=inbound.sender_nick):
            return "**{}** 当前没有在等回答（可能刚被答过）。".format(request.id)
        self.store.save(request)
        if pqk:
            self.index.remove(pqk)
        options = record.get("options") or []
        from .ask import parse_answer

        parsed = parse_answer(body, options)
        chosen = "（选了：{}）".format("、".join(parsed.choices)) if parsed.choices else ""
        return "已把你的答复交给 **{}**{}，它会接着往下做。".format(request.id, chosen)

    # -- intake --------------------------------------------------------------
    def _intake(self, text: str, inbound: Inbound) -> str:  # noqa: C901
        """File a requirement locally. Nothing reaches the forge until approved.

        An earlier version created an unqueued issue here, which meant anybody who
        could message the bot could write to the repository. The decision to build
        something needs a durable public record; the *request* to build it does
        not, and an issue tracker full of unapproved one-liners stops being
        readable.
        """
        # A leading registered name routes the requirement. This is the only
        # chance the approver gets when raising one themselves: their own
        # requirement is auto-approved and queued instantly, so there is no
        # approval step to redirect it in.
        named, text = self._split_repo(text)
        entry = self.registry.get(named) if named else self.registry.default
        if entry is None and named is None:
            names = "、".join(self.registry.names()) or "（一个都没配）"
            return md(
                "这台机器服务多个仓库且没有默认，我不知道这条需求归哪个。",
                bullets(
                    "已配置：{}".format(names),
                    "设默认：`loop repos default <名字>`",
                    "或让审批人批准时指定：`同意 <ID> <仓库>`",
                ),
            )

        request = intake_mod.Request(
            id=self.store.new_id(),
            text=text.strip(),
            requester=inbound.sender_nick,
            requester_id=inbound.sender_id,
            conversation=inbound.conversation_id,
            repo=entry.repo,
        )
        consumed_note = (
            "　— 用掉了开头的 `{}`；若那本是需求的一部分，`/repo` 改回".format(named)
            if named else "")
        mine = bool(self.approver) and inbound.sender_id == self.approver

        request = intake_mod.Request(
            id=self.store.new_id(),
            text=text.strip(),
            requester=inbound.sender_nick,
            requester_id=inbound.sender_id,
            conversation=inbound.conversation_id,
            repo=entry.repo,
        )
        self.store.save(request)

        # Everybody's requirement waits for one confirming message, the approver's
        # included. Queueing theirs instantly read as convenient and was not: it
        # is the moment the repository gets chosen, and it was going by without
        # anyone being asked. One round trip buys a look at how the text was
        # understood and where it is about to land.
        choices = bullets(*[
            "`同意 {} {}` → `{}`{}".format(
                request.id, e.name, e.repo,
                "  ← 默认" if entry and e.name == entry.name else "")
            for e in self.registry.all()
        ])
        if mine:
            return md(
                "📥 **{}** 已记下，确认一下就开跑。".format(request.id),
                "> {}".format(request.text.replace("\n", "\n> ")),
                bullets(
                    "拟归入：`{}`（`{}`）{}".format(entry.repo, entry.name, consumed_note),
                    "确认：`同意 {}`".format(request.id),
                    "不要了：`拒绝 {} <理由>`".format(request.id),
                ),
                "换仓库：" if len(self.registry.all()) > 1 else "",
                choices if len(self.registry.all()) > 1 else "",
            )
        return md(
            "📥 已受理 **{}**，待 **{}** 批准。".format(request.id, self.approver_nick),
            "> {}".format(request.text.replace("\n", "\n> ")),
            bullets(
                "拟归入：`{}`（`{}`）{}".format(entry.repo, entry.name, consumed_note),
                "批准：`同意 {}`".format(request.id),
                "改仓库后批准：`同意 {} <仓库>`".format(request.id),
                "驳回：`拒绝 {} <理由>`".format(request.id),
            ),
        )

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
        return md(
            "你是 **{}**".format(inbound.sender_nick or "?"),
            "**粘进 `~/.loop-on-issue/dingtalk.env`**",
            bullets(
                '`LOOP_DINGTALK_APPROVER="{}"`'.format(inbound.sender_id or "?"),
                '`LOOP_DINGTALK_APPROVER_NICK="{}"`'.format(inbound.sender_nick or ""),
                '`LOOP_DINGTALK_CONVERSATIONS="{}"`'.format(inbound.conversation_id or "?"),
            ),
            "本会话{}在白名单里。".format("已经" if listed else "**还不**"),
        )

    def _cmd_ping(self, rest, inbound):
        return "alive · 待答 {} 条".format(len(self.index.all()))

    def _cmd_q(self, rest, inbound):
        records = self.index.all()
        if not records:
            return "没有等待中的问题。"
        return md(
            "**待答问题 {} 条**".format(len(records)),
            bullets(*[
                ("**{}**（需求）· `{}`".format(r["intake"], r.get("repo") or "")
                 if r.get("intake") else
                 "{}#{} {}".format(r.get("repo") or self.default_repo, r["issue"], r.get("url") or ""))
                for r in records[:20]
            ]),
            "引用回复那张卡片即可作答。",
        )

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
        blocks = []
        for name, rows in buckets.items():
            blocks.append("**{}**（{}）".format(name, len(rows)))
            blocks.append(bullets(*rows[:10]))
        return md(*blocks)

    def _resolve_issue_repo(self, token):
        """Which repository an issue reference points at, or a message saying why not."""
        if token:
            entry = self.registry.get(token)
            if not entry:
                return None, md(
                    "不认识仓库 `{}`。".format(token),
                    bullets(*["`{}` → `{}`".format(e.name, e.repo) for e in self.registry.all()]),
                )
            return entry.repo, None
        default = self.registry.default
        if default is None:
            return None, md(
                "这台机器服务多个仓库且没有默认，说清楚是哪个的 issue。",
                bullets(*["`{}:<issue-id>`".format(e.name) for e in self.registry.all()]),
            )
        return default.repo, None

    def _cmd_i(self, rest, inbound):
        token, number, _ = parse_issue_ref(rest)
        if number is None:
            return "用法：`/i <issue-id>`，例如 `/i 612` 或 `/i demo-gh:612`"
        repo, problem = self._resolve_issue_repo(token)
        if problem:
            return problem
        forge = self.forge_for(repo)
        try:
            issue = forge.get_issue(number)
        except Exception:  # noqa: BLE001
            return "在 `{}` 里找不到 #{}。".format(repo, number)
        st, base = state.split_state(issue.title)
        marker = state.latest_marker(forge.list_issue_comments(number)) or {}
        return md(
            "**{}#{} {}**".format(repo, number, base),
            bullets(
                "状态：{}".format(st or "UNCLAIMED"),
                "runner：{}".format(marker.get("runner") or "—"),
                "session：{}".format(marker.get("session") or "—"),
            ),
            issue.url,
        )

    def _cmd_a(self, rest, inbound):
        token, number, text = parse_issue_ref(rest)
        if number is None or not text:
            return "用法：`/a <issue-id> <你的答复>`，例如 `/a 612 用第二个方案`"
        repo, problem = self._resolve_issue_repo(token)
        if problem:
            return problem
        return self._answer(Action(ANSWER), inbound, number=number, repo=repo, text=text)

    def _cmd_new(self, rest, inbound):
        if not (rest or "").strip():
            return "用法：`/new [仓库] <需求>`"
        return self._intake(rest.strip(), inbound)

    def _cmd_repo(self, rest, inbound):
        """Point a request at a different repository, before it starts."""
        request_id, remainder = _split_id(rest)
        request = self.store.get(request_id) if request_id else None
        if not request:
            return "用法：`/repo <R-ID> <仓库名字>`，例如 `/repo R20260824-01 demo-gl`"
        entry = self.registry.get((remainder or "").strip().split(" ")[0])
        if not entry:
            return md(
                "不认识仓库 `{}`。".format((remainder or "").strip() or "（空）"),
                bullets(*["`{}` → `{}`".format(e.name, e.repo) for e in self.registry.all()]),
            )
        if request.status not in (intake_mod.PENDING, intake_mod.APPROVED):
            # The agent is already in the other checkout; moving the label would
            # only make the record lie about where the work happened.
            return "**{}** 已经在跑（或已结束），改不了了。当前：`{}`".format(
                request.id, request.repo)
        request.repo = entry.repo
        self.store.save(request)
        return "**{}** 改归 `{}`（`{}`）。".format(request.id, entry.repo, entry.name)

    def _cmd_report(self, rest, inbound):
        return self.last_report or "还没有可重发的报告。"

    #: How each open state reads to somebody who just sent a requirement. The
    #: words matter: "排入队列" used to be answered by two commands that both
    #: truthfully said "empty", because neither looked at the queue it meant.
    _OPEN_LABELS = (
        (intake_mod.PENDING, "待审批"),
        (intake_mod.APPROVED, "已批准，排队等执行"),
        (intake_mod.RUNNING, "正在执行"),
    )

    def _cmd_p(self, rest, inbound):
        blocks = []
        total = 0
        for status, label in self._OPEN_LABELS:
            rows = self.store.by_status(status)
            if not rows:
                continue
            total += len(rows)
            blocks.append("**{}（{}）**".format(label, len(rows)))
            blocks.append(bullets(*[
                "**{}** · `{}` · {} 提 — {}".format(
                    r.id, r.repo, r.requester or "?", _one_line(r.text, 60))
                for r in rows[:10]
            ]))
        if not total:
            return md(
                "没有在办的需求。",
                bullets(
                    "`/ls` 看 issue 队列（已经拆出来的活）",
                    "直接发一句话就是提新需求",
                ),
            )
        blocks.append("`/r <ID>` 看某一条 · 批准：`同意 <ID>` · 驳回：`拒绝 <ID> <理由>`")
        return md(*blocks)

    def _cmd_r(self, rest, inbound):
        request = self.store.get((rest or "").strip().split(" ")[0])
        if not request:
            return md(
            "找不到需求 `{}`。".format((rest or "").strip() or "（空）"),
            bullets("需求 ID 形如 `R20260824-01`；issue 请用 `/i <issue-id>`",
                    "`/p` 看在办的需求"),
        )
        facts = ["提出：{}".format(request.requester or "?")]
        if request.approved_by:
            facts.append("审批：{}{}".format(
                request.approved_by, "（本人提交免审批）" if request.auto_approved else ""))
        if request.session:
            facts.append("session：`{}`".format(request.session))
        if request.approval_note:
            facts.append("审批备注：{}".format(request.approval_note))
        if request.rejected_reason:
            facts.append("驳回理由：{}".format(request.rejected_reason))
        if request.error:
            facts.append("失败：{}".format(request.error))
        return md(
            "**{}** · {} · `{}`".format(request.id, request.status, request.repo),
            "> {}".format(_one_line(request.text, 200)),
            bullets(*facts),
            ("**产出**\n\n" + bullets(*request.issues)) if request.issues else "",
        )

    def _cmd_repos(self, rest, inbound):
        entries = self.registry.all()
        if not entries:
            return "还没有配置任何仓库。"
        default = self.registry.default
        return md(
            "**本机服务的仓库**",
            bullets(*[
                "`{}` → `{}`{}".format(
                    e.name, e.repo, " ← **默认**" if default and e.name == default.name else "")
                for e in entries
            ]),
            "" if default else "没有默认，裸需求无法路由：`loop repos default <名字>`",
        )

    def _cmd_approve(self, rest, inbound):
        request_id, remainder = _split_id(rest)
        if not request_id:
            return "用法：`同意 <R-ID> [仓库] [备注]`，例如 `同意 R20260824-01`"
        request = self.store.get(request_id)
        if not request:
            return "找不到 `{}`。用 `/p` 看待审批的。".format(request_id)
        repo, note = self._split_repo(remainder)
        try:
            request.approve(by=inbound.sender_nick or self.approver_nick, note=note, repo=repo)
        except intake_mod.NotPending as exc:
            return "{}（不会重复排队）".format(exc).replace("is already", "已经是")
        self.store.save(request)
        self.enqueue(request)
        return md(
            "✅ **{}** 已批准，排入执行队列。".format(request.id),
            bullets(*(
                ["仓库：`{}`".format(request.repo)]
                + (["审批备注：{}".format(note)] if note else [])
                + ["看进度：`/p`，或 `/r {}` 看这一条".format(request.id)]
            )),
            "拆完的 issue 才会出现在 `/ls` 里。",
        )

    def _cmd_reject(self, rest, inbound):
        request_id, reason = _split_id(rest)
        if not request_id:
            return "用法：`拒绝 <R-ID> <理由>`，例如 `拒绝 R20260824-01 已经做过了`"
        request = self.store.get(request_id)
        if not request:
            return "找不到 `{}`。".format(request_id)
        if len(reason.strip()) < 3:
            return "驳回要写理由——提需求的人只会看到这句话。"
        try:
            request.reject(by=inbound.sender_nick or self.approver_nick, reason=reason.strip())
        except intake_mod.NotPending as exc:
            return "{}".format(exc).replace("is already", "已经是")
        self.store.save(request)
        return "🚫 **{}** 已驳回：{}".format(request.id, reason.strip())

    def _cmd_cancel(self, rest, inbound):
        """Stop a job that is not going anywhere, and free the slot it holds."""
        request_id, _ = _split_id(rest)
        request = self.store.get(request_id) if request_id else None
        if not request:
            return md("用法：`/cancel <R-ID>`，例如 `/cancel R20260824-01`", "`/p` 看在办的。")
        if self.cancel_job is None:
            if request.status not in intake_mod.OPEN:
                return "**{}** 已经是 {}。".format(request.id, request.status)
            request.cancel(by=inbound.sender_nick)
            self.store.save(request)
            return "**{}** 已标记取消（本机没有执行器，进程未必停了）。".format(request.id)
        ok, detail = self.cancel_job(request.id, inbound.sender_nick or "")
        return ("🛑 {}".format(detail) if ok else detail)

    def _cmd_dev(self, rest, inbound):
        """Put an agent on one existing issue, now.

        Approver-only for the same reason approving is: it puts an unattended
        agent to work. The job goes through the same store and the same serial
        worker as a decomposition, so it has a log, a status and a reply.
        """
        token, number, remainder = parse_issue_ref(rest)
        if number is None:
            return "用法：`/dev <issue-id>`，例如 `/dev 612` 或 `/dev demo-gh:612`"
        if not token:
            # The trailing form predates the qualified one and still works.
            trailing, _ = self._split_repo(remainder)
            token = trailing
        entry = self.registry.get(token) if token else self.registry.default
        if entry is None:
            return "不知道该在哪个仓库开发 #{}。可用：{}".format(
                number, "、".join(self.registry.names()) or "（一个都没配）")
        request = intake_mod.Request(
            id=self.store.new_id(),
            text="Develop issue #{} in {}".format(number, entry.repo),
            kind=intake_mod.DEVELOP,
            issue=number,
            requester=inbound.sender_nick,
            requester_id=inbound.sender_id,
            conversation=inbound.conversation_id,
            repo=entry.repo,
        )
        request.approve(by=inbound.sender_nick or self.approver_nick, auto=True)
        self.store.save(request)
        self.enqueue(request)
        return "🚀 **{}** 已排入队列：在 `{}` 开发 #{}。".format(request.id, entry.repo, number)

    def _split_repo(self, text: str):
        """Pull a leading repository name out of the rest of an approval.

        Only if it actually resolves — otherwise `同意 R… 尽快` would silently
        redirect the work to nowhere and eat the note.
        """
        text = (text or "").strip()
        if not text:
            return None, ""
        head, _, tail = text.partition(" ")
        entry = self.registry.get(head)
        if entry:
            return entry.repo, tail.strip()
        return None, text

    def _cmd_skip(self, rest, inbound):
        pending_confirm, cleaned = needs_confirm(rest)
        token, number, reason = parse_issue_ref(cleaned)
        if number is None:
            return "用法：`/skip <issue-id> confirm <理由>`，例如 `/skip demo-gh:612 confirm 已经修好了`"
        repo, problem = self._resolve_issue_repo(token)
        if problem:
            return problem
        if pending_confirm:
            return "这会把 #{} 永久退出队列。确认请发：`/skip {} confirm {}`".format(number, number, reason)
        if len(reason) < 15:
            return "退掉一条 issue 要写清楚为什么不需要改代码（至少 15 个字）。"
        forge = self.forge_for(repo)
        forge.add_issue_comment(number, state.stamp(
            "**Skipped — no code change needed.**\n\n{}\n\n"
            "_由 {} 在钉钉操作。移除标题前缀即可放回队列。_".format(reason, inbound.sender_nick)))
        _set_state(forge, number, "SKIP")
        return "已退掉 #{}。".format(number)

    def _cmd_requeue(self, rest, inbound):
        pending_confirm, cleaned = needs_confirm(rest)
        token, number, _ = parse_issue_ref(cleaned)
        if number is None:
            return "用法：`/requeue <issue-id> confirm`，例如 `/requeue demo-gh:612 confirm`"
        repo, problem = self._resolve_issue_repo(token)
        if problem:
            return problem
        if pending_confirm:
            return "这会清掉 #{} 的状态前缀，下一轮可能被重新认领。确认请发：`/requeue {} confirm`".format(
                number, number)
        forge = self.forge_for(repo)
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


#: `612`, `#612`, `demo-gh:612`, `org/name:612`. The slug may contain slashes, so
#: the split is on the last colon.
_ISSUE_REF_RE = re.compile(r"^\s*(?:(?P<repo>[\w.\-/]+):)?#?(?P<number>\d+)\s*(?P<rest>.*)$",
                           re.DOTALL)
#: A requirement id starts with R and a date. Reading its digits as an issue
#: number would act on something entirely unrelated.
_REQUEST_ID_RE = re.compile(r"^\s*R\d{8}-\d+")


def parse_issue_ref(text):
    """`(repo token or None, issue number or None, the rest)`.

    A bare number means the default repository, which is what people type when
    there is only one thing it could mean. The qualified form exists because
    commands acting on an issue used to look *only* at the default, leaving
    every other repository in a multi-repository setup unreachable by name.
    """
    text = (text or "").strip()
    if not text or _REQUEST_ID_RE.match(text):
        return None, None, text
    m = _ISSUE_REF_RE.match(text)
    if not m:
        return None, None, text
    return m.group("repo"), int(m.group("number")), m.group("rest").strip()


def _split_id(text: str):
    """`(request id, the rest)` — ids look like `R20260824-01`."""
    text = (text or "").strip()
    m = re.match(r"(R\d{8}-\d+)\s*(.*)$", text, flags=re.DOTALL)
    if m:
        return m.group(1), m.group(2).strip()
    head, _, tail = text.partition(" ")
    return (head, tail.strip()) if head else (None, "")


def _one_line(text: str, limit: int = 80) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


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
