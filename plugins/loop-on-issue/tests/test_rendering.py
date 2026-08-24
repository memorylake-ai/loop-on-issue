"""Guard the one formatting rule DingTalk enforces silently.

A single newline does **not** break a line there, and `_underscore italics_` are
not rendered at all. Getting either wrong produces no error — the message simply
arrives as one unreadable paragraph, which is how the first `/h` shipped.
"""

import unittest

import _bootstrap  # noqa: F401

from loopkit import listener
from loopkit.models import Comment, Issue


def offending_join(text):
    """The first place a single newline would silently run two lines together.

    Safe after any line: a list item, a blockquote, or a blank line. Anything else
    following a plain line gets joined to it.
    """
    lines = (text or "").split("\n")
    for index in range(len(lines) - 1):
        current, following = lines[index].strip(), lines[index + 1].strip()
        if not current or not following:
            continue
        if following.startswith(("- ", "> ", "1.", "*")) or current.startswith(("- ", "> ")):
            continue
        return "{!r} then {!r}".format(current, following)
    return None


class Helpers(unittest.TestCase):
    def test_md_separates_blocks_with_a_blank_line(self):
        self.assertEqual(listener.md("a", "b"), "a\n\nb")

    def test_md_drops_empty_blocks(self):
        # Optional sections are passed as "" rather than filtered at every call
        # site; they must not leave a hole of blank lines behind.
        self.assertEqual(listener.md("a", "", None, "b"), "a\n\nb")

    def test_bullets_are_list_items(self):
        self.assertEqual(listener.bullets("x", "y"), "- x\n- y")

    def test_bullets_skip_empties(self):
        self.assertEqual(listener.bullets("x", "", "y"), "- x\n- y")


class HelpText(unittest.TestCase):
    def test_no_two_plain_lines_are_joined(self):
        self.assertIsNone(offending_join(listener.HELP), offending_join(listener.HELP))

    def test_no_underscore_italics(self):
        # DingTalk renders these as literal underscores.
        for line in listener.HELP.split("\n"):
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("_") and stripped.endswith("_") and len(stripped) > 2,
                line,
            )

    def test_sections_are_bold_headers(self):
        self.assertIn("**提需求 / 查看**", listener.HELP)

    def test_it_leads_with_the_rule_people_get_wrong(self):
        first = listener.HELP.split("\n\n")[1]
        self.assertIn("提需求", first)


class FakeForge:
    def __init__(self):
        self.issues = {1: Issue(number=1, title="[WORKING] a thing", state="opened",
                                url="https://f/1", labels=["loop"])}

    def get_issue(self, number):
        return self.issues[number]

    def list_issues(self, label=None, assignee=None, state="opened"):
        return list(self.issues.values())

    def list_issue_comments(self, number):
        return [Comment(id=1, author="bot", created_at="t1",
                        body="<!-- loop-on-issue:agent session=abc runner=claude -->\nclaimed")]

    def add_issue_comment(self, number, body):
        return Comment(id=2, author="bot", created_at="t2", body=body)


