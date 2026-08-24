import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import intake as intake_mod
from loopkit import listener, pending, repos as repos_mod, state
from loopkit.models import Comment, Issue


def msg(text="hi", msg_id="m1", sender="u1", nick="张三", conversation="cid-1", pqk=None):
    return listener.Inbound(msg_id=msg_id, text=text, sender_id=sender, sender_nick=nick,
                            conversation_id=conversation, pqk=pqk)


class FakeForge:
    def __init__(self):
        self.comments = {}
        self.issues = {}
        self.titles = {}

    def add(self, number, title="a thing", labels=("loop",)):
        self.issues[number] = Issue(number=number, title=title, state="opened",
                                    url="https://f/{}".format(number), labels=list(labels))
        self.comments.setdefault(number, [])

    def get_issue(self, number):
        if number not in self.issues:
            raise KeyError(number)
        return self.issues[number]

    def list_issues(self, label=None, assignee=None, state="opened"):
        return list(self.issues.values())

    def list_issue_comments(self, number):
        return list(self.comments.get(number, []))

    def add_issue_comment(self, number, body):
        bucket = self.comments.setdefault(number, [])
        c = Comment(id=len(bucket) + 1, author="bot", created_at="t{}".format(len(bucket)), body=body)
        bucket.append(c)
        return c

    def set_issue_title(self, number, title):
        self.titles[number] = title


class Base(unittest.TestCase):
    APPROVER = "staff-approver"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-brain-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = intake_mod.Store(self.dir + "/intake")
        self.index = pending.Index(self.dir + "/pending")
        self.forge = FakeForge()
        self.registry = repos_mod.Registry()
        self.registry.add("widget", "acme/widget", self.dir + "/widget")
        self.registry.add("bloom", "org/bloom", self.dir + "/bloom")
        self.registry.set_default("widget")
        self.enqueued = []
        self.brain = listener.Brain(
            forge_for=lambda repo: self.forge,
            registry=self.registry,
            index=self.index,
            store=self.store,
            conversations=["cid-1"],
            approver=self.APPROVER,
            approver_nick="Julian",
            enqueue=self.enqueued.append,
        )


class Intake(Base):
    def test_a_bare_message_is_always_a_requirement(self):
        # No heuristic: only a quote-reply answers a question, so a plain sentence
        # can never be swallowed as an answer to something else.
        self.index.record("k", {"repo": "acme/widget", "issue": 1})
        self.brain.handle(msg(text="把首页 CTA 改强一点"))
        self.assertEqual(len(self.store.by_status(intake_mod.PENDING)), 1)

    def test_nothing_reaches_the_forge_before_approval(self):
        # The whole reason this moved off the board: anyone who can message the
        # bot could otherwise write to the repository.
        self.brain.handle(msg(text="做个东西"))
        self.assertEqual(self.forge.comments, {})
        self.assertEqual(self.forge.titles, {})

    def test_the_request_is_stored_verbatim_with_its_provenance(self):
        self.brain.handle(msg(text="把首页 CTA 改强一点", nick="王五", sender="staff-9"))
        req = self.store.by_status(intake_mod.PENDING)[0]
        self.assertEqual(req.text, "把首页 CTA 改强一点")
        self.assertEqual((req.requester, req.requester_id), ("王五", "staff-9"))
        self.assertEqual(req.conversation, "cid-1")

    def test_it_lands_in_the_default_repository(self):
        self.brain.handle(msg(text="做个东西"))
        self.assertEqual(self.store.all()[0].repo, "acme/widget")

    def test_the_reply_names_the_id_the_repo_and_who_must_approve(self):
        reply = self.brain.handle(msg(text="做个东西"))
        req = self.store.all()[0]
        self.assertIn(req.id, reply)
        self.assertIn("widget", reply)
        self.assertIn("Julian", reply)

    def test_the_approver_is_asked_to_confirm_rather_than_queued(self):
        # Queueing their own requirement instantly read as convenient and was
        # not: that is the moment the repository gets chosen, and it went by
        # without anyone being asked.
        reply = self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        req = self.store.all()[0]
        self.assertEqual(req.status, intake_mod.PENDING)
        self.assertEqual(self.enqueued, [])
        self.assertIn("确认", reply)
        self.assertIn(req.id, reply)

    def test_the_approver_sees_where_it_would_land_and_the_alternatives(self):
        reply = self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        self.assertIn("acme/widget", reply)
        self.assertIn("org/bloom", reply)

    def test_confirming_queues_it(self):
        self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        req = self.store.all()[0]
        self.brain.handle(msg(text="同意 " + req.id, msg_id="m2", sender=self.APPROVER))
        self.assertEqual(self.store.get(req.id).status, intake_mod.APPROVED)
        self.assertEqual([r.id for r in self.enqueued], [req.id])

    def test_confirming_with_a_repository_redirects_it(self):
        self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        req = self.store.all()[0]
        self.brain.handle(msg(text="同意 {} bloom".format(req.id), msg_id="m2",
                              sender=self.APPROVER))
        self.assertEqual(self.store.get(req.id).repo, "org/bloom")

    def test_an_ordinary_requester_queues_nothing_yet(self):
        self.brain.handle(msg(text="做个东西"))
        self.assertEqual(self.enqueued, [])

    def test_new_files_a_requirement_explicitly(self):
        self.brain.handle(msg(text="/new 做个东西"))
        self.assertEqual(len(self.store.all()), 1)

    def test_a_requirement_with_no_default_repository_is_refused(self):
        # Picking one arbitrarily files work in a stranger's tracker, silently.
        registry = repos_mod.Registry()
        registry.add("a", "org/a", "/c/a")
        registry.add("b", "org/b", "/c/b")
        self.brain.registry = registry
        reply = self.brain.handle(msg(text="做个东西"))
        self.assertIn("哪个", reply)
        self.assertEqual(self.store.all(), [])


