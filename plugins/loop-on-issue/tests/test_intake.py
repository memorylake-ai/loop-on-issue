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


class Evidence(unittest.TestCase):
    """A clean exit is not evidence that anything was produced."""

    def test_a_decomposition_that_filed_no_issues_produced_nothing(self):
        # The failure this exists for: an agent blocked from running the CLI
        # exits 0, writes a thoughtful essay, and creates not one issue.
        self.assertTrue(intake_mod.produced_nothing(
            intake_mod.REQUIREMENT, issues=[], report="I would have filed three."))

    def test_a_decomposition_with_issues_did(self):
        self.assertFalse(intake_mod.produced_nothing(
            intake_mod.REQUIREMENT, issues=["https://f/1"], report=""))

    def test_a_development_job_is_evidenced_by_its_report(self):
        # It creates no issues by design; the change request is named in there.
        self.assertFalse(intake_mod.produced_nothing(
            intake_mod.DEVELOP, issues=[], report="Opened !903."))

    def test_a_development_job_with_no_report_produced_nothing(self):
        self.assertTrue(intake_mod.produced_nothing(intake_mod.DEVELOP, issues=[], report="   "))


class Sessions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def test_starting_records_the_session(self):
        req = intake_mod.Request(id="R1", text="x")
        req.approve(by="Julian", auto=True)
        req.start(session="derived-uuid")
        self.store.save(req)
        self.assertEqual(self.store.get("R1").session, "derived-uuid")

    def test_a_session_id_is_derived_from_the_request_id(self):
        from loopkit import runner

        import uuid as _uuid

        first = runner.intake_session_id("R20260824-01")
        _uuid.UUID(first)
        self.assertEqual(first, runner.intake_session_id("R20260824-01"))
        self.assertNotEqual(first, runner.intake_session_id("R20260824-02"))

    def test_a_generation_yields_a_fresh_session(self):
        from loopkit import runner

        self.assertNotEqual(runner.intake_session_id("R1"),
                            runner.intake_session_id("R1", generation=1))


class Evidence(unittest.TestCase):
    """A clean exit is not evidence that anything was produced."""

    def test_a_decomposition_that_filed_no_issues_produced_nothing(self):
        # The real failure: an agent denied permission to run the CLI exits 0,
        # writes a thoughtful essay, and creates not one issue.
        self.assertTrue(intake_mod.produced_nothing(
            intake_mod.REQUIREMENT, issues=[], report="I would have filed three."))

    def test_a_decomposition_with_issues_did(self):
        self.assertFalse(intake_mod.produced_nothing(
            intake_mod.REQUIREMENT, issues=["https://f/1"], report=""))

    def test_a_development_job_is_evidenced_by_its_report(self):
        # It creates no issues by design; the change request is named in there.
        self.assertFalse(intake_mod.produced_nothing(
            intake_mod.DEVELOP, issues=[], report="Opened !903."))

    def test_a_development_job_with_no_report_produced_nothing(self):
        self.assertTrue(intake_mod.produced_nothing(intake_mod.DEVELOP, issues=[], report="   "))


class Sessions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def test_starting_records_the_session(self):
        req = intake_mod.Request(id="R1", text="x")
        req.approve(by="Julian", auto=True)
        req.start(session="derived-uuid")
        self.store.save(req)
        self.assertEqual(self.store.get("R1").session, "derived-uuid")

    def test_the_session_is_derived_from_the_request_id(self):
        import uuid as _uuid

        from loopkit import runner

        first = runner.intake_session_id("R20260824-01")
        _uuid.UUID(first)
        self.assertEqual(first, runner.intake_session_id("R20260824-01"))
        self.assertNotEqual(first, runner.intake_session_id("R20260824-02"))

    def test_a_generation_yields_a_fresh_session(self):
        from loopkit import runner

        self.assertNotEqual(runner.intake_session_id("R1"),
                            runner.intake_session_id("R1", generation=1))


