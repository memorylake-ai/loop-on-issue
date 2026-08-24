import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

import _bootstrap  # noqa: F401

import gitrepo
from fakecli import FakeCLI

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import loop_cli  # noqa: E402


class CLITest(unittest.TestCase):
    def setUp(self):
        self.root = gitrepo.make()
        self.addCleanup(gitrepo.destroy, self.root)
        self.cli = FakeCLI()
        self.addCleanup(self.cli.cleanup)

    def run_cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = loop_cli.main(["-C", self.root] + list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def issue(self, number=12, title="fix drive URI", labels=("loop",), assignees=("muxuan",)):
        return {
            "number": number, "title": title, "state": "open",
            "html_url": "https://github.com/acme/widget/issues/{}".format(number),
            "labels": [{"name": n} for n in labels],
            "assignees": [{"login": a} for a in assignees],
            "body": "b", "created_at": "2026-08-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
        }


class Claiming(CLITest):
    def test_claims_an_unclaimed_issue(self):
        # Unclaimed on the first read, claimed on the read-back — the two-phase
        # check the claim actually performs.
        self.cli.route("api", "--method GET", "/issues/12", stdout=self.issue(), once=True)
        self.cli.route("api", "PATCH", "/issues/12", stdout=self.issue(title="[CLAIMED] fix drive URI"))
        self.cli.route("api", "/issues/12", stdout=self.issue(title="[CLAIMED] fix drive URI"))
        code, stdout, _ = self.run_cli("claim", "--id", "12", "--assignee", "muxuan")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["state"], "CLAIMED")

    def test_an_already_claimed_issue_is_a_precondition_not_an_error(self):
        # Losing a claim race is routine; the caller moves to the next issue.
        self.cli.route("api", "/issues/12", stdout=self.issue(title="[WORKING] fix drive URI"))
        code, _, stderr = self.run_cli("claim", "--id", "12")
        self.assertEqual(code, 2)
        self.assertIn("already in state WORKING", stderr)

    def test_a_codex_issue_is_not_handed_a_derived_session_id(self):
        # A derived id looks authoritative and resumes into a session that never
        # existed; codex ids only ever come from what a previous run recorded.
        self.cli.route("api", "--method GET", "/issues/12",
                       stdout=self.issue(labels=("loop", "runner::codex")), once=True)
        self.cli.route("api", "PATCH", "/issues/12", stdout=self.issue())
        self.cli.route("api", "/issues/12", stdout=self.issue(title="[CLAIMED] fix drive URI"))
        code, stdout, _ = self.run_cli("claim", "--id", "12", "--assignee", "muxuan")
        payload = json.loads(stdout)
        self.assertEqual((code, payload["runner"], payload["session_id"]), (0, "codex", None))

    def test_an_issue_that_lost_the_queue_label_is_refused(self):
        self.cli.route("api", "/issues/12", stdout=self.issue(labels=("bug",)))
        self.assertEqual(self.run_cli("claim", "--id", "12")[0], 2)

    def test_an_issue_assigned_to_someone_else_is_refused(self):
        self.cli.route("api", "/issues/12", stdout=self.issue(assignees=("someone",)))
        self.assertEqual(self.run_cli("claim", "--id", "12", "--assignee", "muxuan")[0], 2)

    def test_a_claim_that_did_not_stick_is_reported(self):
        # Neither forge offers compare-and-swap on a title, so the write is read
        # back to see whose prefix actually landed.
        self.cli.route("api", "PATCH", "/issues/12", stdout=self.issue())
        self.cli.route("api", "/issues/12", stdout=self.issue())  # still unclaimed on read-back
        code, _, stderr = self.run_cli("claim", "--id", "12")
        self.assertEqual(code, 2)
        self.assertIn("did not stick", stderr)


