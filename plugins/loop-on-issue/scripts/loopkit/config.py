"""Repository-level configuration: `.loop-on-issue/config.json`.

Why JSON and not TOML: `tomllib` landed in Python 3.11, and macOS ships
`/usr/bin/python3` as 3.9. A plugin that cannot run on a stock Mac is not
distributable, so the config format is the one the standard library has always
been able to read.

The file is what makes these skills portable. The private versions they grew out
of hardcoded one machine — an absolute `.venv` path in the test command, `.env` as
the only file worth copying into a worktree, one particular desktop notifier as
the escalation channel. `verify_command`, `env_files` and `escalation_command`
exist to hold exactly that, so the skill text can stop naming somebody's laptop.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List, Optional

CONFIG_DIR = ".loop-on-issue"
CONFIG_FILE = "config.json"

DEFAULTS: Dict[str, Any] = {
    # Which forge, and which project on it. "auto"/null means: work it out from
    # the git remotes, falling back to asking gh/glab about this directory.
    "forge": "auto",
    "repo": None,
    # The label that puts an issue in front of the swarm. An issue carrying it is
    # startable work: the next run may claim it and spend a slot on it.
    "queue_label": "loop",
    # Whose queue this is. The swarm filters on it; an unassigned issue is
    # invisible to it, so there is no safe default to guess.
    "assignee": None,
    # What worktrees branch from and what change requests target. These move
    # together — branching from a release branch and targeting main would smuggle
    # the whole release delta into the diff.
    "base_branch": "origin/main",
    "push_remote": "origin",
    "target_remote": "auto",
    # Which agent runs a session. "claude" derives its resumable session id from
    # the issue; "codex" has to record one after the fact.
    "runner": "claude",
    # Concurrent issues. Each slot is a full session running builds, so more
    # mostly buys swap thrash on a laptop.
    "max_parallel": 2,
    # A runaway detector, not a work-time budget: an agent process can fail to
    # exit at all. Twelve hours.
    "session_timeout": 43200,
    "worktree_dir": ".worktrees",
    "template_lang": "en",
    # This repository's real verification command, injected verbatim into every
    # session brief. Without it a session invents a plausible-looking test command
    # and reports green from a run that tested nothing.
    "verify_command": None,
    # Gitignored files a worktree needs but `git worktree add` will not carry
    # over — credentials, local service endpoints. Missing ones are skipped.
    "env_files": [".env"],
    # Optional command for reaching a human faster than the next scheduled run.
    # Null means the issue thread is the only channel, which always works.
    # The built-in DingTalk channel does not go through here; this stays for
    # anyone wanting Slack, Feishu, or a pager.
    "escalation_command": None,
    # How long `loop ask` waits for an answer before giving up and letting the
    # issue pause. Zero keeps the swarm's rule that a session never blocks on a
    # human; the interactive hook overrides it with a short window.
    "ask_wait": 0,
    # How long a requirement raised in chat waits for a decision before it is
    # retired. Approved ones are never expired — that would pull work out from
    # under the agent holding it.
    "intake_ttl": 604800,
}

_ENUMS = {
    "forge": ("auto", "github", "gitlab"),
    "runner": ("claude", "codex"),
    "template_lang": ("en", "zh"),
}

_POSITIVE_INTS = ("max_parallel", "session_timeout")
_NON_NEGATIVE_INTS = ("ask_wait", "intake_ttl")
_LISTS = ("env_files",)
_STRINGS = (
    "queue_label",
    "base_branch",
    "push_remote",
    "target_remote",
    "worktree_dir",
)


class ConfigError(Exception):
    """The configuration file is unusable, and guessing would be worse."""


class Config:
    """Defaults overlaid with whatever the repository specified."""

    def __init__(self, data: Optional[Dict[str, Any]] = None, path: Optional[str] = None):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        self.unknown: List[str] = []
        for key, value in (data or {}).items():
            if key in DEFAULTS:
                self.data[key] = value
            elif not key.startswith("_"):
                # Forward compatibility: a file written by a newer plugin must not
                # stop an older one from running. Reported by the doctor, not fatal.
                self.unknown.append(key)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["data"][name]
        except KeyError:
            raise AttributeError(name)

    def with_overrides(self, **overrides: Any) -> "Config":
        """A copy with non-null overrides applied.

        `None` means "the flag was not passed", so it must not erase a configured
        value — otherwise every command would need to know which of its flags the
        user actually typed.
        """
        merged = copy.deepcopy(self.data)
        for key, value in overrides.items():
            if value is None or key not in DEFAULTS:
                continue
            merged[key] = value
        clone = Config(merged, self.path)
        clone.unknown = list(self.unknown)
        return clone

    def validate(self) -> "Config":
        for key, allowed in _ENUMS.items():
            value = self.data.get(key)
            if value not in allowed:
                raise ConfigError(
                    "{!r} is not a valid {!r}; allowed: {}".format(
                        value, key, ", ".join(allowed)
                    )
                )
        for key in _POSITIVE_INTS:
            value = self.data.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError("{!r} must be a positive integer, got {!r}".format(key, value))
        for key in _NON_NEGATIVE_INTS:
            value = self.data.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError("{!r} must be a non-negative integer, got {!r}".format(key, value))
        for key in _LISTS:
            value = self.data.get(key)
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ConfigError("{!r} must be a list of strings, got {!r}".format(key, value))
        for key in _STRINGS:
            value = self.data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError("{!r} must be a non-empty string, got {!r}".format(key, value))
        return self

    def as_json(self) -> str:
        ordered = {key: self.data[key] for key in DEFAULTS}
        return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# locating and reading the file
# --------------------------------------------------------------------------- #


def find_config(start: Optional[str] = None) -> Optional[str]:
    """Walk up from `start` looking for the config file.

    Walking up rather than checking one directory is what makes this work from a
    worktree or a subdirectory without every command needing the repo root first.
    """
    current = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(current, CONFIG_DIR, CONFIG_FILE)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load(start: Optional[str] = None) -> Config:
    path = find_config(start)
    if path is None:
        return Config()
    try:
        with open(path) as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise ConfigError("{} is not valid JSON: {}".format(path, exc))
    except OSError as exc:
        raise ConfigError("could not read {}: {}".format(path, exc))
    if not isinstance(data, dict):
        raise ConfigError("{} must contain a JSON object, got {}".format(path, type(data).__name__))
    return Config(data, path)


def save(config: Config, path: str) -> str:
    """Write every key explicitly, so the file documents its own vocabulary."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(config.as_json())
    return path


def default_path(repo_root: str) -> str:
    return os.path.join(repo_root, CONFIG_DIR, CONFIG_FILE)
