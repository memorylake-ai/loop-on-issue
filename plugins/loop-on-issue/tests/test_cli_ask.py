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


class Base(unittest.TestCase):
    def setUp(self):
        self.root = gitrepo.make()
        self.addCleanup(gitrepo.destroy, self.root)
        self.cli = FakeCLI()
        self.addCleanup(self.cli.cleanup)
        self.home = os.path.join(self.root, "fakehome")
        os.makedirs(self.home)
        self._saved_env = {k: os.environ.get(k) for k in ("LOOP_DINGTALK_ENV", "LOOP_PENDING_DIR")}
        os.environ["LOOP_DINGTALK_ENV"] = os.path.join(self.home, "none.env")
        os.environ["LOOP_PENDING_DIR"] = os.path.join(self.home, "pending")
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

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

    def comments(self, *bodies):
        self.cli.route("api", "/issues/12/comments", stdout=[
            {"id": i, "user": {"login": "x"}, "body": b, "created_at": "t{:03d}".format(i)}
            for i, b in enumerate(bodies)
        ])


class Ask(Base):
    def test_the_question_lands_on_the_issue_and_exits_two_when_unanswered(self):
        # Exit 2 is "asked, nobody answered yet" — routine, and the swarm's cue to
        # pause rather than to escalate.
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        self.cli.route("api", "/issues/12/comments", stdout=[])
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, stdout, _ = self.run_cli("ask", "--id", "12", "--question", "Which way?", "--json")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(stdout)["answered"])
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("Which way?", body)
        self.assertTrue(body.startswith("<!-- loop-on-issue:agent"))

    def test_options_are_numbered_on_the_issue(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        self.cli.route("api", "/issues/12/comments", stdout=[])
        self.cli.route("api", "/issues/12", stdout=self.issue())
        self.run_cli("ask", "--id", "12", "--question", "Q?", "--option", "left", "--option", "right")
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("1. left", body)
        self.assertIn("2. right", body)

    def test_dry_run_writes_nothing(self):
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, stdout, _ = self.run_cli("ask", "--id", "12", "--question", "Q?", "--dry-run", "--json")
        self.assertEqual(code, 0)
        self.assertIsNone(self.cli.call_containing("POST"))

    def test_the_question_may_come_from_stdin(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        self.cli.route("api", "/issues/12/comments", stdout=[])
        self.cli.route("api", "/issues/12", stdout=self.issue())
        saved = sys.stdin
        sys.stdin = io.StringIO("a long question\nwith newlines\n")
        try:
            self.run_cli("ask", "--id", "12", "--question-file", "-")
        finally:
            sys.stdin = saved
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("with newlines", body)


class Report(Base):
    def _payload(self):
        return json.dumps({
            "summary": "1 finished, 1 paused",
            "notes": {"12": "Submitted as #88.", "13": "Paused: needs a product call."},
        })

    def test_each_note_becomes_a_comment_on_its_own_issue(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        saved = sys.stdin
        sys.stdin = io.StringIO(self._payload())
        try:
            code, stdout, _ = self.run_cli("report", "--json-file", "-")
        finally:
            sys.stdin = saved
        self.assertEqual(code, 0)
        posted = [c for c in self.cli.calls if "POST" in c["joined"] and "/comments" in c["joined"]]
        self.assertEqual(len(posted), 2)
        self.assertIn("/issues/12/comments", posted[0]["joined"])
        self.assertIn("/issues/13/comments", posted[1]["joined"])

    def test_notes_are_marked_as_agent_written(self):
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        saved = sys.stdin
        sys.stdin = io.StringIO(self._payload())
        try:
            self.run_cli("report", "--json-file", "-")
        finally:
            sys.stdin = saved
        body = json.loads(self.cli.call_containing("POST", "/issues/12/comments")["stdin"])["body"]
        self.assertTrue(body.startswith("<!-- loop-on-issue:agent"))
        self.assertIn("Submitted as #88.", body)

    def test_it_reports_that_the_group_could_not_be_reached(self):
        # No DingTalk configured is normal, not an error — but silently dropping
        # half of a dual write is how the two surfaces drift apart.
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        saved = sys.stdin
        sys.stdin = io.StringIO(self._payload())
        try:
            code, stdout, _ = self.run_cli("report", "--json-file", "-", "--json")
        finally:
            sys.stdin = saved
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout)["notified"])

    def test_a_summary_alone_is_allowed(self):
        saved = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"summary": "nothing to do this round"}))
        try:
            code, _, _ = self.run_cli("report", "--json-file", "-")
        finally:
            sys.stdin = saved
        self.assertEqual(code, 0)

    def test_malformed_input_is_refused(self):
        saved = sys.stdin
        sys.stdin = io.StringIO("{not json")
        try:
            code, _, stderr = self.run_cli("report", "--json-file", "-")
        finally:
            sys.stdin = saved
        self.assertEqual(code, 1)


