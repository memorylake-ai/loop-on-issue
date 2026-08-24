"""GitHub, driven through the `gh` CLI.

Going through `gh` rather than a raw token means this works wherever
`gh auth status` is happy, including Enterprise hosts and whatever credential
helper the machine already uses — the same reasoning that made the GitLab side
use `glab`.

Three GitHub-specific hazards shape this file:

* **The issues endpoint returns pull requests.** Every listing has to drop
  entries carrying a `pull_request` key, or the swarm will claim a pull request
  as though it were queued work.
* **REST review comments carry no resolution state.** Whether a review thread is
  resolved exists only in GraphQL, so that one read goes through `gh api graphql`
  and degrades to an empty list rather than failing the run.
* **Attribution has a native answer here.** GitHub records the `closes #N` link
  between a pull request and an issue, so it is asked first and the `to #N` title
  convention is only a fallback.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .errors import Precondition
from .forge import Forge, did_you_mean, pick_cr, title_claims_issue
from .models import ChangeRequest, Comment, Issue, Repo, ReviewThread
from .proc import CommandError, run, run_result

_STATE_IN = {"opened": "open", "open": "open", "closed": "closed", "all": "all"}
_STATE_OUT = {"open": "opened", "closed": "closed"}

_LINKED_PRS_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    issue(number:$number) {
      closedByPullRequestsReferences(first:20, includeClosedPrs:true) {
        nodes { number title state url isDraft headRefName baseRefName createdAt }
      }
    }
  }
}
"""

