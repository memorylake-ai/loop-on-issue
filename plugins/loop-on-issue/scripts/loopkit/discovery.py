"""Find the git repositories under a workspace container.

`loop init` operates on one repository. But people keep several side by side under
a single directory — `~/work/zootopia`, `~/work/zootopia-eval`, … — and setting
each up meant a `cd` and an `init` per repo. This walks the immediate children of
a container once and reports what it finds, so the init flow can offer them all at
once and let the human pick a few.

It reports facts, it does not decide: the forge and project path (so a repo can be
registered without asking), whether the repo is already registered or already
initialised (so re-runs are safe), the remote's default branch (so the base is not
wired to a nonexistent `origin/main`), and *candidate* verify commands scanned from
the repo's own build files. Which verify command is the right gate is a judgement —
`check-all` over `test-all`, say — so the candidates are surfaced, never chosen.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from . import remotes
from . import repos as repos_mod
from .proc import CommandError

_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:")
# Ordered by how canonical a verification gate they make: a repo's own CI script
# beats a make target beats a package script beats a bare pytest.
_PREFERRED_MAKE_TARGETS = ("test", "lint", "check")


def is_git_repo(path: str) -> bool:
    """A checkout has a `.git` entry — a directory for a normal clone, a file for
    a worktree or submodule. Either counts as a repository to set up."""
    return os.path.exists(os.path.join(path, ".git"))


def _make_targets(path: str) -> List[str]:
    for name in ("Makefile", "makefile"):
        makefile = os.path.join(path, name)
        if os.path.isfile(makefile):
            break
    else:
        return []
    targets: List[str] = []
    try:
        with open(makefile) as fh:
            for line in fh:
                match = _MAKE_TARGET.match(line)
                if match:
                    targets.append(match.group(1))
    except OSError:
        return []
    return targets


def _npm_scripts(path: str) -> Dict[str, Any]:
    pkg = os.path.join(path, "package.json")
    if not os.path.isfile(pkg):
        return {}
    try:
        with open(pkg) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def verify_candidates(path: str) -> List[str]:
    """Plausible verification commands for a repo, most canonical first.

    A best-effort list for a human to choose from, not a decision. Empty when
    nothing recognisable is present — better an unset `verify_command` the doctor
    warns about than a confident wrong guess a session reports green from.
    """
    candidates: List[str] = []

    for script in ("cicd/check-all-locally.sh", "cicd/test-all.sh"):
        if os.path.isfile(os.path.join(path, script)):
            candidates.append("./" + script)

    targets = _make_targets(path)
    for target in _PREFERRED_MAKE_TARGETS:
        if target in targets:
            candidates.append("make " + target)

    scripts = _npm_scripts(path)
    if "test" in scripts:
        candidates.append("npm test")
    if "lint" in scripts:
        candidates.append("npm run lint")

    if os.path.isfile(os.path.join(path, "pyproject.toml")) and os.path.isdir(
        os.path.join(path, "tests")
    ):
        candidates.append("python -m pytest")

    # Preserve order, drop duplicates.
    seen = set()
    ordered = []
    for cmd in candidates:
        if cmd not in seen:
            seen.add(cmd)
            ordered.append(cmd)
    return ordered


def _forge_and_path(path: str):
    """(forge, project_path) for a checkout, or (None, None) when undecidable."""
    try:
        repo = remotes.detect(cwd=path)
    except CommandError:
        return None, None
    return repo.forge, repo.path


def _already_registered(registry: "repos_mod.Registry", path: str, repo_path: Optional[str]) -> bool:
    if repo_path and registry.get(repo_path):
        return True
    real = os.path.realpath(path)
    return any(os.path.realpath(entry.path) == real for entry in registry.all())


def discover(container: str, registry: Optional["repos_mod.Registry"] = None) -> List[Dict[str, Any]]:
    """Report every git repository directly under `container`.

    Immediate children only — deliberately not recursive, so a submodule
    superproject reports as one repo rather than dragging its submodules in.
    """
    registry = registry if registry is not None else repos_mod.Registry.load()
    try:
        names = sorted(os.listdir(container))
    except OSError:
        return []

    found: List[Dict[str, Any]] = []
    for name in names:
        path = os.path.join(container, name)
        if not os.path.isdir(path) or not is_git_repo(path):
            continue
        forge, repo_path = _forge_and_path(path)
        base_ref = remotes.default_base_ref(path)
        found.append(
            {
                "name": name,
                "path": os.path.abspath(path),
                "forge": forge,
                "repo": repo_path,
                "default_branch": base_ref.split("/", 1)[1] if base_ref and "/" in base_ref else None,
                "registered": _already_registered(registry, path, repo_path),
                "has_config": os.path.isfile(os.path.join(path, ".loop-on-issue", "config.json")),
                "verify_candidates": verify_candidates(path),
            }
        )
    return found
