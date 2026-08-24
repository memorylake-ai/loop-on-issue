import json
import os
import unittest

import _bootstrap  # noqa: F401

import gitrepo
from loopkit import config as cfg
from loopkit import scaffold
from loopkit import templates as tpl
from loopkit.models import Repo


def _by_target(actions, needle):
    return next((a for a in actions if needle in a.target), None)


def _by_kind(actions, kind):
    return next((a for a in actions if a.kind == kind), None)


class Planning(unittest.TestCase):
    def setUp(self):
        self.root = gitrepo.make()
        self.addCleanup(gitrepo.destroy, self.root)
        self.config = cfg.Config({"assignee": "muxuan"})

    def test_a_fresh_github_repo_gets_config_and_both_templates(self):
        repo = Repo("github", "github.com", "acme/widget")
        actions = scaffold.plan(self.root, repo, self.config, existing_labels=[])
        self.assertEqual(
            sorted(a.target for a in actions),
            sorted([
                ".github/ISSUE_TEMPLATE/loop-task.md",
                ".github/pull_request_template.md",
                os.path.join(cfg.CONFIG_DIR, cfg.CONFIG_FILE),
                "loop",
            ]),
        )
        self.assertTrue(all(a.pending for a in actions))

    def test_a_gitlab_repo_gets_gitlab_paths(self):
        repo = Repo("gitlab", "gitlab.example.com", "darwin/zootopia")
        targets = [a.target for a in scaffold.plan(self.root, repo, self.config, existing_labels=[])]
        self.assertIn(".gitlab/issue_templates/loop-task.md", targets)
        self.assertIn(".gitlab/merge_request_templates/loop.md", targets)

    def test_existing_files_are_left_alone(self):
        path = os.path.join(self.root, ".github", "pull_request_template.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("ours\n")
        repo = Repo("github", "github.com", "acme/widget")
        action = _by_target(scaffold.plan(self.root, repo, self.config, existing_labels=[]), "pull_request")
        self.assertEqual(action.status, scaffold.EXISTS)
        self.assertFalse(action.pending)

    def test_force_overwrites_but_says_so(self):
        path = os.path.join(self.root, ".github", "pull_request_template.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("ours\n")
        repo = Repo("github", "github.com", "acme/widget")
        action = _by_target(
            scaffold.plan(self.root, repo, self.config, force=True, existing_labels=[]), "pull_request"
        )
        self.assertEqual(action.status, scaffold.OVERWRITE)

    def test_an_existing_queue_label_is_not_recreated(self):
        repo = Repo("github", "github.com", "acme/widget")
        action = _by_kind(scaffold.plan(self.root, repo, self.config, existing_labels=["loop"]), "label")
        self.assertEqual((action.target, action.status), ("loop", scaffold.EXISTS))

    def test_github_issue_template_carries_chooser_metadata(self):
        repo = Repo("github", "github.com", "acme/widget")
        action = _by_target(scaffold.plan(self.root, repo, self.config, existing_labels=[]), "ISSUE_TEMPLATE")
        self.assertTrue(action.content.startswith("---\n"))
        self.assertIn("labels: loop", action.content)

    def test_gitlab_issue_template_has_no_front_matter(self):
        # GitLab renders it as literal text at the top of every issue.
        repo = Repo("gitlab", "gitlab.example.com", "darwin/zootopia")
        action = _by_target(scaffold.plan(self.root, repo, self.config, existing_labels=[]), "issue_templates")
        self.assertFalse(action.content.startswith("---\n"))

    def test_the_language_choice_reaches_the_written_content(self):
        repo = Repo("github", "github.com", "acme/widget")
        action = _by_target(
            scaffold.plan(self.root, repo, self.config, lang="zh", existing_labels=[]), "ISSUE_TEMPLATE"
        )
        self.assertIn("验收标准", action.content)

    def test_planning_writes_nothing(self):
        repo = Repo("github", "github.com", "acme/widget")
        scaffold.plan(self.root, repo, self.config, existing_labels=[])
        self.assertFalse(os.path.exists(os.path.join(self.root, ".github")))


class Applying(unittest.TestCase):
    def setUp(self):
        self.root = gitrepo.make()
        self.addCleanup(gitrepo.destroy, self.root)
        self.config = cfg.Config({"assignee": "muxuan", "queue_label": "loop"})
        self.repo = Repo("github", "github.com", "acme/widget")

    def test_files_land_where_planned(self):
        actions = scaffold.plan(self.root, self.repo, self.config, existing_labels=["loop"])
        scaffold.apply(actions, self.root)
        for action in actions:
            if action.kind in ("config", "template"):
                self.assertTrue(os.path.isfile(os.path.join(self.root, action.target)), action.target)
                self.assertEqual(action.status, scaffold.DONE)

    def test_the_written_config_is_loadable_and_complete(self):
        actions = scaffold.plan(self.root, self.repo, self.config, existing_labels=["loop"])
        scaffold.apply(actions, self.root)
        with open(cfg.default_path(self.root)) as fh:
            written = json.load(fh)
        self.assertEqual(sorted(written), sorted(cfg.DEFAULTS))
        self.assertEqual(cfg.load(self.root).assignee, "muxuan")

    def test_the_written_issue_template_resolves_as_the_repo_template(self):
        actions = scaffold.plan(self.root, self.repo, self.config, existing_labels=["loop"])
        scaffold.apply(actions, self.root)
        resolved = tpl.resolve("issue", self.root, "github")
        self.assertEqual(resolved.source, "forge")
        self.assertTrue(tpl.has_acceptance_criteria(resolved.text))

    def test_only_the_queue_label_is_ever_created(self):
        created = []

        class FakeForge:
            def create_label(self, name, description=""):
                created.append(name)

        actions = scaffold.plan(self.root, self.repo, self.config, existing_labels=[])
        scaffold.apply(actions, self.root, FakeForge())
        self.assertEqual(created, ["loop"])

    def test_a_label_failure_is_recorded_not_raised(self):
        class ExplodingForge:
            def create_label(self, name, description=""):
                raise OSError("nope")

        actions = scaffold.plan(self.root, self.repo, self.config, existing_labels=[])
        scaffold.apply(actions, self.root, ExplodingForge())
        self.assertEqual(_by_kind(actions, "label").status, scaffold.FAILED)

    def test_non_pending_actions_are_untouched(self):
        path = os.path.join(self.root, ".github", "pull_request_template.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("ours\n")
        actions = scaffold.plan(self.root, self.repo, self.config, existing_labels=["loop"])
        scaffold.apply(actions, self.root)
        with open(path) as fh:
            self.assertEqual(fh.read(), "ours\n")


if __name__ == "__main__":
    unittest.main()