class Transitions(CLITest):
    def test_expect_guards_the_write(self):
        self.cli.route("api", "/issues/12", stdout=self.issue(title="[WORKING] x"))
        code, _, stderr = self.run_cli("transition", "--id", "12", "--to", "FINISHED", "--expect", "CLAIMED")
        self.assertEqual(code, 2)
        self.assertIsNone(self.cli.call_containing("PATCH"))

    def test_a_no_op_transition_says_so_and_writes_nothing(self):
        self.cli.route("api", "/issues/12", stdout=self.issue(title="[WORKING] x"))
        code, stdout, _ = self.run_cli("transition", "--id", "12", "--to", "WORKING")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["noop"])
        self.assertIsNone(self.cli.call_containing("PATCH"))

    def test_releasing_back_to_the_queue_strips_the_prefix(self):
        self.cli.route("api", "PATCH", "/issues/12", stdout=self.issue())
        self.cli.route("api", "/issues/12", stdout=self.issue(title="[PAUSED] fix drive URI"))
        self.assertEqual(self.run_cli("transition", "--id", "12", "--to", "NONE")[0], 0)
        payload = json.loads(self.cli.call_containing("PATCH")["stdin"])
        self.assertEqual(payload["title"], "fix drive URI")


class Skipping(CLITest):
    def test_a_thin_reason_is_refused_and_nothing_is_written(self):
        # An unexplained [SKIP] is indistinguishable from an agent dodging work.
        code, _, stderr = self.run_cli("skip", "--id", "12", "--reason", "no")
        self.assertEqual(code, 2)
        self.assertIn("substantive reason", stderr)
        self.assertEqual(self.cli.calls, [])

    def test_the_reason_is_posted_before_the_state_changes(self):
        # If the title write fails, the reasoning still survives on the issue.
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        self.cli.route("api", "PATCH", "/issues/12", stdout=self.issue())
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, _, _ = self.run_cli("skip", "--id", "12", "--reason", "Already fixed by #881; that write path is gone.")
        self.assertEqual(code, 0)
        order = [c["joined"] for c in self.cli.calls]
        self.assertLess(next(i for i, c in enumerate(order) if "comments" in c),
                        next(i for i, c in enumerate(order) if "PATCH" in c))


class Comments(CLITest):
    def test_agent_comments_carry_the_marker(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        self.run_cli("comment", "--id", "12", "--body", "plan posted")
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertTrue(body.startswith("<!-- loop-on-issue:agent"))

    def test_no_marker_is_available_for_relaying_human_text(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        self.run_cli("comment", "--id", "12", "--no-marker", "--body", "the human said: ship it")
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertFalse(body.startswith("<!--"))

    def test_an_empty_body_is_refused(self):
        self.assertEqual(self.run_cli("comment", "--id", "12", "--body", "   ")[0], 2)

    def test_a_change_request_comment_targets_the_change_request(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        self.run_cli("comment", "--pr", "88", "--body", "Addressed in abc123.")
        self.assertIn("/issues/88/comments", self.cli.call_containing("POST")["joined"])


class HumanReply(CLITest):
    def _comments(self, *bodies):
        self.cli.route("api", "/issues/12/comments", stdout=[
            {"id": i, "user": {"login": "x"}, "body": b, "created_at": "t{}".format(i)}
            for i, b in enumerate(bodies)
        ])

    def test_waiting_exits_two(self):
        self._comments("<!-- loop-on-issue:agent -->\nasked a question")
        self.assertEqual(self.run_cli("human-reply", "--id", "12")[0], 2)

    def test_an_answer_exits_zero_and_is_returned(self):
        self._comments("<!-- loop-on-issue:agent -->\nasked", "use the second option")
        code, stdout, _ = self.run_cli("human-reply", "--id", "12")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["replies"][0]["body"], "use the second option")

    def test_the_legacy_marker_still_counts_as_ours(self):
        # Otherwise a board mid-migration reads our own old notes as human replies
        # and wakes every paused issue at once.
        self._comments("<!-- loop-swarm-agent -->\nasked")
        self.assertEqual(self.run_cli("human-reply", "--id", "12")[0], 2)


class ChangeRequestStatus(CLITest):
    def _no_link(self):
        self.cli.route("graphql", "closedByPullRequestsReferences",
                       stdout={"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": []}}}}})
        self.cli.route("api", "search/issues", stdout={"items": []})

    def test_missing_change_request_names_the_near_misses(self):
        self._no_link()
        self.cli.route("api", "/timeline", stdout=[
            {"event": "cross-referenced", "source": {"issue": {
                "number": 90, "title": "stacked on #12", "state": "open",
                "html_url": "u", "created_at": "t", "pull_request": {}}}}])
        code, _, stderr = self.run_cli("pr-status", "--id", "12")
        self.assertEqual(code, 2)
        self.assertIn("#90", stderr)
        self.assertIn("NOT attributed", stderr)

    def test_mr_status_is_the_same_command(self):
        self._no_link()
        self.cli.route("api", "/timeline", stdout=[])
        self.assertEqual(self.run_cli("mr-status", "--id", "12")[0], 2)

    def test_feedback_on_a_merged_change_request_is_not_outstanding_work(self):
        # Without this guard every historical comment reads as unaddressed and the
        # issue bounces out of FINISHED forever.
        self.cli.route("graphql", "closedByPullRequestsReferences", stdout={"data": {"repository": {"issue": {
            "closedByPullRequestsReferences": {"nodes": [{
                "number": 88, "title": "to #12: x", "state": "MERGED", "url": "u", "isDraft": False,
                "headRefName": "h", "baseRefName": "main", "createdAt": "t"}]}}}}})
        code, _, stderr = self.run_cli("pr-feedback", "--id", "12")
        self.assertEqual(code, 2)
        self.assertIn("not open for review", stderr)


