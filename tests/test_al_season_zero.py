# -*- coding: utf-8 -*-
"""
Tests for season 0 (specials) on anime-loads (Maja fork).

Every season guard in the AL path used truthiness, and 0 is falsy in Python.
A specials request therefore skipped ALL of them:

  * no season parsing intent (`requested_season=True if season else False`),
  * the arc guard `apply_arc_season()` returned early, so its
    `title_season_conflicts()` drop never ran,
  * the final season-mismatch exclusion `if season and ...` never ran,
  * and on the download path the re-guess branch of `_check_release()` had no
    season guard at all - unlike its release-notes sibling, which got one in
    maja.19.

Live consequence (2026-08-19, Solo Leveling): Sonarr's S00E01 search returned
three "…S00E01…" releases, and grabbing one made Quasarr log
"Adjusted guessed release title to …S01E01…" and deliver season-1 files. The
episode link is picked positionally (`selection = episode_in_title - 1`), so a
S00E01 title fetches the FIRST link of the selected tab - on a main-season tab
that is S01E01.

The guessed title describes exactly the tab+link selection about to be fetched.
When its season contradicts the grabbed title, renaming to the guess imports the
wrong episode under a plausible name and keeping the grabbed name imports it
under a lying one. Neither is acceptable, so the grab is refused (release_id 0,
which the caller turns into "Download failed").
"""

import unittest
from unittest.mock import patch

from quasarr.downloads.sources import al as al_download
from quasarr.downloads.sources.al import _check_release, apply_arc_season
from quasarr.downloads.sources.helpers.anime_title import ReleaseInfo

DETAILS_HTML = """
<html><head><title>Solo Leveling (Serie)</title></head>
<body><div class="tab-pane" id="download_2"></div></body></html>
"""


def _info(season, ep, release_title=None):
    return ReleaseInfo(
        release_title=release_title,
        audio_langs=["German"],
        subtitle_langs=["German"],
        episode_title=None,
        resolution="1080p",
        audio="AAC",
        video="x264",
        source="WEB-DL",
        release_group="DK",
        season_part=None,
        season=season,
        episode_min=ep,
        episode_max=ep,
    )


class ApplyArcSeasonZeroTests(unittest.TestCase):
    def test_season_zero_is_a_requested_season_not_a_no_op(self):
        # Main-season release offered for a specials request -> must be dropped,
        # exactly as any other season disagreement is.
        info = _info(1, 1, release_title="Solo.Leveling.S01E01.German.DL")
        self.assertIsNone(apply_arc_season(info, season=0, season_specific_match=False))

    def test_season_zero_keeps_a_genuine_special(self):
        info = _info(0, 1, release_title="Solo.Leveling.S00E01.German.DL")
        out = apply_arc_season(info, season=0, season_specific_match=False)
        self.assertIsNotNone(out)
        self.assertEqual("Solo.Leveling.S00E01.German.DL", out.release_title)

    def test_no_requested_season_is_still_a_no_op(self):
        # Regression guard for F4: without a requested season nothing is dropped.
        info = _info(None, 57, release_title="Bleach.E57.German.DL")
        out = apply_arc_season(info, season=None, season_specific_match=False)
        self.assertIsNotNone(out)
        self.assertIsNone(out.season)

    def test_unparseable_season_is_a_no_op(self):
        info = _info(1, 1, release_title="Show.S01E01.German")
        out = apply_arc_season(info, season="not-a-number", season_specific_match=False)
        self.assertIsNotNone(out)


class CheckReleaseSeasonContradictionTests(unittest.TestCase):
    """The download path must never resolve a season contradiction silently."""

    def _run(self, grabbed_title, guessed_title, episode_in_title=1):
        # The re-guess branch is reached when the details page yields no release
        # title of its own; patch both helpers in the consuming namespace so the
        # test drives the guard, not AL's HTML parsing.
        with (
            patch.object(
                al_download,
                "_parse_info_from_download_item",
                return_value=_info(1, episode_in_title),
            ),
            patch.object(al_download, "_guess_title", return_value=guessed_title),
        ):
            return _check_release(
                shared_state=None,
                details_html=DETAILS_HTML,
                release_id=2,
                title=grabbed_title,
                episode_in_title=episode_in_title,
            )

    def test_specials_grab_resolving_to_season_one_is_refused(self):
        # The live case: grabbed as S00E01, details page resolves to S01E01.
        title, release_id = self._run(
            "Solo.Leveling.S00E01.German.DL.GerSub.EngSub.1080p.WEB-DL.x264-DK",
            "Solo.Leveling.S01E01.German.DL.GerSub.EngSub.1080p.WEB-DL.x264-DK",
        )
        # release_id 0 is the caller's abort signal ("No valid release ID found").
        self.assertEqual(0, release_id)
        # The grabbed title is returned unchanged - it is only used for logging.
        self.assertEqual(
            "Solo.Leveling.S00E01.German.DL.GerSub.EngSub.1080p.WEB-DL.x264-DK", title
        )

    def test_cour_style_contradiction_is_refused_too(self):
        # Same guard, the maja.19 direction: grabbed S17, details claims S03.
        _, release_id = self._run(
            "Bleach.Thousand-Year.Blood.War.S17E14.German.ML",
            "Bleach.Thousand-Year.Blood.War.S03E14.German.ML.GerSub",
            episode_in_title=14,
        )
        self.assertEqual(0, release_id)

    def test_absolute_grabbed_title_still_accepts_the_guess(self):
        # F4: no season claim in the grabbed title -> the guess may still win.
        title, release_id = self._run(
            "Bleach.E113.German.DL",
            "Bleach.S06E09.German.DL.1080p",
            episode_in_title=113,
        )
        self.assertEqual(2, release_id)
        self.assertEqual("Bleach.S06E09.German.DL.1080p", title)

    def test_agreeing_season_still_accepts_the_richer_guess(self):
        # The normal case the re-guess exists for: same season, more detail.
        title, release_id = self._run(
            "Bleach.S06E09.German",
            "Bleach.S06E09.German.DL.AAC.1080p.WEB-DL.x264-GRP",
            episode_in_title=9,
        )
        self.assertEqual(2, release_id)
        self.assertEqual("Bleach.S06E09.German.DL.AAC.1080p.WEB-DL.x264-GRP", title)


if __name__ == "__main__":
    unittest.main()
