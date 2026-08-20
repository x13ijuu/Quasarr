# -*- coding: utf-8 -*-
"""
Tests for episode-link membership on anime-loads (Maja fork).

The old gate asked `requested_episode <= len(episode_links)`. That conflates a
COUNT with a NUMBERING and is off by one, because AL numbers `data-loop`
0-BASED and prefixes the list with a non-numeric "cnl" entry (Click'n'Load for
the whole release).

Measured live on the One Piece page (2026-08-20):

    alle data-loop-Links      : 1174
    nur Ziffern (echter Code) : 1173      <- "cnl" faellt raus
    numerische Werte          : 0..1173

Episode 1174 lives at data-loop 1173 and is present — but `1174 <= 1173` is
false, so EVERY AL release was discarded and targeted searches for it returned
nothing at all. E1120 still worked (`1120 <= 1173`), which is exactly the split
Sonarr's history shows: searches found E1120 in August, E1174 never.

The same count was used by the maja.26 bounds check on the download side, so it
would have refused those grabs too — stopping the series entirely instead of
only stopping wrong ones.

Fix: ask whether the link EXISTS (`episode - 1` in the loop set).
"""
import unittest

from bs4 import BeautifulSoup

from quasarr.downloads.sources.al import (
    _episode_link_count,
    _episode_loops,
    _tab_offers_episode,
)


def _tab(loops, with_cnl=True):
    """Build a tab the way AL does: an optional 'cnl' entry, then 0-based loops."""
    links = '<a data-loop="cnl">Click\'n\'Load</a>' if with_cnl else ""
    links += "".join(f'<a data-loop="{i}">Folge {i + 1}</a>' for i in loops)
    html = f'<div class="tab-pane" id="download_1"><div class="episodes">{links}</div></div>'
    return BeautifulSoup(html, "html.parser").find("div", class_="tab-pane")


class OnePieceShapeTests(unittest.TestCase):
    """The exact shape measured on the live page."""

    def setUp(self):
        # 1173 numeric links, values 0..1172, plus one "cnl" -> 1174 elements.
        self.tab = _tab(range(0, 1173))

    def test_cnl_is_not_counted(self):
        self.assertEqual(1173, _episode_link_count(self.tab))

    def test_episode_at_the_last_link_is_offered(self):
        # Episode 1173 sits at data-loop 1172 — the last one present.
        self.assertTrue(_tab_offers_episode(self.tab, 1173))

    def test_the_old_count_gate_was_the_bug(self):
        # Both episodes are <= the count, so the old gate kept them; the point of
        # this test is that membership agrees for the in-range case.
        for ep in (1, 500, 1120, 1173):
            with self.subTest(ep=ep):
                self.assertTrue(_tab_offers_episode(self.tab, ep))

    def test_episode_beyond_the_links_is_not_offered(self):
        self.assertFalse(_tab_offers_episode(self.tab, 1174))


class OffByOneTests(unittest.TestCase):
    def test_highest_episode_is_max_loop_plus_one_not_the_count(self):
        # A tab whose loops are 0..1173 offers episode 1174 even though the
        # numeric link COUNT is 1174 — and would offer it at count 1173 too if
        # one value were missing. Membership does not care about the count.
        tab = _tab(range(0, 1174))
        self.assertTrue(_tab_offers_episode(tab, 1174))
        self.assertFalse(_tab_offers_episode(tab, 1175))

    def test_gap_in_the_middle_is_respected(self):
        # Count-based logic cannot see a hole; membership can.
        tab = _tab([0, 1, 2, 5, 6])
        self.assertEqual(5, _episode_link_count(tab))
        self.assertTrue(_tab_offers_episode(tab, 3))    # loop 2
        self.assertFalse(_tab_offers_episode(tab, 4))   # loop 3 missing
        self.assertTrue(_tab_offers_episode(tab, 6))    # loop 5 — beyond the count
        self.assertTrue(_tab_offers_episode(tab, 7))    # loop 6 — beyond the count

    def test_first_episode_maps_to_loop_zero(self):
        tab = _tab([0])
        self.assertTrue(_tab_offers_episode(tab, 1))
        self.assertFalse(_tab_offers_episode(tab, 2))


class NoEpisodeListTests(unittest.TestCase):
    def test_tab_without_episode_list_returns_none(self):
        # Whole-release tabs carry no per-episode links; callers must not treat
        # that as "episode missing".
        tab = BeautifulSoup(
            '<div class="tab-pane" id="download_1"></div>', "html.parser"
        ).find("div", class_="tab-pane")
        self.assertIsNone(_episode_loops(tab))
        self.assertIsNone(_tab_offers_episode(tab, 5))
        self.assertIsNone(_episode_link_count(tab))

    def test_unparseable_episode_returns_none(self):
        self.assertIsNone(_tab_offers_episode(_tab([0, 1]), "nicht-numerisch"))


if __name__ == "__main__":
    unittest.main()