class ClaimWritesTheSession(Base):
    def _claimable(self, labels=("loop",)):
        self.cli.route("api", "--method GET", "/issues/12", stdout=self.issue(labels=labels), once=True)
        self.cli.route("api", "PATCH", "/issues/12", stdout=self.issue())
        self.cli.route("api", "/issues/12", stdout=self.issue(title="[CLAIMED] fix drive URI", labels=labels))
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})

    def test_claiming_records_the_session_on_the_issue(self):
        # The board becomes the record: a later run reads which session owns this
        # issue without recomputing anything.
        self._claimable()
        code, stdout, _ = self.run_cli("claim", "--id", "12", "--assignee", "muxuan")
        self.assertEqual(code, 0)
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("session=", body)
        self.assertIn("runner=claude", body)
        self.assertIn(json.loads(stdout)["session_id"], body)

    def test_a_codex_claim_records_the_runner_but_no_session(self):
        # codex assigns its own id at start; a placeholder would look
        # authoritative and resume into a session that never existed.
        self._claimable(labels=("loop", "runner::codex"))
        code, _, _ = self.run_cli("claim", "--id", "12", "--assignee", "muxuan")
        self.assertEqual(code, 0)
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("runner=codex", body)
        self.assertNotIn("session=", body)

    def test_a_failed_claim_records_nothing(self):
        self.cli.route("api", "/issues/12", stdout=self.issue(title="[WORKING] x"))
        self.assertEqual(self.run_cli("claim", "--id", "12")[0], 2)
        self.assertIsNone(self.cli.call_containing("POST", "/comments"))


class SessionIdPrefersTheBoard(Base):
    def test_a_recorded_session_wins_over_derivation(self):
        self.comments("<!-- loop-on-issue:agent session=recorded-abc runner=claude -->\nclaimed")
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, stdout, _ = self.run_cli("session-id", "--id", "12")
        self.assertEqual((code, stdout.strip()), (0, "recorded-abc"))

    def test_derivation_still_answers_an_issue_from_before_this_change(self):
        # Boards that predate session comments must keep resuming.
        self.comments()
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, stdout, _ = self.run_cli("session-id", "--id", "12")
        self.assertEqual(code, 0)
        import uuid

        uuid.UUID(stdout.strip())

    def test_a_recorded_runner_outranks_a_label(self):
        # Switching runners mid-issue would strand the session that is already
        # holding this issue's context.
        self.comments("<!-- loop-on-issue:agent runner=codex -->\nclaimed")
        self.cli.route("api", "/issues/12", stdout=self.issue(labels=("loop", "runner::claude")))
        code, _, stderr = self.run_cli("session-id", "--id", "12")
        self.assertEqual(code, 2)
        self.assertIn("session-record", stderr)

    def test_an_explicit_runner_still_wins(self):
        self.comments("<!-- loop-on-issue:agent runner=codex -->\nclaimed")
        self.cli.route("api", "/issues/12", stdout=self.issue())
        code, stdout, _ = self.run_cli("session-id", "--id", "12", "--runner", "claude")
        self.assertEqual(code, 0)
        self.assertTrue(stdout.strip())


if __name__ == "__main__":
    unittest.main()
