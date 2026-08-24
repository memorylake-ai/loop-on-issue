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

TOOL_INPUT = {
    "questions": [
        {
            "question": "Which storage backend?",
            "header": "Storage",
            "multiSelect": False,
            "options": [
                {"label": "Postgres", "description": "already deployed"},
                {"label": "SQLite", "description": "no ops burden"},
            ],
        }
    ]
}


class Hook(unittest.TestCase):
    def setUp(self):
        self.root = gitrepo.make()
        self.addCleanup(gitrepo.destroy, self.root)
        self.cli = FakeCLI()
        self.addCleanup(self.cli.cleanup)
        self.saved = {k: os.environ.get(k) for k in ("LOOP_ISSUE", "LOOP_ASK_WAIT", "LOOP_DINGTALK_ENV")}
        os.environ["LOOP_DINGTALK_ENV"] = os.path.join(self.root, "none.env")
        os.environ["LOOP_ASK_WAIT"] = "0"
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_hook(self, payload, issue="12"):
        if issue is None:
            os.environ.pop("LOOP_ISSUE", None)
        else:
            os.environ["LOOP_ISSUE"] = issue
        stdout, stderr = io.StringIO(), io.StringIO()
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = loop_cli.main(["-C", self.root, "hook", "ask-user-question"])
        finally:
            sys.stdin = saved_stdin
        return code, stdout.getvalue(), stderr.getvalue()

    def _issue(self):
        return {
            "number": 12, "title": "t", "state": "open",
            "html_url": "https://github.com/acme/widget/issues/12",
            "labels": [], "assignees": [], "body": "", "created_at": "c", "updated_at": "u",
        }

    def _routes(self, replies=()):
        """Model the thread as it really evolves: our question, then the answer.

        An answer that predates the question is not an answer to it, so the fake
        has to deliver replies on a *later* read than the one that anchors them.
        """
        self.cli.route("api", "--method POST", "/comments", stdout={"id": 1})
        asked = {"id": 1, "user": {"login": "bot"}, "created_at": "t500",
                 "body": "<!-- loop-on-issue:agent -->\nasked"}
        if replies:
            self.cli.route("api", "/issues/12/comments", stdout=[asked], once=True)
            self.cli.route("api", "/issues/12/comments", stdout=[asked] + [
                {"id": 2 + i, "user": {"login": "dev"}, "body": b,
                 "created_at": "z{:03d}".format(600 + i)}
                for i, b in enumerate(replies)
            ])
        else:
            self.cli.route("api", "/issues/12/comments", stdout=[asked])
        self.cli.route("api", "/issues/12", stdout=self._issue())

    # -- staying out of the way ---------------------------------------------
    def test_outside_a_loop_session_the_tool_is_left_alone(self):
        # A developer's own interactive session must be untouched.
        code, stdout, stderr = self.run_hook({"tool_input": TOOL_INPUT}, issue=None)
        self.assertEqual(code, 0)
        self.assertEqual(self.cli.calls, [])

    def test_a_disabled_bot_makes_the_hook_a_no_op(self):
        # The chat bot is optional. Switched off, AskUserQuestion has to behave
        # exactly as it does in a plugin that never had the feature.
        env_path = os.path.join(self.root, "off.env")
        with open(env_path, "w") as fh:
            fh.write('LOOP_DINGTALK_ENABLED="0"\n')
        os.environ["LOOP_DINGTALK_ENV"] = env_path
        code, _, _ = self.run_hook({"tool_input": TOOL_INPUT})
        self.assertEqual(code, 0)
        self.assertEqual(self.cli.calls, [])

    def test_an_unparseable_payload_does_not_block_the_session(self):
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO("{not json")
        os.environ["LOOP_ISSUE"] = "12"
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = loop_cli.main(["-C", self.root, "hook", "ask-user-question"])
        finally:
            sys.stdin = saved_stdin
        self.assertEqual(code, 0)

    # -- relaying ------------------------------------------------------------
    def test_the_question_reaches_the_issue(self):
        self._routes()
        self.run_hook({"tool_input": TOOL_INPUT})
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("Which storage backend?", body)
        self.assertIn("1. Postgres", body)
        self.assertIn("2. SQLite", body)

    def test_option_descriptions_are_carried_across(self):
        # They are often where the actual trade-off is written down.
        self._routes()
        self.run_hook({"tool_input": TOOL_INPUT})
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("already deployed", body)

    def test_an_answer_is_injected_as_the_block_reason(self):
        # Exit 2 is how a PreToolUse hook blocks, and stderr is what Claude Code
        # feeds back to the model. There is no way to supply a tool result, so the
        # answer has to travel as the reason.
        self._routes(replies=["2"])
        code, _, stderr = self.run_hook({"tool_input": TOOL_INPUT})
        self.assertEqual(code, 2)
        self.assertIn("SQLite", stderr)

    def test_free_text_answers_come_through_verbatim(self):
        self._routes(replies=["neither — use the object store we already pay for"])
        code, _, stderr = self.run_hook({"tool_input": TOOL_INPUT})
        self.assertEqual(code, 2)
        self.assertIn("object store we already pay for", stderr)

    def test_nobody_answering_steers_the_session_to_pause(self):
        self._routes()
        code, _, stderr = self.run_hook({"tool_input": TOOL_INPUT})
        self.assertEqual(code, 2)
        self.assertIn("#12", stderr)
        self.assertIn("PAUSED", stderr)

    def test_several_questions_are_relayed_without_option_numbering(self):
        # Numbering across questions would be ambiguous — "2" could mean either
        # question's second option — so multi-question asks go out as free text.
        payload = {"tool_input": {"questions": [
            dict(TOOL_INPUT["questions"][0]),
            {"question": "Ship behind a flag?", "header": "Rollout",
             "options": [{"label": "yes"}, {"label": "no"}]},
        ]}}
        self._routes()
        self.run_hook(payload)
        body = json.loads(self.cli.call_containing("POST", "/comments")["stdin"])["body"]
        self.assertIn("Which storage backend?", body)
        self.assertIn("Ship behind a flag?", body)
        self.assertNotIn("直接在本 issue 回复编号", body)

    def test_a_forge_failure_blocks_rather_than_pretending_to_ask(self):
        # Allowing the tool through would leave a headless session waiting on a
        # prompt nobody can answer.
        os.environ["LOOP_ISSUE"] = "12"
        code, _, stderr = self.run_hook({"tool_input": TOOL_INPUT})
        self.assertEqual(code, 2)
        self.assertTrue(stderr.strip())