class Approving(Base):
    def _pending(self):
        self.brain.handle(msg(text="做个东西"))
        return self.store.all()[0]

    def test_only_the_approver_may_approve(self):
        req = self._pending()
        reply = self.brain.handle(msg(text="同意 " + req.id, msg_id="m2", sender="someone", nick="路人"))
        self.assertIn("只有", reply)
        self.assertEqual(self.store.get(req.id).status, intake_mod.PENDING)

    def test_approval_queues_the_work(self):
        req = self._pending()
        self.brain.handle(msg(text="同意 " + req.id, msg_id="m2", sender=self.APPROVER))
        self.assertEqual(self.store.get(req.id).status, intake_mod.APPROVED)
        self.assertEqual([r.id for r in self.enqueued], [req.id])

    def test_an_approval_note_is_kept_with_the_request(self):
        req = self._pending()
        self.brain.handle(msg(text="同意 {} 注意别动定价页".format(req.id),
                              msg_id="m2", sender=self.APPROVER))
        self.assertIn("定价页", self.store.get(req.id).approval_note)

    def test_approval_may_redirect_the_repository(self):
        req = self._pending()
        self.brain.handle(msg(text="同意 {} bloom".format(req.id), msg_id="m2", sender=self.APPROVER))
        self.assertEqual(self.store.get(req.id).repo, "org/bloom")

    def test_a_redirect_does_not_eat_the_note(self):
        req = self._pending()
        self.brain.handle(msg(text="同意 {} bloom 注意别动定价页".format(req.id),
                              msg_id="m2", sender=self.APPROVER))
        found = self.store.get(req.id)
        self.assertEqual(found.repo, "org/bloom")
        self.assertIn("定价页", found.approval_note)

    def test_a_word_that_is_not_a_repository_stays_in_the_note(self):
        req = self._pending()
        self.brain.handle(msg(text="同意 {} 尽快".format(req.id), msg_id="m2", sender=self.APPROVER))
        found = self.store.get(req.id)
        self.assertEqual(found.repo, "acme/widget")
        self.assertIn("尽快", found.approval_note)

    def test_rejection_needs_a_reason(self):
        req = self._pending()
        reply = self.brain.handle(msg(text="拒绝 " + req.id, msg_id="m2", sender=self.APPROVER))
        self.assertIn("理由", reply)
        self.assertEqual(self.store.get(req.id).status, intake_mod.PENDING)

    def test_rejection_records_it(self):
        req = self._pending()
        self.brain.handle(msg(text="拒绝 {} 这个已经做过了".format(req.id),
                              msg_id="m2", sender=self.APPROVER))
        self.assertEqual(self.store.get(req.id).status, intake_mod.REJECTED)

    def test_approving_twice_says_so_instead_of_queueing_twice(self):
        req = self._pending()
        self.brain.handle(msg(text="同意 " + req.id, msg_id="m2", sender=self.APPROVER))
        reply = self.brain.handle(msg(text="同意 " + req.id, msg_id="m3", sender=self.APPROVER))
        self.assertIn("已经", reply)
        self.assertEqual(len(self.enqueued), 1)

    def test_approving_something_unknown_says_so(self):
        reply = self.brain.handle(msg(text="同意 R20260101-99", sender=self.APPROVER))
        self.assertIn("R20260101-99", reply)


