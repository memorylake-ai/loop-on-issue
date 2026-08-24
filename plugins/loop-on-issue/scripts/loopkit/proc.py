"""Subprocess plumbing shared by every backend.

Kept in its own module so tests can point the forge backends at a fake `gh` or
`glab` on `PATH` and assert on what was actually invoked.
"""

from __future__ import annotations

import subprocess
from typing import List, Optional


class CommandError(RuntimeError):
    """A subprocess failed, or its output could not be understood."""

    def __init__(self, message: str, returncode: int = 1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def run(
    cmd: List[str],
    stdin: Optional[str] = None,
    cwd: Optional[str] = None,
    check: bool = True,
) -> str:
    """Run a command and return stdout.

    `check=False` returns stdout even on failure, for probes where a non-zero exit
    is itself the answer (`gh auth status` on a logged-out machine, say).
    """
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=cwd,
        )
    except OSError as exc:
        raise CommandError("{}: {}".format(cmd[0], exc), 1, str(exc))
    if check and proc.returncode != 0:
        raise CommandError(
            "command failed ({}): {}\n{}".format(
                proc.returncode, " ".join(cmd), (proc.stderr or "").strip()
            ),
            proc.returncode,
            proc.stderr or "",
        )
    return proc.stdout


def which(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name)
