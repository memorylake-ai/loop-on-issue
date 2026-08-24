"""Put `loop` on PATH, so the CLI can be found by name.

Skills resolve the CLI with a three-step fallback: `command -v loop`, then
`$CLAUDE_PLUGIN_ROOT`, then a filesystem search. Only the first is cheap and
stable — the plugin-root variable is set for hooks but not for a skill's shell,
and the search rescans two plugin trees on every session and breaks whenever a
version bump moves the cache directory.

A symlink makes the first step hit. It is opt-in because it writes outside the
repository, into a directory on somebody's PATH.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List, Optional

NAME = "loop"

#: Conventional user-level bin directories, most preferred first.
CANDIDATES = ("~/.local/bin", "~/bin")

CREATED = "created"
ALREADY = "already"
REPOINTED = "repointed"
BLOCKED = "blocked"
FAILED = "failed"


@dataclass
class Result:
    status: str
    path: str = ""
    detail: str = ""


def choose_dir(
    candidates: Optional[List[str]] = None,
    path: Optional[str] = None,
    exists: Optional[Callable[[str], bool]] = None,
) -> str:
    """Where to put the link.

    Prefer a candidate the shell already searches. A link somewhere PATH does not
    reach is worse than no link: it looks done and changes nothing.
    """
    candidates = [os.path.expanduser(c) for c in (candidates or CANDIDATES)]
    entries = (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep)
    entries = [e.rstrip("/") for e in entries if e]
    for candidate in candidates:
        if candidate.rstrip("/") in entries:
            return candidate
    return candidates[0]


def install(source: str, directory: Optional[str] = None) -> Result:
    source = os.path.abspath(source)
    directory = os.path.expanduser(directory or choose_dir())
    target = os.path.join(directory, NAME)

    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return Result(FAILED, target, str(exc))

    if os.path.islink(target):
        if os.path.realpath(target) == os.path.realpath(source):
            return Result(ALREADY, target)
        # A stale link is the *normal* state after an upgrade: the plugin cache
        # path carries the version, so every bump moves it.
        try:
            os.unlink(target)
            os.symlink(source, target)
        except OSError as exc:
            return Result(FAILED, target, str(exc))
        return Result(REPOINTED, target)

    if os.path.exists(target):
        # Somebody may have their own `loop`. Overwriting it silently would be
        # the worst possible outcome of a convenience feature.
        return Result(BLOCKED, target, "a file that is not our symlink is already there")

    try:
        os.symlink(source, target)
    except OSError as exc:
        return Result(FAILED, target, str(exc))
    return Result(CREATED, target)


def remove(directory: Optional[str] = None) -> bool:
    """Remove the link, and only if it is a link — never somebody else's binary."""
    directory = os.path.expanduser(directory or choose_dir())
    target = os.path.join(directory, NAME)
    if not os.path.islink(target):
        return False
    try:
        os.unlink(target)
    except OSError:
        return False
    return True


def current_source() -> str:
    """The `loop` entry point inside this installation."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, NAME)
