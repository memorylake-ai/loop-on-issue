"""A throwaway git repository, for tests that need real git behaviour."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile


def _git(cwd, *args):
    subprocess.run(
        ["git"] + list(args), cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def make(remote_url="git@github.com:acme/widget.git", commit=True):
    root = tempfile.mkdtemp(prefix="loop-repo-")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")
    if remote_url:
        _git(root, "remote", "add", "origin", remote_url)
    if commit:
        with open(os.path.join(root, "README.md"), "w") as fh:
            fh.write("# test\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")
    return root


def destroy(root):
    shutil.rmtree(root, ignore_errors=True)
