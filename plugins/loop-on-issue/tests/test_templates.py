import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import templates as tpl


def _write(root, relpath, text="body\n"):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


class Candidates(unittest.TestCase):
    def test_override_is_first_on_both_forges(self):
        for forge in ("github", "gitlab"):
            first = tpl.candidates("issue", forge)[0]
            self.assertTrue(first.startswith(".loop-on-issue/templates/"), forge)

    def test_github_issue_candidates_are_github_native(self):
        paths = tpl.candidates("issue", "github")
        self.assertIn(".github/ISSUE_TEMPLATE/loop-task.md", paths)
        self.assertTrue(all(".gitlab/" not in p for p in paths))

    def test_gitlab_issue_candidates_are_gitlab_native(self):
        paths = tpl.candidates("issue", "gitlab")
        self.assertIn(".gitlab/issue_templates/loop-task.md", paths)
        self.assertTrue(all(".github/" not in p for p in paths))

    def test_github_pr_candidates_cover_the_usual_locations(self):
        paths = tpl.candidates("pr", "github")
        self.assertIn(".github/pull_request_template.md", paths)
        self.assertIn("docs/pull_request_template.md", paths)

    def test_gitlab_mr_candidates(self):
        self.assertIn(".gitlab/merge_request_templates/loop.md", tpl.candidates("pr", "gitlab"))

    def test_unknown_kind_is_an_error(self):
        with self.assertRaises(ValueError):
            tpl.candidates("changelog", "github")


class Resolve(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)

    def test_falls_back_to_the_bundled_default(self):
        r = tpl.resolve("issue", self.root, "github", lang="en")
        self.assertEqual(r.source, "bundled")
        self.assertTrue(r.text.strip())

    def test_forge_native_beats_bundled(self):
        path = _write(self.root, ".github/ISSUE_TEMPLATE/loop-task.md", "native\n")
        r = tpl.resolve("issue", self.root, "github", lang="en")
        self.assertEqual((r.source, r.path, r.text), ("forge", path, "native\n"))

    def test_override_beats_forge_native(self):
        _write(self.root, ".github/ISSUE_TEMPLATE/loop-task.md", "native\n")
        path = _write(self.root, ".loop-on-issue/templates/issue.md", "mine\n")
        r = tpl.resolve("issue", self.root, "github", lang="en")
        self.assertEqual((r.source, r.path, r.text), ("override", path, "mine\n"))

    def test_a_gitlab_template_does_not_satisfy_a_github_repo(self):
        _write(self.root, ".gitlab/issue_templates/loop-task.md", "wrong forge\n")
        self.assertEqual(tpl.resolve("issue", self.root, "github", lang="en").source, "bundled")

    def test_zh_bundle_differs_from_en(self):
        en = tpl.resolve("issue", self.root, "github", lang="en").text
        zh = tpl.resolve("issue", self.root, "github", lang="zh").text
        self.assertNotEqual(en, zh)

    def test_pr_template_resolves_too(self):
        r = tpl.resolve("pr", self.root, "gitlab", lang="en")
        self.assertEqual(r.source, "bundled")
        self.assertIn("closes", r.text.lower())

    def test_empty_file_is_skipped_rather_than_winning(self):
        # An empty template is almost always a leftover touch(1), and honouring it
        # would hand the agent nothing to fill in.
        _write(self.root, ".github/ISSUE_TEMPLATE/loop-task.md", "   \n")
        self.assertEqual(tpl.resolve("issue", self.root, "github", lang="en").source, "bundled")


class BundledContent(unittest.TestCase):
    def test_issue_templates_carry_the_marker(self):
        for lang in ("en", "zh"):
            text = tpl.bundled("issue", lang)
            self.assertIn(tpl.TEMPLATE_MARKER, text, lang)

    def test_issue_templates_have_every_slot_the_swarm_reads(self):
        for lang in ("en", "zh"):
            found = tpl.slots(tpl.bundled("issue", lang))
            self.assertEqual(len(found), 6, "{}: {}".format(lang, found))

    def test_acceptance_criteria_slot_is_detectable(self):
        # Omit it and an issue is done when the session decides it is.
        self.assertTrue(tpl.has_acceptance_criteria(tpl.bundled("issue", "en")))
        self.assertTrue(tpl.has_acceptance_criteria(tpl.bundled("issue", "zh")))

    def test_a_generic_template_is_not_mistaken_for_a_loop_one(self):
        self.assertFalse(tpl.is_loop_template("## Description\n\nWhat changed?\n"))

    def test_pr_templates_reference_the_closing_keyword(self):
        for lang in ("en", "zh"):
            self.assertIn("closes #", tpl.bundled("pr", lang))

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(tpl.bundled("issue", "fr"), tpl.bundled("issue", "en"))


class FrontMatter(unittest.TestCase):
    def test_stripped_from_the_body(self):
        text = "---\nname: Loop task\n---\n\n### One\n"
        self.assertEqual(tpl.strip_front_matter(text), "### One\n")

    def test_absent_front_matter_is_a_no_op(self):
        self.assertEqual(tpl.strip_front_matter("### One\n"), "### One\n")

    def test_only_the_leading_block_is_stripped(self):
        # A `---` horizontal rule further down is content, not metadata.
        text = "---\nname: x\n---\n\n### One\n\n---\n\n### Two\n"
        self.assertIn("---", tpl.strip_front_matter(text))

    def test_resolved_body_drops_front_matter(self):
        r = tpl.Resolved("issue", "forge", None, "---\nname: x\n---\n\n### One\n")
        self.assertEqual(r.body, "### One\n")

    def test_github_issue_scaffold_gets_chooser_metadata(self):
        fm = tpl.front_matter_for("issue", "loop")
        self.assertTrue(fm.startswith("---\n"))
        self.assertIn("labels: loop", fm)

    def test_gitlab_gets_no_metadata_even_for_issues(self):
        # GitLab has no template chooser and renders this as literal text at the
        # top of every issue.
        self.assertEqual(tpl.front_matter_for("issue", "loop", "gitlab"), "")

    def test_pr_scaffold_gets_no_metadata(self):
        # GitHub reads no front matter on a pull request template, and GitLab
        # would render it as literal text at the top of every description.
        self.assertEqual(tpl.front_matter_for("pr", "loop"), "")

    def test_scaffold_targets_exist_for_both_forges(self):
        for forge in ("github", "gitlab"):
            for kind in tpl.KINDS:
                target = tpl.SCAFFOLD_TARGET[forge][kind]
                self.assertIn(target, tpl.candidates(kind, forge), (forge, kind))


class Slots(unittest.TestCase):
    def test_reads_markdown_headings(self):
        text = "### One\na\n\n### Two\nb\n"
        self.assertEqual(tpl.slots(text), ["One", "Two"])

    def test_ignores_hashes_inside_fenced_code(self):
        text = "### One\n\n```sh\n# not a heading\n```\n\n### Two\n"
        self.assertEqual(tpl.slots(text), ["One", "Two"])

    def test_ignores_yaml_front_matter(self):
        text = "---\nname: Loop task\nabout: x\n---\n\n### One\n"
        self.assertEqual(tpl.slots(text), ["One"])


if __name__ == "__main__":
    unittest.main()
