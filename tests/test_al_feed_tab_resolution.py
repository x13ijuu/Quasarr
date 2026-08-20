# -*- coding: utf-8 -*-
"""
Tests for feed-grab tab resolution and positional episode selection (Maja fork).

Two defects at the same seam, both proven live on 2026-08-20:

1. A feed (RSS) grab carries no tab id. `_get_release_id()` only finds one when
   the feed block literally says "Release N:", so RSS entries arrive as 0.
   Upstream then hard-coded tab 1 "to achieve successful download". On a page
   whose first tab is a batch collection, that is simply the wrong release.
   Evidence: four One Piece episodes (abs 1169/1170/1171/1174) were each grabbed
   from RSS as "…German.ML.GerSub.EngSub.1080p.WEB-DL" and each came back as a
   720p japanese-audio CR.WEB file off tab 1 - 4 of 4 RSS grabs wrong, 0 of 3
   search-path grabs (which carry a real tab id) wrong.

2. The episode inside a tab is picked positionally: `selection =
   episode_in_title - 1`, an index into `div.episodes a[data-loop]`. For "E1174"
   that is index 1173; for "S00E01" it is index 0, i.e. the first link of a
   main-season tab (the Solo Leveling case). The tab's own episode offset is not
   stated anywhere in the markup, so an out-of-range index cannot be translated -
   only refused.

Both now refuse instead of guessing, in line with maja.19: rather no episode
than the wrong one.
"""

import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from quasarr.downloads.sources import al as al_download
from quasarr.downloads.sources.al import (
    _check_release,
    _episode_link_count,
    _iter_download_tabs,
    _resolve_release_id_by_title,
)
from quasarr.downloads.sources.helpers.anime_title import ReleaseInfo


def _page(tabs_html):
    return BeautifulSoup(
        "<html><head><title>One Piece (Serie)</title></head>"
        f"<body>{tabs_html}</body></html>",
        "html.parser",
    )


def _tab(dom_id, episodes=0):
    links = "".join(f'<a data-loop="{i}">Folge {i + 1}</a>' for i in range(episodes))
    eps = f'<div class="episodes">{links}</div>' if episodes else ""
    return f'<div class="tab-pane" id="download_{dom_id}">{eps}</div>'


def _info(release_title):
    return ReleaseInfo(
        release_title=release_title,
        audio_langs=["German"],
        subtitle_langs=[],
        episode_title=None,
        resolution="1080p",
        audio="AAC",
        video="x264",
        source="WEB-DL",
        release_group="GRP",
        season_part=None,
        season=1,
        episode_min=1,
        episode_max=1,
    )


class DomIdReadbackTests(unittest.TestCase):
    def test_tabs_are_read_by_their_real_dom_id_not_by_position(self):
        # The search path numbers tabs 1..N positionally; the download path looks
        # up id="download_<n>". A gapped set is exactly where those diverge.
        soup = _page(_tab(1) + _tab(4) + _tab(9))
        self.assertEqual([1, 4, 9], [i for i, _ in _iter_download_tabs(soup)])

    def test_episode_link_count_is_none_without_a_list(self):
        soup = _page(_tab(1))
        tab = soup.find("div", id="download_1")
        self.assertIsNone(_episode_link_count(tab))
        soup = _page(_tab(1, episodes=12))
        tab = soup.find("div", id="download_1")
        self.assertEqual(12, _episode_link_count(tab))


class FeedTabResolutionTests(unittest.TestCase):
    WANTED = "One.Piece.E1174.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"
    BATCH = "One.Piece.E001-206.GerSub.480p.AAC.Web-DL.x264-Tanuki"

    def _resolve(self, soup, titles, wanted):
        # titles: {dom_id: release_title the tab parses to}
        def fake_parse(tab, _soup, **kwargs):
            match = tab.get("id", "").replace("download_", "")
            return _info(titles.get(int(match)))

        with (
            patch.object(al_download, "_parse_info_from_download_item", fake_parse),
            patch.object(al_download, "_guess_title", lambda _pt, ri: ri.release_title),
        ):
            return _resolve_release_id_by_title(soup, wanted)

    def test_picks_the_tab_that_actually_carries_the_title(self):
        # The live shape: tab 1 is the batch collection, the wanted release is 4.
        soup = _page(_tab(1) + _tab(4) + _tab(9))
        got = self._resolve(
            soup, {1: self.BATCH, 4: self.WANTED, 9: "Other"}, self.WANTED
        )
        self.assertEqual(4, got)

    def test_refuses_when_no_tab_carries_the_title(self):
        # Exactly the One Piece failure: nothing matches -> must NOT fall back to 1.
        soup = _page(_tab(1) + _tab(4))
        got = self._resolve(soup, {1: self.BATCH, 4: "Something.Else"}, self.WANTED)
        self.assertEqual(0, got)

    def test_refuses_when_the_title_is_ambiguous(self):
        soup = _page(_tab(1) + _tab(4))
        got = self._resolve(soup, {1: self.WANTED, 4: self.WANTED}, self.WANTED)
        self.assertEqual(0, got)

    def test_feed_grab_without_match_falls_back_but_bounds_check_still_bites(self):
        # Scan-only phase: no unique match -> tab 1, exactly as before. What
        # stops the live One Piece failure is the bounds check (1174 > 206).
        soup_html = str(_page(_tab(1, episodes=206) + _tab(4, episodes=74)))
        with (
            patch.object(
                al_download,
                "_parse_info_from_download_item",
                lambda tab, _s, **k: _info(
                    self.BATCH if tab.get("id") == "download_1" else "Nope"
                ),
            ),
            patch.object(al_download, "_guess_title", lambda _pt, ri: ri.release_title),
        ):
            title, release_id = _check_release(
                shared_state=None,
                details_html=soup_html,
                release_id=0,
                title=self.WANTED,
                episode_in_title=1174,
            )
        # 0 is the caller's abort signal ("No valid release ID found").
        self.assertEqual(0, release_id)
        self.assertEqual(self.WANTED, title)


class PositionalEpisodeBoundsTests(unittest.TestCase):
    def _run(self, episodes, episode_in_title, title="Show.S01E01.German"):
        soup_html = str(_page(_tab(1, episodes=episodes)))
        with (
            patch.object(
                al_download,
                "_parse_info_from_download_item",
                lambda *a, **k: _info(title),
            ),
            patch.object(al_download, "_guess_title", lambda _pt, ri: ri.release_title),
        ):
            return _check_release(
                shared_state=None,
                details_html=soup_html,
                release_id=1,
                title=title,
                episode_in_title=episode_in_title,
            )

    def test_absolute_episode_beyond_the_tab_is_refused(self):
        # "E1174" against a tab offering 74 links -> index 1173 is meaningless.
        _, release_id = self._run(episodes=74, episode_in_title=1174)
        self.assertEqual(0, release_id)

    def test_in_range_episode_still_works(self):
        # The normal per-season case must be untouched: E14 of a 24-episode tab.
        _, release_id = self._run(episodes=24, episode_in_title=14)
        self.assertEqual(1, release_id)

    def test_tab_without_episode_list_is_not_bounds_checked(self):
        # Whole-release tabs have no per-episode links; the check must not fire.
        _, release_id = self._run(episodes=0, episode_in_title=3)
        self.assertEqual(1, release_id)


if __name__ == "__main__":
    unittest.main()
