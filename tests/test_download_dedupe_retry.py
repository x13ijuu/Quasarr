# -*- coding: utf-8 -*-
"""
Regression tests for the "swallowed retry" root-cause fix (Maja fork).

Upstream bug: download() called package_id_exists(), which returned True for a
release sitting in the *failed* DB, then skipped the download but still returned
{"success": True}. The SAB caller reported "added successfully" to Sonarr while
JDownloader never received anything — the episode stayed missing forever.

Fix: find_existing_package() distinguishes a stale "failed" marker (a retry
request) from a genuine duplicate (downloading / history / awaiting CAPTCHA).
A failed marker is cleared and the download actually runs; a genuine duplicate
returns {"duplicate": True} without a second add.
"""
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import quasarr.downloads as downloads
from quasarr.downloads import download, find_existing_package
from quasarr.providers import shared_state as shared_state_module
from quasarr.storage.sqlite_database import DataBase


class FindExistingPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        shared_state_module.values = {
            "dbfile": os.path.join(self.tmpdir.name, "Quasarr.db")
        }
        shared_state_module.lock = None
        self.state = MagicMock()
        self.state.get_db.side_effect = lambda table: DataBase(table)
        # No JD packages by default.
        self.state.run_device_request.return_value = {"queue": [], "history": []}

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_failed_marker_is_reported_as_failed_not_duplicate(self):
        DataBase("failed").store("Quasarr_tv_abc", "{}")
        self.assertEqual("failed", find_existing_package(self.state, "Quasarr_tv_abc"))

    def test_protected_marker(self):
        DataBase("protected").store("Quasarr_tv_p", "{}")
        self.assertEqual("protected", find_existing_package(self.state, "Quasarr_tv_p"))

    def test_queue_duplicate(self):
        self.state.run_device_request.return_value = {
            "queue": [{"nzo_id": "Quasarr_tv_q"}],
            "history": [],
        }
        self.assertEqual("queue", find_existing_package(self.state, "Quasarr_tv_q"))

    def test_unknown_returns_none(self):
        self.assertIsNone(find_existing_package(self.state, "Quasarr_tv_none"))


class DownloadRetryTests(unittest.TestCase):
    """download() must actually retry a previously-failed release, never swallow it."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        shared_state_module.values = {
            "dbfile": os.path.join(self.tmpdir.name, "Quasarr.db")
        }
        shared_state_module.lock = None
        self.state = MagicMock()
        self.state.values = {"config": lambda section: {"al": "anime-loads.org"}}
        self.state.get_db.side_effect = lambda table: DataBase(table)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run_with_source(self, stack, links=None):
        source = MagicMock()
        source.get_download_links.return_value = (
            {"links": links} if links is not None else None
        )
        stack.enter_context(
            patch("quasarr.downloads.get_download_category_mirrors", return_value=[])
        )
        stack.enter_context(
            patch("quasarr.downloads.get_download_sources", return_value={"al": source})
        )
        stack.enter_context(
            patch("quasarr.downloads.normalize_download_title", side_effect=lambda t: t)
        )
        stack.enter_context(
            patch("quasarr.downloads.extract_client_type", return_value="sonarr")
        )
        mock_process = stack.enter_context(
            patch(
                "quasarr.downloads.process_links",
                return_value={"success": True},
            )
        )
        return source, mock_process

    def test_previously_failed_release_is_retried_not_swallowed(self):
        title = "Bleach.E57.German.1080p.WEB-DL"
        # Pre-compute the deterministic id and seed a stale failed marker.
        pkg_id = downloads.generate_deterministic_package_id(title, "al", "sonarr", "tv")
        DataBase("failed").store(pkg_id, "{}")

        with ExitStack() as stack:
            source, mock_process = self._run_with_source(
                stack, links=[["http://anime-loads.org/x", "al"]]
            )
            result = download(
                self.state, "Sonarr/4.0", "tv", title,
                "http://anime-loads.org/media/bleach", 1024, None, None, "al",
            )

        # The stale failed marker was cleared and the download actually ran.
        self.assertIsNone(DataBase("failed").retrieve(pkg_id))
        source.get_download_links.assert_called_once()
        mock_process.assert_called_once()
        self.assertNotIn("duplicate", result)

    def test_genuine_queue_duplicate_is_skipped(self):
        title = "Bleach.E58.German.1080p.WEB-DL"
        pkg_id = downloads.generate_deterministic_package_id(title, "al", "sonarr", "tv")

        with ExitStack() as stack:
            source, mock_process = self._run_with_source(
                stack, links=[["http://anime-loads.org/x", "al"]]
            )
            # Same package already downloading in JD.
            self.state.run_device_request.return_value = {
                "queue": [{"nzo_id": pkg_id}],
                "history": [],
            }
            result = download(
                self.state, "Sonarr/4.0", "tv", title,
                "http://anime-loads.org/media/bleach", 1024, None, None, "al",
            )

        self.assertTrue(result["success"])
        self.assertTrue(result.get("duplicate"))
        mock_process.assert_not_called()

    def test_host_banned_parks_grab_instead_of_failing(self):
        """A HostBannedError from the source parks the grab (waiting), never fails it."""
        from quasarr.providers.host_bans import HostBannedError, get_waiting, is_banned

        title = "Bleach.E60.German.1080p.WEB-DL"
        with ExitStack() as stack:
            source, mock_process = self._run_with_source(stack, links=None)
            source.get_download_links.side_effect = HostBannedError("al", "noadblock")
            result = download(
                self.state, "Sonarr/4.0", "tv", title,
                "http://anime-loads.org/media/bleach", 1024, None, None, "al",
            )

        pkg_id = result["package_id"]
        self.assertTrue(result["success"])
        self.assertTrue(result.get("waiting"))
        self.assertNotIn("failed", result)
        mock_process.assert_not_called()
        # Host recorded as banned and the grab parked with full retry context.
        self.assertTrue(is_banned("al"))
        parked = get_waiting(pkg_id)
        self.assertIsNotNone(parked)
        self.assertEqual(title, parked["title"])
        self.assertEqual("al", parked["source_key"])

    def test_banned_host_gate_parks_without_hitting_source(self):
        """When the host is already known-banned, the grab is parked without a request."""
        from quasarr.providers.host_bans import get_waiting, record_ban

        record_ban("al", "noadblock")
        title = "Bleach.E61.German.1080p.WEB-DL"
        with ExitStack() as stack:
            source, mock_process = self._run_with_source(stack, links=[["u", "al"]])
            result = download(
                self.state, "Sonarr/4.0", "tv", title,
                "http://anime-loads.org/media/bleach", 1024, None, None, "al",
            )

        self.assertTrue(result.get("waiting"))
        source.get_download_links.assert_not_called()  # never hit the banned host
        mock_process.assert_not_called()
        self.assertIsNotNone(get_waiting(result["package_id"]))


if __name__ == "__main__":
    unittest.main()
