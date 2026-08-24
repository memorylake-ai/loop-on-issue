import json
import os
import unittest

import _bootstrap  # noqa: F401

import gitrepo
from fakecli import FakeCLI
from loopkit import config as cfg
from loopkit import doctor


def _status(report, check_id):
    for check in report.checks:
        if check.id == check_id:
            return check
    return None


class Doctoring(unittest.TestCase):
    def setUp(self):
        self.root = gitrepo.make()
        self.addCleanup(gitrepo.destroy, self.root)
        self.cli = FakeCLI()
        self.addCleanup(self.cli.cleanup)

    def _happy_routes(self, labels=("loop",), assignees=("muxuan",)):
        self.cli.route("auth status", stdout="github.com\n  Token scopes: 'repo', 'read:org'\n")
        self.cli.route("api", "--version", stdout="gh version 2.82.1\n")
        self.cli.route("--version", stdout="gh version 2.82.1\n")
        self.cli.route("api", "/labels", stdout=[{"name": n} for n in labels])
        self.cli.route("api", "/assignees", stdout=[{"login": n} for n in assignees])
        self.cli.route("api", "repos/acme/widget", stdout={"permissions": {"push": True}})

    def _config(self, **kwargs):
        base = {"assignee": "muxuan", "base_branch": "HEAD", "verify_command": "pytest"}
        base.update(kwargs)
        return cfg.Config(base)

    # -- outside a repository ------------------------------------------------
    def test_outside_a_git_repository_it_stops_immediately(self):
        report = doctor.diagnose("/", self._config())
        self.assertEqual(_status(report, "git.repo").status, doctor.FAIL)
        self.assertEqual(report.exit_code, 2)

    # -- the CLI -------------------------------------------------------------
    def test_missing_cli_is_a_hard_failure_with_an_install_line(self):
        self.cli.cleanup()
        self.cli = FakeCLI(names=("glab",))  # a machine with no gh installed
        self.addCleanup(self.cli.cleanup)
        report = doctor.diagnose(self.root, self._config())
        check = _status(report, "cli.installed")
        self.assertEqual(check.status, doctor.FAIL)
        self.assertTrue(check.fix)

    def test_a_missing_cli_short_circuits_the_forge_checks(self):
        # Nothing downstream can be answered without it, and inventing answers
        # would bury the one problem worth reporting.
        self.cli.cleanup()
        self.cli = FakeCLI(names=("glab",))
        self.addCleanup(self.cli.cleanup)
        report = doctor.diagnose(self.root, self._config())
        self.assertIsNone(_status(report, "repo.access"))

    def test_unauthenticated_points_at_the_login_command(self):
        self.cli.route("auth status", exit=1, stderr="You are not logged into any GitHub hosts\n")
        report = doctor.diagnose(self.root, self._config())
        check = _status(report, "cli.auth")
        self.assertEqual(check.status, doctor.FAIL)
        self.assertIn("gh auth login", check.fix)

    def test_a_token_without_repo_scope_is_caught_before_it_fails_a_write(self):
        self.cli.route("auth status", stdout="github.com\n  Token scopes: 'gist', 'read:org'\n")
        self.cli.route("api", "/labels", stdout=[{"name": "loop"}])
        self.cli.route("api", "/assignees", stdout=[{"login": "muxuan"}])
        self.cli.route("api", "repos/acme/widget", stdout={"permissions": {"push": True}})
        report = doctor.diagnose(self.root, self._config())
        check = _status(report, "cli.scopes")
        self.assertEqual(check.status, doctor.FAIL)
        self.assertIn("gh auth refresh", check.fix)

    # -- the board -----------------------------------------------------------
    def test_missing_queue_label_is_a_failure(self):
        # Without it every scan comes back empty and looks like an idle queue.
        self._happy_routes(labels=("bug",))
        report = doctor.diagnose(self.root, self._config())
        check = _status(report, "labels.queue")
        self.assertEqual(check.status, doctor.FAIL)
        self.assertIn("loop init", check.fix)

    def test_unresolvable_assignee_is_a_failure(self):
        self._happy_routes(assignees=("someone-else",))
        report = doctor.diagnose(self.root, self._config())
        self.assertEqual(_status(report, "assignee.set").status, doctor.FAIL)

    def test_unset_assignee_is_only_a_warning(self):
        self._happy_routes()
        report = doctor.diagnose(self.root, self._config(assignee=None))
        self.assertEqual(_status(report, "assignee.set").status, doctor.WARN)

    def test_read_only_access_is_a_failure(self):
        self.cli.route("auth status", stdout="github.com\n  Token scopes: 'repo'\n")
        self.cli.route("api", "/labels", stdout=[{"name": "loop"}])
        self.cli.route("api", "/assignees", stdout=[{"login": "muxuan"}])
        self.cli.route("api", "repos/acme/widget", stdout={"permissions": {"push": False, "pull": True}})
        report = doctor.diagnose(self.root, self._config())
        self.assertEqual(_status(report, "repo.access").status, doctor.FAIL)

    # -- templates and config ------------------------------------------------
    def test_falling_back_to_the_bundled_template_is_a_warning(self):
        self._happy_routes()
        report = doctor.diagnose(self.root, self._config())
        check = _status(report, "template.issue")
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn(".github/ISSUE_TEMPLATE/loop-task.md", check.fix)

    def test_a_repo_template_without_acceptance_criteria_is_flagged(self):
        self._happy_routes()
        path = os.path.join(self.root, ".github", "ISSUE_TEMPLATE", "loop-task.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("### Description\n\nwhat happened?\n")
        report = doctor.diagnose(self.root, self._config())
        check = _status(report, "template.issue")
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("acceptance", check.detail)

    def test_unknown_config_keys_warn_rather_than_stop_the_run(self):
        self._happy_routes()
        report = doctor.diagnose(self.root, cfg.Config({"assignee": "muxuan", "base_branch": "HEAD",
                                                        "verify_command": "pytest", "from_the_future": 1}))
        self.assertEqual(_status(report, "config.unknown").status, doctor.WARN)

    def test_missing_verify_command_warns(self):
        self._happy_routes()
        report = doctor.diagnose(self.root, self._config(verify_command=None))
        self.assertEqual(_status(report, "verify.command").status, doctor.WARN)

    def test_unresolvable_base_branch_is_a_failure(self):
        self._happy_routes()
        report = doctor.diagnose(self.root, self._config(base_branch="upstream/nonesuch"))
        check = _status(report, "git.base")
        self.assertEqual(check.status, doctor.FAIL)
        self.assertIn("git fetch upstream", check.fix)

    # -- overall -------------------------------------------------------------
    def test_a_healthy_repo_reports_no_failures(self):
        self._happy_routes()
        path = os.path.join(self.root, ".github", "ISSUE_TEMPLATE", "loop-task.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("### Acceptance criteria\n\n1. it works\n")
        with open(os.path.join(self.root, ".github", "pull_request_template.md"), "w") as fh:
            fh.write("### Motivation\n")
        report = doctor.diagnose(self.root, self._config())
        self.assertEqual(report.failures, [], [c.as_dict() for c in report.failures])
        self.assertEqual(report.exit_code, 0)

    def test_report_serialises_for_machine_readers(self):
        self._happy_routes()
        payload = json.loads(json.dumps(doctor.diagnose(self.root, self._config()).as_dict()))
        self.assertEqual(payload["repo"]["forge"], "github")
        self.assertTrue(payload["checks"])


if __name__ == "__main__":
    unittest.main()
