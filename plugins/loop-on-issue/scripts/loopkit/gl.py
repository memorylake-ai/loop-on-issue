"""GitLab, driven through the `glab` CLI.

This is the original implementation these skills grew out of, refactored to the
common interface. Two behaviours here are load-bearing and were each learned the
expensive way:

* **Request bodies travel as JSON on stdin.** `glab api --input -` sends the body
  verbatim without guessing a media type, and GitLab answers 415 unless JSON is
  declared explicitly — but going through stdin is also what keeps newlines,
  quotes and 中文 in titles and comment bodies from being mangled by shell
  quoting.
* **Only a `to #<iid>` title attributes a merge request to an issue.**
  `related_merge_requests` is far wider than "the merge request for this issue":
  it returns every one that so much as mentions it. See `forge.title_claims_issue`
  for what that cost.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, List, Optional

from .errors import Precondition
from .forge import Forge, did_you_mean, pick_cr, title_claims_issue
from .models import ChangeRequest, Comment, Issue, Repo, ReviewThread
from .proc import CommandError, run, run_result


class GitLab(Forge):
    name = "gitlab"
    cli = "glab"
    cr_word = "merge request"
    cr_short = "MR"
    cr_sigil = "!"

    def __init__(self, repo: Repo):
        super().__init__(repo)
        self._labels: Optional[List[str]] = None
        self._members: Optional[Dict[str, int]] = None

    # -- transport ----------------------------------------------------------
    def api(self, path: str, method: str = "GET", body: Optional[Dict[str, Any]] = None) -> Any:
        cmd = ["glab", "api", "--method", method]
        stdin = None
        if body is not None:
            cmd += ["--input", "-", "--header", "Content-Type: application/json"]
            stdin = json.dumps(body, ensure_ascii=False)
        cmd.append(path)
        out = run(cmd, stdin=stdin).strip()
        return json.loads(out) if out else None

    def _p(self, suffix: str = "") -> str:
        return "projects/{}{}".format(_enc(self.repo.path), suffix)

    # -- issues -------------------------------------------------------------
    def list_issues(self, label=None, assignee=None, state="opened") -> List[Issue]:
        params = [
            "state={}".format(state if state in ("opened", "closed", "all") else "opened"),
            "per_page=100",
            "order_by=created_at",
            "sort=asc",
        ]
        if label:
            params.append("labels={}".format(_enc(label)))
        if assignee:
            params.append("assignee_username[]={}".format(_enc(assignee)))
        raw = self.api(self._p("/issues?{}".format("&".join(params)))) or []
        return [_issue(item) for item in raw]

    def get_issue(self, number: int) -> Issue:
        raw = self.api(self._p("/issues/{}".format(number)))
        if raw is None:
            raise Precondition("#{} not found in {}".format(number, self.repo.path))
        return _issue(raw)

    def set_issue_title(self, number: int, title: str) -> None:
        self.api(self._p("/issues/{}".format(number)), method="PUT", body={"title": title})

    def create_issue(self, title, body, labels=None, assignees=None) -> Issue:
        payload: Dict[str, Any] = {"title": title, "description": body}
        if labels is not None:
            payload["labels"] = ",".join(labels)
        if assignees:
            payload["assignee_ids"] = [self.resolve_assignee(a) for a in assignees]
        raw = self.api(self._p("/issues"), method="POST", body=payload)
        return _issue(raw)

    # -- labels and people --------------------------------------------------
    def list_labels(self) -> List[str]:
        if self._labels is None:
            raw = self.api(self._p("/labels?per_page=100&with_counts=false")) or []
            self._labels = sorted(item["name"] for item in raw)
        return self._labels

    def create_label(self, name: str, color: str = "#ededed", description: str = "") -> None:
        if not color.startswith("#"):
            color = "#" + color
        self.api(
            self._p("/labels"),
            method="POST",
            body={"name": name, "color": color, "description": description},
        )
        self._labels = None

    def _project_members(self) -> Dict[str, int]:
        if self._members is None:
            raw = self.api(self._p("/members/all?per_page=100")) or []
            self._members = {m["username"]: m["id"] for m in raw}
        return self._members

    def assignable_users(self) -> List[str]:
        return sorted(self._project_members())

    def resolve_assignee(self, username: str) -> int:
        """Map a username to the numeric id the issues API requires."""
        members = self._project_members()
        if username in members:
            return members[username]

        # Not a project member, but may still have access through a group.
        found = self.api("users?username={}".format(_enc(username))) or []
        if found:
            return found[0]["id"]

        raise Precondition(
            "unknown assignee {!r}{}\n"
            "       an unassignable issue is never picked up by the swarm.".format(
                username, did_you_mean(username, sorted(members))
            )
        )

    # -- comments -----------------------------------------------------------
    def list_issue_comments(self, number: int) -> List[Comment]:
        return self._notes("issues", number)

    def add_issue_comment(self, number: int, body: str) -> Comment:
        return self._add_note("issues", number, body)

    def list_cr_comments(self, number: int) -> List[Comment]:
        return self._notes("merge_requests", number)

    def add_cr_comment(self, number: int, body: str) -> Comment:
        return self._add_note("merge_requests", number, body)

    def _notes(self, kind: str, number: int) -> List[Comment]:
        raw = self.api(
            self._p("/{}/{}/notes?per_page=100&order_by=created_at&sort=asc".format(kind, number))
        ) or []
        return [_comment(item) for item in raw]

    def _add_note(self, kind: str, number: int, body: str) -> Comment:
        raw = self.api(
            self._p("/{}/{}/notes".format(kind, number)), method="POST", body={"body": body}
        )
        return _comment(raw or {})

    # -- change requests ----------------------------------------------------
    def crs_referencing_issue(self, number: int) -> List[ChangeRequest]:
        raw = self.api(self._p("/issues/{}/related_merge_requests".format(number))) or []
        return [_cr(item) for item in raw]

    def find_cr_for_issue(self, number: int) -> Optional[ChangeRequest]:
        candidates = [c for c in self.crs_referencing_issue(number) if title_claims_issue(c.title, number)]
        return pick_cr(candidates)

    def cr_review_threads(self, number: int) -> List[ReviewThread]:
        raw = self.api(self._p("/merge_requests/{}/discussions?per_page=100".format(number))) or []
        threads = []
        for discussion in raw:
            notes = discussion.get("notes") or []
            # A non-resolvable discussion is a plain comment, not a review thread;
            # it reaches the caller through the comment signal instead.
            resolvable = [n for n in notes if n.get("resolvable")]
            if not resolvable:
                continue
            first = resolvable[0]
            threads.append(
                ReviewThread(
                    id=discussion.get("id") or "",
                    resolved=all(bool(n.get("resolved")) for n in resolvable),
                    author=((first.get("author") or {}).get("username")) or "",
                    created_at=first.get("created_at") or "",
                    body=first.get("body") or "",
                    path=((first.get("position") or {}).get("new_path")),
                )
            )
        return threads

    # -- diagnostics --------------------------------------------------------
    def auth_status(self):
        cmd = ["glab", "auth", "status"]
        if self.repo.host and self.repo.host != "gitlab.com":
            cmd += ["--hostname", self.repo.host]
        result = run_result(cmd)
        detail = (result.stdout + "\n" + result.stderr).strip()
        return result.ok, detail

    def permissions(self):
        try:
            raw = self.api(self._p())
        except CommandError as exc:
            return False, (exc.stderr or str(exc)).strip()
        access = ((raw or {}).get("permissions") or {}).get("project_access") or {}
        level = access.get("access_level")
        if level is None:
            group = ((raw or {}).get("permissions") or {}).get("group_access") or {}
            level = group.get("access_level")
        if level is None:
            return True, "reachable (access level not reported)"
        # 30 is Developer — the floor for pushing branches and opening MRs.
        return (level >= 30), "access level {}".format(level)


# --------------------------------------------------------------------------- #
# translation
# --------------------------------------------------------------------------- #


def _enc(value: str) -> str:
    """URL-encode a value for use as a path segment or query parameter.

    Project paths in particular have to become `group%2Fproject`, since GitLab
    takes the whole path as the `:id` segment.
    """
    return urllib.parse.quote(str(value).strip("/"), safe="")


def _issue(raw: Dict[str, Any]) -> Issue:
    return Issue(
        number=raw["iid"],
        title=raw.get("title") or "",
        state=raw.get("state") or "",
        url=raw.get("web_url") or "",
        labels=list(raw.get("labels") or []),
        assignees=[a["username"] for a in (raw.get("assignees") or [])],
        body=raw.get("description") or "",
        created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or "",
    )


def _comment(raw: Dict[str, Any]) -> Comment:
    return Comment(
        id=raw.get("id"),
        author=((raw.get("author") or {}).get("username")) or "",
        created_at=raw.get("created_at") or "",
        body=raw.get("body") or "",
        system=bool(raw.get("system")),
    )


def _cr(raw: Dict[str, Any]) -> ChangeRequest:
    return ChangeRequest(
        number=raw["iid"],
        title=raw.get("title") or "",
        state=raw.get("state") or "",
        url=raw.get("web_url") or "",
        source_branch=raw.get("source_branch") or "",
        target_branch=raw.get("target_branch") or "",
        draft=bool(raw.get("draft") or raw.get("work_in_progress")),
        created_at=raw.get("created_at") or "",
    )
