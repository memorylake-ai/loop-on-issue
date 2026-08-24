import unittest

import _bootstrap  # noqa: F401

from loopkit import forge


class TitleClaimsIssue(unittest.TestCase):
    def test_house_convention(self):
        self.assertTrue(forge.title_claims_issue("to #612: fix drive URI", 612))

    def test_without_the_colon(self):
        self.assertTrue(forge.title_claims_issue("to #612 fix drive URI", 612))

    def test_draft_and_wip_prefixes_are_tolerated(self):
        self.assertTrue(forge.title_claims_issue("Draft: to #612: x", 612))
        self.assertTrue(forge.title_claims_issue("WIP: to #612: x", 612))
        self.assertTrue(forge.title_claims_issue("draft: to #612: x", 612))

    def test_a_different_issue_number_does_not_match(self):
        self.assertFalse(forge.title_claims_issue("to #61: x", 612))
        self.assertFalse(forge.title_claims_issue("to #6120: x", 612))

    def test_merely_mentioning_the_issue_is_not_a_claim(self):
        # This is the whole point: a change request whose description says
        # "stacked on #630" must not be read as #630's work. Attributing the
        # wrong one is silent and lasting; reporting none merely pauses the issue
        # in front of a human.
        self.assertFalse(forge.title_claims_issue("stacked on #612", 612))
        self.assertFalse(forge.title_claims_issue("refactor, see #612", 612))

    def test_case_insensitive(self):
        self.assertTrue(forge.title_claims_issue("TO #612: x", 612))

    def test_empty_title(self):
        self.assertFalse(forge.title_claims_issue("", 612))
        self.assertFalse(forge.title_claims_issue(None, 612))


class PickCR(unittest.TestCase):
    def _cr(self, number, state, created):
        from loopkit.models import ChangeRequest

        return ChangeRequest(number=number, title="t", state=state, url="u", created_at=created)

    def test_prefers_an_open_change_request(self):
        crs = [self._cr(1, "merged", "2026-01-01"), self._cr(2, "opened", "2025-01-01")]
        self.assertEqual(forge.pick_cr(crs).number, 2)

    def test_newest_among_open_ones(self):
        crs = [self._cr(1, "opened", "2026-01-01"), self._cr(2, "opened", "2026-02-01")]
        self.assertEqual(forge.pick_cr(crs).number, 2)

    def test_falls_back_to_the_newest_closed_one(self):
        # A merged change request still tells the caller the work landed.
        crs = [self._cr(1, "merged", "2026-01-01"), self._cr(2, "closed", "2026-02-01")]
        self.assertEqual(forge.pick_cr(crs).number, 2)

    def test_no_candidates(self):
        self.assertIsNone(forge.pick_cr([]))


class DidYouMean(unittest.TestCase):
    def test_names_close_matches(self):
        msg = forge.did_you_mean("web_admin", ["web-admin", "backend"])
        self.assertIn("web-admin", msg)

    def test_silent_when_nothing_is_close(self):
        self.assertEqual(forge.did_you_mean("zzz", ["web-admin"]), "")


if __name__ == "__main__":
    unittest.main()