class Developing(Base):
    def test_the_owner_can_start_development_on_an_issue(self):
        self.forge.add(612)
        reply = self.brain.handle(msg(text="/dev 612", sender=self.APPROVER))
        job = self.enqueued[0]
        self.assertEqual((job.kind, job.issue), (intake_mod.DEVELOP, 612))
        self.assertEqual(job.status, intake_mod.APPROVED)
        self.assertIn("612", reply)

    def test_nobody_else_can(self):
        # Approving a requirement and starting a session are the same class of
        # act: both put an unattended agent to work.
        self.forge.add(612)
        reply = self.brain.handle(msg(text="/dev 612", sender="someone"))
        self.assertIn("只有", reply)
        self.assertEqual(self.enqueued, [])

    def test_it_may_name_a_repository(self):
        self.forge.add(612)
        self.brain.handle(msg(text="/dev 612 bloom", sender=self.APPROVER))
        self.assertEqual(self.enqueued[0].repo, "org/bloom")

    def test_it_needs_an_issue_number(self):
        reply = self.brain.handle(msg(text="/dev", sender=self.APPROVER))
        self.assertIn("用法", reply)


class Listing(Base):
    def test_p_lists_what_is_waiting_for_approval(self):
        self.brain.handle(msg(text="做个东西"))
        reply = self.brain.handle(msg(text="/p", msg_id="m2"))
        self.assertIn("做个东西", reply)
        self.assertIn(self.store.all()[0].id, reply)

    def test_p_says_so_when_nothing_waits(self):
        self.assertIn("没有", self.brain.handle(msg(text="/p")))

    def test_r_shows_one_request_with_its_status(self):
        self.brain.handle(msg(text="做个东西"))
        req = self.store.all()[0]
        reply = self.brain.handle(msg(text="/r " + req.id, msg_id="m2"))
        self.assertIn(intake_mod.PENDING, reply)
        self.assertIn("做个东西", reply)

    def test_repos_lists_the_registry_and_marks_the_default(self):
        reply = self.brain.handle(msg(text="/repos"))
        self.assertIn("acme/widget", reply)
        self.assertIn("org/bloom", reply)
        self.assertIn("默认", reply)


class Help(Base):
    def test_help_names_every_command_the_brain_implements(self):
        reply = self.brain.handle(msg(text="/h"))
        implemented = {name[len("_cmd_"):] for name in dir(self.brain) if name.startswith("_cmd_")}
        missing = [c for c in implemented if "/{}".format(c) not in reply and c not in reply]
        self.assertEqual(missing, [], "not documented in /h: {}".format(missing))

    def test_help_says_that_a_plain_message_files_a_requirement(self):
        self.assertIn("需求", self.brain.handle(msg(text="/h")))

    def test_help_marks_what_only_the_approver_may_do(self):
        reply = self.brain.handle(msg(text="/h"))
        self.assertIn("审批人", reply)


if __name__ == "__main__":
    unittest.main()


