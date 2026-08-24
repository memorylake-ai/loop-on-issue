"""What `loop init` writes, and the two things it deliberately refuses to do.

Init is doctor plus remediation, and it stays boring on purpose: it plans, shows
the plan, and only then writes. Two boundaries are hard.

**It creates exactly one label — the queue label.** Both forges create a label on
first use, so a typo files work under a label no board filters on and nothing
errors. That silent failure is the entire reason the create path validates labels
at all, and init's convenience must not dilute it: every *other* unknown label
stays a hard failure with close matches named.

**It never runs `gh auth login` or `glab auth login`.** Those are interactive and
touch credentials; init prints the command and lets the human run it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Optional

from . import config as cfg
from . import templates as tpl
from .errors import Precondition
from .models import Repo
from .proc import CommandError

CREATE = "create"
OVERWRITE = "overwrite"
EXISTS = "exists"
DONE = "done"
FAILED = "failed"


@dataclass
class Action:
    kind: str      # "config" | "template" | "label"
    target: str
    status: str
    detail: str = ""
    content: Optional[str] = None

    @property
    def pending(self) -> bool:
        return self.status in (CREATE, OVERWRITE)

    def as_dict(self):
        return {"kind": self.kind, "target": self.target, "status": self.status, "detail": self.detail}


def plan(
    root: str,
    repo: Repo,
    config: Any,
    lang: Optional[str] = None,
    force: bool = False,
    existing_labels: Optional[List[str]] = None,
) -> List[Action]:
    """Everything init would write, without writing any of it."""
    lang = lang or config.template_lang
    actions: List[Action] = []

    config_path = cfg.default_path(root)
    if os.path.exists(config_path) and not force:
        actions.append(Action("config", _rel(root, config_path), EXISTS, "left as it is"))
    else:
        actions.append(
            Action(
                "config",
                _rel(root, config_path),
                OVERWRITE if os.path.exists(config_path) else CREATE,
                "every key written explicitly, so the file documents itself",
                content=config.as_json(),
            )
        )

    for kind in ("issue", "pr"):
        relpath = tpl.SCAFFOLD_TARGET[repo.forge][kind]
        path = os.path.join(root, relpath)
        body = tpl.front_matter_for(kind, config.queue_label, repo.forge) + tpl.bundled(kind, lang)
        if os.path.exists(path) and not force:
            actions.append(Action("template", relpath, EXISTS, "left as it is"))
        else:
            actions.append(
                Action(
                    "template",
                    relpath,
                    OVERWRITE if os.path.exists(path) else CREATE,
                    "{} template, {}".format(kind, lang),
                    content=body,
                )
            )

    if existing_labels is not None:
        if config.queue_label in existing_labels:
            actions.append(Action("label", config.queue_label, EXISTS, "already defined"))
        else:
            actions.append(
                Action(
                    "label",
                    config.queue_label,
                    CREATE,
                    "the only label this tooling ever creates",
                )
            )
    return actions


def apply(actions: List[Action], root: str, forge: Any = None) -> List[Action]:
    """Carry out a plan. Anything not pending is left untouched."""
    for action in actions:
        if not action.pending:
            continue
        try:
            if action.kind in ("config", "template"):
                path = os.path.join(root, action.target)
                directory = os.path.dirname(path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(path, "w") as fh:
                    fh.write(action.content or "")
                action.status = DONE
            elif action.kind == "label":
                if forge is None:
                    action.status = FAILED
                    action.detail = "no forge available to create the label"
                    continue
                forge.create_label(
                    action.target,
                    description="Queued for the loop-on-issue swarm",
                )
                action.status = DONE
        except (OSError, CommandError, Precondition) as exc:
            action.status = FAILED
            action.detail = str(exc)
    return actions


def _rel(root: str, path: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path
