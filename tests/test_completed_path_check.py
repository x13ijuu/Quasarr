# -*- coding: utf-8 -*-
"""
Tests for the honest-Completed path check (Maja fork, F5).

Upstream reports a package as "Completed" the moment JDownloader finishes, even
though the files still sit on the downloader's working disk. In our topology an
external mover then relocates them to the shared destination (/data/downloads);
until that happens Sonarr would import from an unpopulated path. With COMPLETED_DIR
set, a finished package stays in the queue as "Moving" until its folder actually
appears at the destination, at which point history reports the final path.
"""
import os
import tempfile
import unittest

from quasarr.downloads.packages import (
    completed_destination,
    package_at_destination,
    package_source_gone,
)


class CompletedDestinationEnvTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("COMPLETED_DIR", None)

    def test_unset_is_none_upstream_behaviour(self):
        os.environ.pop("COMPLETED_DIR", None)
        self.assertIsNone(completed_destination())

    def test_set_returns_path(self):
        os.environ["COMPLETED_DIR"] = "/data/downloads"
        self.assertEqual("/data/downloads", completed_destination())

    def test_blank_is_none(self):
        os.environ["COMPLETED_DIR"] = "   "
        self.assertIsNone(completed_destination())


class PackageAtDestinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_present_when_folder_exists(self):
        pkg = "Bleach.S01E57.German.1080p.WEB-DL"
        os.mkdir(os.path.join(self.dest, pkg))
        present, final = package_at_destination(f"/staging/{pkg}", self.dest)
        self.assertTrue(present)
        self.assertEqual(os.path.join(self.dest, pkg), final)

    def test_absent_while_still_on_working_disk(self):
        pkg = "Bleach.S01E58.German.1080p.WEB-DL"
        present, final = package_at_destination(f"/staging/{pkg}", self.dest)
        self.assertFalse(present)
        self.assertEqual(os.path.join(self.dest, pkg), final)

    def test_trailing_slash_and_empty_inputs(self):
        pkg = "Show.S01E01"
        os.mkdir(os.path.join(self.dest, pkg))
        present, _ = package_at_destination(f"/staging/{pkg}/", self.dest)
        self.assertTrue(present)
        self.assertEqual((False, None), package_at_destination("", self.dest))
        self.assertEqual((False, None), package_at_destination("/staging/x", None))


class LocationDecisionTests(unittest.TestCase):
    """The finished->history vs finished->Moving-queue decision (mirrors get_packages)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _decide(self, finished, error, save_to, completed_dir):
        moving = False
        final_storage = None
        if finished and not error and completed_dir:
            present, final_path = package_at_destination(save_to, completed_dir)
            if present:
                final_storage = final_path
            elif package_source_gone(save_to):
                pass  # F5b: gone from working dir AND destination -> Completed
            else:
                moving = True
        location = "queue" if moving else ("history" if error or finished else "queue")
        return location, moving, final_storage

    def test_finished_but_not_moved_stays_queue_moving(self):
        loc, moving, final = self._decide(True, None, "/staging/PkgX", self.dest)
        self.assertEqual("queue", loc)
        self.assertTrue(moving)
        self.assertIsNone(final)

    def test_finished_and_moved_goes_history_with_final_path(self):
        os.mkdir(os.path.join(self.dest, "PkgX"))
        loc, moving, final = self._decide(True, None, "/staging/PkgX", self.dest)
        self.assertEqual("history", loc)
        self.assertFalse(moving)
        self.assertEqual(os.path.join(self.dest, "PkgX"), final)

    def test_failed_always_history_regardless_of_path(self):
        loc, moving, _ = self._decide(True, "some error", "/staging/PkgX", self.dest)
        self.assertEqual("history", loc)
        self.assertFalse(moving)

    def test_without_completed_dir_upstream_behaviour(self):
        # finished -> history immediately (no path gating)
        loc, moving, _ = self._decide(True, None, "/staging/PkgX", None)
        self.assertEqual("history", loc)
        self.assertFalse(moving)

    def test_still_downloading_stays_queue(self):
        loc, moving, _ = self._decide(False, None, "/staging/PkgX", self.dest)
        self.assertEqual("queue", loc)
        self.assertFalse(moving)


class PackageSourceGoneTests(unittest.TestCase):
    """F5b: prove-the-source-is-gone check (terminal state for 'Moving' zombies)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name
        os.environ["DOWNLOADER_WORKING_DIR"] = self.work

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("DOWNLOADER_WORKING_DIR", None)

    def test_env_unset_never_claims_gone(self):
        os.environ.pop("DOWNLOADER_WORKING_DIR", None)
        self.assertFalse(package_source_gone(f"{self.work}/PkgX"))

    def test_working_dir_not_visible_never_claims_gone(self):
        os.environ["DOWNLOADER_WORKING_DIR"] = "/nonexistent-mount"
        self.assertFalse(package_source_gone("/nonexistent-mount/PkgX"))

    def test_save_to_outside_working_dir_never_claims_gone(self):
        self.assertFalse(package_source_gone("/somewhere/else/PkgX"))
        self.assertFalse(package_source_gone(self.work))  # root itself
        # prefix trap: /stagingX must not match root /staging
        self.assertFalse(package_source_gone(self.work + "X/PkgX"))

    def test_missing_package_folder_is_gone(self):
        self.assertTrue(package_source_gone(f"{self.work}/PkgX"))

    def test_folder_with_files_is_not_gone(self):
        pkg = os.path.join(self.work, "PkgX")
        os.makedirs(os.path.join(pkg, "sub"))
        with open(os.path.join(pkg, "sub", "episode.mkv"), "w") as f:
            f.write("x")
        self.assertFalse(package_source_gone(pkg))

    def test_empty_folder_tree_counts_as_gone(self):
        pkg = os.path.join(self.work, "PkgX")
        os.makedirs(os.path.join(pkg, "leftover-empty-subdir"))
        self.assertTrue(package_source_gone(pkg))


class LocationDecisionF5bTests(LocationDecisionTests):
    """Decision tests with DOWNLOADER_WORKING_DIR configured (F5b active)."""

    def setUp(self):
        super().setUp()
        self.worktmp = tempfile.TemporaryDirectory()
        self.work = self.worktmp.name
        os.environ["DOWNLOADER_WORKING_DIR"] = self.work

    def tearDown(self):
        super().tearDown()
        self.worktmp.cleanup()
        os.environ.pop("DOWNLOADER_WORKING_DIR", None)

    def test_finished_source_still_present_stays_moving(self):
        pkg = os.path.join(self.work, "PkgX")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "episode.mkv"), "w") as f:
            f.write("x")
        loc, moving, _ = self._decide(True, None, pkg, self.dest)
        self.assertEqual("queue", loc)
        self.assertTrue(moving)

    def test_finished_source_and_destination_gone_goes_history(self):
        # the exact zombie scenario: mover moved, *arr imported, cleanup removed
        loc, moving, final = self._decide(True, None, f"{self.work}/PkgX", self.dest)
        self.assertEqual("history", loc)
        self.assertFalse(moving)
        self.assertIsNone(final)


if __name__ == "__main__":
    unittest.main()
