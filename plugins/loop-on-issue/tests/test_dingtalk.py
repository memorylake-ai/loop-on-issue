import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import dingtalk


class EnvLoading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-dt-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, name, body):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def test_reads_shell_style_quoted_values(self):
        path = self._write("a.env", 'DINGTALK_CLIENT_ID="abc"\nDINGTALK_CLIENT_SECRET=\'xyz\'\n')
        env = dingtalk.load_env([path], environ={})
        self.assertEqual(env["DINGTALK_CLIENT_ID"], "abc")
        self.assertEqual(env["DINGTALK_CLIENT_SECRET"], "xyz")

    def test_ignores_comments_and_blank_lines(self):
        path = self._write("a.env", "# a comment\n\nDINGTALK_CLIENT_ID=abc\n")
        self.assertEqual(dingtalk.load_env([path], environ={})["DINGTALK_CLIENT_ID"], "abc")

    def test_tolerates_export_prefix(self):
        path = self._write("a.env", 'export DINGTALK_CLIENT_ID="abc"\n')
        self.assertEqual(dingtalk.load_env([path], environ={})["DINGTALK_CLIENT_ID"], "abc")

    def test_earlier_files_win(self):
        first = self._write("a.env", "DINGTALK_CLIENT_ID=first\n")
        second = self._write("b.env", "DINGTALK_CLIENT_ID=second\n")
        self.assertEqual(dingtalk.load_env([first, second], environ={})["DINGTALK_CLIENT_ID"], "first")

    def test_an_explicit_path_excludes_every_other_source(self):
        saved = os.environ.get("LOOP_DINGTALK_ENV")
        os.environ["LOOP_DINGTALK_ENV"] = "/some/explicit.env"
        try:
            self.assertEqual(dingtalk.default_env_paths(), ["/some/explicit.env"])
        finally:
            if saved is None:
                os.environ.pop("LOOP_DINGTALK_ENV", None)
            else:
                os.environ["LOOP_DINGTALK_ENV"] = saved

    def test_a_missing_file_is_skipped(self):
        path = self._write("a.env", "DINGTALK_CLIENT_ID=abc\n")
        env = dingtalk.load_env(["/nonexistent/x.env", path], environ={})
        self.assertEqual(env["DINGTALK_CLIENT_ID"], "abc")

    def test_process_environment_is_the_last_resort(self):
        env = dingtalk.load_env([], environ={"DINGTALK_CLIENT_ID": "from-env"})
        self.assertEqual(env["DINGTALK_CLIENT_ID"], "from-env")

    def test_a_file_value_beats_the_process_environment(self):
        # The file is the deliberate configuration; a stray exported variable in
        # somebody's shell profile should not silently redirect the bot.
        path = self._write("a.env", "DINGTALK_CLIENT_ID=from-file\n")
        env = dingtalk.load_env([path], environ={"DINGTALK_CLIENT_ID": "from-env"})
        self.assertEqual(env["DINGTALK_CLIENT_ID"], "from-file")

    def test_conversation_allow_list_is_split(self):
        env = dingtalk.load_env([], environ={"LOOP_DINGTALK_CONVERSATIONS": "cid1, cid2 ,cid3"})
        self.assertEqual(dingtalk.conversations(env), ["cid1", "cid2", "cid3"])

    def test_no_allow_list_means_no_conversations(self):
        # Fail closed: an empty allow-list must not mean "every group".
        self.assertEqual(dingtalk.conversations({}), [])


class Configured(unittest.TestCase):
    def test_needs_both_halves_of_the_credential(self):
        self.assertFalse(dingtalk.DingTalk({"DINGTALK_CLIENT_ID": "a"}).configured)
        self.assertFalse(dingtalk.DingTalk({"DINGTALK_CLIENT_SECRET": "b"}).configured)
        self.assertTrue(
            dingtalk.DingTalk({"DINGTALK_CLIENT_ID": "a", "DINGTALK_CLIENT_SECRET": "b"}).configured
        )

    def test_a_webhook_alone_is_enough_to_send(self):
        # Send-only fallback: no app credentials, so no pqk and no inbound, but a
        # notification still reaches the group.
        dt = dingtalk.DingTalk({"LOOP_DINGTALK_WEBHOOK": "https://oapi.dingtalk.com/robot/send?access_token=x"})
        self.assertFalse(dt.configured)
        self.assertTrue(dt.can_send)


