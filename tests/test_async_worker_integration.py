# -*- coding: utf-8 -*-
"""
Integration test for the async-accept chain (Maja fork):

    api/arr enqueue_grab()  ->  host_bans waiting table (pending)
                            ->  waiting_worker_tick() drains it
                            ->  download(package_id=<minted id>)  (background)

This is the end-to-end guarantee maja's own suite never covered: the grab that
enqueue_grab() returns INSTANTLY actually gets processed by the background worker,
and the worker hands the UNIQUE id straight back to download() (never regenerated
mid-flight — that is what keeps Sonarr's tracking attached). Without the worker a
parked grab would hang forever, so wiring api/arr to enqueue_grab is only correct
together with a running worker — this test locks that coupling down.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from quasarr import waiting_worker_tick
from quasarr.downloads import enqueue_grab
from quasarr.providers import host_bans as hb
from quasarr.providers import shared_state as shared_state_module
from quasarr.storage.sqlite_database import DataBase


class AsyncWorkerIntegrationTests(unittest.TestCase):
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

    def test_enqueued_grab_is_drained_by_the_worker_with_the_minted_id(self):
        title = "Bleach.E114.German.1080p.WEB-DL"
        with patch("quasarr.downloads.download") as mock_dl:
            mock_dl.return_value = {"success": True, "package_id": "x", "title": title}

            result = enqueue_grab(
                self.state, "Sonarr/4.0", "tv", title, "http://u", 1, None, None, "al"
            )
            pid = result["package_id"]

            # Parked and visible as pending BEFORE anything downloads.
            self.assertTrue(result.get("queued"))
            self.assertIsNotNone(hb.get_waiting(pid))
            self.assertTrue(hb.get_waiting(pid).get("pending"))
            mock_dl.assert_not_called()

            # One worker tick drains it in the background.
            waiting_worker_tick(self.state, tick=1)

        mock_dl.assert_called_once()
        _, kwargs = mock_dl.call_args
        self.assertEqual(pid, kwargs.get("package_id"))  # the exact minted id, reused
        # Drained: the pending row is gone, so the next tick won't re-process it.
        self.assertIsNone(hb.get_waiting(pid))

    def test_each_enqueue_is_a_fresh_unique_id(self):
        # Two grabs of the SAME release must never collide (that collision is the
        # root cause of orphaned, never-imported re-grabs — E137 2026-07-08).
        title = "Bleach.E114.German.1080p.WEB-DL"
        with patch("quasarr.downloads.download") as mock_dl:
            mock_dl.side_effect = lambda *a, **k: {
                "success": True,
                "package_id": k.get("package_id"),
                "title": title,
            }
            r1 = enqueue_grab(
                self.state, "Sonarr/4.0", "tv", title, "http://u", 1, None, None, "al"
            )
            r2 = enqueue_grab(
                self.state, "Sonarr/4.0", "tv", title, "http://u", 1, None, None, "al"
            )
            self.assertNotEqual(r1["package_id"], r2["package_id"])
            waiting_worker_tick(self.state, tick=1)

        # Both unique grabs were drained, each with its own id.
        drained_ids = {c.kwargs.get("package_id") for c in mock_dl.call_args_list}
        self.assertEqual({r1["package_id"], r2["package_id"]}, drained_ids)
        self.assertIsNone(hb.get_waiting(r1["package_id"]))
        self.assertIsNone(hb.get_waiting(r2["package_id"]))


if __name__ == "__main__":
    unittest.main()
