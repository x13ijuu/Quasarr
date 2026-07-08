# -*- coding: utf-8 -*-
"""
Tests for the async addurl accept path (Maja fork).

Root cause of Rufus' 'downloads hang / disappear / never import' under load:
Quasarr's addurl called download() SYNCHRONOUSLY (scrape + CAPTCHA + JD add,
15-40s) before answering Sonarr. A burst of grabs blocked Sonarr's client
requests → timeouts / downloadClientUnavailable → Sonarr dropped the tracking
while Quasarr finished in the background → orphaned completed downloads.

enqueue_grab() must: (1) return the deterministic nzo_id instantly, (2) NEVER
run the download inline, (3) persist the grab so it's visible + processed by the
background worker, (4) dedup re-grabs.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from quasarr.providers import shared_state as shared_state_module
from quasarr.storage.sqlite_database import DataBase
import quasarr.downloads as downloads
from quasarr.downloads import enqueue_grab
from quasarr.providers import host_bans as hb


class EnqueueGrabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shared_state_module.values = {
            "dbfile": os.path.join(self.tmp.name, "Quasarr.db"),
            "config": lambda section: {"al": "anime-loads.org"},
        }
        shared_state_module.lock = None
        self.state = MagicMock()
        self.state.values = shared_state_module.values
        self.state.get_db.side_effect = lambda t: DataBase(t)
        self.state.run_device_request.return_value = {"queue": [], "history": []}

    def tearDown(self):
        self.tmp.cleanup()

    def test_addurl_returns_instantly_and_never_downloads(self):
        title = "Bleach.E110.German.1080p.WEB-DL"
        with patch("quasarr.downloads.download") as mock_download:
            result = enqueue_grab(
                self.state, "Sonarr/4.0", "tv", title,
                "http://anime-loads.org/x", 1024, None, None, "al",
            )
        # never ran the (blocking) download inline
        mock_download.assert_not_called()
        self.assertTrue(result["success"])
        self.assertTrue(result.get("queued"))
        pid = result["package_id"]
        # persisted so it's visible to Sonarr + the worker
        self.assertIsNotNone(hb.get_waiting(pid))
        self.assertTrue(hb.get_waiting(pid).get("pending"))

    def test_deterministic_id_matches_download_path(self):
        title = "Bleach.E111.German.1080p.WEB-DL"
        expect = downloads.generate_deterministic_package_id(title, "al", "sonarr", "tv")
        with patch("quasarr.downloads.download"):
            result = enqueue_grab(
                self.state, "Sonarr/4.0", "tv", title,
                "http://anime-loads.org/x", 1024, None, None, "al",
            )
        self.assertEqual(expect, result["package_id"])

    def test_regrab_of_pending_is_duplicate(self):
        title = "Bleach.E112.German.1080p.WEB-DL"
        with patch("quasarr.downloads.download"):
            r1 = enqueue_grab(self.state, "Sonarr/4.0", "tv", title, "u", 1, None, None, "al")
            r2 = enqueue_grab(self.state, "Sonarr/4.0", "tv", title, "u", 1, None, None, "al")
        self.assertTrue(r1.get("queued"))
        self.assertTrue(r2.get("duplicate"))
        self.assertEqual(r1["package_id"], r2["package_id"])

    def test_regrab_of_failed_clears_marker_and_requeues(self):
        title = "Bleach.E113.German.1080p.WEB-DL"
        pid = downloads.generate_deterministic_package_id(title, "al", "sonarr", "tv")
        DataBase("failed").store(pid, "{}")
        with patch("quasarr.downloads.download"):
            result = enqueue_grab(self.state, "Sonarr/4.0", "tv", title, "u", 1, None, None, "al")
        self.assertTrue(result.get("queued"))
        self.assertIsNone(DataBase("failed").retrieve(pid))   # stale failed cleared
        self.assertIsNotNone(hb.get_waiting(pid))             # re-queued


if __name__ == "__main__":
    unittest.main()
