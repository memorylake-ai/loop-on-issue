"""The forge interface, and the rules both backends share.

GitHub and GitLab differ in field names, in pagination, in what an "issue" even
is — but the workflow above them does not care. Each backend translates into the
dataclasses in `models`, and everything here is what must behave identically on
both no matter how differently it is implemented underneath.
"""

from __future__ import annotations

import difflib
import re
from typing import List, Optional, Sequence

from .errors import Precondition
from .models import ChangeRequest, Comment, Issue, ReviewThread, Repo

GITHUB = "github"
GITLAB = "gitlab"

# House convention, stamped on every change request this tooling opens:
# `to #<number>: <Title>`. Tolerates a leading Draft:/WIP: and a missing colon.
_CLAIM_RE = re.compile(r"^\s*(?:draft|wip)\s*:\s*", re.IGNORECASE)


def title_claims_issue(title: Optional[str], number: int) -> bool:
    """Does this change request's title claim to implement that issue?

    Merely *mentioning* an issue is not a claim. Both forges offer a "related"
    listing that includes any change request whose description name-drops the
    issue, and picking the newest of those silently attributes a sibling's work:
    observed on 2026-08-19, where two issues both resolved to a third issue's MR
    and review feedback on the real ones could never wake them back up.

    A mis-attribution is silent and lasting. An unmatched change request merely
    reports "none", which the caller turns into a paused issue and a human
    question — so prefer the failure a person gets asked about.
    """
    if not title:
        return False
    stripped = _CLAIM_RE.sub("", title)
    return re.match(r"\s*to\s+#{}\b".format(number), stripped, flags=re.IGNORECASE) is not None


def pick_cr(candidates: Sequence[ChangeRequest]) -> Optional[ChangeRequest]:
    """Which of several claiming change requests to report.

    Prefer an open one — that is the one still accepting review — and otherwise
    the most recently created, so a merged or closed one is still visible to a
    caller deciding whether the work landed.
    """
    if not candidates:
        return None
    open_ones = [c for c in candidates if c.state == "opened"]
    pool = open_ones or list(candidates)
    return max(pool, key=lambda c: c.created_at)


def did_you_mean(name: str, known: Sequence[str]) -> str:
    close = difflib.get_close_matches(name, list(known), n=3, cutoff=0.5)
    return " did you mean {}?".format(", ".join(close)) if close else ""


class Forge:
    """What the workflow needs from a code-hosting platform."""

    name = ""
    cli = ""
    cr_word = ""       # "pull request" | "merge request"
    cr_short = ""      # "PR" | "MR"
    cr_sigil = "#"     # how a change request is referenced in prose

    def __init__(self, repo: Repo):
        self.repo = repo

    # -- issues -------------------------------------------------------------
    def list_issues(self, label=None, assignee=None, state="opened") -> List[Issue]:
        raise NotImplementedError

    def get_issue(self, number: int) -> Issue:
        raise NotImplementedError

    def set_issue_title(self, number: int, title: str) -> None:
        raise NotImplementedError

    def create_issue(self, title, body, labels=None, assignees=None) -> Issue:
        raise NotImplementedError

    # -- labels and people --------------------------------------------------
    def list_labels(self) -> List[str]:
        raise NotImplementedError

    def create_label(self, name: str, color: str = "", description: str = "") -> None:
        raise NotImplementedError

    def assignable_users(self) -> List[str]:
        raise NotImplementedError

    def resolve_assignee(self, username: str):
        raise NotImplementedError

    # -- comments -----------------------------------------------------------
    def list_issue_comments(self, number: int) -> List[Comment]:
        raise NotImplementedError

    def add_issue_comment(self, number: int, body: str) -> Comment:
        raise NotImplementedError

    def list_cr_comments(self, number: int) -> List[Comment]:
        raise NotImplementedError

    def add_cr_comment(self, number: int, body: str) -> Comment:
        raise NotImplementedError

    # -- change requests ----------------------------------------------------
    def crs_referencing_issue(self, number: int) -> List[ChangeRequest]:
        """Every change request the forge associates with the issue, however loosely."""
        raise NotImplementedError

    def find_cr_for_issue(self, number: int) -> Optional[ChangeRequest]:
        raise NotImplementedError

    def unattributed_crs(self, number: int) -> List[ChangeRequest]:
        """Referencing change requests that do *not* claim the issue.

        Only used to make a "no change request" message actionable: an issue whose
        change request was hand-titled looks identical to one whose submit step
        failed, and naming the near-misses settles it in a glance.
        """
        return [c for c in self.crs_referencing_issue(number) if not title_claims_issue(c.title, number)]

    def cr_review_threads(self, number: int) -> List[ReviewThread]:
        raise NotImplementedError

    # -- diagnostics --------------------------------------------------------
    def auth_status(self):
        """`(ok, detail)` for the doctor."""
        raise NotImplementedError

    def permissions(self):
        """`(can_write, detail)` for the doctor."""
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------
    def check_labels(self, wanted: Sequence[str]) -> List[str]:
        """Reject any label the project does not already define.

        Both forges create a label on first use. `--label web_admin` does not fail
        on the typo: it adds a new label and the issue silently drops out of every
        board filter built on `web-admin`. That is a mechanical mistake, so it
        belongs in code rather than in a warning nobody reads at 2am.
        """
        wanted = list(wanted)
        if not wanted:
            return []
        known = self.list_labels()
        unknown = [lb for lb in wanted if lb not in known]
        if unknown:
            detail = "; ".join("{!r}{}".format(lb, did_you_mean(lb, known)) for lb in unknown)
            raise Precondition(
                "unknown label(s): {}\n"
                "       creating one would silently add it to the project. "
                "known labels: {}".format(detail, ", ".join(known))
            )
        return wanted


def for_repo(repo: Repo) -> Forge:
    from . import gh, gl

    if repo.forge == GITHUB:
        return gh.GitHub(repo)
    if repo.forge == GITLAB:
        return gl.GitLab(repo)
    raise Precondition("unsupported forge {!r}; expected github or gitlab".format(repo.forge))
