"""Work out which forge this repository lives on, and under what path.

Getting this wrong is not a soft failure: aiming GitLab's REST shape at GitHub
produces a wall of 404s that reads like an auth problem. So the rule is to decide
only when the evidence is unambiguous, and otherwise ask the installed CLIs
rather than guess from a hostname.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

from .models import Repo
from .proc import CommandError, run

GITHUB = "github"
GITLAB = "gitlab"

# scp-style (`git@host:path`), or a URL with a scheme (`https://`, `ssh://`,
# `git://`). Anything else — a bare path, a relative path — is a local remote and
# carries no forge.
_SCP_RE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^/:]+):(?P<path>.+)$")
_URL_RE = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://"
    r"(?:[^@/]+@)?"            # credentials, discarded
    r"(?P<host>[^/:]+)"
    r"(?::\d+)?"               # port, discarded
    r"/(?P<path>.+)$"
)


@dataclass
class RemoteURL:
    host: str
    path: str


def _clean_path(path: str) -> str:
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path.strip("/")


def parse_remote_url(url: Optional[str]) -> Optional[RemoteURL]:
    """Split a git remote URL into host and `owner/name` path.

    The path is kept whole rather than truncated to two segments: GitLab projects
    nest arbitrarily deep (`group/sub/deep/proj`), and dropping the middle would
    address a different project entirely.
    """
    if not url:
        return None
    url = url.strip()

    m = _URL_RE.match(url)
    if m:
        # `file://` is a local remote wearing a URL; it has no forge.
        if m.group("scheme").lower() == "file":
            return None
        path = _clean_path(m.group("path"))
        return RemoteURL(m.group("host"), path) if path else None

    if url.startswith("/") or url.startswith(".") or url.startswith("~"):
        return None

    m = _SCP_RE.match(url)
    if m:
        path = _clean_path(m.group("path"))
        # `host:/absolute/path` is an scp-style *local* copy, not a project path.
        if not path or path.startswith("/"):
            return None
        return RemoteURL(m.group("host"), path)

    return None


def forge_from_host(host: Optional[str]) -> Optional[str]:
    """Name the forge a host belongs to, or `None` when it is not decidable.

    Enterprise installs are usually named for the product (`github.acme.net`,
    `gitlab.acme.net`), which covers most self-hosted cases. A host that says
    nothing — `git.acme.internal` — returns `None`, and so does a dotless SSH
    alias like `github-work`, whose real destination lives in `~/.ssh/config`
    and may well be neither. The caller probes the CLIs instead of guessing:
    a wrong forge produces a wall of 404s that reads like an auth failure.
    """
    if not host:
        return None
    labels = host.lower().split(".")
    if len(labels) < 2:
        return None
    if "github" in labels:
        return GITHUB
    if "gitlab" in labels:
        return GITLAB
    return None


def remote_order(names: List[str], prefer: Optional[str] = None) -> List[str]:
    """Which remote to believe about where issues live.

    `upstream` outranks `origin` because of the fork workflow: branches are pushed
    to a personal `origin`, while issues and change requests live upstream. An
    explicit preference from config outranks both, but only if that remote exists.
    """
    ordered: List[str] = []
    if prefer and prefer in names:
        ordered.append(prefer)
    for name in ("upstream", "origin"):
        if name in names and name not in ordered:
            ordered.append(name)
    for name in names:
        if name not in ordered:
            ordered.append(name)
    return ordered


# --------------------------------------------------------------------------- #
# git and CLI probes
# --------------------------------------------------------------------------- #


def git_remotes(cwd: Optional[str] = None) -> List[str]:
    try:
        out = run(["git", "remote"], cwd=cwd)
    except CommandError:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def remote_url(name: str, cwd: Optional[str] = None) -> Optional[str]:
    try:
        return run(["git", "remote", "get-url", name], cwd=cwd).strip()
    except CommandError:
        return None


def _probe_cli(cwd: Optional[str]) -> Optional[Repo]:
    """Ask the installed CLIs which project this directory is.

    This is what rescues SSH host aliases and unbranded self-hosted instances:
    both CLIs resolve the remote through their own configuration, including
    `~/.ssh/config` and `GH_HOST` / `glab` host entries, so a successful answer is
    authoritative in a way a hostname regex cannot be.
    """
    try:
        out = run(["gh", "repo", "view", "--json", "nameWithOwner,url"], cwd=cwd)
    except (CommandError, FileNotFoundError):
        pass
    else:
        import json

        try:
            data = json.loads(out)
            path = data.get("nameWithOwner")
            if path:
                host = "github.com"
                parsed = parse_remote_url(data.get("url") or "")
                if parsed:
                    host = parsed.host
                return Repo(GITHUB, host, path)
        except (ValueError, KeyError):
            pass

    try:
        out = run(["glab", "repo", "view", "-F", "json"], cwd=cwd)
    except (CommandError, FileNotFoundError):
        return None
    import json

    try:
        data = json.loads(out)
    except ValueError:
        return None
    path = data.get("path_with_namespace") or data.get("full_path")
    if not path:
        return None
    web = parse_remote_url(data.get("web_url") or "")
    return Repo(GITLAB, web.host if web else "gitlab.com", path)


def detect(
    cwd: Optional[str] = None,
    forge: Optional[str] = None,
    repo_path: Optional[str] = None,
    prefer_remote: Optional[str] = None,
) -> Repo:
    """Resolve the repository issues and change requests live in.

    Order: explicit configuration, then remote URLs (`upstream` before `origin`),
    then a CLI probe. Raises rather than guessing when all three come up empty —
    a wrong forge fails later, further from the cause, and much less legibly.
    """
    forge = None if forge in (None, "auto") else forge
    names = git_remotes(cwd)
    parsed = []
    for name in remote_order(names, prefer_remote):
        url = parse_remote_url(remote_url(name, cwd))
        if url:
            parsed.append(url)

    if forge and repo_path:
        host = next(
            (u.host for u in parsed if forge_from_host(u.host) == forge),
            parsed[0].host if parsed else _default_host(forge),
        )
        return Repo(forge, host, repo_path.strip("/"))

    for url in parsed:
        guess = forge or forge_from_host(url.host)
        if guess:
            return Repo(guess, url.host, (repo_path or url.path).strip("/"))

    probed = _probe_cli(cwd)
    if probed:
        if repo_path:
            probed.path = repo_path.strip("/")
        if forge:
            probed.forge = forge
        return probed

    raise CommandError(
        "could not tell whether this repository is on GitHub or GitLab.\n"
        "       remotes seen: {}\n"
        '       Fix it by setting "forge" and "repo" in .loop-on-issue/config.json, '
        "or run `loop init`.".format(", ".join(names) or "none")
    )


def _default_host(forge: str) -> str:
    if forge == GITHUB:
        return os.environ.get("GH_HOST") or "github.com"
    return "gitlab.com"
