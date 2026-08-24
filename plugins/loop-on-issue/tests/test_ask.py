import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import ask as ask_mod
from loopkit import pending, state
from loopkit.models import Comment, Issue


class ParseAnswer(unittest.TestCase):
    OPTS = ["rebuild the index", "skip the migration"]

    def test_a_bare_number_selects_an_option(self):
        a = ask_mod.parse_answer("1", self.OPTS)
        self.assertEqual((a.kind, a.indexes, a.choices), ("option", [1], ["rebuild the index"]))

    def test_options_are_one_based(self):
        self.assertEqual(ask_mod.parse_answer("2", self.OPTS).choices, ["skip the migration"])

    def test_several_numbers_keep_their_order(self):
        # loopcue's rule: answering "2、1" means do both, in that order.
        a = ask_mod.parse_answer("2、1", self.OPTS)
        self.assertEqual(a.indexes, [2, 1])

    def test_separators_may_be_commas_spaces_or_chinese_punctuation(self):
        for text in ("1,2", "1 2", "1、2", "1，2", "1/2"):
            self.assertEqual(ask_mod.parse_answer(text, self.OPTS).indexes, [1, 2], text)

    def test_an_out_of_range_number_is_not_silently_mapped(self):
        a = ask_mod.parse_answer("7", self.OPTS)
        self.assertEqual(a.kind, "text")

    def test_zero_is_not_an_option(self):
        self.assertEqual(ask_mod.parse_answer("0", self.OPTS).kind, "text")

    def test_a_number_inside_a_sentence_is_free_text(self):
        # "2 because it's faster" is an explanation, not a selection; treating it
        # as one would drop everything the human actually said.
        a = ask_mod.parse_answer("2 because it is faster", self.OPTS)
        self.assertEqual(a.kind, "text")
        self.assertEqual(a.raw, "2 because it is faster")

    def test_free_text_when_there_are_no_options(self):
        self.assertEqual(ask_mod.parse_answer("1", []).kind, "text")

    def test_whitespace_is_trimmed(self):
        self.assertEqual(ask_mod.parse_answer("  1  ", self.OPTS).indexes, [1])

    def test_a_relay_header_is_stripped_before_parsing(self):
        body = state.relay("1", by="张三", via="dingtalk")
        self.assertEqual(ask_mod.parse_answer(body, self.OPTS).indexes, [1])

    def test_the_relayed_answerer_is_recoverable(self):
        body = state.relay("go with the second one", by="张三", via="dingtalk")
        a = ask_mod.parse_answer(body, self.OPTS)
        self.assertEqual((a.by, a.via), ("张三", "dingtalk"))
        self.assertEqual(a.raw, "go with the second one")

    def test_an_answer_typed_straight_onto_the_issue_has_no_relay_fields(self):
        a = ask_mod.parse_answer("just do it", self.OPTS)
        self.assertIsNone(a.by)


class Clock:
    """A clock that only moves when the code under test sleeps.

    Without this the polling tests busy-wait through real wall-clock seconds —
    which cost the suite two minutes before anyone noticed.
    """

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakeForge:
    cr_word = "pull request"

    def __init__(self, issue_url="https://f/612", comments=None):
        self.comments = list(comments or [])
        self.posted = []
        self._url = issue_url
        self._n = 0
        self._scheduled = []
        self._polls = 0

    def get_issue(self, number):
        return Issue(number=number, title="t", state="opened", url=self._url)

    def add_issue_comment(self, number, body):
        self._n += 1
        c = Comment(id=self._n, author="bot", created_at="t{:03d}".format(self._n), body=body)
        self.comments.append(c)
        self.posted.append(body)
        return c

    def list_issue_comments(self, number):
        self._polls += 1
        for entry in list(self._scheduled):
            if entry["after"] <= self._polls:
                self._scheduled.remove(entry)
                self.arrive(entry["body"])
        return list(self.comments)

    def arrive(self, body, at=None):
        """A comment already on the thread, from before anything was asked."""
        self._n += 1
        self.comments.append(
            Comment(id=self._n, author="dev", created_at=at or "z{:03d}".format(self._n), body=body)
        )

    def deliver(self, body, after=2):
        """A human answers partway through the wait."""
        self._scheduled.append({"body": body, "after": after})


