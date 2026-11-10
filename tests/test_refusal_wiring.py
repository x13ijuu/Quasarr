# -*- coding: utf-8 -*-
"""
Refusal-ledger wiring into download() (Maja fork, regrab-loop D2).

When no source can resolve a release, download() records it as unresolvable at the
shared funnel (not inside one of 22 sources), so the backstop that stops the feed
from re-offering it covers every source. Verifies the call-site fires end-to-end;
the ledger logic itself is covered by test_refusal_ledger.
"""
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from quasarr.downloads import download
from quasarr.identity import refusals
from quasarr.providers import shared_state as shared_state_module
from quasarr.storage.sqlite_database import DataBase


class RefusalWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        shared_state_module.values = {
            "dbfile": os.path.join(self.tmpdir.name, "Quasarr.db")
        }
        shared_state_module.lock = None
        self.state = MagicMock()
        self.state.values = {"config": lambda section: {"al": "anime-loads.org"}}
        self.state.get_db.side_effect = lambda table: DataBase(table)
        self.state.run_device_request.return_value = {"queue": [], "history": []}

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_unresolvable_release_is_recorded_at_the_funnel(self):
        title = "Bleach.E70.German.1080p.WEB-DL"
        url = "http://anime-loads.org/media/bleach"
        with ExitStack() as stack:
            source = MagicMock()
            source.get_download_links.return_value = None  # claims nothing
            stack.enter_context(
                patch("quasarr.downloads.get_download_category_mirrors", return_value=[])
            )
            stack.enter_context(
                patch(
                    "quasarr.downloads.get_download_sources",
                    return_value={"al": source},
                )
            )
            stack.enter_context(
                patch(
                    "quasarr.downloads.normalize_download_title", side_effect=lambda t: t
                )
            )
            stack.enter_context(
                patch("quasarr.downloads.extract_client_type", return_value="sonarr")
            )

            self.assertFalse(refusals.is_refused("al", url, title))
            result = download(
                self.state, "Sonarr/4.0", "tv", title, url, 1024, None, None, "al"
            )

        # Failed honestly AND recorded so the next feed won't re-offer it blindly.
        self.assertTrue(result.get("failed"))
        self.assertTrue(refusals.is_refused("al", url, title))


if __name__ == "__main__":
    unittest.main()