class Transport(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def http(url, payload, headers, method="POST"):
            self.calls.append({"url": url, "payload": payload, "headers": headers})
            if "accessToken" in url:
                return {"accessToken": "TOK", "expireIn": 7200}
            return {"processQueryKey": "PQK-1"}

        self.dt = dingtalk.DingTalk(
            {"DINGTALK_CLIENT_ID": "cid", "DINGTALK_CLIENT_SECRET": "sec", "DINGTALK_ROBOT_CODE": "rc"},
            http=http,
        )

    def test_group_send_returns_the_routing_key(self):
        # The processQueryKey is the whole basis of quote-reply routing; a send
        # that loses it produces a card nobody can answer precisely.
        self.assertEqual(self.dt.send_group("cid-1", "title", "body"), "PQK-1")

    def test_the_token_is_fetched_once_and_reused(self):
        self.dt.send_group("cid-1", "t", "b")
        self.dt.send_group("cid-1", "t", "b")
        self.assertEqual(sum(1 for c in self.calls if "accessToken" in c["url"]), 1)

    def test_the_token_travels_in_the_documented_header(self):
        self.dt.send_group("cid-1", "t", "b")
        send = [c for c in self.calls if "groupMessages" in c["url"]][0]
        self.assertEqual(send["headers"]["x-acs-dingtalk-access-token"], "TOK")

    def test_group_send_targets_the_conversation_and_robot(self):
        self.dt.send_group("cid-1", "t", "b")
        send = [c for c in self.calls if "groupMessages" in c["url"]][0]
        self.assertEqual(send["payload"]["openConversationId"], "cid-1")
        self.assertEqual(send["payload"]["robotCode"], "rc")

    def test_the_robot_code_falls_back_to_the_client_id(self):
        dt = dingtalk.DingTalk({"DINGTALK_CLIENT_ID": "cid", "DINGTALK_CLIENT_SECRET": "s"},
                               http=lambda *a, **k: {"processQueryKey": "P"})
        self.assertEqual(dt.robot_code, "cid")

    def test_markdown_payload_shape(self):
        self.dt.send_group("cid-1", "the title", "the **body**")
        send = [c for c in self.calls if "groupMessages" in c["url"]][0]
        param = json.loads(send["payload"]["msgParam"])
        self.assertEqual(send["payload"]["msgKey"], "sampleMarkdown")
        self.assertEqual(param["title"], "the title")
        self.assertIn("the **body**", param["text"])

    def test_send_reports_nothing_delivered_when_there_is_no_target(self):
        # Credentials present but no conversation configured: half a dual write
        # would otherwise vanish while the command reported success.
        self.assertIsNone(self.dt.send("t", "b"))

    def test_send_uses_the_configured_conversation(self):
        dt = dingtalk.DingTalk(
            {"DINGTALK_CLIENT_ID": "c", "DINGTALK_CLIENT_SECRET": "s",
             "LOOP_DINGTALK_CONVERSATIONS": "cid-7"},
            http=lambda url, p, h, method="POST": (
                {"accessToken": "T", "expireIn": 100} if "accessToken" in url else {"processQueryKey": "K"}
            ),
        )
        self.assertEqual(dt.send("t", "b"), "K")

    def test_a_configured_dm_user_wins_over_a_conversation(self):
        # A private chat and a group need different endpoints, and a card sent to
        # the group endpoint with a one-to-one id is accepted and never delivered.
        calls = []

        def http(url, payload, headers, method="POST"):
            calls.append(url)
            return ({"accessToken": "T", "expireIn": 100} if "accessToken" in url
                    else {"processQueryKey": "K"})

        dt = dingtalk.DingTalk(
            {"DINGTALK_CLIENT_ID": "c", "DINGTALK_CLIENT_SECRET": "s",
             "LOOP_DINGTALK_CONVERSATIONS": "cid-7", "LOOP_DINGTALK_DM_USERS": "staff-1"},
            http=http,
        )
        self.assertEqual(dt.send("t", "b"), "K")
        self.assertTrue(any("oToMessages" in u for u in calls))
        self.assertFalse(any("groupMessages" in u for u in calls))

    def test_dm_users_are_split(self):
        env = dingtalk.load_env([], environ={"LOOP_DINGTALK_DM_USERS": "a, b"})
        self.assertEqual(dingtalk.dm_users(env), ["a", "b"])

    def test_no_dm_users_by_default(self):
        self.assertEqual(dingtalk.dm_users({}), [])

    def test_direct_message_uses_the_one_to_one_endpoint(self):
        self.dt.send_dm("staff-1", "t", "b")
        self.assertTrue(any("oToMessages" in c["url"] for c in self.calls))


class WebhookSigning(unittest.TestCase):
    def test_signature_is_appended_when_a_secret_is_set(self):
        url = dingtalk.sign_webhook("https://oapi.dingtalk.com/robot/send?access_token=x",
                                    "SECRET", timestamp_ms=1700000000000)
        self.assertIn("timestamp=1700000000000", url)
        self.assertIn("sign=", url)

    def test_unsigned_when_no_secret(self):
        url = "https://oapi.dingtalk.com/robot/send?access_token=x"
        self.assertEqual(dingtalk.sign_webhook(url, "", timestamp_ms=1), url)

    def test_the_signature_is_stable_for_the_same_inputs(self):
        a = dingtalk.sign_webhook("https://x/y", "S", timestamp_ms=42)
        b = dingtalk.sign_webhook("https://x/y", "S", timestamp_ms=42)
        self.assertEqual(a, b)


class Cards(unittest.TestCase):
    def test_a_question_card_numbers_its_options(self):
        text = dingtalk.question_card("acme/widget", 612, "https://u/612",
                                      "Which way?", ["left", "right"])
        self.assertIn("1. left", text)
        self.assertIn("2. right", text)

    def test_a_question_card_says_how_to_answer(self):
        text = dingtalk.question_card("acme/widget", 612, "https://u/612", "Q?", ["a"])
        self.assertIn("引用回复", text)

    def test_an_open_question_has_no_option_list(self):
        text = dingtalk.question_card("acme/widget", 612, "https://u/612", "Q?", [])
        self.assertNotIn("1.", text)

    def test_the_card_links_the_issue(self):
        text = dingtalk.question_card("acme/widget", 612, "https://u/612", "Q?", [])
        self.assertIn("https://u/612", text)
        self.assertIn("#612", text)


if __name__ == "__main__":
    unittest.main()
