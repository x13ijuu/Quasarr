# -*- coding: utf-8 -*-
"""
Host-ban parking (Maja fork, F3).

When a DDL source rate-limits/bans us, failing the grab would make Sonarr/Radarr
blocklist the (often only) release. Instead download() parks the grab in the
waiting table so the background waiting_worker retries it after the host unbans:

  * already-known-banned host  -> park WITHOUT hitting the source again (gate);
  * HostBannedError mid-request -> record the ban + park with full retry context.

Kept separate from the dedupe/skip contract on purpose — this only adds the two
ban paths and never touches package_id_exists().
"""
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from quasarr.downloads import download
from quasarr.providers import shared_state as shared_state_module
from quasarr.storage.sqlite_database import DataBase


class HostBanParkingTests(unittest.TestCase):
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
            patch("quasarr.downloads.process_links", return_value={"success": True})
        )
        return source, mock_process

    def test_host_banned_parks_grab_instead_of_failing(self):
        """A HostBannedError from the source parks the grab (waiting), never fails it."""
        from quasarr.providers.host_bans import HostBannedError, get_waiting, is_banned

        title = "Bleach.E60.German.1080p.WEB-DL"
        with ExitStack() as stack:
            source, mock_process = self._run_with_source(stack, links=None)
            source.get_download_links.side_effect = HostBannedError("al", "noadblock")
            result = download(
                self.state,
                "Sonarr/4.0",
                "tv",
                title,
                "http://anime-loads.org/media/bleach",
                1024,
                None,
                None,
                "al",
            )

        pkg_id = result["package_id"]
        self.assertTrue(result["success"])
        self.assertTrue(result.get("waiting"))
        self.assertNotIn("failed", result)
        mock_process.assert_not_called()
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
                self.state,
                "Sonarr/4.0",
                "tv",
                title,
                "http://anime-loads.org/media/bleach",
                1024,
                None,
                None,
                "al",
            )

        self.assertTrue(result.get("waiting"))
        source.get_download_links.assert_not_called()  # never hit the banned host
        mock_process.assert_not_called()
        self.assertIsNotNone(get_waiting(result["package_id"]))


if __name__ == "__main__":
    unittest.main()
