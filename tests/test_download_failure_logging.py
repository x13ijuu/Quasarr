# -*- coding: utf-8 -*-

import unittest
from unittest.mock import MagicMock, call, patch

from quasarr.downloads import fail


class DownloadFailureLoggingTests(unittest.TestCase):
    def test_terminal_failure_logs_error_and_failed_state_warning(self):
        shared_state = MagicMock()
        stats = MagicMock()

        with (
            patch("quasarr.downloads.StatsHelper", return_value=stats),
            patch("quasarr.downloads.error") as error_log,
            patch("quasarr.downloads.warn") as warn_log,
        ):
            result = fail(
                "Synthetic.Release",
                "synthetic-package-id",
                shared_state,
                reason="Synthetic redirect failure",
            )

        self.assertEqual(
            {"success": True, "title": "Synthetic.Release", "failed": True}, result
        )
        error_log.assert_called_once_with(
            "Reason for failure: Synthetic redirect failure"
        )
        warn_log.assert_called_once_with(
            'Package "Synthetic.Release" marked as failed!'
        )
        stats.increment_failed_downloads.assert_called_once_with()
        shared_state.get_db.return_value.store.assert_called_once()

    def test_failure_persistence_exception_logs_error(self):
        shared_state = MagicMock()
        shared_state.get_db.side_effect = RuntimeError("synthetic database failure")

        with (
            patch("quasarr.downloads.StatsHelper"),
            patch("quasarr.downloads.error") as error_log,
            patch("quasarr.downloads.warn") as warn_log,
        ):
            fail(
                "Synthetic.Release",
                "synthetic-package-id",
                shared_state,
                reason="Synthetic redirect failure",
            )

        self.assertEqual(
            [
                call("Reason for failure: Synthetic redirect failure"),
                call(
                    'Error marking package "synthetic-package-id" as failed: '
                    "synthetic database failure"
                ),
            ],
            error_log.call_args_list,
        )
        warn_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
