# -*- coding: utf-8 -*-
"""
Publication-date anchoring (Maja fork, regrab-loop root cause D1).

A source that states no date used to get datetime.now() stamped into the feed —
a NEW pubDate on every poll. Sonarr matches releases against its blocklist by
publication date, so a moving date makes the blocklist structurally unable to
hold a release: it is grabbed, it fails, it is blocklisted, and the next sync
offers it again as something never seen before (measured 2026-08-31: 29 grabs of
One Piece E1176 in 7h against 29 blocklist rows, none ever matched).

first_seen.anchor() remembers the first date a release was offered with and
serves it from then on, so the blocklist can finally hold.
"""
import os
import tempfile
import unittest

from quasarr.identity import first_seen
from quasarr.providers import shared_state as shared_state_module

D1 = "Mon, 03 Aug 2026 10:00:00 +0000"
D2 = "Tue, 04 Aug 2026 11:00:00 +0000"
URL = "http://anime-loads.org/media/show"


class FirstSeenAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shared_state_module.values = {
            "dbfile": os.path.join(self.tmp.name, "Quasarr.db")
        }
        shared_state_module.lock = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_sighting_wins_across_polls(self):
        # Same release, a later poll computes a newer "now" — the anchor ignores it.
        first = first_seen.anchor("al", URL, "Show.E01", D1)
        again = first_seen.anchor("al", URL, "Show.E01", D2)
        self.assertEqual(D1, first)
        self.assertEqual(D1, again)  # stable -> Sonarr's blocklist can hold it

    def test_distinct_releases_keep_distinct_anchors(self):
        e1 = first_seen.anchor("al", URL, "Show.E01", D1)
        e2 = first_seen.anchor("al", URL, "Show.E02", D2)
        self.assertEqual(D1, e1)
        self.assertEqual(D2, e2)  # different episode -> its own anchor


if __name__ == "__main__":
    unittest.main()
