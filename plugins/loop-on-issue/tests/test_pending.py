import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import pending


class Index(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-pending-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.idx = pending.Index(self.dir)

    def test_slug_survives_a_key_with_url_unsafe_characters(self):
        # processQueryKey is base64-ish and contains / and =, which cannot be a
        # filename. Hashing is what makes it addressable on disk.
        key = "abc/def+ghi=="
        self.assertNotIn("/", pending.slug(key))
        self.assertEqual(pending.slug(key), pending.slug(key))

    def test_distinct_keys_do_not_collide(self):
        self.assertNotEqual(pending.slug("a"), pending.slug("b"))

    def test_round_trip(self):
        self.idx.record("pqk-1", {"repo": "acme/widget", "issue": 612, "options": ["A", "B"]})
        found = self.idx.lookup("pqk-1")
        self.assertEqual((found["repo"], found["issue"]), ("acme/widget", 612))

    def test_unknown_key_is_none(self):
        self.assertIsNone(self.idx.lookup("nope"))

    def test_records_carry_their_own_key_back(self):
        self.idx.record("pqk-1", {"issue": 1})
        self.assertEqual(self.idx.lookup("pqk-1")["pqk"], "pqk-1")

    def test_newest_is_what_a_bare_reply_answers(self):
        self.idx.record("old", {"issue": 1}, now=100.0)
        self.idx.record("new", {"issue": 2}, now=200.0)
        self.assertEqual(self.idx.newest()["issue"], 2)

    def test_newest_of_nothing(self):
        self.assertIsNone(self.idx.newest())

    def test_remove(self):
        self.idx.record("pqk-1", {"issue": 1})
        self.idx.remove("pqk-1")
        self.assertIsNone(self.idx.lookup("pqk-1"))

    def test_removing_something_absent_is_not_an_error(self):
        # ask cleans up in a finally block, which may run after a failure that
        # already removed the entry.
        self.idx.remove("never-existed")

    def test_sweep_collects_orphans_left_by_a_kill(self):
        self.idx.record("stale", {"issue": 1}, now=0.0)
        self.idx.record("fresh", {"issue": 2}, now=10_000.0)
        removed = self.idx.sweep(ttl=3600, now=10_000.0)
        self.assertEqual(removed, 1)
        self.assertIsNone(self.idx.lookup("stale"))
        self.assertIsNotNone(self.idx.lookup("fresh"))

    def test_a_corrupt_entry_does_not_break_a_listing(self):
        # A SIGKILL mid-write leaves a truncated file; one bad record must not
        # make every lookup fail.
        self.idx.record("good", {"issue": 1})
        with open(os.path.join(self.dir, "broken.json"), "w") as fh:
            fh.write("{oops")
        self.assertEqual(len(self.idx.all()), 1)

    def test_entries_are_written_readable_only_by_the_owner(self):
        self.idx.record("pqk-1", {"issue": 1})
        path = os.path.join(self.dir, pending.slug("pqk-1") + ".json")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")

    def test_all_is_sorted_newest_first(self):
        self.idx.record("a", {"issue": 1}, now=1.0)
        self.idx.record("b", {"issue": 2}, now=2.0)
        self.assertEqual([r["issue"] for r in self.idx.all()], [2, 1])


if __name__ == "__main__":
    unittest.main()
