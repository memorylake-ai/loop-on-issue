"""The shell wrapper, exercised the way people actually invoke it."""

import os
import shutil
import subprocess
import tempfile
import unittest

import _bootstrap  # noqa: F401

WRAPPER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "loop"
)


def run(path, *args):
    return subprocess.run(
        [path] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True,
    )


class Invocation(unittest.TestCase):
    def setUp(self):
        # realpath, because macOS puts temp dirs under /var, which is itself a
        # symlink to /private/var — a lexically-computed relative path walks the
        # wrong number of `..` through it and the fixture, not the wrapper, breaks.
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="loop-entry-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_direct(self):
        result = run(WRAPPER, "--help")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("doctor", result.stdout)

    def test_through_a_symlink(self):
        # `init --link` puts one on PATH, and "$0" is then the link — whose
        # directory holds no loop_cli.py. Deriving the plugin root from it is how
        # this broke the first time it was used for real.
        link = os.path.join(self.dir, "loop")
        os.symlink(WRAPPER, link)
        result = run(link, "--help")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("doctor", result.stdout)

    def test_through_a_chain_of_symlinks(self):
        first = os.path.join(self.dir, "loop-1")
        second = os.path.join(self.dir, "loop-2")
        os.symlink(WRAPPER, first)
        os.symlink(first, second)
        result = run(second, "--help")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_through_a_relative_symlink(self):
        scripts = os.path.join(self.dir, "scripts")
        os.makedirs(scripts)
        os.symlink(os.path.relpath(WRAPPER, scripts), os.path.join(scripts, "loop"))
        result = run(os.path.join(scripts, "loop"), "--help")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_exit_codes_survive_the_wrapper(self):
        # 0/2/1 are load-bearing for every caller; a wrapper that swallowed them
        # would make every precondition look like success.
        result = run(WRAPPER, "-C", self.dir, "doctor")
        self.assertIn(result.returncode, (0, 2))
        self.assertIn("Git repository", result.stdout)

    def test_a_polluted_interpreter_environment_does_not_stop_it(self):
        # A GUI launcher can export PYTHONHOME into every shell it spawns, which
        # kills any interpreter that is not its own before a line of code runs.
        env = dict(os.environ, PYTHONHOME="/nonexistent", PYTHONPATH="/nonexistent")
        result = subprocess.run(
            [WRAPPER, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
