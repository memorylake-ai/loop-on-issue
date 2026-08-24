import unittest
import uuid

import _bootstrap  # noqa: F401

from loopkit import runner
from loopkit.models import Repo

GH = Repo("github", "github.com", "acme/widget")
GL = Repo("gitlab", "gitlab.example.com", "darwin/zootopia")


class SessionId(unittest.TestCase):
    def test_is_deterministic(self):
        self.assertEqual(runner.session_id(GH, 12), runner.session_id(GH, 12))

    def test_is_a_uuid(self):
        uuid.UUID(runner.session_id(GH, 12))

    def test_differs_per_issue(self):
        self.assertNotEqual(runner.session_id(GH, 12), runner.session_id(GH, 13))

    def test_gitlab_keeps_the_legacy_key(self):
        # Issues already sitting at [PAUSED] on a board driven by the private
        # skills must still resume into their original session after upgrading.
        legacy = str(uuid.uuid5(uuid.NAMESPACE_URL, "loop-issue://darwin/zootopia#612"))
        self.assertEqual(runner.session_id(GL, 612), legacy)

    def test_github_ids_are_forge_qualified(self):
        same_path_gh = Repo("github", "github.com", "darwin/zootopia")
        self.assertNotEqual(runner.session_id(same_path_gh, 612), runner.session_id(GL, 612))

    def test_generation_yields_a_fresh_id(self):
        self.assertNotEqual(runner.session_id(GH, 12), runner.session_id(GH, 12, generation=1))


class RunnerSelection(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(runner.select("codex", ["runner::claude"], "claude"), "codex")

    def test_scoped_label_beats_config(self):
        self.assertEqual(runner.select(None, ["loop", "runner::codex"], "claude"), "codex")

    def test_plain_label_form_also_works(self):
        self.assertEqual(runner.select(None, ["runner:codex"], "claude"), "codex")

    def test_config_is_the_fallback(self):
        self.assertEqual(runner.select(None, ["loop"], "codex"), "codex")

    def test_default_is_claude(self):
        self.assertEqual(runner.select(None, [], None), "claude")

    def test_an_unknown_runner_label_is_ignored_rather_than_obeyed(self):
        self.assertEqual(runner.select(None, ["runner::gpt"], "claude"), "claude")

    def test_unknown_explicit_runner_is_an_error(self):
        with self.assertRaises(ValueError):
            runner.select("gpt", [], "claude")


class Commands(unittest.TestCase):
    def test_claude_start_pins_the_session_id(self):
        # Without it the session has no id to resume, and a paused issue can only
        # be restarted from scratch.
        cmd = runner.start_command("claude", "SID", "the brief")
        self.assertIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "SID")
        self.assertEqual(cmd[-1], "the brief")

    def test_claude_start_is_non_interactive(self):
        cmd = runner.start_command("claude", "SID", "b")
        self.assertIn("-p", cmd)
        self.assertIn("--permission-mode", cmd)

    def test_claude_resume_does_not_re_pin_the_id(self):
        cmd = runner.resume_command("claude", "SID", "continue")
        self.assertIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_codex_start_asks_for_jsonl(self):
        # The session id cannot be chosen up front, so it has to be read back out
        # of the event stream — which only exists with --json.
        self.assertIn("--json", runner.start_command("codex", None, "b"))

    def test_codex_start_needs_no_session_id(self):
        cmd = runner.start_command("codex", None, "b")
        self.assertEqual(cmd[:2], ["codex", "exec"])

    def test_codex_resume_targets_the_recorded_thread(self):
        cmd = runner.resume_command("codex", "THREAD", "continue")
        self.assertEqual(cmd[:3], ["codex", "exec", "resume"])
        self.assertIn("THREAD", cmd)
        self.assertEqual(cmd[-1], "continue")

    def test_codex_runs_without_approval_prompts(self):
        joined = " ".join(runner.start_command("codex", None, "b"))
        self.assertIn("approval_policy", joined)

    def test_resume_requires_a_session_for_codex(self):
        with self.assertRaises(ValueError):
            runner.resume_command("codex", None, "continue")


class Timeout(unittest.TestCase):
    def test_uses_the_portable_perl_alarm(self):
        # macOS ships neither timeout nor gtimeout.
        cmd = runner.wrap_timeout(["claude", "-p", "x"], 60)
        self.assertEqual(cmd[0], "perl")
        self.assertIn("alarm", " ".join(cmd))
        self.assertEqual(cmd[-3:], ["claude", "-p", "x"])
        self.assertIn("60", cmd)

    def test_zero_or_none_means_unbounded(self):
        self.assertEqual(runner.wrap_timeout(["claude"], 0), ["claude"])
        self.assertEqual(runner.wrap_timeout(["claude"], None), ["claude"])

    def test_expiry_exit_code_is_documented(self):
        self.assertEqual(runner.TIMEOUT_EXIT, 142)  # 128 + SIGALRM


class ExtractSessionId(unittest.TestCase):
    def test_reads_the_thread_started_event(self):
        stream = '{"type":"thread.started","thread_id":"01a031d1-d0c5-7962-b6b5-13f0f23859c2"}\n' \
                 '{"type":"turn.started"}\n'
        self.assertEqual(runner.extract_session_id(stream), "01a031d1-d0c5-7962-b6b5-13f0f23859c2")

    def test_skips_non_json_noise(self):
        # Codex writes tracing lines to the same stream before the first event.
        stream = 'ERROR codex_models_manager: failed to load cache\n' \
                 '{"type":"thread.started","thread_id":"abc-123"}\n'
        self.assertEqual(runner.extract_session_id(stream), "abc-123")

    def test_tolerates_a_renamed_field(self):
        self.assertEqual(
            runner.extract_session_id('{"session":{"session_id":"xyz"}}\n'), "xyz"
        )

    def test_returns_none_when_nothing_identifies_the_session(self):
        self.assertIsNone(runner.extract_session_id('{"type":"turn.started"}\n'))
        self.assertIsNone(runner.extract_session_id(""))


if __name__ == "__main__":
    unittest.main()


class MCPIsolation(unittest.TestCase):
    """A spawned session must not inherit the human's MCP servers.

    Two reasons, and the second is the serious one: every configured server costs
    a process start the task did not ask for, and an unattended session running
    under bypassPermissions would be handed tools for the human's mail, chat and
    design files.
    """

    def test_start_is_isolated(self):
        self.assertIn("--strict-mcp-config", runner.start_command("claude", "SID", "b"))

    def test_resume_is_isolated_too(self):
        # A resumed session is the same session, with the same reach.
        self.assertIn("--strict-mcp-config", runner.resume_command("claude", "SID", "go on"))

    def test_the_prompt_is_still_last(self):
        cmd = runner.start_command("claude", "SID", "the brief")
        self.assertEqual(cmd[-1], "the brief")

    def test_the_session_id_is_still_pinned(self):
        cmd = runner.start_command("claude", "SID", "b")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "SID")
