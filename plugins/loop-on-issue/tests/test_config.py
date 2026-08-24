import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import config as cfg


class Defaults(unittest.TestCase):
    def test_every_documented_key_has_a_default(self):
        c = cfg.Config()
        for key in (
            "forge", "repo", "queue_label", "assignee", "base_branch", "push_remote",
            "target_remote", "runner", "max_parallel", "session_timeout", "worktree_dir",
            "template_lang", "verify_command", "env_files", "escalation_command",
            "ask_wait", "intake_ttl",
        ):
            self.assertIn(key, c.data, key)

    def test_sensible_starting_values(self):
        c = cfg.Config()
        self.assertEqual(c.forge, "auto")
        self.assertEqual(c.queue_label, "loop")
        self.assertEqual(c.runner, "claude")
        self.assertEqual(c.max_parallel, 2)
        self.assertEqual(c.env_files, [".env"])
        self.assertIsNone(c.verify_command)

    def test_attribute_access_matches_the_dict(self):
        c = cfg.Config({"queue_label": "queue"})
        self.assertEqual(c.queue_label, c.data["queue_label"])

    def test_unknown_attribute_still_raises(self):
        with self.assertRaises(AttributeError):
            cfg.Config().nonesuch


class Overrides(unittest.TestCase):
    def test_file_values_beat_defaults(self):
        c = cfg.Config({"queue_label": "queue", "max_parallel": 5})
        self.assertEqual(c.queue_label, "queue")
        self.assertEqual(c.max_parallel, 5)

    def test_explicit_overrides_beat_file_values(self):
        c = cfg.Config({"runner": "codex"}).with_overrides(runner="claude")
        self.assertEqual(c.runner, "claude")

    def test_none_overrides_are_ignored_so_unset_flags_do_not_erase_config(self):
        c = cfg.Config({"assignee": "muxuan"}).with_overrides(assignee=None)
        self.assertEqual(c.assignee, "muxuan")

    def test_overrides_do_not_mutate_the_original(self):
        base = cfg.Config({"runner": "codex"})
        base.with_overrides(runner="claude")
        self.assertEqual(base.runner, "codex")


class Validation(unittest.TestCase):
    def test_unknown_keys_are_reported_but_not_fatal(self):
        # Forward compatibility: a config written by a newer plugin must not stop
        # an older one from running.
        c = cfg.Config({"future_key": 1})
        self.assertEqual(c.unknown, ["future_key"])

    def test_bad_enum_names_the_allowed_values(self):
        with self.assertRaises(cfg.ConfigError) as ctx:
            cfg.Config({"forge": "bitbucket"}).validate()
        self.assertIn("github", str(ctx.exception))

    def test_bad_runner_is_rejected(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.Config({"runner": "gpt"}).validate()

    def test_max_parallel_must_be_a_positive_int(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.Config({"max_parallel": 0}).validate()
        with self.assertRaises(cfg.ConfigError):
            cfg.Config({"max_parallel": "two"}).validate()

    def test_env_files_must_be_a_list(self):
        with self.assertRaises(cfg.ConfigError):
            cfg.Config({"env_files": ".env"}).validate()

    def test_defaults_validate(self):
        cfg.Config().validate()


class Loading(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def _write(self, payload, where=None):
        d = os.path.join(where or self.root, cfg.CONFIG_DIR)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, cfg.CONFIG_FILE)
        with open(path, "w") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def test_missing_file_yields_defaults_and_no_path(self):
        c = cfg.load(self.root)
        self.assertIsNone(c.path)
        self.assertEqual(c.queue_label, "loop")

    def test_reads_the_file_when_present(self):
        path = self._write({"queue_label": "queue"})
        c = cfg.load(self.root)
        self.assertEqual(c.path, path)
        self.assertEqual(c.queue_label, "queue")

    def test_searches_upwards_from_a_subdirectory(self):
        self._write({"queue_label": "queue"})
        deep = os.path.join(self.root, "a", "b", "c")
        os.makedirs(deep)
        self.assertEqual(cfg.load(deep).queue_label, "queue")

    def test_malformed_json_names_the_file(self):
        path = self._write("{not json")
        with self.assertRaises(cfg.ConfigError) as ctx:
            cfg.load(self.root)
        self.assertIn(path, str(ctx.exception))

    def test_non_object_json_is_rejected(self):
        self._write("[1, 2]")
        with self.assertRaises(cfg.ConfigError):
            cfg.load(self.root)

    def test_dump_round_trips_through_load(self):
        c = cfg.Config({"queue_label": "queue", "runner": "codex"})
        target = os.path.join(self.root, cfg.CONFIG_DIR, cfg.CONFIG_FILE)
        cfg.save(c, target)
        self.assertEqual(cfg.load(self.root).queue_label, "queue")

    def test_saved_file_spells_out_every_key(self):
        # The written file is the documentation: someone opening it should see the
        # full vocabulary, not just what happens to differ from the defaults.
        target = os.path.join(self.root, cfg.CONFIG_DIR, cfg.CONFIG_FILE)
        cfg.save(cfg.Config(), target)
        with open(target) as fh:
            written = json.load(fh)
        self.assertEqual(sorted(written), sorted(cfg.DEFAULTS))


if __name__ == "__main__":
    unittest.main()
