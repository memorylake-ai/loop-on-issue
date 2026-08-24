import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import listener, pending, state
from loopkit.models import Comment, Issue


def msg(text="hi", msg_id="m1", sender="u1", nick="张三", conversation="cid-1", pqk=None):
    return listener.Inbound(
        msg_id=msg_id, text=text, sender_id=sender, sender_nick=nick,
        conversation_id=conversation, pqk=pqk,
    )


class Dedupe(unittest.TestCase):
    def test_the_same_delivery_twice_is_seen_once(self):
        # DingTalk Stream is at-least-once: a reconnect redelivers the same msgId.
        # Treating a redelivered bare reply as new would answer the *second*
        # newest question — a real production failure in the prior art.
        d = listener.Dedupe(window=600)
        self.assertFalse(d.seen("m1", now=0))
        self.assertTrue(d.seen("m1", now=1))

    def test_different_messages_are_independent(self):
        d = listener.Dedupe(window=600)
        d.seen("m1", now=0)
        self.assertFalse(d.seen("m2", now=0))

    def test_the_window_eventually_forgets(self):
        d = listener.Dedupe(window=600)
        d.seen("m1", now=0)
        self.assertFalse(d.seen("m1", now=10_000))

    def test_old_entries_do_not_accumulate_forever(self):
        d = listener.Dedupe(window=600)
        for i in range(50):
            d.seen("m{}".format(i), now=i)
        d.seen("late", now=100_000)
        self.assertLess(len(d._seen), 5)


class Dispatch(unittest.TestCase):
    ALLOWED = ["cid-1"]

    def test_an_unlisted_conversation_is_ignored(self):
        # Fail closed: a bot answering any group it is added to is a bot anyone
        # can put work into.
        action = listener.dispatch(msg(conversation="cid-other"), self.ALLOWED, has_pending=False)
        self.assertEqual(action.kind, listener.IGNORE)

    def test_an_empty_allow_list_ignores_everything(self):
        self.assertEqual(listener.dispatch(msg(), [], has_pending=False).kind, listener.IGNORE)

    def test_whoami_answers_from_anywhere(self):
        # Otherwise the allow-list is a bootstrap deadlock: you cannot learn a
        # conversationId from a conversation that ignores you.
        action = listener.dispatch(msg(text="/whoami", conversation="cid-new"), self.ALLOWED,
                                   has_pending=False)
        self.assertEqual((action.kind, action.command), (listener.COMMAND, "whoami"))

    def test_no_other_command_escapes_the_allow_list(self):
        for text in ("/ls", "/skip 1 confirm x", "同意 700", "做个东西"):
            action = listener.dispatch(msg(text=text, conversation="cid-new"), self.ALLOWED,
                                       has_pending=False)
            self.assertEqual(action.kind, listener.IGNORE, text)

    def test_a_slash_prefix_is_a_command(self):
        action = listener.dispatch(msg(text="/ls"), self.ALLOWED, has_pending=False)
        self.assertEqual((action.kind, action.command), (listener.COMMAND, "ls"))

    def test_a_quote_reply_routes_by_its_key(self):
        action = listener.dispatch(msg(text="2", pqk="PQK-9"), self.ALLOWED, has_pending=True)
        self.assertEqual((action.kind, action.pqk), (listener.ANSWER, "PQK-9"))

    def test_a_bare_message_is_a_requirement_even_with_a_question_open(self):
        # Only a quote-reply answers. Reading a bare sentence as an answer
        # whenever some question happened to be open meant a new requirement
        # could be swallowed by a question nobody was thinking about.
        action = listener.dispatch(msg(text="把首页 CTA 改强一点"), self.ALLOWED, has_pending=True)
        self.assertEqual(action.kind, listener.INTAKE)

    def test_a_bare_message_with_nothing_pending_is_also_a_requirement(self):
        action = listener.dispatch(msg(text="把首页 CTA 改强一点"), self.ALLOWED, has_pending=False)
        self.assertEqual(action.kind, listener.INTAKE)

    def test_a_command_wins_even_while_a_question_is_open(self):
        action = listener.dispatch(msg(text="/q"), self.ALLOWED, has_pending=True)
        self.assertEqual(action.kind, listener.COMMAND)

    def test_a_quote_reply_is_an_answer_even_with_nothing_indexed(self):
        # Quoting a card is an unambiguous statement of intent; the index may
        # simply have been swept.
        action = listener.dispatch(msg(text="1", pqk="PQK-9"), self.ALLOWED, has_pending=False)
        self.assertEqual(action.kind, listener.ANSWER)

    def test_an_empty_message_is_ignored(self):
        self.assertEqual(listener.dispatch(msg(text="   "), self.ALLOWED, has_pending=True).kind,
                         listener.IGNORE)

    def test_the_bot_mention_is_stripped_before_anything_is_decided(self):
        action = listener.dispatch(msg(text="@Loop助手 /ls"), self.ALLOWED, has_pending=False)
        self.assertEqual((action.kind, action.command), (listener.COMMAND, "ls"))


