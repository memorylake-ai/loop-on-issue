"""A fake `gh` / `glab` on PATH, so the forge backends can be tested for real.

Both backends drive an external CLI rather than a raw token — a deliberate choice
(it inherits whatever auth the human already has), but it means the interesting
behaviour is in how a command line is built and how its output is parsed. Mocking
at the Python level would test neither.

Two things about the shape of this fake, both of them performance findings rather
than taste:

* It is POSIX sh, not Python. The suite spawns it hundreds of times and an
  interpreter start per call dominated everything else.
* The executable itself is written **once per process** and never rewritten.
  macOS assesses a newly created executable the first time it runs, which cost
  roughly half a second per test when each test wrote its own copy. Per-test
  routing therefore lives in a *sourced* data file, which needs no exec bit and
  no assessment.
"""

from __future__ import annotations

import atexit
import json
import os
import shlex
import shutil
import stat
import tempfile
from typing import Any, Dict, List, Optional, Sequence

_SEP = "\x1f"

_LAUNCHER = r'''#!/bin/sh
joined="$*"
stdin=""
case "$joined" in *--input*) stdin=$(cat);; esac
has() { case "$joined" in *"$1"*) return 0;; *) return 1;; esac; }
printf "%s\037%s\n" "$joined" "$stdin" >> "$FAKE_CLI_LOG"
if [ -f "$FAKE_CLI_ROUTES" ]; then . "$FAKE_CLI_ROUTES"; fi
printf "fake: no route matched: %s\n" "$joined" >&2
exit 97
'''

_BIN_DIR: Optional[str] = None


def _bin_dir(names: Sequence[str]) -> str:
    """The one directory of fake executables this process ever creates."""
    global _BIN_DIR
    if _BIN_DIR is None:
        root = tempfile.mkdtemp(prefix="loop-fakecli-bin-")
        atexit.register(shutil.rmtree, root, True)
        for name in names:
            path = os.path.join(root, name)
            with open(path, "w") as fh:
                fh.write(_LAUNCHER)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        _BIN_DIR = root
    return _BIN_DIR


class FakeCLI:
    """Installs fake executables on PATH for the duration of a test."""

    def __init__(self, names: Sequence[str] = ("gh", "glab")):
        self.bin_dir = _bin_dir(names)
        self.dir = tempfile.mkdtemp(prefix="loop-fakecli-")
        self.log = os.path.join(self.dir, "calls.log")
        self.routes_file = os.path.join(self.dir, "routes.sh")
        open(self.log, "w").close()
        self._routes: List[Dict[str, Any]] = []
        self._render()

        self._saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_CLI_LOG", "FAKE_CLI_ROUTES")}
        os.environ["PATH"] = self.bin_dir + os.pathsep + (self._saved["PATH"] or "")
        os.environ["FAKE_CLI_LOG"] = self.log
        os.environ["FAKE_CLI_ROUTES"] = self.routes_file

    # -- routes -------------------------------------------------------------
    def route(self, *match: str, **kwargs: Any) -> "FakeCLI":
        """Respond to any invocation whose argv contains all of `match`.

        Routes are tried in the order they were added, so a narrow route added
        first wins over a broader one added later.
        """
        stdout = kwargs.get("stdout", "")
        if not isinstance(stdout, str):
            stdout = json.dumps(stdout)
        self._routes.append(
            {
                "match": list(match),
                "stdout": stdout,
                "stderr": kwargs.get("stderr", ""),
                "exit": int(kwargs.get("exit", 0)),
            }
        )
        self._render()
        return self

    def hide(self, *names: str) -> "FakeCLI":
        """Make a CLI look uninstalled, for tests about a machine that lacks it."""
        for name in names:
            path = os.path.join(self.bin_dir, name)
            if os.path.exists(path):
                os.chmod(path, 0o600)
                self._unhide = getattr(self, "_unhide", [])
                self._unhide.append(path)
        return self

    def _render(self) -> None:
        lines = []
        for route in self._routes:
            cond = " && ".join("has {}".format(shlex.quote(m)) for m in route["match"]) or "true"
            lines.append("if {}; then".format(cond))
            if route["stdout"]:
                lines.append("  printf '%s' {}".format(shlex.quote(route["stdout"])))
            if route["stderr"]:
                lines.append("  printf '%s' {} >&2".format(shlex.quote(route["stderr"])))
            lines.append("  exit {}".format(route["exit"]))
            lines.append("fi")
        with open(self.routes_file, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    # -- assertions ---------------------------------------------------------
    @property
    def calls(self) -> List[Dict[str, str]]:
        with open(self.log) as fh:
            records = []
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                joined, _, stdin = line.partition(_SEP)
                records.append({"joined": joined, "stdin": stdin})
            return records

    def call_containing(self, *needles: str) -> Optional[Dict[str, str]]:
        for call in self.calls:
            if all(n in call["joined"] for n in needles):
                return call
        return None

    def cleanup(self) -> None:
        for path in getattr(self, "_unhide", []):
            os.chmod(path, 0o755)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)