class EveryReply(unittest.TestCase):
    """Whatever a command answers with has to survive the same renderer."""

    def setUp(self):
        import shutil
        import tempfile

        from loopkit import intake as intake_mod
        from loopkit import pending, repos as repos_mod

        self.dir = tempfile.mkdtemp(prefix="loop-render-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        registry = repos_mod.Registry()
        registry.add("widget", "acme/widget", self.dir)
        registry.add("bloom", "org/bloom", self.dir)
        registry.set_default("widget")
        self.store = intake_mod.Store(self.dir + "/intake")
        self.index = pending.Index(self.dir + "/pending")
        self.index.record("k", {"repo": "acme/widget", "issue": 1, "url": "https://f/1"})
        self.brain = listener.Brain(
            forge_for=lambda repo: FakeForge(),
            registry=registry,
            index=self.index,
            store=self.store,
            conversations=["cid-1"],
            approver="approver",
            approver_nick="Julian",
        )

    def _say(self, text, sender="approver"):
        return self.brain.handle(listener.Inbound(
            msg_id=text, text=text, sender_id=sender, sender_nick="穆轩",
            conversation_id="cid-1"))

    def test_every_command_reply_renders(self):
        # A requirement first, so /p and /r have something to show.
        self._say("给 CLI 加一个 version 子命令", sender="somebody")
        request_id = self.store.all()[0].id
        for message in ("/h", "/ping", "/whoami", "/repos", "/q", "/ls", "/i 1",
                        "/p", "/r " + request_id, "/dev 1", "/skip 1 已经修好了不需要动",
                        "/requeue 1", "/frobnicate"):
            reply = self._say(message)
            self.assertTrue(reply, message)
            self.assertIsNone(offending_join(reply), "{}: {}".format(message, offending_join(reply)))

    def test_the_intake_acknowledgement_renders(self):
        reply = self._say("把首页 CTA 改强一点", sender="somebody")
        self.assertIsNone(offending_join(reply), offending_join(reply))
        self.assertIn("待", reply)

    def test_the_approval_reply_renders(self):
        self._say("做个东西", sender="somebody")
        request_id = self.store.all()[0].id
        reply = self._say("同意 {} bloom 注意别动定价页".format(request_id))
        self.assertIsNone(offending_join(reply), offending_join(reply))

    def test_the_no_default_refusal_renders(self):
        from loopkit import repos as repos_mod

        registry = repos_mod.Registry()
        registry.add("a", "org/a", self.dir)
        registry.add("b", "org/b", self.dir)
        self.brain.registry = registry
        reply = self._say("做个东西", sender="somebody")
        self.assertIsNone(offending_join(reply), offending_join(reply))


if __name__ == "__main__":
    unittest.main()


class PlaceholdersAreDistinguishable(unittest.TestCase):
    """Two different things must not be told apart by letter case alone.

    The help used `<ID>` for a requirement (R20260824-01) and `<id>` for an issue
    number (612). Nobody reads case as meaning, and on a phone it is invisible —
    `/r <ID>` and `/i <id>` sat next to each other differing by one capital.
    """

    def test_no_placeholder_differs_from_another_only_by_case(self):
        import re

        found = set(re.findall(r"<[^>]+>", listener.HELP))
        lowered = {}
        for name in found:
            key = name.lower()
            lowered.setdefault(key, set()).add(name)
        clashes = {k: v for k, v in lowered.items() if len(v) > 1}
        self.assertEqual(clashes, {}, "case-only distinctions: {}".format(clashes))

    def test_the_two_kinds_of_number_are_named_differently(self):
        # A requirement id and an issue number are not interchangeable anywhere.
        self.assertIn("R-ID", listener.HELP)
        self.assertIn("issue-id", listener.HELP)

    def test_the_help_shows_what_each_looks_like(self):
        # The shapes are unmistakable; showing them beats naming them.
        self.assertIn("R20260824-01", listener.HELP)
        self.assertIn("demo-gh:612", listener.HELP)

    def test_issue_commands_all_use_the_same_placeholder(self):
        import re

        for line in listener.HELP.splitlines():
            if "/dev" in line or "/i " in line or "/a " in line or "/skip" in line:
                self.assertNotIn("<id>", line, line)
                self.assertNotIn("<issue>", line, line)


class PlaceholdersAgreeEverywhere(unittest.TestCase):
    """A renamed placeholder has to be renamed in every reply, not just the help.

    `/p` kept offering `同意 <ID>` in its footer after the help had moved to
    `<R-ID>`, which is exactly the drift that teaches people to distrust the docs.
    """

    def _all_replies(self):
        import shutil
        import tempfile

        from loopkit import intake as intake_mod
        from loopkit import pending, repos as repos_mod

        directory = tempfile.mkdtemp(prefix="loop-ph-")
        self.addCleanup(shutil.rmtree, directory, True)
        registry = repos_mod.Registry()
        registry.add("widget", "acme/widget", directory)
        registry.set_default("widget")
        store = intake_mod.Store(directory + "/intake")
        brain = listener.Brain(
            forge_for=lambda repo: FakeForge(), registry=registry,
            index=pending.Index(directory + "/pending"), store=store,
            conversations=["cid-1"], approver="approver", approver_nick="Julian")

        def say(text, sender="approver"):
            return brain.handle(listener.Inbound(
                msg_id=text, text=text, sender_id=sender, sender_nick="穆轩",
                conversation_id="cid-1"))

        say("一条需求", sender="somebody")
        rid = store.all()[0].id
        return [say(m) for m in ("/h", "/p", "/r " + rid, "/i 1", "/repos",
                                 "/cancel", "/dev", "同意", "拒绝", "/repo",
                                 "/a", "/skip", "/requeue")]

    def test_no_reply_uses_the_retired_placeholder(self):
        for reply in self._all_replies():
            self.assertNotIn("<ID>", reply, reply[:120])
            self.assertNotIn("<id>", reply, reply[:120])

    def test_requirement_placeholders_are_uniform(self):
        for reply in self._all_replies():
            if "R-ID" in reply or "R编号" in reply:
                self.assertNotIn("R编号", reply, reply[:120])
