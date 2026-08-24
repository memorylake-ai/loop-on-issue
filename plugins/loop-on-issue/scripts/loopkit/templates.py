"""Resolve the issue and change-request templates a repository actually uses.

Three layers, most specific first:

1. `.loop-on-issue/templates/{issue,pr}.md` — an explicit override for a repo
   that wants the agents to follow something other than what its humans see.
2. The forge's own template location. This is the default because a template
   only worth having is one the person opening an issue in the web UI gets too;
   a template only the agent knows about drifts from reality within a week.
3. The templates bundled with the plugin, so a repo that has set nothing up
   still produces briefs with acceptance criteria in them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

KINDS = ("issue", "pr")

#: Marks a template as written for this workflow, so tooling can tell one apart
#: from whatever generic `## Description` template the repo already had.
TEMPLATE_MARKER = "<!-- loop-on-issue:template -->"

_OVERRIDE = {
    "issue": [".loop-on-issue/templates/issue.md"],
    "pr": [".loop-on-issue/templates/pr.md"],
}

_NATIVE = {
    "github": {
        "issue": [
            ".github/ISSUE_TEMPLATE/loop-task.md",
            ".github/ISSUE_TEMPLATE/loop.md",
            ".github/ISSUE_TEMPLATE.md",
            "docs/ISSUE_TEMPLATE.md",
        ],
        "pr": [
            ".github/pull_request_template.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/PULL_REQUEST_TEMPLATE/loop.md",
            "pull_request_template.md",
            "docs/pull_request_template.md",
        ],
    },
    "gitlab": {
        "issue": [
            ".gitlab/issue_templates/loop-task.md",
            ".gitlab/issue_templates/loop.md",
            ".gitlab/issue_templates/Default.md",
        ],
        "pr": [
            ".gitlab/merge_request_templates/loop.md",
            ".gitlab/merge_request_templates/Default.md",
        ],
    },
}

#: Where `init` writes, per forge and kind.
SCAFFOLD_TARGET = {
    "github": {"issue": ".github/ISSUE_TEMPLATE/loop-task.md", "pr": ".github/pull_request_template.md"},
    "gitlab": {"issue": ".gitlab/issue_templates/loop-task.md", "pr": ".gitlab/merge_request_templates/loop.md"},
}

_BUNDLE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "templates"
)

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
_ACCEPTANCE_RE = re.compile(r"accept|criteri|验收", re.IGNORECASE)


@dataclass
class Resolved:
    kind: str
    source: str  # "override" | "forge" | "bundled"
    path: Optional[str]
    text: str

    @property
    def body(self) -> str:
        """The template without YAML front matter.

        GitHub reads front matter to name a template in its chooser; GitLab
        renders it as literal text and an agent copying it into an issue body
        would ship `name: Loop task` as the first line.
        """
        return strip_front_matter(self.text)


def candidates(kind: str, forge: str) -> List[str]:
    if kind not in KINDS:
        raise ValueError("unknown template kind {!r}; expected one of {}".format(kind, ", ".join(KINDS)))
    native = _NATIVE.get(forge, {}).get(kind, [])
    return list(_OVERRIDE[kind]) + list(native)


def bundled_path(kind: str, lang: str = "en") -> str:
    if kind not in KINDS:
        raise ValueError("unknown template kind {!r}".format(kind))
    path = os.path.join(_BUNDLE_DIR, lang, "{}.md".format(kind))
    if not os.path.isfile(path):
        path = os.path.join(_BUNDLE_DIR, "en", "{}.md".format(kind))
    return path


def bundled(kind: str, lang: str = "en") -> str:
    with open(bundled_path(kind, lang)) as fh:
        return fh.read()


def resolve(kind: str, repo_root: str, forge: str, lang: str = "en") -> Resolved:
    for relpath in candidates(kind, forge):
        path = os.path.join(repo_root, relpath)
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            text = fh.read()
        if not text.strip():
            # Almost always a leftover `touch`; honouring it hands the agent an
            # empty brief, which is worse than falling through to a default.
            continue
        source = "override" if relpath.startswith(".loop-on-issue/") else "forge"
        return Resolved(kind, source, path, text)
    return Resolved(kind, "bundled", bundled_path(kind, lang), bundled(kind, lang))


# --------------------------------------------------------------------------- #
# reading a template
# --------------------------------------------------------------------------- #


def strip_front_matter(text: str) -> str:
    return _FRONT_MATTER_RE.sub("", text or "", count=1)


def slots(text: str) -> List[str]:
    """The markdown headings a template defines, in order.

    Fenced code is skipped so a `# comment` inside a shell example does not read
    as a section, and front matter is dropped so `---` keys do not either.
    """
    found: List[str] = []
    in_fence = False
    for line in strip_front_matter(text).splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            found.append(m.group("text").strip())
    return found


def is_loop_template(text: str) -> bool:
    return TEMPLATE_MARKER in (text or "")


def has_acceptance_criteria(text: str) -> bool:
    """Does this template ask for a definition of done?

    Without one, an issue is finished when the session decides it is — which is
    exactly what "done" means when nobody wrote it down.
    """
    return any(_ACCEPTANCE_RE.search(s) for s in slots(text))


def front_matter_for(kind: str, queue_label: str) -> str:
    """GitHub's template chooser metadata, prepended when scaffolding there.

    GitLab has no equivalent and renders this as text, so it is added at the
    scaffold step for GitHub only rather than living in the bundled file.
    """
    if kind != "issue":
        return ""
    return (
        "---\n"
        "name: Loop task\n"
        "about: A slice an unattended agent can pick up and finish in one session\n"
        "title: ''\n"
        "labels: {}\n"
        "assignees: ''\n"
        "---\n\n".format(queue_label)
    )