_REVIEW_THREADS_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          comments(first:10) { nodes { author { login } body createdAt path } }
        }
      }
    }
  }
}
"""


class GitHub(Forge):
    name = "github"
    cli = "gh"
    cr_word = "pull request"
    cr_short = "PR"
    cr_sigil = "#"

    def __init__(self, repo: Repo):
        super().__init__(repo)
        self._labels: Optional[List[str]] = None
        self._assignees: Optional[List[str]] = None

    # -- transport ----------------------------------------------------------
    def _base_cmd(self) -> List[str]:
        cmd = ["gh", "api"]
        if self.repo.host and self.repo.host != "github.com":
            cmd += ["--hostname", self.repo.host]
        return cmd

    def api(
        self,
        path: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        paginate: bool = False,
    ) -> Any:
        cmd = self._base_cmd() + ["--method", method]
        stdin = None
        if body is not None:
            # The payload travels as JSON on stdin, which is what keeps newlines,
            # quotes and non-ASCII in titles and comment bodies intact — argv
            # string interpolation mangles all three.
            cmd += ["--input", "-"]
            stdin = json.dumps(body, ensure_ascii=False)
        if paginate:
            cmd.append("--paginate")
        cmd.append(path)
        out = run(cmd, stdin=stdin).strip()
        if not out:
            return None
        if paginate:
            return _merge_pages(out)
        return json.loads(out)

    def graphql(self, query: str, **variables: Any) -> Any:
        cmd = self._base_cmd() + ["graphql", "-f", "query={}".format(query)]
        for key, value in variables.items():
            flag = "-F" if isinstance(value, (int, bool)) else "-f"
            cmd += [flag, "{}={}".format(key, value)]
        out = run(cmd).strip()
        return json.loads(out) if out else None

    def _repo_path(self) -> str:
        return self.repo.path

    # -- issues -------------------------------------------------------------
    def list_issues(self, label=None, assignee=None, state="opened") -> List[Issue]:
        params = ["per_page=100", "sort=created", "direction=asc",
                  "state={}".format(_STATE_IN.get(state, "open"))]
        if label:
            params.append("labels={}".format(_q(label)))
        if assignee:
            params.append("assignee={}".format(_q(assignee)))
        raw = self.api(
            "repos/{}/issues?{}".format(self._repo_path(), "&".join(params)), paginate=True
        ) or []
        return [_issue(item) for item in raw if "pull_request" not in item]

    def get_issue(self, number: int) -> Issue:
        raw = self.api("repos/{}/issues/{}".format(self._repo_path(), number))
        if raw is None:
            raise Precondition("#{} not found in {}".format(number, self._repo_path()))
        if "pull_request" in raw:
            raise Precondition(
                "#{} is a pull request, not an issue — GitHub numbers them in one "
                "sequence, so a queue number and a change-request number can look "
                "identical".format(number)
            )
        return _issue(raw)

    def set_issue_title(self, number: int, title: str) -> None:
        self.api(
            "repos/{}/issues/{}".format(self._repo_path(), number),
            method="PATCH",
            body={"title": title},
        )

    def create_issue(self, title, body, labels=None, assignees=None) -> Issue:
        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        if assignees:
            payload["assignees"] = list(assignees)
        raw = self.api("repos/{}/issues".format(self._repo_path()), method="POST", body=payload)
        return _issue(raw)

    # -- labels and people --------------------------------------------------
    def list_labels(self) -> List[str]:
        if self._labels is None:
            raw = self.api("repos/{}/labels?per_page=100".format(self._repo_path()), paginate=True) or []
            self._labels = sorted(item["name"] for item in raw)
        return self._labels

    def create_label(self, name: str, color: str = "ededed", description: str = "") -> None:
        self.api(
            "repos/{}/labels".format(self._repo_path()),
            method="POST",
            body={"name": name, "color": color, "description": description},
        )
        self._labels = None

    def assignable_users(self) -> List[str]:
        if self._assignees is None:
            raw = self.api("repos/{}/assignees?per_page=100".format(self._repo_path()), paginate=True) or []
            self._assignees = sorted(item["login"] for item in raw)
        return self._assignees

    def resolve_assignee(self, username: str) -> str:
        known = self.assignable_users()
        if username in known:
            return username
        raise Precondition(
            "unknown assignee {!r}{}\n"
            "       an unassignable issue is never picked up by the swarm.".format(
                username, did_you_mean(username, known)
            )
        )

    # -- comments -----------------------------------------------------------
    def list_issue_comments(self, number: int) -> List[Comment]:
        raw = self.api(
            "repos/{}/issues/{}/comments?per_page=100".format(self._repo_path(), number),
            paginate=True,
        ) or []
        return [_comment(item) for item in raw]

    def add_issue_comment(self, number: int, body: str) -> Comment:
        raw = self.api(
            "repos/{}/issues/{}/comments".format(self._repo_path(), number),
            method="POST",
            body={"body": body},
        )
        return _comment(raw or {})

    # A pull request *is* an issue as far as comments go, so both share a path.
    list_cr_comments = list_issue_comments
    add_cr_comment = add_issue_comment

    # -- change requests ----------------------------------------------------
    def find_cr_for_issue(self, number: int) -> Optional[ChangeRequest]:
        """The pull request implementing this issue.

        The native development link — what GitHub records when a pull request
        says `closes #N` — is asked first and is authoritative, so renaming a
        pull request does not strand its issue. The `to #N` title convention is
        the fallback, which also covers the window before GitHub's search index
        catches up with a brand new pull request.
        """
        linked = self._linked_pulls(number)
        chosen = pick_cr(linked)
        if chosen:
            return chosen
        return pick_cr(self._pulls_claiming_by_title(number))

    def _linked_pulls(self, number: int) -> List[ChangeRequest]:
        owner, name = self.repo.owner, self.repo.name
        try:
            data = self.graphql(_LINKED_PRS_QUERY, owner=owner, name=name, number=int(number))
        except CommandError:
            return []
        try:
            nodes = data["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["nodes"]
        except (TypeError, KeyError):
            return []
        return [_cr_from_graphql(node) for node in nodes or []]

    def _pulls_claiming_by_title(self, number: int) -> List[ChangeRequest]:
        query = 'repo:{} type:pr in:title "to #{}"'.format(self._repo_path(), number)
        try:
            found = self.api("search/issues?per_page=20&q={}".format(_q(query)))
        except CommandError:
            return []
        items = (found or {}).get("items") or []
        results = []
        for item in items:
            try:
                raw = self.api("repos/{}/pulls/{}".format(self._repo_path(), item["number"]))
            except CommandError:
                continue
            if raw and title_claims_issue(raw.get("title"), number):
                results.append(_cr_from_rest(raw))
        return results

    def crs_referencing_issue(self, number: int) -> List[ChangeRequest]:
        try:
            events = self.api(
                "repos/{}/issues/{}/timeline?per_page=100".format(self._repo_path(), number),
                paginate=True,
            ) or []
        except CommandError:
            return []
        seen = {}
        for event in events:
            if event.get("event") != "cross-referenced":
                continue
            source = (event.get("source") or {}).get("issue") or {}
            if "pull_request" not in source:
                continue
            seen[source["number"]] = ChangeRequest(
                number=source["number"],
                title=source.get("title") or "",
                state=_STATE_OUT.get(source.get("state"), source.get("state") or ""),
                url=source.get("html_url") or "",
                created_at=source.get("created_at") or "",
            )
        return list(seen.values())

    def cr_review_threads(self, number: int) -> List[ReviewThread]:
        """Review threads with their resolution state.

        REST does not expose whether a thread is resolved, so this is the one read
        that requires GraphQL. Where GraphQL is unavailable the caller still has
        the plain-comment signal, so an empty list is the honest answer rather
        than a failed run.
        """
        owner, name = self.repo.owner, self.repo.name
        try:
            data = self.graphql(_REVIEW_THREADS_QUERY, owner=owner, name=name, number=int(number))
        except CommandError:
            return []
        try:
            nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        except (TypeError, KeyError):
            return []
        threads = []
        for node in nodes or []:
            comments = ((node.get("comments") or {}).get("nodes")) or [{}]
            first = comments[0]
            threads.append(
                ReviewThread(
                    id=node.get("id") or "",
                    resolved=bool(node.get("isResolved")),
                    author=((first.get("author") or {}).get("login")) or "",
                    created_at=first.get("createdAt") or "",
                    body=first.get("body") or "",
                    path=first.get("path"),
                )
            )
        return threads

    # -- diagnostics --------------------------------------------------------
    def auth_status(self):
        cmd = ["gh", "auth", "status"]
        if self.repo.host and self.repo.host != "github.com":
            cmd += ["--hostname", self.repo.host]
        result = run_result(cmd)
        # `gh auth status` prints its report to stderr on some versions and stdout
        # on others, so both are considered.
        detail = (result.stdout + "\n" + result.stderr).strip()
        return result.ok, detail

    def permissions(self):
        try:
            raw = self.api("repos/{}".format(self._repo_path()))
        except CommandError as exc:
            return False, (exc.stderr or str(exc)).strip()
        perms = (raw or {}).get("permissions") or {}
        if perms.get("push") or perms.get("admin") or perms.get("maintain"):
            return True, "push access"
        if perms:
            return False, "read-only: {}".format(", ".join(sorted(k for k, v in perms.items() if v)) or "none")
        # A token scoped to a fine-grained app may omit `permissions` entirely.
        return True, "reachable (permissions not reported)"


# --------------------------------------------------------------------------- #
# translation
# --------------------------------------------------------------------------- #


def _q(value: str) -> str:
    import urllib.parse

    return urllib.parse.quote(str(value), safe="")


def _merge_pages(out: str) -> Any:
    """`gh api --paginate` concatenates one JSON document per page.

    For arrays it emits `[...][...]`; for objects with an `items` key it emits one
    object per page. Both have to be flattened before anything above sees them.
    """
    decoder = json.JSONDecoder()
    documents = []
    index = 0
    while index < len(out):
        while index < len(out) and out[index] in " \t\r\n":
            index += 1
        if index >= len(out):
            break
        doc, end = decoder.raw_decode(out, index)
        documents.append(doc)
        index = end
    if not documents:
        return None
    if len(documents) == 1:
        return documents[0]
    if all(isinstance(d, list) for d in documents):
        merged: List[Any] = []
        for doc in documents:
            merged.extend(doc)
        return merged
    if all(isinstance(d, dict) and "items" in d for d in documents):
        merged_items: List[Any] = []
        for doc in documents:
            merged_items.extend(doc["items"])
        head = dict(documents[0])
        head["items"] = merged_items
        return head
    return documents[0]


def _issue(raw: Dict[str, Any]) -> Issue:
    return Issue(
        number=raw["number"],
        title=raw.get("title") or "",
        state=_STATE_OUT.get(raw.get("state"), raw.get("state") or ""),
        url=raw.get("html_url") or "",
        labels=[lb["name"] if isinstance(lb, dict) else lb for lb in (raw.get("labels") or [])],
        assignees=[a["login"] for a in (raw.get("assignees") or [])],
        body=raw.get("body") or "",
        created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or "",
    )


def _comment(raw: Dict[str, Any]) -> Comment:
    return Comment(
        id=raw.get("id"),
        author=((raw.get("user") or {}).get("login")) or "",
        created_at=raw.get("created_at") or "",
        body=raw.get("body") or "",
        # GitHub keeps its state changes in the timeline, not among comments, so
        # nothing arriving here is machine chatter.
        system=False,
    )


def _cr_from_graphql(node: Dict[str, Any]) -> ChangeRequest:
    state = (node.get("state") or "").lower()
    return ChangeRequest(
        number=node["number"],
        title=node.get("title") or "",
        state={"open": "opened", "merged": "merged", "closed": "closed"}.get(state, state),
        url=node.get("url") or "",
        source_branch=node.get("headRefName") or "",
        target_branch=node.get("baseRefName") or "",
        draft=bool(node.get("isDraft")),
        created_at=node.get("createdAt") or "",
    )


def _cr_from_rest(raw: Dict[str, Any]) -> ChangeRequest:
    if raw.get("merged_at"):
        state = "merged"
    else:
        state = _STATE_OUT.get(raw.get("state"), raw.get("state") or "")
    return ChangeRequest(
        number=raw["number"],
        title=raw.get("title") or "",
        state=state,
        url=raw.get("html_url") or "",
        source_branch=((raw.get("head") or {}).get("ref")) or "",
        target_branch=((raw.get("base") or {}).get("ref")) or "",
        draft=bool(raw.get("draft")),
        created_at=raw.get("created_at") or "",
    )