class Sessions(CLITest):
    def test_claude_session_ids_are_derived(self):
        # session-id now reads the board before deriving, so the comment thread
        # has to be answerable even when it is empty.
        self.cli.route("api", "/issues/12/comments", stdout=[])
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, stdout, _ = self.run_cli("session-id", "--id", "12")
        self.assertEqual(code, 0)
        import uuid

        uuid.UUID(stdout.strip())

    def test_codex_without_a_recorded_session_is_a_precondition(self):
        self.cli.route("api", "/issues/12/comments", stdout=[])
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, _, stderr = self.run_cli("session-id", "--id", "12", "--runner", "codex")
        self.assertEqual(code, 2)
        self.assertIn("session-record", stderr)

    def test_a_recorded_codex_session_is_read_back(self):
        self.cli.route("api", "/issues/12/comments", stdout=[
            {"id": 1, "user": {"login": "x"}, "created_at": "t1",
             "body": "<!-- loop-on-issue:agent session=thread-abc runner=codex -->\nstarted"}])
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, stdout, _ = self.run_cli("session-id", "--id", "12", "--runner", "codex")
        self.assertEqual((code, stdout.strip()), (0, "thread-abc"))

    def test_a_runner_label_on_the_issue_selects_the_runner(self):
        self.cli.route("api", "/issues/12/comments", stdout=[])
        self.cli.route("api", "/issues/12", stdout=self.issue(labels=("loop", "runner::codex")))
        self.assertEqual(self.run_cli("session-id", "--id", "12")[0], 2)  # codex, none recorded

    def test_recording_writes_a_marker_comment(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        code, _, _ = self.run_cli("session-record", "--id", "12", "--session", "thread-abc", "--runner", "codex")
        self.assertEqual(code, 0)
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("session=thread-abc", body)
        self.assertIn("runner=codex", body)


class Creating(CLITest):
    def _labels(self, *names):
        self.cli.route("api", "/labels", stdout=[{"name": n} for n in names])

    def _assignees(self, *names):
        self.cli.route("api", "/assignees", stdout=[{"login": n} for n in names])

    def test_an_unknown_label_is_refused_before_anything_is_written(self):
        # Both forges create a label on first use; the issue would silently drop
        # out of every board filter built on the real one.
        self._labels("loop", "web-admin")
        code, _, stderr = self.run_cli("create", "--title", "t", "--body", "b",
                                       "--label", "web_admin", "--assignee", "muxuan")
        self.assertEqual(code, 2)
        self.assertIn("web-admin", stderr)
        self.assertIsNone(self.cli.call_containing("POST"))

    def test_blocked_work_may_not_carry_the_queue_label(self):
        code, _, stderr = self.run_cli("create", "--title", "t", "--body", "b",
                                       "--label", "loop", "--blocked-by", "13", "--assignee", "muxuan")
        self.assertEqual(code, 2)
        self.assertIn("cannot start", stderr)

    def test_blocked_work_is_created_unqueued_with_the_dependency_stamped_in(self):
        self._labels("loop")
        self._assignees("muxuan")
        code, stdout, _ = self.run_cli("create", "--title", "t", "--body", "b",
                                       "--blocked-by", "13", "--assignee", "muxuan", "--dry-run")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertFalse(payload["queued"])
        self.assertIn("Blocked by #13", payload["body"])

    def test_the_queue_label_is_added_automatically(self):
        self._labels("loop")
        self._assignees("muxuan")
        _, stdout, _ = self.run_cli("create", "--title", "t", "--body", "b",
                                    "--assignee", "muxuan", "--dry-run")
        self.assertTrue(json.loads(stdout)["queued"])

    def test_an_empty_body_is_refused(self):
        code, _, stderr = self.run_cli("create", "--title", "t", "--body", "  ", "--assignee", "muxuan")
        self.assertEqual(code, 2)
        self.assertIn("[SKIP]", stderr)

    def test_a_missing_assignee_is_refused(self):
        self._labels("loop")
        code, _, stderr = self.run_cli("create", "--title", "t", "--body", "b")
        self.assertEqual(code, 2)
        self.assertIn("invisible", stderr)

    def test_dry_run_writes_nothing(self):
        self._labels("loop")
        self._assignees("muxuan")
        self.run_cli("create", "--title", "t", "--body", "b", "--assignee", "muxuan", "--dry-run")
        self.assertIsNone(self.cli.call_containing("POST"))

    def test_the_repeatable_label_flag_does_not_become_the_queue_label(self):
        # --label means two different things on `create` and on `list`; sharing an
        # argparse dest let one silently overwrite the other in the config overlay.
        self._labels("loop", "web-admin")
        self._assignees("muxuan")
        _, stdout, _ = self.run_cli("create", "--title", "t", "--body", "b", "--label", "web-admin",
                                    "--assignee", "muxuan", "--dry-run")
        payload = json.loads(stdout)
        self.assertEqual(sorted(payload["labels"]), ["loop", "web-admin"])
        self.assertTrue(payload["queued"])

    def test_an_epic_reference_becomes_a_backlink(self):
        self._labels("loop")
        self._assignees("muxuan")
        _, stdout, _ = self.run_cli("create", "--title", "t", "--body", "b",
                                    "--assignee", "muxuan", "--epic", "600", "--dry-run")
        self.assertTrue(json.loads(stdout)["body"].startswith("Part of #600"))


class Setup(CLITest):
    def test_init_without_yes_writes_nothing(self):
        self.cli.route("api", "/labels", stdout=[{"name": "loop"}])
        code, stdout, _ = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertIn("Nothing has been written", stdout)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".github")))

    def test_init_applies_with_yes(self):
        self.cli.route("api", "/labels", stdout=[{"name": "loop"}])
        code, _, _ = self.run_cli("init", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".github", "ISSUE_TEMPLATE", "loop-task.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".loop-on-issue", "config.json")))

    def test_init_survives_a_forge_it_cannot_reach_yet(self):
        # Templates and config are still worth writing on a machine that has not
        # authenticated; only the label step is unavailable.
        self.cli.route("api", "/labels", exit=1, stderr="HTTP 401")
        code, _, _ = self.run_cli("init", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".loop-on-issue", "config.json")))

    def test_template_show_reports_which_layer_won(self):
        code, stdout, stderr = self.run_cli("template", "show", "issue")
        self.assertEqual(code, 0)
        self.assertIn("source: bundled", stderr)
        self.assertIn("Acceptance criteria", stdout)

    def test_doctor_json_is_machine_readable(self):
        self.cli.route("auth status", stdout="github.com\n  Token scopes: 'repo'\n")
        self.cli.route("--version", stdout="gh version 2.82.1\n")
        self.cli.route("api", "/labels", stdout=[{"name": "loop"}])
        self.cli.route("api", "/assignees", stdout=[{"login": "muxuan"}])
        self.cli.route("api", "repos/acme/widget", stdout={"permissions": {"push": True}})
        code, stdout, _ = self.run_cli("doctor", "--json")
        payload = json.loads(stdout)
        self.assertEqual(payload["repo"]["forge"], "github")
        self.assertIn(code, (0, 2))


if __name__ == "__main__":
    unittest.main()
