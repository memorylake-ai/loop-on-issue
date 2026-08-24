import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401

from loopkit import link as link_mod


class Target(unittest.TestCase):
    def test_prefers_a_directory_already_on_path(self):
        # Creating a link somewhere PATH does not reach is worse than not
        # creating one: it looks done and changes nothing.
        chosen = link_mod.choose_dir(["/opt/x/bin", "/home/me/.local/bin"],
                                     path="/usr/bin:/home/me/.local/bin",
                                     exists=lambda p: True)
        self.assertEqual(chosen, "/home/me/.local/bin")

    def test_falls_back_to_the_first_candidate_when_none_are_on_path(self):
        chosen = link_mod.choose_dir(["/home/me/.local/bin", "/opt/x/bin"],
                                     path="/usr/bin", exists=lambda p: True)
        self.assertEqual(chosen, "/home/me/.local/bin")

    def test_a_candidate_that_does_not_exist_is_still_usable(self):
        # ~/.local/bin is conventional and often simply not created yet.
        chosen = link_mod.choose_dir(["/home/me/.local/bin"], path="/usr/bin",
                                     exists=lambda p: False)
        self.assertEqual(chosen, "/home/me/.local/bin")

    def test_on_path_is_matched_exactly_not_by_prefix(self):
        chosen = link_mod.choose_dir(["/home/me/.local/bin"],
                                     path="/home/me/.local/binaries",
                                     exists=lambda p: True)
        self.assertEqual(chosen, "/home/me/.local/bin")  # fallback, not a match


class Installing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loop-link-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = os.path.join(self.dir, "bin")
        self.source = os.path.join(self.dir, "scripts", "loop")
        os.makedirs(os.path.dirname(self.source))
        with open(self.source, "w") as fh:
            fh.write("#!/bin/sh\necho hi\n")
        os.chmod(self.source, 0o755)

    def test_creates_the_directory_and_the_link(self):
        result = link_mod.install(self.source, self.bin)
        self.assertEqual(result.status, link_mod.CREATED)
        self.assertTrue(os.path.islink(result.path))
        self.assertEqual(os.path.realpath(result.path), os.path.realpath(self.source))

    def test_the_link_is_executable_through_the_link(self):
        result = link_mod.install(self.source, self.bin)
        self.assertTrue(os.access(result.path, os.X_OK))

    def test_re_running_is_a_no_op(self):
        link_mod.install(self.source, self.bin)
        result = link_mod.install(self.source, self.bin)
        self.assertEqual(result.status, link_mod.ALREADY)

    def test_a_link_pointing_elsewhere_is_repointed(self):
        # The plugin cache path changes on every version bump, so a stale link is
        # the normal state after an upgrade, not an anomaly.
        os.makedirs(self.bin)
        other = os.path.join(self.dir, "old-loop")
        with open(other, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.symlink(other, os.path.join(self.bin, "loop"))
        result = link_mod.install(self.source, self.bin)
        self.assertEqual(result.status, link_mod.REPOINTED)
        self.assertEqual(os.path.realpath(result.path), os.path.realpath(self.source))

    def test_a_real_file_in_the_way_is_refused_not_clobbered(self):
        # Somebody may have their own `loop` on PATH. Overwriting it silently
        # would be the worst possible outcome of a convenience feature.
        os.makedirs(self.bin)
        occupied = os.path.join(self.bin, "loop")
        with open(occupied, "w") as fh:
            fh.write("#!/bin/sh\necho not ours\n")
        result = link_mod.install(self.source, self.bin)
        self.assertEqual(result.status, link_mod.BLOCKED)
        with open(occupied) as fh:
            self.assertIn("not ours", fh.read())

    def test_an_unwritable_directory_reports_rather_than_raises(self):
        result = link_mod.install(self.source, "/proc/nonexistent-loop-bin")
        self.assertEqual(result.status, link_mod.FAILED)
        self.assertTrue(result.detail)

    def test_remove(self):
        link_mod.install(self.source, self.bin)
        self.assertTrue(link_mod.remove(self.bin))
        self.assertFalse(os.path.exists(os.path.join(self.bin, "loop")))

    def test_remove_leaves_a_file_that_is_not_ours_alone(self):
        os.makedirs(self.bin)
        occupied = os.path.join(self.bin, "loop")
        with open(occupied, "w") as fh:
            fh.write("mine\n")
        self.assertFalse(link_mod.remove(self.bin))
        self.assertTrue(os.path.exists(occupied))


if __name__ == "__main__":
    unittest.main()