class Asking(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-ask-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.index = pending.Index(self.dir)
        self.forge = FakeForge()
        self.clock = Clock()
        self.slept = self.clock.slept

    def _ask(self, **kwargs):
        kwargs.setdefault("question", "Which way?")
        kwargs.setdefault("options", ["left", "right"])
        kwargs.setdefault("wait", 0)
        kwargs.setdefault("index", self.index)
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("sleep", self.clock.sleep)
        return ask_mod.ask(self.forge, "acme/widget", 612, **kwargs)

    def test_the_question_is_always_written_to_the_issue(self):
        # The durable channel works with no DingTalk configured at all.
        self._ask()
        self.assertEqual(len(self.forge.posted), 1)
        self.assertIn("Which way?", self.forge.posted[0])

    def test_the_question_comment_is_marked_as_ours(self):
        # It has to be, or it becomes the "human reply" that answers itself.
        self._ask()
        self.assertTrue(state.is_agent_note(self.forge.posted[0]))

    def test_options_are_listed_on_the_issue_too(self):
        self._ask()
        self.assertIn("1. left", self.forge.posted[0])

    def test_not_waiting_returns_unanswered_immediately(self):
        result = self._ask(wait=0)
        self.assertFalse(result.answered)
        self.assertEqual(self.slept, [])

    def test_an_answer_arriving_during_the_wait_is_picked_up(self):
        self.forge.deliver("2", after=2)
        result = self._ask(wait=30)
        self.assertTrue(result.answered)
        self.assertEqual(result.answer.choices, ["right"])

    def test_polling_stops_at_the_deadline(self):
        result = self._ask(wait=20)
        self.assertFalse(result.answered)
        self.assertTrue(self.slept)
        self.assertLessEqual(sum(self.slept), 20)  # never overshoots the bound

    def test_a_notifier_is_told_and_its_routing_key_recorded(self):
        sent = {}
        recorded = []

        def notify(title, text):
            sent["title"], sent["text"] = title, text
            return "PQK-9"

        class SpyIndex(pending.Index):
            def record(self, pqk, data, now=None):
                recorded.append((pqk, dict(data)))
                return pending.Index.record(self, pqk, data, now)

        # Asserted at write time, not after: ask removes its own entry on the way
        # out, which a separate test covers.
        self._ask(notify=notify, index=SpyIndex(self.dir))
        self.assertIn("Which way?", sent["text"])
        self.assertEqual(recorded[0][0], "PQK-9")
        self.assertEqual(recorded[0][1]["issue"], 612)
        self.assertEqual(recorded[0][1]["options"], ["left", "right"])
        self.assertEqual(recorded[0][1]["repo"], "acme/widget")

    def test_a_send_only_channel_records_nothing(self):
        # A webhook returns no processQueryKey; there is nothing to route back, so
        # there is nothing worth indexing.
        self._ask(notify=lambda title, text: None)
        self.assertEqual(self.index.all(), [])

    def test_the_index_entry_is_removed_once_answered(self):
        self.forge.deliver("1", after=2)
        self._ask(wait=30, notify=lambda t, x: "PQK-9")
        self.assertIsNone(self.index.lookup("PQK-9"))

    def test_an_unanswered_question_keeps_its_routing_entry(self):
        # The normal case is wait=0: ask, return, and let a human answer later.
        # Dropping the entry on the way out would leave their quote-reply with
        # nothing to route to — which is exactly what happened the first time this
        # was pointed at a real DingTalk account.
        self._ask(wait=0, notify=lambda t, x: "PQK-9")
        self.assertEqual(self.index.lookup("PQK-9")["issue"], 612)

    def test_an_unanswered_entry_is_eventually_swept(self):
        self._ask(wait=0, notify=lambda t, x: "PQK-9")
        self.assertEqual(self.index.sweep(ttl=0, now=1e12), 1)

    def test_a_broken_notifier_does_not_lose_the_question(self):
        # The issue comment is the durable channel; a DingTalk outage must not
        # turn a blocker into a silently dropped question.
        def boom(title, text):
            raise RuntimeError("dingtalk is down")

        result = self._ask(notify=boom)
        self.assertEqual(len(self.forge.posted), 1)
        self.assertFalse(result.answered)
        self.assertIn("dingtalk is down", result.notify_error)

    def test_only_replies_newer_than_our_question_count(self):
        # A comment thread full of old human chatter must not read as an answer
        # to a question just asked.
        self.forge.arrive("some old remark", at="t000")
        result = self._ask(wait=10)
        self.assertFalse(result.answered)

    def test_the_answer_carries_who_gave_it(self):
        self.forge.deliver(state.relay("1", by="张三", via="dingtalk"), after=2)
        result = self._ask(wait=30)
        self.assertEqual(result.answer.by, "张三")


class Relay(unittest.TestCase):
    def test_a_relayed_answer_still_reads_as_a_human_reply(self):
        # This is the whole trick: the relay marker is deliberately *not* the
        # agent marker, so `unanswered` sees it and the issue wakes up.
        body = state.relay("go left", by="张三", via="dingtalk")
        self.assertFalse(state.is_agent_note(body))

    def test_relay_round_trip(self):
        body = state.relay("go left", by="张三", via="dingtalk")
        fields, text = state.parse_relay(body)
        self.assertEqual(fields, {"by": "张三", "via": "dingtalk"})
        self.assertEqual(text, "go left")

    def test_a_plain_body_has_no_relay_fields(self):
        self.assertEqual(state.parse_relay("just text"), (None, "just text"))

    def test_the_relayed_text_is_visible_to_a_human_reading_the_issue(self):
        body = state.relay("go left", by="张三", via="dingtalk")
        self.assertIn("张三", body)
        self.assertIn("go left", body)


if __name__ == "__main__":
    unittest.main()
