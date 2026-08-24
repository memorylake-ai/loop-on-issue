"""The manifests and file layout both runtimes will try to load.

A broken manifest fails at install time on someone else's machine, which is the
worst place to find out. These are cheap structural assertions that a marketplace
repository should never be able to publish without.
"""

import json
import os
import re
import unittest

import _bootstrap  # noqa: F401

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(PLUGIN))

_FRONT_MATTER = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)


def read_json(*parts):
    with open(os.path.join(*parts)) as fh:
        return json.load(fh)


def front_matter(path):
    """Parse the flat `key: value` front matter a skill or command declares."""
    with open(path) as fh:
        text = fh.read()
    m = _FRONT_MATTER.match(text)
    if not m:
        return None
    fields = {}
    key = None
    for line in m.group("body").splitlines():
        if re.match(r"^\w[\w-]*:", line):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"')
        elif key:
            fields[key] += " " + line.strip()
    return fields


class Marketplaces(unittest.TestCase):
    def test_claude_marketplace_points_at_a_real_plugin(self):
        data = read_json(REPO, ".claude-plugin", "marketplace.json")
        self.assertTrue(data["plugins"])
        for entry in data["plugins"]:
            self.assertTrue(os.path.isdir(os.path.join(REPO, entry["source"])), entry["source"])

    def test_codex_marketplace_points_at_a_real_plugin(self):
        data = read_json(REPO, ".agents", "plugins", "marketplace.json")
        for entry in data["plugins"]:
            path = entry["source"]["path"]
            self.assertTrue(os.path.isdir(os.path.join(REPO, path)), path)

    def test_both_marketplaces_list_the_same_plugins(self):
        claude = {p["name"] for p in read_json(REPO, ".claude-plugin", "marketplace.json")["plugins"]}
        codex = {p["name"] for p in read_json(REPO, ".agents", "plugins", "marketplace.json")["plugins"]}
        self.assertEqual(claude, codex)


class Manifests(unittest.TestCase):
    def setUp(self):
        self.claude = read_json(PLUGIN, ".claude-plugin", "plugin.json")
        self.codex = read_json(PLUGIN, ".codex-plugin", "plugin.json")

    def test_names_and_versions_agree(self):
        self.assertEqual(self.claude["name"], self.codex["name"])
        self.assertEqual(self.claude["version"], self.codex["version"])

    def test_the_marketplace_entry_agrees_with_the_manifest(self):
        entry = read_json(REPO, ".claude-plugin", "marketplace.json")["plugins"][0]
        self.assertEqual(entry["name"], self.claude["name"])
        self.assertEqual(entry["version"], self.claude["version"])

    def test_the_plugin_directory_is_named_for_the_plugin(self):
        self.assertEqual(os.path.basename(PLUGIN), self.claude["name"])

    def test_codex_skill_path_exists(self):
        self.assertTrue(os.path.isdir(os.path.join(PLUGIN, self.codex["skills"])))


class Skills(unittest.TestCase):
    def skill_dirs(self):
        root = os.path.join(PLUGIN, "skills")
        return [os.path.join(root, n) for n in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, n))]

    def test_there_are_skills(self):
        self.assertTrue(self.skill_dirs())

    def test_every_skill_has_front_matter_naming_itself(self):
        for path in self.skill_dirs():
            skill = os.path.join(path, "SKILL.md")
            self.assertTrue(os.path.isfile(skill), skill)
            fields = front_matter(skill)
            self.assertIsNotNone(fields, skill)
            # The name is the addressable identity; a mismatch means the skill
            # cannot be invoked by the name its directory implies.
            self.assertEqual(fields.get("name"), os.path.basename(path), skill)

    def test_every_skill_describes_when_to_use_it(self):
        for path in self.skill_dirs():
            description = front_matter(os.path.join(path, "SKILL.md")).get("description", "")
            self.assertGreater(len(description), 120, path)
            # The description is the only thing a runtime sees when deciding
            # whether to load the skill, so it has to say when to.
            self.assertTrue(
                "Use this skill" in description or "Trigger this skill" in description, path
            )

    def test_skills_resolve_the_cli_before_using_it(self):
        # Every skill shells out to `loop`; one that assumes it is on PATH fails
        # on a machine where the plugin was installed but never symlinked.
        for path in self.skill_dirs():
            with open(os.path.join(path, "SKILL.md")) as fh:
                text = fh.read()
            if '"$LOOP"' in text:
                self.assertIn("PLUGIN_ROOT", text, path)


class Commands(unittest.TestCase):
    def command_files(self):
        root = os.path.join(PLUGIN, "commands")
        return [os.path.join(root, n) for n in sorted(os.listdir(root)) if n.endswith(".md")]

    def test_every_command_declares_a_description(self):
        for path in self.command_files():
            fields = front_matter(path)
            self.assertIsNotNone(fields, path)
            self.assertTrue(fields.get("description"), path)

    def test_every_command_names_a_skill_that_exists(self):
        known = set(os.listdir(os.path.join(PLUGIN, "skills")))
        for path in self.command_files():
            with open(path) as fh:
                text = fh.read()
            named = [name for name in known if "`{}`".format(name) in text]
            self.assertTrue(named, "{} names no known skill".format(path))


class Assets(unittest.TestCase):
    def test_bundled_templates_exist_for_every_language_and_kind(self):
        for lang in ("en", "zh"):
            for kind in ("issue", "pr"):
                path = os.path.join(PLUGIN, "templates", lang, "{}.md".format(kind))
                self.assertTrue(os.path.isfile(path), path)

    def test_the_cli_entry_point_is_executable(self):
        self.assertTrue(os.access(os.path.join(PLUGIN, "scripts", "loop"), os.X_OK))

    def test_the_test_runner_is_executable(self):
        self.assertTrue(os.access(os.path.join(PLUGIN, "run-tests.sh"), os.X_OK))


if __name__ == "__main__":
    unittest.main()
