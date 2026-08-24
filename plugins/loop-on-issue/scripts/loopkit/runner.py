"""Which agent develops an issue, and how its session is resumed.

Every issue is developed by its **own agent subprocess**, not an in-process
subagent. That is not a style choice: an in-process subagent is one-shot and has
no session id, so when an issue pauses for an answer or its change request comes
back for rework, there is nothing to resume — a fresh agent would rebuild its
reasoning from issue comments alone, then quietly redo or contradict its own work.

The two supported runners get there differently:

* **claude** can be told its session id up front, so the id is *derived* from the
  issue's identity and any later run recomputes it with no stored state at all.
* **codex** cannot. `codex exec` assigns a thread id itself, so the id has to be
  read out of the `--json` event stream on the first run and recorded in a marker
  comment for the next one. That台账 is the price of the second runner, and it is
  why `--json` is not optional there.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, List, Optional, Sequence

from .models import Repo

CLAUDE = "claude"
CODEX = "codex"
RUNNERS = (CLAUDE, CODEX)

#: `perl -e alarm` exits 128 + SIGALRM when the bound expires.
TIMEOUT_EXIT = 142

_RUNNER_LABEL_RE = re.compile(r"^runner::?(?P<name>[\w.-]+)$", re.IGNORECASE)
_ID_KEY_RE = re.compile(r"(session|thread|conversation)_?id$", re.IGNORECASE)


def session_id(repo: Repo, number: int, generation: int = 0) -> str:
    """The resumable session id for an issue.

    Deterministic, so a later run — a different process, hours later, after a
    reboot — recomputes the same id with nothing persisted. `generation` lets a
    human deliberately abandon a context that has gone bad and start fresh.

    GitLab repositories keep the original key format. Changing it would change
    every id, and issues already sitting at `[PAUSED]` on a live board would
    resume into a session that never existed.
    """
    if repo.forge == "gitlab":
        key = "loop-issue://{}#{}".format(repo.path, number)
    else:
        key = "loop-issue://{}:{}#{}".format(repo.forge, repo.path, number)
    if generation:
        key += "@{}".format(generation)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def select(
    explicit: Optional[str],
    labels: Sequence[str],
    configured: Optional[str],
    recorded: Optional[str] = None,
) -> str:
    """Which runner develops this issue.

    Precedence: an explicit request, then whatever the board says is *already*
    running this issue, then a `runner::codex` label (so a human can route a new
    issue without touching config), then config, then claude.

    The recorded runner outranks the label on purpose: once a session holds this
    issue's context, switching runners mid-issue strands it — the label is a
    routing preference for work not yet started, not a live override.

    An unrecognised *label* is ignored rather than obeyed, since a typo should not
    silently change which agent does the work; an unrecognised explicit request is
    an error, because somebody typed it deliberately.
    """
    if explicit:
        if explicit not in RUNNERS:
            raise ValueError(
                "unknown runner {!r}; expected one of {}".format(explicit, ", ".join(RUNNERS))
            )
        return explicit
    if recorded in RUNNERS:
        return recorded
    for label in labels or []:
        m = _RUNNER_LABEL_RE.match(label.strip())
        if m and m.group("name").lower() in RUNNERS:
            return m.group("name").lower()
    if configured in RUNNERS:
        return configured
    return CLAUDE


# --------------------------------------------------------------------------- #
# command construction
# --------------------------------------------------------------------------- #


def start_command(
    name: str,
    session: Optional[str],
    prompt: str,
    permission_mode: str = "acceptEdits",
    sandbox: str = "workspace-write",
) -> List[str]:
    """The command that begins an issue's session.

    Both forms are non-interactive on purpose: an unattended run that stops at an
    approval prompt holds a slot until something kills it.
    """
    if name == CLAUDE:
        cmd = ["claude", "-p", "--permission-mode", permission_mode]
        if session:
            cmd += ["--session-id", session]
        cmd.append(prompt)
        return cmd
    if name == CODEX:
        # --json is load-bearing, not diagnostics: the thread id it emits is the
        # only way this session can ever be resumed.
        return [
            "codex", "exec", "--json",
            "--sandbox", sandbox,
            "-c", "approval_policy=\"never\"",
            prompt,
        ]
    raise ValueError("unknown runner {!r}".format(name))


def resume_command(
    name: str,
    session: Optional[str],
    prompt: str,
    permission_mode: str = "acceptEdits",
    sandbox: str = "workspace-write",
) -> List[str]:
    """The command that continues an existing session.

    The brief is deliberately **not** repeated here. A resume already holds the
    issue, the plan and everything tried; re-pasting the brief invites a restart.
    """
    if not session:
        raise ValueError("cannot resume without a session id")
    if name == CLAUDE:
        return ["claude", "-p", "--permission-mode", permission_mode, "--resume", session, prompt]
    if name == CODEX:
        return [
            "codex", "exec", "resume", session,
            "--json",
            "--sandbox", sandbox,
            "-c", "approval_policy=\"never\"",
            prompt,
        ]
    raise ValueError("unknown runner {!r}".format(name))


def wrap_timeout(cmd: Sequence[str], seconds: Optional[int]) -> List[str]:
    """Bound a subprocess without `timeout(1)`, which macOS does not ship.

    This is a runaway detector, not a work-time budget: real issues do run over an
    hour, and killing one mid-progress wastes more than it saves. It exists
    because an agent process can fail to exit at all — one was found alive for
    fifteen days, still burning CPU, orphaned from a listener replaced twice since.

    Note it kills the agent process but not its process group, so look for
    stragglers if it fires repeatedly.
    """
    cmd = list(cmd)
    if not seconds:
        return cmd
    return ["perl", "-e", "alarm shift @ARGV; exec @ARGV", str(int(seconds))] + cmd


# --------------------------------------------------------------------------- #
# reading a session id back out
# --------------------------------------------------------------------------- #


def extract_session_id(stream: str) -> Optional[str]:
    """Find the session id in a runner's JSONL output.

    Codex announces it as `{"type":"thread.started","thread_id":"..."}` on the
    first line, but tracing output shares the stream and the field has been
    renamed before, so this scans rather than indexes: any JSON line, any nested
    key that looks like a session/thread/conversation id.
    """
    for line in (stream or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        found = _find_id(event)
        if found:
            return found
    return None


def _find_id(node: Any) -> Optional[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value and _ID_KEY_RE.search(key):
                return value
        for value in node.values():
            found = _find_id(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_id(value)
            if found:
                return found
    return None
