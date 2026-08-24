import os
import shutil
import subprocess
import tempfile
import unittest

import _bootstrap  # noqa: F401

import gitrepo
from loopkit import discovery
from loopkit import repos as repos_mod


class VerifyCandidates(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-vc-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, relpath, body=""):
        path = os.path.join(self.dir, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)

    def test_a_cicd_check_all_script_is_offered_first(self):
        self._write("cicd/check-all-locally.sh", "#!/bin/sh\n")
        candidates = discovery.verify_candidates(self.dir)
        self.assertEqual(candidates[0], "./cicd/check-all-locally.sh")

    def test_a_make_test_target_is_offered(self):
        self._write("Makefile", "test:\n\t./run\n\nlint:\n\t./lint\n")
        self.assertIn("make test", discovery.verify_candidates(self.dir))

    def test_an_npm_test_script_is_offered(self):
        self._write("package.json", '{"scripts": {"test": "jest"}}')
        self.assertIn("npm test", discovery.verify_candidates(self.dir))

    def test_pytest_when_pyproject_and_a_tests_dir_exist(self):
        self._write("pyproject.toml", "[project]\nname='x'\n")
        os.makedirs(os.path.join(self.dir, "tests"))
        self.assertIn("python -m pytest", discovery.verify_candidates(self.dir))

    def test_nothing_recognisable_yields_no_candidates(self):
        self.assertEqual(discovery.verify_candidates(self.dir), [])


class Discover(unittest.TestCase):
    def setUp(self):
        self.container = tempfile.mkdtemp(prefix="loop-workspace-")
        self.addCleanup(shutil.rmtree, self.container, True)

    def _repo(self, name, remote):
        return gitrepo.make_at(os.path.join(self.container, name), remote_url=remote, commit=True)

    def _by_name(self, registry=None):
        found = discovery.discover(self.container, registry=registry or repos_mod.Registry())
        return {r["name"]: r for r in found}

    def test_lists_each_git_repo_with_its_forge_and_project_path(self):
        self._repo("widget", "git@github.com:acme/widget.git")
        self._repo("proj", "git@gitlab.example.com:grp/proj.git")
        found = self._by_name()
        self.assertEqual(set(found), {"widget", "proj"})
        self.assertEqual((found["widget"]["forge"], found["widget"]["repo"]),
                         ("github", "acme/widget"))
        self.assertEqual(found["proj"]["forge"], "gitlab")

    def test_a_plain_directory_that_is_not_a_repo_is_skipped(self):
        self._repo("widget", "git@github.com:acme/widget.git")
        os.makedirs(os.path.join(self.container, "notes"))
        self.assertEqual(set(self._by_name()), {"widget"})

    def test_a_repo_already_in_the_registry_is_marked(self):
        path = self._repo("widget", "git@github.com:acme/widget.git")
        reg = repos_mod.Registry()
        reg.add("widget", "acme/widget", path)
        self.assertTrue(self._by_name(registry=reg)["widget"]["registered"])
        self.assertFalse(self._by_name()["widget"]["registered"])

    def test_a_repo_that_already_has_config_is_marked(self):
        path = self._repo("widget", "git@github.com:acme/widget.git")
        os.makedirs(os.path.join(path, ".loop-on-issue"))
        with open(os.path.join(path, ".loop-on-issue", "config.json"), "w") as fh:
            fh.write("{}")
        self.assertTrue(self._by_name()["widget"]["has_config"])

    def test_reports_the_default_branch_when_the_remote_head_is_known(self):
        path = self._repo("widget", "git@github.com:acme/widget.git")
        subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD",
                        "refs/remotes/origin/master"],
                       cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(self._by_name()["widget"]["default_branch"], "master")

    def test_a_missing_container_is_empty_not_an_error(self):
        self.assertEqual(discovery.discover(os.path.join(self.container, "nope"),
                                            registry=repos_mod.Registry()), [])


if __name__ == "__main__":
    unittest.main()