class CommandParsing(unittest.TestCase):
    def test_name_and_rest(self):
        name, rest = listener.parse_command("/skip 612 already fixed")
        self.assertEqual((name, rest), ("skip", "612 already fixed"))

    def test_aliases(self):
        for text, expected in (("/help", "help"), ("/h", "help"), ("/list", "ls"), ("/ls", "ls")):
            self.assertEqual(listener.parse_command(text)[0], expected, text)

    def test_chinese_approval_words_are_commands_without_a_slash(self):
        # Nobody types a slash on a phone to say yes.
        self.assertEqual(listener.parse_command("同意 712")[0], "approve")
        self.assertEqual(listener.parse_command("批准 712")[0], "approve")
        self.assertEqual(listener.parse_command("拒绝 712 不做")[0], "reject")

    def test_an_unknown_slash_command_is_reported_not_guessed(self):
        self.assertEqual(listener.parse_command("/frobnicate")[0], "frobnicate")

    def test_plain_text_is_not_a_command(self):
        self.assertIsNone(listener.parse_command("just some words")[0])


class Confirmation(unittest.TestCase):
    def test_a_destructive_command_needs_the_word_confirm(self):
        needed, rest = listener.needs_confirm("612 already fixed by #881")
        self.assertTrue(needed)

    def test_confirm_may_follow_the_target(self):
        needed, rest = listener.needs_confirm("612 confirm already fixed by #881")
        self.assertFalse(needed)
        self.assertEqual(rest, "612 already fixed by #881")

    def test_confirmation_is_stateless(self):
        # Nothing is remembered between the two messages, so a listener restart
        # between them changes nothing.
        _, rest = listener.needs_confirm("612 confirm because")
        self.assertNotIn("confirm", rest)


class FakeForge:
    def __init__(self):
        self.comments = {}
        self.issues = {}
        self.created = []
        self.titles = {}

    def add(self, number, title="a thing", labels=("loop",), state_="opened"):
        self.issues[number] = Issue(number=number, title=title, state=state_,
                                    url="https://f/{}".format(number), labels=list(labels))
        self.comments.setdefault(number, [])
        return self.issues[number]

    def get_issue(self, number):
        if number not in self.issues:
            raise KeyError(number)
        return self.issues[number]

    def list_issues(self, label=None, assignee=None, state="opened"):
        return [i for i in self.issues.values() if not label or label in i.labels]

    def list_issue_comments(self, number):
        return list(self.comments.get(number, []))

    def add_issue_comment(self, number, body):
        bucket = self.comments.setdefault(number, [])
        c = Comment(id=len(bucket) + 1, author="bot", created_at="t{:03d}".format(len(bucket)), body=body)
        bucket.append(c)
        return c

    def create_issue(self, title, body, labels=None, assignees=None):
        number = 700 + len(self.created)
        self.created.append({"title": title, "body": body, "labels": list(labels or [])})
        issue = self.add(number, title=title, labels=labels or [])
        issue.body = body
        return issue

    def set_issue_title(self, number, title):
        self.titles[number] = title
        self.issues[number].title = title