class HookWait(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("LOOP_ASK_WAIT")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            os.environ.pop("LOOP_ASK_WAIT", None)
        else:
            os.environ["LOOP_ASK_WAIT"] = self.saved

    def _wait(self, value):
        if value is None:
            os.environ.pop("LOOP_ASK_WAIT", None)
        else:
            os.environ["LOOP_ASK_WAIT"] = value
        return loop_cli.hook_wait()

    def test_default_when_unset(self):
        self.assertEqual(self._wait(None), loop_cli.HOOK_WAIT_DEFAULT)

    def test_read_from_the_environment(self):
        self.assertEqual(self._wait("45"), 45)

    def test_a_malformed_value_falls_back_instead_of_raising(self):
        # A typo in a routine's environment must not take out every session it
        # starts.
        self.assertEqual(self._wait("not-a-number"), loop_cli.HOOK_WAIT_DEFAULT)

    def test_zero_is_honoured(self):
        self.assertEqual(self._wait("0"), 0)

    def test_negative_is_clamped_to_zero(self):
        self.assertEqual(self._wait("-5"), 0)

    def test_an_absurd_value_is_capped(self):
        # A blocked session holds a slot; past a few minutes the durable channel
        # is strictly better than waiting.
        self.assertEqual(self._wait("999999"), loop_cli.HOOK_WAIT_MAX)


if __name__ == "__main__":
    unittest.main()