class OpenQueueVisibility(Base):
    """The queue an acknowledgement names must be the queue a command shows.

    "排入队列" was answered by /ls ("empty") and /p ("none pending"), both
    truthfully, because the request was in a third queue nothing could see.
    """

    def _queue_one(self):
        self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        request = self.store.all()[0]
        self.brain.handle(msg(text="同意 " + request.id, msg_id="ok", sender=self.APPROVER))
        return self.store.get(request.id)

    def test_p_shows_a_request_that_is_queued_for_execution(self):
        self._queue_one()
        reply = self.brain.handle(msg(text="/p", msg_id="m2"))
        self.assertIn(self.store.all()[0].id, reply)
        self.assertIn("排队等执行", reply)

    def test_p_shows_a_running_request(self):
        request = self._queue_one()
        request.start(session="s")
        self.store.save(request)
        self.assertIn("正在执行", self.brain.handle(msg(text="/p", msg_id="m2")))

    def test_p_separates_waiting_for_approval_from_waiting_to_run(self):
        self.brain.handle(msg(text="别人提的", sender="somebody"))
        self._queue_one()
        reply = self.brain.handle(msg(text="/p", msg_id="m3"))
        self.assertIn("待审批", reply)
        self.assertIn("排队等执行", reply)

    def test_a_finished_request_is_no_longer_open(self):
        request = self._queue_one()
        request.start()
        request.finish(issues=["https://f/1"])
        self.store.save(request)
        self.assertIn("没有在办", self.brain.handle(msg(text="/p", msg_id="m2")))

    def test_an_empty_p_points_at_the_other_queue(self):
        # Being told "empty" without being told where else to look is what made
        # the original confusing.
        reply = self.brain.handle(msg(text="/p"))
        self.assertIn("/ls", reply)

    def test_the_acknowledgement_names_the_command_that_can_see_it(self):
        self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        request = self.store.all()[0]
        reply = self.brain.handle(msg(text="同意 " + request.id, msg_id="ok",
                                      sender=self.APPROVER))
        self.assertIn("/p", reply)

    def test_r_shows_the_resumable_session(self):
        request = self._queue_one()
        request.start(session="abc-123")
        self.store.save(request)
        self.assertIn("abc-123", self.brain.handle(msg(text="/r " + request.id, msg_id="m2")))


class ChoosingTheRepo(Base):
    """A requirement has to be routable at the moment it is raised.

    The approver's own requirement is auto-approved and queued instantly, so
    there is no approval step to redirect it in — which left the one person
    allowed to choose a repository with no way to say which.
    """

    def test_a_leading_registered_name_routes_the_requirement(self):
        self.brain.handle(msg(text="bloom 把首页 CTA 改强一点", sender=self.APPROVER))
        request = self.store.all()[0]
        self.assertEqual(request.repo, "org/bloom")

    def test_the_name_is_not_left_in_the_requirement_text(self):
        self.brain.handle(msg(text="bloom 把首页 CTA 改强一点", sender=self.APPROVER))
        self.assertEqual(self.store.all()[0].text, "把首页 CTA 改强一点")

    def test_a_leading_word_that_is_not_a_repository_stays_in_the_text(self):
        # Consuming it would silently eat a word from somebody's requirement.
        self.brain.handle(msg(text="紧急 把首页 CTA 改强一点", sender=self.APPROVER))
        request = self.store.all()[0]
        self.assertEqual(request.text, "紧急 把首页 CTA 改强一点")
        self.assertEqual(request.repo, "acme/widget")

    def test_the_acknowledgement_says_a_name_was_consumed(self):
        # A false positive has to be visible, since it costs a word of the text.
        reply = self.brain.handle(msg(text="bloom 做个东西", sender=self.APPROVER))
        self.assertIn("bloom", reply)
        self.assertIn("/repo", reply)

    def test_new_takes_a_repository_too(self):
        self.brain.handle(msg(text="/new bloom 做个东西", sender=self.APPROVER))
        self.assertEqual(self.store.all()[0].repo, "org/bloom")

    def test_a_full_project_path_works_as_well_as_the_short_name(self):
        self.brain.handle(msg(text="org/bloom 做个东西", sender=self.APPROVER))
        self.assertEqual(self.store.all()[0].repo, "org/bloom")


