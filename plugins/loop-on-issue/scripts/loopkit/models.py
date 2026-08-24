"""Normalised shapes shared by both forge backends.

GitHub and GitLab disagree on nearly every field name — `iid` versus `number`,
`notes` versus `comments`, `web_url` versus `html_url` — so each backend
translates into these dataclasses at its boundary. Everything above the forge
layer (the state machine, the doctor, the CLI) then works on one shape and needs
no idea which forge it is talking to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Issue:
    number: int
    title: str
    state: str  # "opened" | "closed", normalised from GitHub's "open"/"closed"
    url: str
    labels: List[str] = field(default_factory=list)
    assignees: List[str] = field(default_factory=list)
    body: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Comment:
    id: Any
    author: str
    created_at: str
    body: str
    system: bool = False


@dataclass
class ChangeRequest:
    """A GitHub pull request or a GitLab merge request."""

    number: int
    title: str
    state: str  # "opened" | "merged" | "closed"
    url: str
    source_branch: str = ""
    target_branch: str = ""
    draft: bool = False
    created_at: str = ""

    @property
    def merged(self) -> bool:
        return self.state == "merged"


@dataclass
class ReviewThread:
    id: str
    resolved: bool
    author: str
    created_at: str
    body: str
    path: Optional[str] = None


@dataclass
class Repo:
    """Where the issues and change requests live."""

    forge: str  # "github" | "gitlab"
    host: str
    path: str  # "owner/name" on GitHub, "group/subgroup/project" on GitLab

    @property
    def owner(self) -> str:
        return self.path.rsplit("/", 1)[0]

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def as_dict(self) -> Dict[str, str]:
        return {"forge": self.forge, "host": self.host, "path": self.path}
