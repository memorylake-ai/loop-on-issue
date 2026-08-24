import json
import unittest

import _bootstrap  # noqa: F401

from fakecli import FakeCLI
from loopkit import gh
from loopkit.errors import Precondition
from loopkit.models import Repo

REPO = Repo("github", "github.com", "acme/widget")

ISSUE = {
    "number": 12,
    "title": "[WORKING] fix drive URI",
    "state": "open",
    "html_url": "https://github.com/acme/widget/issues/12",
    "labels": [{"name": "loop"}, {"name": "bug"}],
    "assignees": [{"login": "muxuan"}],
    "body": "body text",
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-02T00:00:00Z",
}

PULL_MASQUERADING_AS_ISSUE = {
    "number": 88,
    "title": "to #12: fix drive URI",
    "state": "open",
    "html_url": "https://github.com/acme/widget/pull/88",
    "labels": [],
    "assignees": [],
    "body": "",
    "created_at": "2026-08-03T00:00:00Z",
    "updated_at": "2026-08-03T00:00:00Z",
    "pull_request": {"url": "https://api.github.com/repos/acme/widget/pulls/88"},
}


class GitHubBackend(unittest.TestCase):
    def setUp(self):
        self.cli = FakeCLI()
        self.addCleanup(self.cli.cleanup)
        self.forge = gh.GitHub(REPO)

    # -- issues ------------------------------------------------------------
    def test_pull_requests_are_excluded_from_the_issue_listing(self):
        # GitHub's issues endpoint returns pull requests too. Without this filter
        # the swarm claims a pull request as if it were queued work.
        self.cli.route("api", "/issues?", stdout=[ISSUE, PULL_MASQUERADING_AS_ISSUE])
        issues = self.forge.list_issues(label="loop", assignee="muxuan")
        self.assertEqual([i.number for i in issues], [12])

    def test_issue_fields_are_normalised(self):
        self.cli.route("api", "/issues?", stdout=[ISSUE])
        issue = self.forge.list_issues()[0]
        self.assertEqual(issue.state, "opened")  # GitLab's vocabulary, everywhere
        self.assertEqual(issue.labels, ["loop", "bug"])
        self.assertEqual(issue.assignees, ["muxuan"])
        self.assertEqual(issue.url, ISSUE["html_url"])

    def test_filters_reach_the_api(self):
        self.cli.route("api", "/issues?", stdout=[])
        self.forge.list_issues(label="loop", assignee="muxuan", state="opened")
        joined = self.cli.call_containing("/issues?")["joined"]
        self.assertIn("labels=loop", joined)
        self.assertIn("assignee=muxuan", joined)
        self.assertIn("state=open", joined)  # GitHub says "open", not "opened"

    def test_get_issue_refuses_a_pull_request_number(self):
        self.cli.route("api", "/issues/88", stdout=PULL_MASQUERADING_AS_ISSUE)
        with self.assertRaises(Precondition) as ctx:
            self.forge.get_issue(88)
        self.assertIn("pull request", str(ctx.exception))

    def test_set_issue_title_patches_the_issue(self):
        self.cli.route("api", "/issues/12", stdout=ISSUE)
        self.forge.set_issue_title(12, "[FINISHED] fix drive URI")
        call = self.cli.call_containing("PATCH", "/issues/12")
        self.assertEqual(json.loads(call["stdin"])["title"], "[FINISHED] fix drive URI")

    def test_create_issue_sends_labels_and_assignees(self):
        self.cli.route("api", "--method POST", "/issues", stdout=ISSUE)
        self.forge.create_issue("t", "b", labels=["loop"], assignees=["muxuan"])
        call = self.cli.call_containing("POST", "/issues")
        payload = json.loads(call["stdin"])
        self.assertEqual(payload["labels"], ["loop"])
        self.assertEqual(payload["assignees"], ["muxuan"])

    def test_titles_with_newlines_and_unicode_survive(self):
        # The body travels as JSON on stdin rather than as an argv string, which
        # is what keeps quotes, newlines and 中文 from being mangled by the shell.
        self.cli.route("api", "--method POST", "/issues", stdout=ISSUE)
        self.forge.create_issue("修复 \"drive\" URI", "line1\nline2\n", labels=[], assignees=[])
        payload = json.loads(self.cli.call_containing("POST", "/issues")["stdin"])
        self.assertEqual(payload["title"], '修复 "drive" URI')
        self.assertEqual(payload["body"], "line1\nline2\n")

    # -- labels and people --------------------------------------------------
    def test_list_labels(self):
        self.cli.route("api", "/labels", stdout=[{"name": "loop"}, {"name": "bug"}])
        self.assertEqual(self.forge.list_labels(), ["bug", "loop"])

    def test_unknown_label_is_refused_with_a_suggestion(self):
        self.cli.route("api", "/labels", stdout=[{"name": "web-admin"}])
        with self.assertRaises(Precondition) as ctx:
            self.forge.check_labels(["web_admin"])
        self.assertIn("web-admin", str(ctx.exception))

    def test_unknown_assignee_is_refused(self):
        # An unassignable issue is created, looks fine on the board, and is never
        # picked up — a silent no-op rather than an error.
        self.cli.route("api", "/assignees", stdout=[{"login": "muxuan"}])
        with self.assertRaises(Precondition) as ctx:
            self.forge.resolve_assignee("muxaun")
        self.assertIn("muxuan", str(ctx.exception))

    def test_known_assignee_resolves(self):
        self.cli.route("api", "/assignees", stdout=[{"login": "muxuan"}])
        self.assertEqual(self.forge.resolve_assignee("muxuan"), "muxuan")

    # -- comments -----------------------------------------------------------
    def test_comments_are_normalised(self):
        self.cli.route(
            "api",
            "/issues/12/comments",
            stdout=[{"id": 1, "user": {"login": "dev"}, "body": "hi", "created_at": "2026-08-01T00:00:00Z"}],
        )
        c = self.forge.list_issue_comments(12)[0]
        self.assertEqual((c.author, c.body, c.system), ("dev", "hi", False))

    def test_add_issue_comment_posts_the_body(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 9})
        self.forge.add_issue_comment(12, "note with **markdown** and 中文")
        payload = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])
        self.assertEqual(payload["body"], "note with **markdown** and 中文")

    # -- change requests ----------------------------------------------------
    def _graphql_link(self, nodes):
        return {"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": nodes}}}}}

    def test_native_development_link_is_used_first(self):
        self.cli.route(
            "graphql",
            "closedByPullRequestsReferences",
            stdout=self._graphql_link(
                [
                    {
                        "number": 88,
                        "title": "anything at all",
                        "state": "OPEN",
                        "url": "https://github.com/acme/widget/pull/88",
                        "isDraft": False,
                        "headRefName": "fix/uri",
                        "baseRefName": "main",
                        "createdAt": "2026-08-03T00:00:00Z",
                    }
                ]
            ),
        )
        cr = self.forge.find_cr_for_issue(12)
        # The native link is authoritative, so the title need not follow the
        # `to #N` convention for the change request to be found.
        self.assertEqual((cr.number, cr.state, cr.source_branch), (88, "opened", "fix/uri"))

    def test_merged_state_is_normalised(self):
        self.cli.route(
            "graphql",
            "closedByPullRequestsReferences",
            stdout=self._graphql_link(
                [{"number": 88, "title": "t", "state": "MERGED", "url": "u", "isDraft": False,
                  "headRefName": "h", "baseRefName": "main", "createdAt": "2026-08-03T00:00:00Z"}]
            ),
        )
        cr = self.forge.find_cr_for_issue(12)
        self.assertTrue(cr.merged)

    def test_falls_back_to_the_title_convention(self):
        self.cli.route("graphql", "closedByPullRequestsReferences", stdout=self._graphql_link([]))
        self.cli.route("api", "search/issues", stdout={"items": [{"number": 88}]})
        self.cli.route(
            "api",
            "/pulls/88",
            stdout={
                "number": 88, "title": "to #12: fix drive URI", "state": "open", "merged_at": None,
                "html_url": "u", "draft": False, "head": {"ref": "fix/uri"}, "base": {"ref": "main"},
                "created_at": "2026-08-03T00:00:00Z",
            },
        )
        cr = self.forge.find_cr_for_issue(12)
        self.assertEqual(cr.number, 88)

    def test_fallback_ignores_a_pull_request_that_only_mentions_the_issue(self):
        self.cli.route("graphql", "closedByPullRequestsReferences", stdout=self._graphql_link([]))
        self.cli.route("api", "search/issues", stdout={"items": [{"number": 90}]})
        self.cli.route(
            "api",
            "/pulls/90",
            stdout={
                "number": 90, "title": "refactor, stacked on #12", "state": "open", "merged_at": None,
                "html_url": "u", "draft": False, "head": {"ref": "x"}, "base": {"ref": "main"},
                "created_at": "2026-08-03T00:00:00Z",
            },
        )
        self.assertIsNone(self.forge.find_cr_for_issue(12))

    def test_no_change_request_at_all(self):
        self.cli.route("graphql", "closedByPullRequestsReferences", stdout=self._graphql_link([]))
        self.cli.route("api", "search/issues", stdout={"items": []})
        self.assertIsNone(self.forge.find_cr_for_issue(12))

    # -- review threads -----------------------------------------------------
    def test_review_threads_expose_resolution(self):
        self.cli.route(
            "graphql",
            "reviewThreads",
            stdout={
                "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
                    {"id": "T1", "isResolved": False, "comments": {"nodes": [
                        {"author": {"login": "dev"}, "body": "nit", "createdAt": "2026-08-04T00:00:00Z", "path": "a.py"}]}},
                    {"id": "T2", "isResolved": True, "comments": {"nodes": [
                        {"author": {"login": "dev"}, "body": "done", "createdAt": "2026-08-04T00:00:00Z", "path": "b.py"}]}},
                ]}}}}
            },
        )
        threads = self.forge.cr_review_threads(88)
        self.assertEqual([(t.id, t.resolved, t.path) for t in threads],
                         [("T1", False, "a.py"), ("T2", True, "b.py")])

    def test_review_threads_degrade_instead_of_exploding(self):
        # Resolution state is GraphQL-only. Where GraphQL is unavailable — an old
        # Enterprise build, a token without the scope — the caller still has the
        # plain-comment signal, so an empty list is the right answer, not a crash.
        self.cli.route("graphql", "reviewThreads", exit=1, stderr="GraphQL: Resource not accessible")
        self.assertEqual(self.forge.cr_review_threads(88), [])

    def test_unattributed_change_requests_are_named(self):
        self.cli.route(
            "api",
            "/timeline",
            stdout=[
                {"event": "cross-referenced", "source": {"type": "issue", "issue": {
                    "number": 90, "title": "stacked on #12", "state": "open",
                    "html_url": "u90", "created_at": "2026-08-05T00:00:00Z",
                    "pull_request": {"url": "x"}}}},
                {"event": "labeled"},
            ],
        )
        near = self.forge.unattributed_crs(12)
        self.assertEqual([c.number for c in near], [90])


if __name__ == "__main__":
    unittest.main()
