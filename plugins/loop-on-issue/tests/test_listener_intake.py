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

    def test_the_approver_raising_it_is_auto_approved_and_queued(self):
        self.brain.handle(msg(text="做个东西", sender=self.APPROVER))
        req = self.store.all()[0]
        self.assertTrue(req.auto_approved)
        self.assertEqual(req.status, intake_mod.APPROVED)
        self.assertEqual([r.id for r in self.enqueued], [req.id])

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