class Redirecting(Base):
    def _queued(self):
        self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        request = self.store.all()[0]
        self.brain.handle(msg(text="同意 " + request.id, msg_id="ok", sender=self.APPROVER))
        return self.store.get(request.id)

    def test_a_queued_request_can_still_be_redirected(self):
        request = self._queued()
        reply = self.brain.handle(msg(text="/repo {} bloom".format(request.id),
                                      msg_id="m2", sender=self.APPROVER))
        self.assertEqual(self.store.get(request.id).repo, "org/bloom")
        self.assertIn("bloom", reply)

    def test_a_running_request_cannot_be(self):
        # The agent is already in the other checkout; moving the label would only
        # make the record lie about where the work happened.
        request = self._queued()
        request.start(session="s")
        self.store.save(request)
        reply = self.brain.handle(msg(text="/repo {} bloom".format(request.id),
                                      msg_id="m2", sender=self.APPROVER))
        self.assertIn("已经在跑", reply)
        self.assertEqual(self.store.get(request.id).repo, "acme/widget")

    def test_only_the_approver_may_redirect(self):
        request = self._queued()
        reply = self.brain.handle(msg(text="/repo {} bloom".format(request.id),
                                      msg_id="m2", sender="somebody"))
        self.assertIn("只有", reply)

    def test_an_unregistered_name_is_refused_with_the_options(self):
        request = self._queued()
        reply = self.brain.handle(msg(text="/repo {} nowhere".format(request.id),
                                      msg_id="m2", sender=self.APPROVER))
        self.assertIn("widget", reply)
        self.assertEqual(self.store.get(request.id).repo, "acme/widget")


class Cancelling(Base):
    """A queue with no way out of a stuck job is a queue that stops."""

    def setUp(self):
        Base.setUp(self)
        self.cancelled = []

        def cancel(request_id, by):
            self.cancelled.append((request_id, by))
            request = self.store.get(request_id)
            if request and request.status in intake_mod.OPEN:
                request.cancel(by=by)
                self.store.save(request)
                return True, "已停掉 {}".format(request_id)
            return False, "{} 没有在办".format(request_id)

        self.brain.cancel_job = cancel

    def _running(self):
        self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        request = self.store.all()[0]
        self.brain.handle(msg(text="同意 " + request.id, msg_id="ok", sender=self.APPROVER))
        request = self.store.get(request.id)
        request.start(session="s", pid=4242)
        self.store.save(request)
        return request

    def test_the_owner_can_stop_a_running_job(self):
        request = self._running()
        reply = self.brain.handle(msg(text="/cancel " + request.id, msg_id="m2",
                                      sender=self.APPROVER))
        self.assertEqual(self.store.get(request.id).status, intake_mod.CANCELLED)
        self.assertIn(request.id, reply)

    def test_it_goes_through_whoever_owns_the_process(self):
        # Marking the record without stopping the process relabels a stuck job
        # while it still holds the worker.
        request = self._running()
        self.brain.handle(msg(text="/cancel " + request.id, msg_id="m2", sender=self.APPROVER))
        self.assertEqual(self.cancelled, [(request.id, "张三")])

    def test_nobody_else_can(self):
        request = self._running()
        reply = self.brain.handle(msg(text="/cancel " + request.id, msg_id="m2",
                                      sender="somebody"))
        self.assertIn("只有", reply)
        self.assertEqual(self.store.get(request.id).status, intake_mod.RUNNING)

    def test_cancelling_something_already_finished_says_so(self):
        request = self._running()
        request.finish(issues=["https://f/1"])
        self.store.save(request)
        reply = self.brain.handle(msg(text="/cancel " + request.id, msg_id="m2",
                                      sender=self.APPROVER))
        self.assertIn("没有在办", reply)

    def test_an_unknown_id_shows_the_usage(self):
        reply = self.brain.handle(msg(text="/cancel R20260101-99", sender=self.APPROVER))
        self.assertIn("/p", reply)

    def test_a_cancelled_job_leaves_the_open_queue(self):
        request = self._running()
        self.brain.handle(msg(text="/cancel " + request.id, msg_id="m2", sender=self.APPROVER))
        self.assertIn("没有在办", self.brain.handle(msg(text="/p", msg_id="m3")))


