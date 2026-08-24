import unittest

import _bootstrap  # noqa: F401

from loopkit import state
from loopkit.models import Comment


class SplitState(unittest.TestCase):
    def test_plain_title_has_no_state(self):
        self.assertEqual(state.split_state("fix drive URI"), (None, "fix drive URI"))

    def test_reads_a_single_prefix(self):
        self.assertEqual(
            state.split_state("[WORKING] fix drive URI"), ("WORKING", "fix drive URI")
        )

    def test_stacked_prefixes_keep_the_first(self):
        # An interrupted transition can leave two prefixes; the title must still
        # normalise instead of growing one more every run.
        self.assertEqual(state.split_state("[CLAIMED][WORKING] x"), ("CLAIMED", "x"))
        self.assertEqual(state.split_state("[WORKING] [PAUSED] x"), ("WORKING", "x"))

    def test_unrelated_prefix_is_left_alone(self):
        self.assertEqual(state.split_state("[TEST] something"), (None, "[TEST] something"))
        self.assertEqual(state.split_state("[WORKING] [TEST] x"), ("WORKING", "[TEST] x"))

    def test_state_names_are_case_sensitive(self):
        self.assertEqual(state.split_state("[working] x"), (None, "[working] x"))

    def test_brackets_later_in_the_title_are_not_a_prefix(self):
        self.assertEqual(
            state.split_state("fix [WORKING] mid-title"), (None, "fix [WORKING] mid-title")
        )

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(state.split_state("  [SKIP]   trailing  "), ("SKIP", "trailing"))

    def test_every_declared_state_round_trips(self):
        for name in state.STATES:
            self.assertEqual(state.split_state(state.compose(name, "t")), (name, "t"))


class Compose(unittest.TestCase):
    def test_none_state_yields_the_bare_title(self):
        self.assertEqual(state.compose(None, "x"), "x")

    def test_state_is_prefixed_with_a_single_space(self):
        self.assertEqual(state.compose("WORKING", "x"), "[WORKING] x")


class AgentMarker(unittest.TestCase):
    def test_stamped_body_is_recognised(self):
        self.assertTrue(state.is_agent_note(state.stamp("hello")))

    def test_legacy_marker_is_still_recognised(self):
        # Boards already running the private skills must not lose their
        # "has a human replied" anchor when the plugin takes over.
        self.assertTrue(state.is_agent_note("<!-- loop-swarm-agent -->\nold note"))

    def test_marker_with_attributes_is_recognised(self):
        body = state.stamp("x", session="abc", runner="codex")
        self.assertTrue(state.is_agent_note(body))

    def test_human_text_is_not_an_agent_note(self):
        self.assertFalse(state.is_agent_note("looks good to me"))

    def test_attributes_round_trip(self):
        body = state.stamp("x", session="8f14e45f", runner="codex")
        self.assertEqual(
            state.parse_marker(body), {"session": "8f14e45f", "runner": "codex"}
        )

    def test_marker_without_attributes_parses_to_empty(self):
        self.assertEqual(state.parse_marker(state.stamp("x")), {})

    def test_unmarked_body_parses_to_none(self):
        self.assertIsNone(state.parse_marker("plain text"))

    def test_stamp_keeps_the_body_intact(self):
        self.assertIn("hello **world** 中文", state.stamp("hello **world** 中文"))


def _c(author, at, body, system=False):
    return Comment(id=at, author=author, created_at=at, body=body, system=system)


class Unanswered(unittest.TestCase):
    def test_human_note_after_our_last_marker_counts(self):
        notes = [_c("bot", "1", state.stamp("asked")), _c("dev", "2", "answered")]
        self.assertEqual([n.body for n in state.unanswered(notes)], ["answered"])

    def test_nothing_outstanding_once_we_replied_again(self):
        notes = [
            _c("bot", "1", state.stamp("asked")),
            _c("dev", "2", "answered"),
            _c("bot", "3", state.stamp("thanks")),
        ]
        self.assertEqual(state.unanswered(notes), [])

    def test_human_notes_count_when_we_never_posted(self):
        notes = [_c("dev", "1", "first word on this")]
        self.assertEqual(len(state.unanswered(notes)), 1)

    def test_system_notes_are_ignored(self):
        notes = [
            _c("bot", "1", state.stamp("asked")),
            _c("gitlab", "2", "changed the title", system=True),
        ]
        self.assertEqual(state.unanswered(notes), [])

    def test_ordering_is_by_timestamp_not_list_order(self):
        notes = [_c("dev", "2", "answered"), _c("bot", "1", state.stamp("asked"))]
        self.assertEqual([n.body for n in state.unanswered(notes)], ["answered"])


class LatestSession(unittest.TestCase):
    def test_returns_the_newest_recorded_session(self):
        notes = [
            _c("bot", "1", state.stamp("start", session="aaa", runner="codex")),
            _c("bot", "3", state.stamp("restart", session="ccc", runner="codex")),
            _c("bot", "2", state.stamp("noise")),
        ]
        self.assertEqual(state.latest_session(notes), {"session": "ccc", "runner": "codex"})

    def test_returns_none_when_nothing_was_recorded(self):
        self.assertIsNone(state.latest_session([_c("bot", "1", state.stamp("x"))]))


if __name__ == "__main__":
    unittest.main()
