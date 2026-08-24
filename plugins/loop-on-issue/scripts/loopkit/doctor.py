"""Answer "is this machine and this repository actually ready?" before a run.

An unattended loop compounds a bad setup across runs: a missing queue label means
every scan comes back empty and looks like an idle queue; an unresolvable
assignee means issues are created, look right on the board, and are never picked
up. Both are silent. This turns them into one screen with a paste-ready fix.

Every check is isolated and tolerant of the ones before it — a machine with no
`gh` installed should still be told about its missing templates in the same pass,
rather than one problem per invocation.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import templates as tpl
from . import config as cfg
from . import remotes, runner as runner_mod
from .errors import Precondition
from .forge import for_repo
from .models import Repo
from .proc import CommandError, run, which

OK = "ok"
WARN = "warn"
FAIL = "fail"

_INSTALL_HINT = {
    "gh": {
        "Darwin": "brew install gh",
        "Linux": "see https://github.com/cli/cli#installation",
        "Windows": "winget install --id GitHub.cli",
    },
    "glab": {
        "Darwin": "brew install glab",
        "Linux": "see https://gitlab.com/gitlab-org/cli#installation",
        "Windows": "winget install --id GLab.GLab",
    },
}


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str = ""
    fix: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    repo: Optional[Repo] = None
    config: Optional[Any] = None
    repo_root: Optional[str] = None

    def add(self, *args: Any, **kwargs: Any) -> Check:
        check = Check(*args, **kwargs)
        self.checks.append(check)
        return check

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def exit_code(self) -> int:
        """0 when nothing is broken.

        Warnings are printed but do not fail the run: an unset `verify_command`
        is worth knowing about and is not a reason to refuse to start.
        """
        return 2 if self.failures else 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": not self.failures,
            "repo": self.repo.as_dict() if self.repo else None,
            "config": self.config.path if self.config else None,
            "checks": [c.as_dict() for c in self.checks],
        }


def install_hint(cli: str) -> str:
    return _INSTALL_HINT.get(cli, {}).get(platform.system(), "install {}".format(cli))


def repo_root(cwd: Optional[str] = None) -> Optional[str]:
    try:
        return run(["git", "rev-parse", "--show-toplevel"], cwd=cwd).strip() or None
    except CommandError:
        return None


def diagnose(cwd: Optional[str] = None, config: Optional[Any] = None) -> Report:
    cwd = cwd or os.getcwd()
    report = Report()

    root = repo_root(cwd)
    report.repo_root = root
    if root:
        report.add("git.repo", "Git repository", OK, root)
    else:
        report.add(
            "git.repo", "Git repository", FAIL,
            "{} is not inside a git repository".format(cwd),
            "cd into the repository you want to work on",
        )
        return report

    _check_identity(report, root)

    if config is None:
        try:
            config = cfg.load(root)
        except cfg.ConfigError as exc:
            report.add("config.file", "Configuration", FAIL, str(exc),
                       "fix the JSON, or delete it and run `loop init`")
            config = cfg.Config()
    report.config = config
    _check_config(report, config, root)

    repo = _check_forge(report, config, root)
    report.repo = repo
    if repo is None:
        return report

    forge = for_repo(repo)
    if not _check_cli(report, forge):
        return report
    if not _check_auth(report, forge):
        return report
    _check_access(report, forge)
    _check_queue_label(report, forge, config)
    _check_assignee(report, forge, config)
    _check_templates(report, repo, config, root)
    _check_runner(report, config)
    _check_base_branch(report, config, root)
    _check_link(report)
    _check_chat(report, config)
    return report


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #


def _check_identity(report: Report, root: str) -> None:
    name = _git_config("user.name", root)
    email = _git_config("user.email", root)
    if name and email:
        report.add("git.identity", "Git identity", OK, "{} <{}>".format(name, email))
    else:
        report.add(
            "git.identity", "Git identity", FAIL,
            "user.name or user.email is unset, so commits from a session will fail",
            'git config --global user.name "Your Name" && '
            'git config --global user.email "you@example.com"',
        )


def _git_config(key: str, root: str) -> str:
    try:
        return run(["git", "config", "--get", key], cwd=root).strip()
    except CommandError:
        return ""


def _check_config(report: Report, config: Any, root: str) -> None:
    if config.path is None:
        report.add(
            "config.file", "Configuration", WARN,
            "no {}/{} — running on defaults".format(cfg.CONFIG_DIR, cfg.CONFIG_FILE),
            "loop init",
        )
    else:
        report.add("config.file", "Configuration", OK, config.path)
    if getattr(config, "unknown", None):
        report.add(
            "config.unknown", "Unrecognised config keys", WARN,
            ", ".join(config.unknown),
            "remove them, or upgrade the plugin if they belong to a newer version",
        )
    try:
        config.validate()
    except cfg.ConfigError as exc:
        report.add("config.valid", "Configuration values", FAIL, str(exc),
                   "edit {}".format(config.path or "the config file"))


def _check_forge(report: Report, config: Any, root: str) -> Optional[Repo]:
    names = remotes.git_remotes(root)
    if not names:
        report.add(
            "git.remotes", "Git remotes", FAIL, "this repository has no remotes",
            "git remote add origin <url>",
        )
        return None
    report.add("git.remotes", "Git remotes", OK, ", ".join(names))
    try:
        repo = remotes.detect(
            cwd=root,
            forge=config.forge,
            repo_path=config.repo,
            prefer_remote=None if config.target_remote == "auto" else config.target_remote,
        )
    except CommandError as exc:
        report.add(
            "forge.detect", "Forge", FAIL, str(exc),
            'set "forge" and "repo" in {}/{}'.format(cfg.CONFIG_DIR, cfg.CONFIG_FILE),
        )
        return None
    report.add(
        "forge.detect", "Forge", OK,
        "{} · {} · {}".format(repo.forge, repo.host, repo.path),
    )
    return repo


def _check_cli(report: Report, forge: Any) -> bool:
    path = which(forge.cli)
    if path:
        version = ""
        try:
            version = run([forge.cli, "--version"], check=False).strip().splitlines()[0]
        except (CommandError, IndexError):
            pass
        report.add("cli.installed", "{} CLI".format(forge.cli), OK,
                   "{}{}".format(path, "  ({})".format(version) if version else ""))
        return True
    report.add(
        "cli.installed", "{} CLI".format(forge.cli), FAIL,
        "{} is not on PATH; every forge operation goes through it".format(forge.cli),
        install_hint(forge.cli),
    )
    return False


def _check_auth(report: Report, forge: Any) -> bool:
    ok, detail = forge.auth_status()
    host = forge.repo.host
    if ok:
        report.add("cli.auth", "{} authentication".format(forge.cli), OK,
                   _first_meaningful_line(detail) or "authenticated")
        _check_scopes(report, forge, detail)
        return True
    report.add(
        "cli.auth", "{} authentication".format(forge.cli), FAIL,
        _first_meaningful_line(detail) or "not authenticated",
        "{} auth login --hostname {}".format(forge.cli, host),
    )
    return False


def _check_scopes(report: Report, forge: Any, detail: str) -> None:
    """GitHub only: a token without `repo` cannot write issues or labels."""
    if forge.name != "github" or "Token scopes" not in (detail or ""):
        return
    scopes = detail.split("Token scopes:", 1)[1].splitlines()[0]
    if "'repo'" in scopes or '"repo"' in scopes:
        report.add("cli.scopes", "Token scopes", OK, scopes.strip())
        return
    report.add(
        "cli.scopes", "Token scopes", FAIL,
        "missing 'repo'; issue and label writes will be rejected —{}".format(scopes),
        "gh auth refresh -h {} -s repo,read:org".format(forge.repo.host),
    )


def _check_access(report: Report, forge: Any) -> None:
    can_write, detail = forge.permissions()
    if can_write:
        report.add("repo.access", "Repository access", OK, detail)
    else:
        report.add(
            "repo.access", "Repository access", FAIL, detail,
            "ask for write access to {}, or point \"repo\" at a fork you can push to".format(
                forge.repo.path
            ),
        )


def _check_queue_label(report: Report, forge: Any, config: Any) -> None:
    try:
        labels = forge.list_labels()
    except (CommandError, Precondition) as exc:
        report.add("labels.list", "Labels", FAIL, str(exc), "")
        return
    if config.queue_label in labels:
        report.add("labels.queue", "Queue label", OK,
                   "{!r} exists ({} labels defined)".format(config.queue_label, len(labels)))
    else:
        report.add(
            "labels.queue", "Queue label", FAIL,
            "{!r} is not defined; the swarm will scan an empty queue forever".format(
                config.queue_label
            ),
            "loop init --yes   (creates it; this is the only label the tooling creates)",
        )


def _check_assignee(report: Report, forge: Any, config: Any) -> None:
    if not config.assignee:
        report.add(
            "assignee.set", "Queue owner", WARN,
            'no "assignee" configured; the swarm cannot guess whose queue this is',
            'set "assignee" in {}/{}'.format(cfg.CONFIG_DIR, cfg.CONFIG_FILE),
        )
        return
    try:
        forge.resolve_assignee(config.assignee)
    except Precondition as exc:
        report.add("assignee.set", "Queue owner", FAIL, str(exc).splitlines()[0],
                   "correct the username; an unassignable issue is never picked up")
        return
    except CommandError as exc:
        report.add("assignee.set", "Queue owner", WARN, str(exc), "")
        return
    report.add("assignee.set", "Queue owner", OK, config.assignee)


def _check_templates(report: Report, repo: Repo, config: Any, root: str) -> None:
    for kind, label in (("issue", "Issue template"), ("pr", "Change request template")):
        resolved = tpl.resolve(kind, root, repo.forge, config.template_lang)
        if resolved.source == "bundled":
            report.add(
                "template.{}".format(kind), label, WARN,
                "using the plugin's built-in default; nobody opening one in the web UI sees it",
                "loop init   (writes {})".format(tpl.SCAFFOLD_TARGET[repo.forge][kind]),
            )
            continue
        note = "{} ({})".format(resolved.path, resolved.source)
        if kind == "issue" and not tpl.has_acceptance_criteria(resolved.text):
            report.add(
                "template.{}".format(kind), label, WARN,
                "{} — no acceptance-criteria section, so an issue is done when the "
                "session decides it is".format(note),
                "add a section naming what must hold for the work to be finished",
            )
        else:
            report.add("template.{}".format(kind), label, OK, note)


def _check_runner(report: Report, config: Any) -> None:
    name = config.runner
    path = which(name)
    if path:
        report.add("runner.binary", "Runner", OK, "{} ({})".format(name, path))
    else:
        report.add(
            "runner.binary", "Runner", FAIL,
            "{!r} is not on PATH, so no issue can be developed".format(name),
            "install it, or set \"runner\" to one you have",
        )
    if name == runner_mod.CODEX:
        report.add(
            "runner.codex", "Codex session bookkeeping", WARN,
            "codex exec cannot be given a session id, so ids are recorded in issue "
            "comments; deleting those comments loses the ability to resume",
            "",
        )
    if not config.verify_command:
        report.add(
            "verify.command", "Verification command", WARN,
            'no "verify_command" set; a session will invent a plausible test command '
            "and may report green from a run that tested nothing",
            'set "verify_command" in {}/{}'.format(cfg.CONFIG_DIR, cfg.CONFIG_FILE),
        )
    else:
        report.add("verify.command", "Verification command", OK, config.verify_command)


def _check_link(report: Report) -> None:
    """Is the CLI findable by name?

    Skills fall back to scanning the plugin directories when it is not, which
    works but rescans two trees every session and breaks whenever a version bump
    moves the plugin cache.
    """
    from . import link as link_mod

    found = which(link_mod.NAME)
    if found:
        report.add("cli.onpath", "loop on PATH", OK, found)
        return
    report.add(
        "cli.onpath", "loop on PATH", WARN,
        "not findable by name; skills fall back to scanning the plugin directories",
        "loop init --yes --link",
    )


def _check_chat(report: Report, config: Any) -> None:
    """The chat channel is optional throughout, so nothing here can fail a run.

    Without it a blocker still lands on the issue and the next scheduled run still
    picks up the answer; what is lost is the minutes-instead-of-an-interval path,
    and the ability to put work in from where the team talks.
    """
    from . import dingtalk as dt
    from . import pending as pending_mod

    env = dt.load_env()
    client = dt.DingTalk(env)
    if not client.can_send:
        report.add(
            "chat.configured", "Chat channel", WARN,
            "no DingTalk configured; questions live on the issue only and wait for "
            "the next scheduled run",
            "write credentials to ~/.loop-on-issue/dingtalk.env — see dingtalk/README.md",
        )
        return
    if not client.configured:
        report.add(
            "chat.configured", "Chat channel", WARN,
            "webhook only: notifications go out, but nobody can answer in DingTalk",
            "add DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET to answer from chat",
        )
    else:
        report.add("chat.configured", "Chat channel", OK, "DingTalk app robot")

    conv = dt.conversations(env)
    if client.configured and not conv:
        report.add(
            "chat.conversations", "Chat allow-list", WARN,
            "empty, so the listener ignores every message (deliberately fail-closed)",
            "send `@bot /whoami` in the group and paste the conversation id in",
        )
    elif conv:
        report.add("chat.conversations", "Chat allow-list", OK, ", ".join(conv))

    if client.configured and not env.get("LOOP_DINGTALK_APPROVER"):
        report.add(
            "chat.approver", "Requirement approver", WARN,
            "unset, so nobody can approve a requirement raised in chat",
            "send `@bot /whoami` as the approver and paste the staffId in",
        )
    elif env.get("LOOP_DINGTALK_APPROVER"):
        report.add("chat.approver", "Requirement approver", OK,
                   env.get("LOOP_DINGTALK_APPROVER_NICK") or env["LOOP_DINGTALK_APPROVER"])

    waiting = len(pending_mod.Index().all())
    if waiting:
        report.add("chat.pending", "Open questions", WARN,
                   "{} question(s) waiting on a human".format(waiting), "loop dingtalk sweep")


def _check_base_branch(report: Report, config: Any, root: str) -> None:
    ref = config.base_branch
    try:
        sha = run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=root).strip()
    except CommandError:
        sha = ""
    if sha:
        report.add("git.base", "Base branch", OK, "{} → {}".format(ref, sha[:10]))
        return
    remote = ref.split("/", 1)[0] if "/" in ref else "origin"
    report.add(
        "git.base", "Base branch", FAIL,
        "{!r} does not resolve; worktrees would branch from nothing".format(ref),
        "git fetch {}".format(remote),
    )


def _first_meaningful_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""