class DeferredWorkIsVisible(Base):
    """During an outage, a queue full of waiting work must not look idle."""

    def _deferred(self):
        self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        request = self.store.all()[0]
        self.brain.handle(msg(text="同意 " + request.id, msg_id="ok", sender=self.APPROVER))
        request = self.store.get(request.id)
        request.start(session="s")
        request.defer("API Error: 529 Overloaded", 300)
        return self.store.save(request)

    def test_p_shows_it_as_waiting_on_the_server(self):
        request = self._deferred()
        reply = self.brain.handle(msg(text="/p", msg_id="m2"))
        self.assertIn(request.id, reply)
        self.assertIn("服务端故障", reply)

    def test_it_says_how_long(self):
        self._deferred()
        self.assertIn("秒后重试", self.brain.handle(msg(text="/p", msg_id="m2")))

    def test_r_distinguishes_waiting_from_failed(self):
        request = self._deferred()
        reply = self.brain.handle(msg(text="/r " + request.id, msg_id="m2"))
        self.assertIn("等待重试", reply)
        self.assertNotIn("失败：", reply)

    def test_it_can_still_be_cancelled(self):
        # An outage nobody wants to wait out is still somebody's decision.
        request = self._deferred()
        self.brain.cancel_job = lambda rid, by: (True, "已停掉 " + rid)
        self.assertIn("已停掉", self.brain.handle(
            msg(text="/cancel " + request.id, msg_id="m2", sender=self.APPROVER)))


class WorkAnswersWhereItCame(Base):
    """A requirement raised in a group keeps its conversation.

    Replies ride the inbound session webhook and always land correctly. Cards the
    bot *initiates* — a question mid-job, a finished report — used a fixed target,
    so work raised in a group produced questions in one person's private chat that
    the group never saw and could not answer.
    """

    def test_the_conversation_is_recorded_with_the_requirement(self):
        self.brain.handle(msg(text="做个东西", conversation="cid-1", sender="somebody"))
        self.assertEqual(self.store.all()[0].conversation, "cid-1")

    def test_it_survives_approval_and_queueing(self):
        self.brain.handle(msg(text="做个东西", conversation="cid-1", sender="somebody"))
        request = self.store.all()[0]
        self.brain.handle(msg(text="同意 " + request.id, msg_id="m2", sender=self.APPROVER))
        self.assertEqual(self.enqueued[0].conversation, "cid-1")

    def test_a_development_job_records_where_it_was_asked_for(self):
        self.forge.add(612)
        self.brain.handle(msg(text="/dev 612", conversation="cid-1", sender=self.APPROVER))
        self.assertEqual(self.enqueued[0].conversation, "cid-1")


class SelfServiceAllowList(Base):
    """Adding a group should not mean editing a file and restarting."""

    def setUp(self):
        Base.setUp(self)
        self.allowed = []
        self.denied = []
        self.brain.allow_conversation = lambda cid, private: self.allowed.append((cid, private))
        self.brain.deny_conversation = self.denied.append

    def _say(self, text, sender=None, conversation="cid-new", ctype="2"):
        return self.brain.handle(listener.Inbound(
            msg_id=text + conversation, text=text, sender_id=sender or self.APPROVER,
            sender_nick="穆轩", conversation_id=conversation, conversation_type=ctype))

    def test_the_approver_can_allow_the_conversation_they_are_in(self):
        # It has to work from somewhere not yet listed — that is the entire point
        # — so it is exempt from the allow-list and gated on the approver instead.
        reply = self._say("/allow")
        self.assertEqual(self.allowed, [("cid-new", False)])
        self.assertIn("白名单", reply)

    def test_nobody_else_can(self):
        self._say("/allow", sender="somebody")
        self.assertEqual(self.allowed, [])

    def test_a_private_chat_is_recorded_as_one(self):
        # The ids do not say which is which, and a card sent to the group endpoint
        # with a private id is accepted and never delivered.
        self._say("/allow", ctype="1")
        self.assertEqual(self.allowed, [("cid-new", True)])

    def test_it_takes_effect_immediately(self):
        self._say("/allow")
        self.assertIn("cid-new", self.brain.conversations)
        # A command that was ignored a moment ago now answers.
        self.assertTrue(self._say("/ping"))

    def test_deny_removes_it(self):
        self._say("/allow")
        self._say("/deny")
        self.assertEqual(self.denied, ["cid-new"])
        self.assertNotIn("cid-new", self.brain.conversations)

    def test_an_unlisted_conversation_still_ignores_everything_else(self):
        self.assertEqual(self._say("/ls"), "")
        self.assertEqual(self._say("做个东西", sender="somebody"), "")

    def test_a_machine_with_no_writable_config_says_so(self):
        self.brain.allow_conversation = None
        self.assertIn("加不了", self._say("/allow"))