class Brains(unittest.TestCase):
    APPROVER = "staff-approver"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-listener-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.index = pending.Index(self.dir + "/pending")
        self.forge = FakeForge()
        from loopkit import intake as intake_mod
        from loopkit import repos as repos_mod

        registry = repos_mod.Registry()
        registry.add("widget", "acme/widget", self.dir + "/widget")
        self.brain = listener.Brain(
            forge_for=lambda repo: self.forge,
            registry=registry,
            index=self.index,
            store=intake_mod.Store(self.dir + "/intake"),
            conversations=["cid-1"],
            approver=self.APPROVER,
            approver_nick="Julian",
            queue_label="loop",
            assignee="muxuan",
        )

    # -- answers -------------------------------------------------------------
    def test_a_quoted_answer_lands_on_the_issue_it_answers(self):
        self.forge.add(612)
        self.index.record("PQK-9", {"repo": "acme/widget", "issue": 612, "options": ["left", "right"]})
        self.brain.handle(msg(text="2", pqk="PQK-9"))
        body = self.forge.comments[612][-1].body
        self.assertIn("right", body)

    def test_a_relayed_answer_still_reads_as_a_human_reply(self):
        # If it read as an agent note the issue would never wake up.
        self.forge.add(612)
        self.index.record("PQK-9", {"repo": "acme/widget", "issue": 612})
        self.brain.handle(msg(text="go left", pqk="PQK-9"))
        self.assertFalse(state.is_agent_note(self.forge.comments[612][-1].body))

    def test_the_answerer_is_recorded(self):
        self.forge.add(612)
        self.index.record("PQK-9", {"repo": "acme/widget", "issue": 612})
        self.brain.handle(msg(text="go left", pqk="PQK-9", nick="李四"))
        self.assertIn("李四", self.forge.comments[612][-1].body)

    def test_an_unknown_routing_key_says_so_rather_than_guessing(self):
        reply = self.brain.handle(msg(text="1", pqk="PQK-gone"))
        self.assertIn("过期", reply)

    def test_an_explicit_answer_still_reaches_a_specific_issue(self):
        self.forge.add(612)
        self.brain.handle(msg(text="/a 612 go left"))
        self.assertIn("go left", self.forge.comments[612][-1].body)

    # -- commands ------------------------------------------------------------
    def test_whoami_hands_back_pasteable_configuration(self):
        reply = self.brain.handle(msg(text="/whoami", sender="staff-9", nick="王五",
                                      conversation="cid-unlisted"))
        self.assertIn("staff-9", reply)
        self.assertIn("cid-unlisted", reply)
        self.assertIn("LOOP_DINGTALK_CONVERSATIONS", reply)

    def test_whoami_says_whether_this_conversation_is_listed(self):
        self.assertIn("还不", self.brain.handle(msg(text="/whoami", conversation="cid-nope")))
        self.assertIn("已经", self.brain.handle(msg(text="/whoami", msg_id="m2", conversation="cid-1")))

    def test_help_lists_commands(self):
        self.assertIn("/ls", self.brain.handle(msg(text="/h")))

    def test_ping_answers(self):
        self.assertTrue(self.brain.handle(msg(text="/ping")))

    def test_ls_reports_the_board_by_state(self):
        self.forge.add(612, title="[WORKING] a")
        self.forge.add(613, title="b")
        reply = self.brain.handle(msg(text="/ls"))
        self.assertIn("WORKING", reply)
        self.assertIn("612", reply)

    def test_q_lists_open_questions(self):
        self.index.record("k", {"repo": "acme/widget", "issue": 612, "url": "https://f/612"})
        self.assertIn("612", self.brain.handle(msg(text="/q")))

    def test_answering_an_issue_explicitly(self):
        self.forge.add(612)
        self.brain.handle(msg(text="/a 612 go left"))
        self.assertIn("go left", self.forge.comments[612][-1].body)

    def test_skip_refuses_without_confirmation(self):
        self.forge.add(612)
        reply = self.brain.handle(msg(text="/skip 612 already fixed by #881"))
        self.assertIn("confirm", reply)
        self.assertEqual(self.forge.titles, {})

    def test_skip_with_confirmation_retires_the_issue(self):
        self.forge.add(612)
        self.brain.handle(msg(text="/skip 612 confirm already fixed by #881"))
        self.assertTrue(self.forge.titles[612].startswith("[SKIP]"))

    def test_requeue_needs_confirmation_too(self):
        self.forge.add(612, title="[PAUSED] a")
        self.brain.handle(msg(text="/requeue 612"))
        self.assertEqual(self.forge.titles, {})
        self.brain.handle(msg(text="/requeue 612 confirm", msg_id="m2"))
        self.assertEqual(self.forge.titles[612], "a")

    def test_an_unknown_command_is_named_back(self):
        reply = self.brain.handle(msg(text="/frobnicate"))
        self.assertIn("frobnicate", reply)

    def test_a_command_about_a_missing_issue_says_so(self):
        self.assertIn("999", self.brain.handle(msg(text="/i 999")))


