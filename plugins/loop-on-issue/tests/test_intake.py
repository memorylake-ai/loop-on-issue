import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import intake as intake_mod


class Ids(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def test_ids_carry_their_date_so_a_filename_never_repeats_it(self):
        self.assertTrue(self.store.new_id(day="20260824").startswith("R20260824-"))

    def test_ids_increment_within_a_day(self):
        first = self.store.save(intake_mod.Request(id=self.store.new_id(day="20260824"), text="a")).id
        second = self.store.new_id(day="20260824")
        self.assertEqual((first[-2:], second[-2:]), ("01", "02"))

    def test_a_new_day_restarts_the_counter(self):
        self.store.save(intake_mod.Request(id=self.store.new_id(day="20260824"), text="a"))
        self.assertTrue(self.store.new_id(day="20260825").endswith("-01"))


class Storing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def _req(self, **kwargs):
        kwargs.setdefault("id", self.store.new_id(day="20260824"))
        kwargs.setdefault("text", "把首页 CTA 改强一点")
        kwargs.setdefault("requester", "王五")
        return self.store.save(intake_mod.Request(**kwargs))

    def test_round_trip(self):
        req = self._req(repo="acme/widget")
        found = self.store.get(req.id)
        self.assertEqual((found.text, found.repo, found.requester),
                         (req.text, "acme/widget", "王五"))

    def test_a_new_request_is_pending(self):
        self.assertEqual(self._req().status, intake_mod.PENDING)

    def test_nothing_is_written_to_a_repository(self):
        # The whole point of the change: an unapproved requirement must not touch
        # the forge or anybody's version control.
        req = self._req()
        self.assertTrue(self.store.dir_for(req.id).startswith(self.dir))

    def test_each_request_gets_its_own_directory_for_logs_and_output(self):
        req = self._req()
        path = self.store.dir_for(req.id)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(os.path.isfile(os.path.join(path, "request.json")))

    def test_unknown_id_is_none(self):
        self.assertIsNone(self.store.get("R20260824-99"))

    def test_listing_by_status(self):
        a = self._req()
        b = self._req()
        b.status = intake_mod.APPROVED
        self.store.save(b)
        self.assertEqual([r.id for r in self.store.by_status(intake_mod.PENDING)], [a.id])

    def test_listing_is_oldest_first_so_a_queue_drains_fairly(self):
        first = self._req()
        second = self._req()
        self.assertEqual([r.id for r in self.store.all()], [first.id, second.id])

    def test_a_corrupt_record_does_not_break_a_listing(self):
        self._req()
        broken = os.path.join(self.dir, "R20260824-88")
        os.makedirs(broken)
        with open(os.path.join(broken, "request.json"), "w") as fh:
            fh.write("{oops")
        self.assertEqual(len(self.store.all()), 1)

    def test_records_are_owner_readable_only(self):
        req = self._req()
        path = os.path.join(self.store.dir_for(req.id), "request.json")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")

    def test_the_log_path_is_inside_the_request_directory(self):
        req = self._req()
        self.assertTrue(self.store.log_for(req.id).startswith(self.store.dir_for(req.id)))


class Approval(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)
        self.req = self.store.save(intake_mod.Request(
            id=self.store.new_id(day="20260824"), text="做个东西", repo="acme/widget"))

    def test_approving_records_who_and_when_and_the_note(self):
        # An approval note narrows scope as surely as the request did, so it is
        # kept beside the request rather than left in a chat log.
        self.req.approve(by="Julian", note="注意别动定价页", at="2026-08-24T10:00:00")
        self.store.save(self.req)
        found = self.store.get(self.req.id)
        self.assertEqual(found.status, intake_mod.APPROVED)
        self.assertEqual((found.approved_by, found.approval_note), ("Julian", "注意别动定价页"))

    def test_approving_may_redirect_the_repository(self):
        self.req.approve(by="Julian", repo="org/bloom")
        self.assertEqual(self.req.repo, "org/bloom")

    def test_approving_without_a_redirect_keeps_the_repository(self):
        self.req.approve(by="Julian")
        self.assertEqual(self.req.repo, "acme/widget")

    def test_self_raised_requests_are_marked_auto_approved(self):
        self.req.approve(by="Julian", auto=True)
        self.assertTrue(self.req.auto_approved)

    def test_rejecting_records_the_reason(self):
        self.req.reject(by="Julian", reason="这个已经做过了")
        self.assertEqual(self.req.status, intake_mod.REJECTED)
        self.assertIn("做过", self.req.rejected_reason)

    def test_a_finished_request_records_what_it_produced(self):
        self.req.finish(issues=["https://f/1", "https://f/2"])
        self.assertEqual(self.req.status, intake_mod.DONE)
        self.assertEqual(len(self.req.issues), 2)

    def test_a_failed_request_records_why(self):
        self.req.fail("claude exited 1")
        self.assertEqual(self.req.status, intake_mod.FAILED)
        self.assertIn("exited 1", self.req.error)

    def test_only_pending_requests_can_be_approved(self):
        self.req.approve(by="Julian")
        with self.assertRaises(intake_mod.NotPending):
            self.req.approve(by="Someone")

    def test_a_rejected_request_cannot_be_approved_later(self):
        self.req.reject(by="Julian", reason="不做")
        with self.assertRaises(intake_mod.NotPending):
            self.req.approve(by="Julian")


class Kinds(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def test_a_request_is_a_requirement_by_default(self):
        req = intake_mod.Request(id="R1", text="x")
        self.assertEqual(req.kind, intake_mod.REQUIREMENT)

    def test_a_develop_request_names_the_issue_it_is_for(self):
        # Same store, same log directory, same serial worker: both kinds are "run
        # one agent in a checkout and report what came of it".
        req = intake_mod.Request(id="R1", text="develop #612", kind=intake_mod.DEVELOP, issue=612)
        self.store.save(req)
        found = self.store.get("R1")
        self.assertEqual((found.kind, found.issue), (intake_mod.DEVELOP, 612))

    def test_both_kinds_share_the_queue(self):
        a = self.store.save(intake_mod.Request(id="R1", text="a"))
        b = intake_mod.Request(id="R2", text="b", kind=intake_mod.DEVELOP, issue=9)
        b.approve(by="Julian", auto=True)
        self.store.save(b)
        self.assertEqual([r.id for r in self.store.by_status(intake_mod.APPROVED)], ["R2"])
        self.assertEqual([r.id for r in self.store.by_status(intake_mod.PENDING)], [a.id])


class Expiry(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def test_a_request_nobody_ever_approved_expires(self):
        req = self.store.save(intake_mod.Request(
            id=self.store.new_id(day="20260824"), text="x", created_at=0.0))
        expired = self.store.expire_stale(ttl=3600, now=10_000)
        self.assertEqual([r.id for r in expired], [req.id])
        self.assertEqual(self.store.get(req.id).status, intake_mod.EXPIRED)

    def test_an_approved_request_is_never_expired_out_from_under_the_runner(self):
        req = intake_mod.Request(id=self.store.new_id(day="20260824"), text="x", created_at=0.0)
        req.approve(by="Julian")
        self.store.save(req)
        self.assertEqual(self.store.expire_stale(ttl=3600, now=10_000), [])