class Retrying(unittest.TestCase):
    """A derived session id can only be created once."""

    def test_a_first_attempt_is_not_a_resume(self):
        req = intake_mod.Request(id="R1", text="x")
        req.approve(by="J", auto=True)
        self.assertFalse(req.resuming)

    def test_a_second_attempt_is(self):
        # `claude -p --session-id X` refuses when X already exists, so a retry
        # that starts afresh dies before doing anything.
        req = intake_mod.Request(id="R1", text="x")
        req.approve(by="J", auto=True)
        req.start(session="derived")
        req.fail("blocked")
        self.assertTrue(req.resuming)

    def test_attempts_are_counted(self):
        req = intake_mod.Request(id="R1", text="x")
        req.approve(by="J", auto=True)
        req.start(session="s")
        req.fail("x")
        req.status = intake_mod.APPROVED
        req.start(session="s")
        self.assertEqual(req.attempts, 2)

    def test_an_attempt_without_a_session_cannot_resume(self):
        req = intake_mod.Request(id="R1", text="x")
        req.approve(by="J", auto=True)
        req.start()
        self.assertFalse(req.resuming)


class Questions(unittest.TestCase):
    """A decomposition job needs to be able to ask, too.

    The hook covered "developing an issue" and missed "decomposing a
    requirement" — the one with more ambiguity and less to check it against.
    The first real run said so itself: "this is exactly what I would have put to
    loop ask", about the ambiguity that decided the whole shape of the work.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)
        self.req = self.store.save(intake_mod.Request(id="R1", text="做个东西"))

    def test_a_request_starts_with_no_questions(self):
        self.assertEqual(self.req.questions, [])

    def test_asking_records_the_question_and_returns_its_id(self):
        qid = self.req.ask("哪种 harness？", ["评测", "agent loop"])
        self.store.save(self.req)
        found = self.store.get("R1").questions[0]
        self.assertEqual((found["id"], found["text"]), (qid, "哪种 harness？"))
        self.assertEqual(found["options"], ["评测", "agent loop"])

    def test_a_fresh_question_is_unanswered(self):
        self.req.ask("哪种？", [])
        self.assertIsNone(self.req.pending_question()["answer"])

    def test_answering_the_open_question(self):
        self.req.ask("哪种？", ["a", "b"])
        self.req.answer("2", by="穆轩")
        answered = self.store.save(self.req) and self.store.get("R1").questions[0]
        self.assertEqual(answered["answer"], "2")
        self.assertEqual(answered["by"], "穆轩")

    def test_pending_question_is_the_newest_unanswered_one(self):
        self.req.ask("第一个", [])
        self.req.answer("答了", by="x")
        second = self.req.ask("第二个", [])
        self.assertEqual(self.req.pending_question()["id"], second)

    def test_nothing_pending_once_everything_is_answered(self):
        self.req.ask("唯一一个", [])
        self.req.answer("好", by="x")
        self.assertIsNone(self.req.pending_question())

    def test_answering_with_nothing_pending_is_refused(self):
        # Otherwise a stray reply would attach itself to a question that was
        # already settled, and the record would show two answers to one question.
        self.assertFalse(self.req.answer("späte Antwort", by="x"))

    def test_questions_survive_a_round_trip(self):
        self.req.ask("哪种？", ["a"])
        self.store.save(self.req)
        self.assertEqual(len(self.store.get("R1").questions), 1)


class Cancelling(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def _running(self):
        req = intake_mod.Request(id="R1", text="x")
        req.approve(by="J", auto=True)
        req.start(session="s", pid=4242)
        return self.store.save(req)

    def test_the_process_is_recorded_so_it_can_be_stopped(self):
        # A status field alone relabels a stuck job without freeing the worker it
        # is holding.
        self._running()
        self.assertEqual(self.store.get("R1").pid, 4242)

    def test_cancelling_records_who(self):
        req = self._running()
        req.cancel(by="穆轩")
        self.store.save(req)
        found = self.store.get("R1")
        self.assertEqual(found.status, intake_mod.CANCELLED)
        self.assertIn("穆轩", found.error)

    def test_cancelling_clears_the_process(self):
        req = self._running()
        req.cancel()
        self.assertEqual(req.pid, 0)

    def test_a_cancelled_job_is_not_open(self):
        req = self._running()
        req.cancel()
        self.store.save(req)
        self.assertEqual(self.store.by_status(*intake_mod.OPEN), [])

    def test_running_for_measures_only_a_running_job(self):
        req = self._running()
        self.assertGreaterEqual(req.running_for, 0.0)
        req.cancel()
        self.assertEqual(req.running_for, 0.0)


class Deferring(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def _deferred(self, seconds=60):
        req = intake_mod.Request(id="R1", text="x")
        req.approve(by="J", auto=True)
        req.start(session="s", pid=1)
        req.defer("API Error: 529 Overloaded", seconds)
        return self.store.save(req)

    def test_a_deferred_job_is_waiting_not_failed(self):
        # An outage is not the same news as a broken requirement, and the board
        # should not report it as one.
        self.assertEqual(self._deferred().status, intake_mod.WAITING)

    def test_it_is_still_open_work(self):
        self._deferred()
        self.assertEqual([r.id for r in self.store.by_status(*intake_mod.OPEN)], ["R1"])

    def test_it_counts_transport_faults_separately_from_attempts(self):
        # A retry after an outage is not the same event as a retry after a real
        # failure, and must not spend the same budget.
        req = self._deferred()
        self.assertEqual((req.attempts, req.transient_failures), (1, 1))

    def test_it_is_not_due_before_its_time(self):
        req = self._deferred(seconds=3600)
        self.assertFalse(req.due())
        self.assertEqual(self.store.due(), [])

    def test_it_becomes_due(self):
        self._deferred(seconds=0)
        self.assertEqual([r.id for r in self.store.due()], ["R1"])

    def test_the_process_is_forgotten(self):
        self.assertEqual(self._deferred().pid, 0)

    def test_the_reason_is_kept_so_a_human_can_see_what_happened(self):
        self.assertIn("529", self._deferred().error)

    def test_something_running_is_never_due(self):
        req = intake_mod.Request(id="R2", text="x")
        req.approve(by="J", auto=True)
        req.start(session="s")
        self.store.save(req)
        self.assertEqual(self.store.due(), [])


class FollowUp(unittest.TestCase):
    """A finished job still has its session; the conversation need not end.

    The agent that decomposed a requirement holds the recon, the drafts and why it
    cut the slices where it did. Asking it to split the second one further should
    resume that, not start something with none of it.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-intake-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir)

    def _done(self):
        req = intake_mod.Request(id="R1", text="做个东西", repo="acme/widget")
        req.approve(by="J", auto=True)
        req.start(session="derived-uuid")
        req.finish(issues=["https://f/1", "https://f/2"])
        return self.store.save(req)

    def test_a_finished_job_can_be_continued(self):
        req = self._done()
        self.assertTrue(req.can_continue)

    def test_continuing_queues_it_again_with_the_message(self):
        req = self._done()
        req.follow_up("把第二条再拆细一点")
        self.store.save(req)
        found = self.store.get("R1")
        self.assertEqual(found.status, intake_mod.APPROVED)
        self.assertEqual(found.followup, "把第二条再拆细一点")

    def test_it_keeps_the_session_so_the_context_survives(self):
        req = self._done()
        req.follow_up("再拆细")
        self.assertEqual(req.session, "derived-uuid")

    def test_a_failed_job_can_be_continued_too(self):
        # Often the most useful case: it explains what blocked it, you answer.
        req = intake_mod.Request(id="R2", text="x")
        req.approve(by="J", auto=True)
        req.start(session="s")
        req.fail("blocked on something")
        self.assertTrue(req.can_continue)

    def test_a_job_that_never_ran_cannot_be(self):
        # There is no session to resume into.
        req = intake_mod.Request(id="R3", text="x")
        self.assertFalse(req.can_continue)

    def test_a_running_job_cannot_be(self):
        # It is mid-thought; a second prompt into the same session would race it.
        req = intake_mod.Request(id="R4", text="x")
        req.approve(by="J", auto=True)
        req.start(session="s")
        self.assertFalse(req.can_continue)

    def test_the_followup_is_cleared_once_taken(self):
        req = self._done()
        req.follow_up("再拆细")
        taken = req.take_followup()
        self.assertEqual(taken, "再拆细")
        self.assertEqual(req.followup, "")

    def test_taking_nothing_gives_nothing(self):
        self.assertEqual(self._done().take_followup(), "")

    def test_the_most_recently_finished_is_findable(self):
        first = self._done()
        second = intake_mod.Request(id="R9", text="later", repo="acme/widget")
        second.approve(by="J", auto=True)
        second.start(session="s2")
        second.finish(issues=["https://f/9"])
        second.finished_at = first.finished_at + 100
        self.store.save(second)
        self.assertEqual(self.store.latest_continuable().id, "R9")

    def test_nothing_continuable_is_none(self):
        self.assertIsNone(self.store.latest_continuable())