if __name__ == "__main__":
    unittest.main()


class DecoratedCommands(unittest.TestCase):
    """Commands survive being copied out of a rendered message.

    Replies offer options as pasteable commands wrapped in backticks. Copying the
    rendered line takes the backticks with it, and the leading one stopped the
    command being recognised at all — so an approval was filed as a brand new
    requirement, and the thing it meant to approve stayed pending.
    """

    def test_backticks_around_the_whole_command(self):
        self.assertEqual(listener.parse_command("`同意 R20260824-05`")[0], "approve")

    def test_backticks_and_a_trailing_rendered_arrow(self):
        name, rest = listener.parse_command(
            "`同意 R20260824-05 demo-gh` → `iDonal/demo-project`")
        self.assertEqual(name, "approve")
        self.assertTrue(rest.startswith("R20260824-05 demo-gh"))

    def test_a_backticked_slash_command(self):
        self.assertEqual(listener.parse_command("`/ls`")[0], "ls")

    def test_bold_markers(self):
        self.assertEqual(listener.parse_command("**同意 R1**")[0], "approve")

    def test_a_leading_bullet_from_a_copied_list_item(self):
        self.assertEqual(listener.parse_command("- `同意 R1`")[0], "approve")

    def test_plain_text_is_still_not_a_command(self):
        self.assertIsNone(listener.parse_command("给 README 加一段说明")[0])

    def test_a_requirement_that_merely_contains_backticks_stays_a_requirement(self):
        # Stripping decoration must not turn prose into a command.
        self.assertIsNone(listener.parse_command("把 `foo()` 改成 `bar()`")[0])

    def test_the_arrow_and_what_follows_do_not_corrupt_the_target(self):
        _, rest = listener.parse_command("`同意 R1 demo-gh` → `owner/name`")
        self.assertNotIn("→", rest.split(" ")[0])


class IssueReferences(unittest.TestCase):
    """`612` when it is obvious, `demo-gh:612` when it is not.

    Commands that act on an issue used to look only at the default repository, so
    with several registered there was no way to name an issue in any of the others.
    """

    def test_a_bare_number(self):
        self.assertEqual(listener.parse_issue_ref("612"), (None, 612, ""))

    def test_a_hash_is_tolerated(self):
        self.assertEqual(listener.parse_issue_ref("#612"), (None, 612, ""))

    def test_a_qualified_reference(self):
        self.assertEqual(listener.parse_issue_ref("demo-gh:612"), ("demo-gh", 612, ""))

    def test_a_full_project_path_qualifies_too(self):
        # The slug may contain slashes, so the split is on the last colon.
        self.assertEqual(listener.parse_issue_ref("org/name:612"), ("org/name", 612, ""))

    def test_a_qualified_reference_with_a_hash(self):
        self.assertEqual(listener.parse_issue_ref("demo-gh:#612"), ("demo-gh", 612, ""))

    def test_the_rest_of_the_line_comes_back(self):
        repo, number, rest = listener.parse_issue_ref("demo-gh:612 用第二个方案")
        self.assertEqual((repo, number, rest), ("demo-gh", 612, "用第二个方案"))

    def test_the_rest_survives_an_unqualified_reference(self):
        self.assertEqual(listener.parse_issue_ref("612 用第二个方案"), (None, 612, "用第二个方案"))

    def test_no_number_at_all(self):
        self.assertEqual(listener.parse_issue_ref("nonsense"), (None, None, "nonsense"))

    def test_empty(self):
        self.assertEqual(listener.parse_issue_ref(""), (None, None, ""))

    def test_a_requirement_id_is_not_an_issue_reference(self):
        # R20260824-01 is a requirement; reading its digits as an issue number
        # would act on a completely unrelated thing.
        self.assertEqual(listener.parse_issue_ref("R20260824-01")[1], None)

    def test_a_colon_with_no_number_is_not_a_reference(self):
        self.assertEqual(listener.parse_issue_ref("demo-gh:")[1], None)
