"""Asking a human from a job that has no issue to hold the question."""

import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import ask as ask_mod
from loopkit import intake as intake_mod
from loopkit import pending


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class AskIntake(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-ask-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir + "/intake")
        self.index = pending.Index(self.dir + "/pending")
        self.request = self.store.save(intake_mod.Request(
            id="R20260824-01", text="初始化一个 harness 模版", repo="acme/widget"))
        self.clock = Clock()

    def _ask(self, **kwargs):
        kwargs.setdefault("question", "哪一种 harness？")
        kwargs.setdefault("options", ["评测用", "agent loop"])
        kwargs.setdefault("wait", 0)
        kwargs.setdefault("index", self.index)
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("sleep", self.clock.sleep)
        return ask_mod.ask_intake(self.store, "R20260824-01", **kwargs)

    def test_the_question_is_recorded_on_the_request(self):
        # There is no issue yet, so the request is the durable home — the same
        # reason the request itself is held locally until approved.
        self._ask()
        self.assertEqual(self.store.get("R20260824-01").questions[0]["text"], "哪一种 harness？")

    def test_unanswered_exits_without_waiting(self):
        result = self._ask(wait=0)
        self.assertFalse(result.answered)
        self.assertEqual(self.clock.slept, [])

    def test_an_answer_arriving_during_the_wait_is_returned(self):
        calls = {"n": 0}
        original = self.store.get

        def get(rid):
            calls["n"] += 1
            request = original(rid)
            if calls["n"] >= 3 and request.pending_question():
                request.answer("2", by="穆轩")
                self.store.save(request)
            return request

        self.store.get = get
        result = self._ask(wait=60)
        self.assertTrue(result.answered)
        self.assertEqual(result.answer.choices, ["agent loop"])
        self.assertEqual(result.answer.by, "穆轩")

    def test_a_notifier_is_told_and_the_routing_key_points_at_the_request(self):
        recorded = []

        class SpyIndex(pending.Index):
            def record(self, pqk, data, now=None):
                recorded.append(dict(data))
                return pending.Index.record(self, pqk, data, now)

        self._ask(notify=lambda title, text: "PQK-1", index=SpyIndex(self.dir + "/pending"))
        self.assertEqual(recorded[0]["intake"], "R20260824-01")
        self.assertIsNone(recorded[0].get("issue"))

    def test_the_card_names_the_requirement_not_an_issue_number(self):
        sent = {}
        self._ask(notify=lambda title, text: sent.update(title=title, text=text))
        self.assertIn("R20260824-01", sent["text"])
        self.assertIn("哪一种 harness？", sent["text"])

    def test_an_unknown_request_is_refused(self):
        with self.assertRaises(Exception):
            ask_mod.ask_intake(self.store, "R20260101-99", question="x", index=self.index)

    def test_a_broken_notifier_does_not_lose_the_question(self):
        def boom(title, text):
            raise RuntimeError("dingtalk is down")

        result = self._ask(notify=boom)
        self.assertEqual(len(self.store.get("R20260824-01").questions), 1)
        self.assertIn("dingtalk is down", result.notify_error)


if __name__ == "__main__":
    unittest.main()
