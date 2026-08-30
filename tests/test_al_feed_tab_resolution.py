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


def _tab(dom_id, episodes=0, audio=()):
    links = "".join(f'<a data-loop="{i}">Folge {i + 1}</a>' for i in range(episodes))
    eps = f'<div class="episodes">{links}</div>' if episodes else ""
    # The speaker row is how the page states a release's audio tracks; the
    # closed-captioning row next to it states subtitles and must never be read
    # as audio.
    flags = "".join(f'<i class="flag flag-{code}"></i>' for code in audio)
    audio_row = (
        f'<table><tr><th><i class="fa-volume-up"></i></th><td>{flags}</td></tr>'
        '<tr><th><i class="fa-closed-captioning"></i></th>'
        '<td><i class="flag flag-de"></i></td></tr></table>'
        if audio
        else ""
    )
    return f'<div class="tab-pane" id="download_{dom_id}">{audio_row}{eps}</div>'


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

    def test_feed_grab_without_any_match_is_refused_not_guessed(self):
        # No title match and no tab carrying the advertised audio. Tab 1 would
        # be a guess, and guessing is what put japanese files under german
        # names four times in a row.
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


class AudioBasedTabResolutionTests(unittest.TestCase):
    """Picking the release by the audio the title advertises.

    The live shape (measured 2026-08-30 on a still-airing season): release 1
    carries the original audio for every episode, release 2 carries the dub but
    only for the episodes dubbed so far. The feed advertises the dub for every
    episode, so tab 1 - the old blind fallback - is wrong for all of them.

    Both halves of the question have to be answered together. Matching on audio
    alone takes a tab that has no such episode; matching on the episode alone
    takes the original audio. Only their intersection is the ordered thing.
    """

    DUBBED = "Show.Name.S04E17.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"
    LATER = "Show.Name.S04E20.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"

    # release 1: original audio, all 20 episodes | release 2: dub, first 17
    PAGE = _tab(1, episodes=20, audio=["jp"]) + _tab(2, episodes=17, audio=["jp", "de"])

    def _resolve(self, title, episode):
        return al_download._resolve_release_id_by_audio(
            _page(self.PAGE), title, episode
        )

    def test_picks_the_dubbed_release_when_it_has_the_episode(self):
        # The obtainable case that was missed 20 times: the dub existed on
        # release 2 the whole time.
        self.assertEqual(2, self._resolve(self.DUBBED, 17))

    def test_refuses_when_the_dub_does_not_reach_this_episode(self):
        # Release 2 stops at 17, so nothing satisfies the claim for 20 - and
        # release 1 must NOT be taken just because it has an episode 20.
        self.assertEqual(0, self._resolve(self.LATER, 20))

    def test_audio_alone_is_not_enough(self):
        # Guard rail against a tempting simplification: dropping the episode
        # half would return release 2 for episode 20.
        soup = _page(self.PAGE)
        by_audio_only = [
            tab_id
            for tab_id, tab in _iter_download_tabs(soup)
            if "German" in al_download._tab_audio_languages(tab)
        ]
        self.assertEqual([2], by_audio_only)
        self.assertEqual(0, self._resolve(self.LATER, 20))

    def test_subtitle_flags_are_not_read_as_audio(self):
        # Release 1 lists a german SUBTITLE. Reading that row as audio would
        # make both tabs match and re-introduce the ambiguity.
        soup = _page(self.PAGE)
        tab = soup.find("div", id="download_1")
        self.assertEqual(["Japanese"], al_download._tab_audio_languages(tab))

    def test_title_without_a_language_claim_has_nothing_to_match_on(self):
        self.assertEqual(0, self._resolve("Show.Name.S04E17.1080p.WEB-DL.x264", 17))

    def test_ambiguous_audio_match_is_refused(self):
        soup_html = _tab(1, episodes=20, audio=["de"]) + _tab(
            2, episodes=20, audio=["de"]
        )
        self.assertEqual(
            0,
            al_download._resolve_release_id_by_audio(_page(soup_html), self.DUBBED, 17),
        )


class FeedGrabEndToEndTests(unittest.TestCase):
    """The same two cases through `_check_release`, the way a real grab runs."""

    DUBBED = "Show.Name.S04E17.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"
    LATER = "Show.Name.S04E20.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"
    PAGE = _tab(1, episodes=20, audio=["jp"]) + _tab(2, episodes=17, audio=["jp", "de"])

    def _run(self, title, episode):
        with (
            patch.object(
                al_download,
                "_parse_info_from_download_item",
                lambda *a, **k: _info("Unrelated.Title"),
            ),
            patch.object(al_download, "_guess_title", lambda _pt, ri: ri.release_title),
        ):
            return _check_release(
                shared_state=None,
                details_html=str(_page(self.PAGE)),
                release_id=0,
                title=title,
                episode_in_title=episode,
            )

    def test_obtainable_dub_is_resolved_to_the_right_release(self):
        _, release_id = self._run(self.DUBBED, 17)
        self.assertEqual(2, release_id)

    def test_unobtainable_dub_is_refused_rather_than_substituted(self):
        _, release_id = self._run(self.LATER, 20)
        self.assertEqual(0, release_id)


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
