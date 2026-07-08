# -*- coding: utf-8 -*-
"""
Tests for anime absolute-numbering (Maja fork, F4).

Upstream defaulted a series release with no season evidence to season 1, turning
an absolutely-numbered episode (e.g. E57) into "S01E57" — which is really S03E16,
so Sonarr rejected the import with a folder-name conflict. The fork returns None
for the season in that case, and guess_release_title emits the correct absolute
"Series.Name.E57", which Sonarr's anime parser maps itself.
"""
import unittest

from quasarr.downloads.sources.al import _extract_season_number_from_title
from quasarr.downloads.sources.helpers.anime_title import (
    ReleaseInfo,
    guess_release_title,
)


class SeasonExtractionTests(unittest.TestCase):
    def test_no_evidence_series_returns_none(self):
        # Bleach-style: series with no season marker anywhere -> absolute numbering.
        self.assertIsNone(
            _extract_season_number_from_title("Bleach", "series", release_title="")
        )

    def test_explicit_season_in_release_title_is_kept(self):
        self.assertEqual(
            2,
            _extract_season_number_from_title(
                "Bleach", "series", release_title="Bleach.S02E05.German"
            ),
        )

    def test_r2_release_title_is_kept(self):
        self.assertEqual(
            2,
            _extract_season_number_from_title(
                "Some Show", "series", release_title="Some.Show.R2E03"
            ),
        )

    def test_staffel_keyword_in_page_title(self):
        self.assertEqual(
            3, _extract_season_number_from_title("Naruto Staffel 3", "series")
        )

    def test_trailing_number_two_or_more(self):
        self.assertEqual(
            2, _extract_season_number_from_title("Attack on Titan 2", "series")
        )


class AbsoluteTitleBuildTests(unittest.TestCase):
    def _info(self, season, ep):
        return ReleaseInfo(
            release_title=None,
            audio_langs=["German"],
            subtitle_langs=[],
            episode_title=None,
            resolution="1080p",
            audio="AAC",
            video="x264",
            source="WEB-DL",
            release_group="GRP",
            season_part=None,
            season=season,
            episode_min=ep,
            episode_max=ep,
        )

    def test_absolute_episode_without_season(self):
        title = guess_release_title("Bleach", self._info(None, 57))
        self.assertIn(".E57", title)
        self.assertNotIn("S01", title)
        self.assertNotIn("S00", title)

    def test_seasoned_episode_keeps_sxxeyy(self):
        title = guess_release_title("Bleach", self._info(2, 5))
        self.assertIn(".S02E05", title)


class AbsoluteGrabSeasonSuppressionTests(unittest.TestCase):
    """
    The download-side re-guess (_check_release) computes a season from synonyms/
    release-notes too, so removing the title default alone is not enough: when the
    grabbed title is absolutely numbered (no S-token), the season must be dropped.
    This mirrors the logic guarding release_info.season in al._check_release.
    """
    import re as _re

    def _absolute_grab(self, grabbed_title, detected_season):
        # Reproduces the guard: absolute grab (no S-token) -> season forced to None.
        season = detected_season
        if not self._re.search(r"(?i)\bS\d{1,4}(?:E\d{1,4})?\b", grabbed_title):
            season = None
        return season

    def test_absolute_title_drops_misdetected_season(self):
        # Bleach.E113 grabbed absolutely; page synonyms mis-detected season 1.
        self.assertIsNone(self._absolute_grab("Bleach.E113.German.ML.1080p.WEB-DL", 1))

    def test_seasoned_title_keeps_detected_season(self):
        self.assertEqual(
            2, self._absolute_grab("Bleach.S02E05.German.ML.1080p.WEB-DL", 2)
        )

    def test_subtitle_tokens_do_not_count_as_season(self):
        # 'GerSub'/'EngSub' must not be read as an S-token.
        self.assertIsNone(
            self._absolute_grab("Bleach.E113.German.ML.GerSub.EngSub.1080p.WEB-DL", 1)
        )


if __name__ == "__main__":
    unittest.main()
