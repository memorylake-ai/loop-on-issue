"""Which repositories this machine's bot serves.

Per-repository settings live in that repository (`.loop-on-issue/config.json`),
but a bot taking requirements in chat has to know about several before it knows
which one a request belongs to. So the registry is machine-level, next to the
credentials, and maps a short name people actually say — "bloom" — onto a project
path and a local checkout.

Both halves are needed: the project path addresses the forge, and the checkout is
where a decomposition agent has to run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".loop-on-issue", "repos.json")


class RegistryError(Exception):
    """The registry file is unusable, and guessing would be worse."""


@dataclass
class Entry:
    name: str
    repo: str
    path: str

    def as_dict(self) -> Dict[str, str]:
        return {"repo": self.repo, "path": self.path}


class Registry:
    def __init__(self, entries: Optional[Dict[str, Entry]] = None, default_name: Optional[str] = None):
        self._entries = entries or {}
        self._default_name = default_name

    # -- loading -------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Registry":
        path = path or DEFAULT_PATH
        if not os.path.isfile(path):
            # A bot serving a single repository never needs this file.
            return cls()
        try:
            with open(path) as fh:
                data = json.load(fh)
        except ValueError as exc:
            raise RegistryError("{} is not valid JSON: {}".format(path, exc))
        except OSError as exc:
            raise RegistryError("could not read {}: {}".format(path, exc))
        if not isinstance(data, dict):
            raise RegistryError("{} must contain a JSON object".format(path))

        entries = {}
        for name, value in (data.get("repos") or {}).items():
            if not isinstance(value, dict):
                continue
            entries[name] = Entry(
                name=name,
                repo=(value.get("repo") or "").strip("/"),
                path=os.path.abspath(os.path.expanduser(value.get("path") or "")),
            )
        return cls(entries, data.get("default"))

    def save(self, path: Optional[str] = None) -> str:
        path = path or DEFAULT_PATH
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {
            "default": self._default_name,
            "repos": {name: entry.as_dict() for name, entry in sorted(self._entries.items())},
        }
        with open(path, "w") as fh:
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return path

    # -- reading -------------------------------------------------------------
    def names(self) -> List[str]:
        return sorted(self._entries)

    def all(self) -> List[Entry]:
        return [self._entries[name] for name in self.names()]

    def get(self, name: Optional[str]) -> Optional[Entry]:
        """Resolve a short name or a full project path, case-insensitively.

        People say "bloom" in chat and "org/bloom" in a config file. Requiring one
        spelling means the person approving has to remember which one this bot
        wants, at the moment they are least inclined to look it up.
        """
        if not name:
            return None
        needle = name.strip().strip("/").lower()
        for entry in self._entries.values():
            if entry.name.lower() == needle or entry.repo.lower() == needle:
                return entry
        return None

    @property
    def default(self) -> Optional[Entry]:
        """The repository a request belongs to when nobody said.

        A single registered repository is the default without being told. Several,
        with none stated, has **no** default — picking one arbitrarily would file
        work in the wrong repository, silently, and the mistake surfaces as a
        stranger's issue tracker filling up.
        """
        if self._default_name:
            found = self.get(self._default_name)
            if found:
                return found
        if len(self._entries) == 1:
            return next(iter(self._entries.values()))
        return None

    # -- writing -------------------------------------------------------------
    def add(self, name: str, repo: str, path: str) -> Entry:
        entry = Entry(name=name, repo=repo.strip("/"),
                      path=os.path.abspath(os.path.expanduser(path)))
        self._entries[name] = entry
        return entry

    def remove(self, name: str) -> bool:
        entry = self.get(name)
        if not entry:
            return False
        del self._entries[entry.name]
        if self._default_name and self._default_name.lower() == entry.name.lower():
            self._default_name = None
        return True

    def set_default(self, name: str) -> None:
        entry = self.get(name)
        if not entry:
            raise RegistryError("{!r} is not registered".format(name))
        self._default_name = entry.name
