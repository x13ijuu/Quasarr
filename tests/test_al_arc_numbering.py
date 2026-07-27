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

from quasarr.downloads.sources.al import (
    apply_arc_season,
    details_title_overrides_grabbed,
    title_season,
    title_season_conflicts,
)
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


class TitleSeasonTruthTests(unittest.TestCase):
    """
    AL splits long arcs into its own cours and names them as seasons: the third
    Thousand-Year-Blood-War cour ships as "Bleach.Thousand-Year.Blood.War.S03E14"
    although it is TVDB S17E14. The release is attributed to season 17 correctly,
    but Sonarr believes the TITLE and mapped it onto S03E14 — an episode that
    already exists, so a good file would have been overwritten (caught live
    2026-07-27, queue item removed before it imported).
    """

    def test_conflicting_title_season_is_dropped(self):
        info = _info(
            17, 14, release_title="Bleach.Thousand-Year.Blood.War.S03E14.German.ML"
        )
        out = apply_arc_season(info, season=17, season_specific_match=False)
        self.assertIsNone(out.release_title)
        self.assertEqual(17, out.season)
        self.assertIn(".S17E14", guess_release_title("Bleach Thousand-Year Blood War", out))

    def test_matching_title_season_is_kept(self):
        info = _info(17, 14, release_title="Bleach.S17E14.German.DL")
        out = apply_arc_season(info, season=17, season_specific_match=False)
        self.assertEqual("Bleach.S17E14.German.DL", out.release_title)

    def test_absolute_title_carries_no_season_claim(self):
        # F4: "Bleach.E14" must survive untouched — no S-token, no conflict.
        info = _info(17, 14, release_title="Bleach.E14.German.DL")
        out = apply_arc_season(info, season=17, season_specific_match=False)
        self.assertEqual("Bleach.E14.German.DL", out.release_title)

    def test_conflict_helper(self):
        self.assertTrue(title_season_conflicts("Show.S03E14.German", 17))
        self.assertFalse(title_season_conflicts("Show.S17E14.German", 17))
        self.assertFalse(title_season_conflicts("Show.E14.German", 17))
        self.assertFalse(title_season_conflicts(None, 17))


class DownloadTitleSeasonTests(unittest.TestCase):
    """
    Third variant of the same damage, on the download path: Sonarr grabbed the
    correct "…Blood.War.S17E14", but _check_release re-read the details page and
    replaced it with AL's cour title "…Blood.War.S03E14". Sonarr then re-mapped
    the download onto the existing S03E14 (caught live 2026-07-27 — the queue
    item was removed before it could overwrite a good file).
    """

    def test_conflicting_details_season_does_not_override(self):
        self.assertFalse(
            details_title_overrides_grabbed(
                "Bleach.Thousand-Year.Blood.War.S17E14.German.ML",
                "Bleach.Thousand-Year.Blood.War.S03E14.German.ML.GerSub",
            )
        )

    def test_same_season_details_title_still_wins(self):
        # The normal case: details page adds group/quality detail, same season.
        self.assertTrue(
            details_title_overrides_grabbed(
                "Bleach.S17E14.German",
                "Bleach.S17E14.German.DL.DTS.1080p.BluRay.x264-GRP",
            )
        )

    def test_absolute_grabbed_title_keeps_old_behaviour(self):
        # No season claim in the grabbed title (F4) -> details title may win.
        self.assertTrue(
            details_title_overrides_grabbed(
                "Bleach.E113.German", "Bleach.S06E09.German.DL"
            )
        )

    def test_title_season_helper(self):
        self.assertEqual(17, title_season("Show.S17E14.German"))
        self.assertEqual(3, title_season("S03E14.German"))
        self.assertIsNone(title_season("Show.E14.German"))
        self.assertIsNone(title_season(None))
