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
    return make_at(tempfile.mkdtemp(prefix="loop-repo-"), remote_url=remote_url, commit=commit)


def make_at(root, remote_url="git@github.com:acme/widget.git", commit=True):
    """Initialise a git repo at an existing (or to-be-created) path.

    For tests that need several repos side by side under one container directory,
    which is what workspace discovery walks.
    """
    os.makedirs(root, exist_ok=True)
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
