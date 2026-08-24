import json
import unittest

import _bootstrap  # noqa: F401

from fakecli import FakeCLI
from loopkit import gl
from loopkit.errors import Precondition
from loopkit.models import Repo

REPO = Repo("gitlab", "gitlab.example.com", "darwin/zootopia")

ISSUE = {
    "iid": 612,
    "title": "[WORKING] fix drive URI",
    "state": "opened",
    "web_url": "https://gitlab.example.com/darwin/zootopia/-/issues/612",
    "labels": ["loop", "bug"],
    "assignees": [{"username": "muxuan"}],
    "description": "body text",
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-02T00:00:00Z",
}


class GitLabBackend(unittest.TestCase):
    def setUp(self):
        self.cli = FakeCLI()
        self.addCleanup(self.cli.cleanup)
        self.forge = gl.GitLab(REPO)

    def test_project_path_is_url_encoded(self):
        self.cli.route("api", "/issues?", stdout=[])
        self.forge.list_issues()
        self.assertIn("darwin%2Fzootopia", self.cli.call_containing("/issues?")["joined"])

    def test_filters_reach_the_api(self):
        self.cli.route("api", "/issues?", stdout=[])
        self.forge.list_issues(label="loop", assignee="muxuan", state="opened")
        joined = self.cli.call_containing("/issues?")["joined"]
        self.assertIn("labels=loop", joined)
        self.assertIn("assignee_username", joined)
        self.assertIn("state=opened", joined)

    def test_issue_fields_are_normalised(self):
        self.cli.route("api", "/issues?", stdout=[ISSUE])
        issue = self.forge.list_issues()[0]
        self.assertEqual(issue.number, 612)  # iid, not the global id
        self.assertEqual(issue.state, "opened")
        self.assertEqual(issue.labels, ["loop", "bug"])
        self.assertEqual(issue.assignees, ["muxuan"])
        self.assertEqual(issue.body, "body text")

    def test_set_issue_title_puts(self):
        self.cli.route("api", "/issues/612", stdout=ISSUE)
        self.forge.set_issue_title(612, "[FINISHED] fix drive URI")
        call = self.cli.call_containing("PUT", "/issues/612")
        self.assertEqual(json.loads(call["stdin"])["title"], "[FINISHED] fix drive URI")

    def test_json_body_declares_its_content_type(self):
        # glab's --input mode sends the body verbatim without guessing a media
        # type, and GitLab answers 415 unless JSON is declared explicitly.
        self.cli.route("api", "/issues/612", stdout=ISSUE)
        self.forge.set_issue_title(612, "x")
        self.assertIn("Content-Type: application/json", self.cli.call_containing("PUT")["joined"])

    def test_create_issue_resolves_the_assignee_to_a_numeric_id(self):
        # The issues API takes ids, and `glab issue create --assignee` quietly
        # drops a name it cannot resolve — leaving an issue the swarm never sees.
        self.cli.route("api", "members/all", stdout=[{"username": "muxuan", "id": 7}])
        self.cli.route("api", "--method POST", "/issues", stdout=ISSUE)
        self.forge.create_issue("t", "b", labels=["loop"], assignees=["muxuan"])
        payload = json.loads(self.cli.call_containing("POST", "/issues")["stdin"])
        self.assertEqual(payload["assignee_ids"], [7])
        self.assertEqual(payload["labels"], "loop")  # GitLab wants a comma string

    def test_assignee_outside_the_project_is_looked_up_globally(self):
        # A user can have access through a group without being a project member.
        self.cli.route("api", "members/all", stdout=[])
        self.cli.route("api", "users?username=muxuan", stdout=[{"username": "muxuan", "id": 9}])
        self.assertEqual(self.forge.resolve_assignee("muxuan"), 9)

    def test_unknown_assignee_is_refused(self):
        self.cli.route("api", "members/all", stdout=[{"username": "muxuan", "id": 7}])
        self.cli.route("api", "users?username=", stdout=[])
        with self.assertRaises(Precondition) as ctx:
            self.forge.resolve_assignee("muxaun")
        self.assertIn("muxuan", str(ctx.exception))

    def test_labels(self):
        self.cli.route("api", "/labels", stdout=[{"name": "loop"}, {"name": "bug"}])
        self.assertEqual(self.forge.list_labels(), ["bug", "loop"])

    def test_system_notes_are_flagged(self):
        self.cli.route(
            "api",
            "/notes",
            stdout=[
                {"id": 1, "author": {"username": "dev"}, "body": "hi", "created_at": "t1", "system": False},
                {"id": 2, "author": {"username": "bot"}, "body": "changed title", "created_at": "t2", "system": True},
            ],
        )
        comments = self.forge.list_issue_comments(612)
        self.assertEqual([c.system for c in comments], [False, True])

    def test_comment_body_travels_as_json(self):
        self.cli.route("api", "--method POST", "/notes", stdout={"id": 5})
        self.forge.add_issue_comment(612, "note with **markdown** and 中文\nsecond line")
        payload = json.loads(self.cli.call_containing("POST", "/notes")["stdin"])
        self.assertEqual(payload["body"], "note with **markdown** and 中文\nsecond line")

    # -- attribution --------------------------------------------------------
    def _related(self, *mrs):
        self.cli.route("api", "related_merge_requests", stdout=list(mrs))

    def _mr(self, iid, title, state="opened", created="2026-08-03T00:00:00Z"):
        return {
            "iid": iid, "title": title, "state": state, "web_url": "u{}".format(iid),
            "source_branch": "s", "target_branch": "main", "draft": False, "created_at": created,
        }

    def test_only_the_title_convention_attributes_a_merge_request(self):
        self._related(self._mr(882, "to #612: fix drive URI"))
        self.assertEqual(self.forge.find_cr_for_issue(612).number, 882)

    def test_a_merge_request_that_merely_mentions_the_issue_is_not_attributed(self):
        # Observed on 2026-08-19: related_merge_requests returns every merge
        # request that name-drops the issue, and picking the newest attributed a
        # third issue's work to two others. Review feedback on the real ones then
        # could never wake them back up.
        self._related(self._mr(906, "to #650: something else, stacked on #612"))
        self.assertIsNone(self.forge.find_cr_for_issue(612))

    def test_open_merge_request_wins_over_a_merged_one(self):
        self._related(
            self._mr(880, "to #612: first try", state="merged", created="2026-08-01T00:00:00Z"),
            self._mr(882, "to #612: rework", state="opened", created="2026-07-01T00:00:00Z"),
        )
        self.assertEqual(self.forge.find_cr_for_issue(612).number, 882)

    def test_unattributed_merge_requests_are_named(self):
        self._related(self._mr(906, "stacked on #612"), self._mr(882, "to #612: real"))
        self.assertEqual([c.number for c in self.forge.unattributed_crs(612)], [906])

    # -- review threads -----------------------------------------------------
    def test_discussions_become_review_threads(self):
        self.cli.route(
            "api",
            "/discussions",
            stdout=[
                {"id": "d1", "notes": [{"resolvable": True, "resolved": False, "author": {"username": "dev"},
                                        "body": "nit", "created_at": "t1", "position": {"new_path": "a.py"}}]},
                {"id": "d2", "notes": [{"resolvable": True, "resolved": True, "author": {"username": "dev"},
                                        "body": "done", "created_at": "t2", "position": {"new_path": "b.py"}}]},
                {"id": "d3", "notes": [{"resolvable": False, "resolved": False, "author": {"username": "dev"},
                                        "body": "plain comment", "created_at": "t3"}]},
            ],
        )
        threads = self.forge.cr_review_threads(882)
        # A non-resolvable discussion is a plain comment, not a review thread; it
        # reaches the caller through the comment signal instead.
        self.assertEqual([(t.id, t.resolved, t.path) for t in threads],
                         [("d1", False, "a.py"), ("d2", True, "b.py")])


if __name__ == "__main__":
    unittest.main()
