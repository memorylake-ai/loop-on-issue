import json
import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import repos as repos_mod


class Registry(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-repos-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "repos.json")

    def _write(self, payload):
        with open(self.path, "w") as fh:
            json.dump(payload, fh)

    def test_an_absent_registry_is_empty_not_an_error(self):
        # One bot serving one repo never needs this file.
        reg = repos_mod.Registry.load(self.path)
        self.assertEqual(reg.names(), [])
        self.assertIsNone(reg.default)

    def test_entries_carry_a_project_path_and_a_checkout(self):
        self._write({"default": "loop", "repos": {
            "loop": {"repo": "acme/widget", "path": "/checkouts/widget"}}})
        entry = repos_mod.Registry.load(self.path).get("loop")
        self.assertEqual((entry.name, entry.repo, entry.path),
                         ("loop", "acme/widget", "/checkouts/widget"))

    def test_lookup_by_alias_or_by_project_path(self):
        # People say "bloom" in chat and "org/bloom" in a config file; both have to
        # resolve, or the approver has to remember which spelling this bot wants.
        self._write({"repos": {"bloom": {"repo": "org/bloom", "path": "/c/bloom"}}})
        reg = repos_mod.Registry.load(self.path)
        self.assertEqual(reg.get("bloom").repo, "org/bloom")
        self.assertEqual(reg.get("org/bloom").repo, "org/bloom")

    def test_lookup_is_case_insensitive(self):
        self._write({"repos": {"Bloom": {"repo": "org/bloom", "path": "/c/bloom"}}})
        self.assertIsNotNone(repos_mod.Registry.load(self.path).get("bloom"))

    def test_an_unknown_name_is_none_rather_than_a_guess(self):
        self._write({"repos": {"bloom": {"repo": "org/bloom", "path": "/c/bloom"}}})
        self.assertIsNone(repos_mod.Registry.load(self.path).get("blorm"))

    def test_the_default_is_named_explicitly(self):
        self._write({"default": "bloom", "repos": {
            "bloom": {"repo": "org/bloom", "path": "/c/bloom"},
            "other": {"repo": "org/other", "path": "/c/other"}}})
        self.assertEqual(repos_mod.Registry.load(self.path).default.name, "bloom")

    def test_a_single_entry_is_the_default_without_being_told(self):
        self._write({"repos": {"only": {"repo": "org/only", "path": "/c/only"}}})
        self.assertEqual(repos_mod.Registry.load(self.path).default.name, "only")

    def test_several_entries_and_no_stated_default_has_no_default(self):
        # Picking one arbitrarily would file work in the wrong repository silently.
        self._write({"repos": {
            "a": {"repo": "org/a", "path": "/c/a"}, "b": {"repo": "org/b", "path": "/c/b"}}})
        self.assertIsNone(repos_mod.Registry.load(self.path).default)

    def test_a_named_default_that_does_not_exist_is_ignored(self):
        self._write({"default": "gone", "repos": {
            "a": {"repo": "org/a", "path": "/c/a"}, "b": {"repo": "org/b", "path": "/c/b"}}})
        self.assertIsNone(repos_mod.Registry.load(self.path).default)

    def test_add_and_save_round_trip(self):
        reg = repos_mod.Registry.load(self.path)
        reg.add("bloom", "org/bloom", "/c/bloom")
        reg.set_default("bloom")
        reg.save(self.path)
        again = repos_mod.Registry.load(self.path)
        self.assertEqual(again.default.repo, "org/bloom")

    def test_an_implicit_default_can_be_made_explicit(self):
        # A lone repository is the default without being told, but that is not
        # persisted — so anything that adds a second one has to write the old
        # default down first, or the newcomer silently takes over.
        reg = repos_mod.Registry.load(self.path)
        reg.add("first", "org/first", "/c/first")
        self.assertEqual(reg.default.name, "first")
        reg.set_default("first")
        reg.add("second", "org/second", "/c/second")
        reg.save(self.path)
        self.assertEqual(repos_mod.Registry.load(self.path).default.name, "first")

    def test_remove(self):
        self._write({"repos": {"a": {"repo": "org/a", "path": "/c/a"}}})
        reg = repos_mod.Registry.load(self.path)
        reg.remove("a")
        self.assertEqual(reg.names(), [])

    def test_malformed_json_names_the_file(self):
        with open(self.path, "w") as fh:
            fh.write("{oops")
        with self.assertRaises(repos_mod.RegistryError) as ctx:
            repos_mod.Registry.load(self.path)
        self.assertIn(self.path, str(ctx.exception))

    def test_paths_are_expanded(self):
        self._write({"repos": {"a": {"repo": "org/a", "path": "~/checkouts/a"}}})
        self.assertTrue(repos_mod.Registry.load(self.path).get("a").path.startswith("/"))
