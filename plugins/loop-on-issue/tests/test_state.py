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


class VisibleAttribution(unittest.TestCase):
    """A human reading the board must be able to tell who wrote what.

    The agent authenticates as the same account as the person it reports to, so
    authorship shows theirs. The machine marker is an HTML comment and renders as
    nothing — correct for the machine, useless for the human, who saw five
    identical-looking notes from themselves.
    """

    def test_an_agent_note_says_so_visibly(self):
        self.assertIn(state.AGENT_PREFIX, state.stamp("posted the plan"))

    def test_the_prefix_comes_before_the_body(self):
        body = state.stamp("posted the plan")
        self.assertLess(body.index(state.AGENT_PREFIX), body.index("posted the plan"))

    def test_the_machine_marker_is_still_there(self):
        # It is what "has a human replied" anchors on; the visible prefix is
        # additive, not a replacement.
        self.assertTrue(state.is_agent_note(state.stamp("x")))

    def test_attributes_still_round_trip(self):
        body = state.stamp("x", session="abc", runner="claude")
        self.assertEqual(state.parse_marker(body), {"session": "abc", "runner": "claude"})

    def test_a_relayed_human_answer_is_not_labelled_as_an_agent(self):
        # It is a human's answer, carried. Labelling it [AGENT] would misattribute
        # the decision to the machine that merely delivered it.
        body = state.relay("go left", by="张三", via="dingtalk")
        self.assertNotIn(state.AGENT_PREFIX, body)
        self.assertFalse(state.is_agent_note(body))

    def test_a_note_from_before_this_change_is_still_recognised(self):
        self.assertTrue(state.is_agent_note("<!-- loop-on-issue:agent -->\nold, unprefixed"))

    def test_a_human_writing_the_prefix_by_hand_is_not_an_agent_note(self):
        # Otherwise anybody could silence their own reply by typing it.
        self.assertFalse(state.is_agent_note("[AGENT] I am pretending"))

    def test_unanswered_still_sees_a_human_reply_after_a_prefixed_note(self):
        notes = [
            Comment(id=1, author="bot", created_at="t1", body=state.stamp("asked")),
            Comment(id=2, author="dev", created_at="t2", body="answered"),
        ]
        self.assertEqual([c.body for c in state.unanswered(notes)], ["answered"])


class VisibleSession(unittest.TestCase):
    """Whatever the marker records, the reader can see.

    The session id was an attribute of an HTML comment: recorded, recomputable,
    and invisible. Somebody looking at a claimed issue could not tell which
    session owned it, nor resume into it themselves — which is most of the point
    of recording it on the board instead of deriving it.
    """

    def test_a_recorded_session_is_rendered(self):
        body = state.stamp("Claimed.", session="9be05ae7-0957", runner="claude")
        self.assertIn("9be05ae7-0957", _rendered(body))

    def test_the_runner_is_named_beside_it(self):
        body = state.stamp("Claimed.", session="abc", runner="codex")
        self.assertIn("codex", _rendered(body))

    def test_it_shows_how_to_get_into_it(self):
        # A человек reading the board should be able to act on it, not just see it.
        self.assertIn("--resume", _rendered(state.stamp("x", session="abc", runner="claude")))

    def test_codex_gets_its_own_resume_command(self):
        rendered = _rendered(state.stamp("x", session="abc", runner="codex"))
        self.assertIn("codex", rendered)
        self.assertNotIn("claude --resume", rendered)

    def test_a_note_with_no_session_gains_no_footer(self):
        # Every progress comment would otherwise carry it, which is noise.
        self.assertNotIn("--resume", _rendered(state.stamp("posted the plan")))

    def test_a_runner_without_a_session_is_still_stated(self):
        # codex has no id until it starts; saying which runner holds the issue is
        # still worth the line.
        rendered = _rendered(state.stamp("Claimed.", runner="codex"))
        self.assertIn("codex", rendered)
        self.assertNotIn("--resume", rendered)

    def test_the_attributes_still_parse(self):
        body = state.stamp("x", session="abc", runner="claude")
        self.assertEqual(state.parse_marker(body), {"session": "abc", "runner": "claude"})

    def test_the_body_is_still_first(self):
        body = _rendered(state.stamp("Claimed.", session="abc", runner="claude"))
        self.assertLess(body.index("Claimed."), body.index("abc"))


def _rendered(body):
    """What a forge shows a human: HTML comments removed."""
    import re

    return re.sub(r"<!--.*?-->\n?", "", body, flags=re.DOTALL).strip()


class SeparatorDoesNotBecomeAHeading(unittest.TestCase):
    def test_a_blank_line_precedes_the_rule(self):
        # `text\n---` is a setext heading: a single newline silently renders the
        # comment's last line as an H2. No error, just wrong on every board.
        body = state.stamp("Claimed for the `claude` runner.", session="abc", runner="claude")
        self.assertIn("runner.\n\n---", body)
        self.assertNotIn("runner.\n---", body)

    def test_it_holds_for_the_runner_only_form(self):
        body = state.stamp("Claimed.", runner="codex")
        self.assertNotIn("Claimed.\n---", body)
