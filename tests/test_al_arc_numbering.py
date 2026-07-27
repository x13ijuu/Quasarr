# -*- coding: utf-8 -*-
"""
Tests for anime arc-numbering (Maja fork).

Later arcs of long anime ship on anime-loads under their own title, numbered
relative to the arc and WITHOUT a season marker (Bleach "Thousand-Year Blood War"
= TVDB S17 shows up as "Bleach.E14"; InuYasha "The Final Act" = TVDB S7). F4's
absolute path emits "Bleach.E14", which Sonarr maps E14 -> absolute 14 -> S01E14
(an episode that already exists) and rejects with "Episode wasn't requested".

When the release is found via a season-specific search variant (Sonarr's
"Staffel N" query or the TheXEM arc name), apply_arc_season() stamps the requested
season so guess_release_title rebuilds "Bleach.S17E14..." — which Sonarr's scene
mapping for the arc resolves to the right episode.

This must NOT fire for the plain absolute case (F4): a whole-series match must
still emit "Series.E57" so Sonarr's own absolute mapping applies.
"""
import unittest

from quasarr.downloads.sources.al import apply_arc_season
from quasarr.downloads.sources.helpers.anime_title import (
    ReleaseInfo,
    guess_release_title,
)


def _info(season, ep, release_title=None):
    return ReleaseInfo(
        release_title=release_title,
        audio_langs=["German", "Japanese"],
        subtitle_langs=["German"],
        episode_title=None,
        resolution="1080p",
        audio="DTS",
        video="x264",
        source="BluRay",
        release_group="AnimeBluRayJunkies",
        season_part=None,
        season=season,
        episode_min=ep,
        episode_max=ep,
    )


class ApplyArcSeasonTests(unittest.TestCase):
    def test_arc_match_stamps_requested_season(self):
        # AL parsed the arc release as S1 (no season marker) with a raw title.
        info = _info(1, 14, release_title="Bleach.E14.German.DL.DTS.1080p.BluRay")
        out = apply_arc_season(info, season=17, season_specific_match=True)
        self.assertEqual(17, out.season)
        # Raw arc title dropped so the guess rebuilds WITH the season.
        self.assertIsNone(out.release_title)
        title = guess_release_title("Bleach", out)
        self.assertIn(".S17E14", title)
        self.assertNotIn(".S01E14", title)

    def test_arc_match_when_season_none(self):
        info = _info(None, 14, release_title="Bleach.E14.German.DL")
        out = apply_arc_season(info, season=17, season_specific_match=True)
        self.assertEqual(17, out.season)
        self.assertIn(".S17E14", guess_release_title("Bleach", out))

    def test_generic_match_left_absolute_for_f4(self):
        # NOT season-specific (whole-series search): keep F4 absolute behaviour.
        info = _info(None, 57, release_title=None)
        out = apply_arc_season(info, season=None, season_specific_match=False)
        self.assertIsNone(out.season)
        title = guess_release_title("Bleach", out)
        self.assertIn(".E57", title)
        self.assertNotIn("S01", title)

    def test_season_specific_but_season_already_matches_is_noop(self):
        # Real S02 release found via season search: keep its own title, don't reset.
        info = _info(2, 5, release_title="Bleach.S02E05.German.DL")
        out = apply_arc_season(info, season=2, season_specific_match=True)
        self.assertEqual(2, out.season)
        self.assertEqual("Bleach.S02E05.German.DL", out.release_title)

    def test_no_season_requested_is_noop(self):
        info = _info(1, 14, release_title="Bleach.E14.German")
        out = apply_arc_season(info, season=None, season_specific_match=True)
        self.assertEqual(1, out.season)
        self.assertEqual("Bleach.E14.German", out.release_title)

    def test_non_integer_season_is_ignored(self):
        info = _info(1, 14, release_title="Bleach.E14")
        out = apply_arc_season(info, season="abc", season_specific_match=True)
        self.assertEqual(1, out.season)


if __name__ == "__main__":
    unittest.main()
