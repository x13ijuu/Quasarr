# -*- coding: utf-8 -*-
"""
Tests for history hygiene across the storage flip (Maja fork, F5b).

`run_history_hygiene()` removes Completed history entries whose folder is gone -
the in-product replacement for the external reconcile guard.

It used to select entries with `storage.startswith(completed_dir)`. But the
reported storage only points at COMPLETED_DIR while the folder still EXISTS
there: get_packages() sets `final_storage` from `package_at_destination()`, and
falls back to the downloader's working dir otherwise. So the moment cleanup
removes the folder - the very event hygiene reacts to - storage flips back to
/staging and the filter excludes the entry. Trigger condition and exclusion
condition were the same event, so the entry lingered forever and Sonarr kept an
unclosable queue item ("No files found are eligible for import").

Observed live on 2026-08-20: a One Piece package whose Quasarr history entry
still read `storage=/staging/…` while the folder existed neither there nor at
/data/downloads. Sonarr had shown that queue entry as importPending for hours.

The 2026-07-07 reconcile race must stay shut: a package still sitting in (or
merely moving through) the working dir is never touched, because
`package_source_gone()` refuses to claim anything it cannot prove.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from quasarr.downloads import packages as pkgmod
from quasarr.downloads.packages import run_history_hygiene

PKG = "Some.Package.S01E01.German.1080p-GRP"


class HistoryHygieneStorageFlipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = os.path.join(self.tmp.name, "staging")
        self.done = os.path.join(self.tmp.name, "downloads")
        os.makedirs(self.work)
        os.makedirs(self.done)
        os.environ["DOWNLOADER_WORKING_DIR"] = self.work
        os.environ["COMPLETED_DIR"] = self.done

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("DOWNLOADER_WORKING_DIR", None)
        os.environ.pop("COMPLETED_DIR", None)

    def _run(self, storage, status="Completed"):
        """Run hygiene over a single history entry, recording deletions."""
        deleted = []
        history = {"history": [{"status": status, "storage": storage, "nzo_id": "NZO1"}]}
        with (
            patch.object(pkgmod, "get_packages", lambda *a, **k: history),
            patch.object(
                pkgmod,
                "delete_package",
                lambda _ss, pid, **k: deleted.append(pid),
            ),
        ):
            removed = run_history_hygiene(shared_state=None)
        return removed, deleted

    # --- the live failure -------------------------------------------------

    def test_staging_storage_with_folder_gone_everywhere_is_removed(self):
        # Exactly the One Piece shape: storage still reads /staging/... (the flip)
        # while the folder exists in neither location.
        storage = os.path.join(self.work, PKG, "Quasarr", PKG)
        removed, deleted = self._run(storage)
        self.assertEqual(1, removed)
        self.assertEqual(["NZO1"], deleted)

    def test_completed_dir_storage_with_folder_gone_is_still_removed(self):
        # The shape the old filter did handle must keep working.
        storage = os.path.join(self.done, PKG)
        removed, deleted = self._run(storage)
        self.assertEqual(1, removed)
        self.assertEqual(["NZO1"], deleted)

    # --- the race guard (2026-07-07) --------------------------------------

    def test_package_still_in_working_dir_is_never_touched(self):
        # Mid-move: files still on the downloader's disk. Deleting the history
        # entry here cascaded a package delete to JD and cost Bleach E50-E56.
        pkg_dir = os.path.join(self.work, PKG)
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "file.mkv"), "w") as fh:
            fh.write("x")
        removed, deleted = self._run(os.path.join(self.work, PKG))
        self.assertEqual(0, removed)
        self.assertEqual([], deleted)

    def test_package_present_at_destination_is_not_touched(self):
        # Waiting to be imported, or imported but not yet cleaned up.
        os.makedirs(os.path.join(self.done, PKG))
        storage = os.path.join(self.work, PKG, "Quasarr", PKG)
        removed, deleted = self._run(storage)
        self.assertEqual(0, removed)
        self.assertEqual([], deleted)

    def test_foreign_path_is_never_claimed_gone(self):
        # Outside the working dir -> unprovable -> hands off.
        removed, deleted = self._run("/somewhere/else/" + PKG)
        self.assertEqual(0, removed)
        self.assertEqual([], deleted)

    def test_working_dir_not_visible_is_never_claimed_gone(self):
        os.environ["DOWNLOADER_WORKING_DIR"] = "/nonexistent-mount"
        removed, deleted = self._run("/nonexistent-mount/" + PKG)
        self.assertEqual(0, removed)
        self.assertEqual([], deleted)

    # --- scope ------------------------------------------------------------

    def test_non_completed_entries_are_ignored(self):
        storage = os.path.join(self.work, PKG, "Quasarr", PKG)
        removed, deleted = self._run(storage, status="Failed")
        self.assertEqual(0, removed)
        self.assertEqual([], deleted)

    def test_empty_storage_is_ignored(self):
        removed, deleted = self._run("")
        self.assertEqual(0, removed)
        self.assertEqual([], deleted)

    def test_no_op_without_completed_dir(self):
        os.environ.pop("COMPLETED_DIR", None)
        storage = os.path.join(self.work, PKG, "Quasarr", PKG)
        removed, deleted = self._run(storage)
        self.assertEqual(0, removed)
        self.assertEqual([], deleted)


if __name__ == "__main__":
    unittest.main()
