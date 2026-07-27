# -*- coding: utf-8 -*-
"""
Tests for scene-numbering translation (Maja fork).

Sonarr applies TheXEM scene mappings BEFORE querying an indexer. Anime whose
mapping collapses every season into scene season 1 with absolute numbering
therefore arrive as a pair that does not exist: InuYasha TVDB S07E01 reaches us
as "season 1, episode 168". DDL sites organise those shows by TVDB seasons
("Staffel 7"), so the scene request finds nothing while the TVDB one finds the
whole arc (verified live: S07E01 -> 14 releases, S01E168 -> 0).

translate_scene_numbering must fire ONLY for that case: valid pairs (Slime
S01E02, Bleach S17E14 — whose scene season really is 17) and absolute-only
searches must pass through untouched.
"""
import unittest

from quasarr.providers.sonarr_api import translate_scene_numbering


def _ep(season, episode, absolute=None):
    return {
        "seasonNumber": season,
        "episodeNumber": episode,
        "absoluteEpisodeNumber": absolute,
    }


# InuYasha: 7 seasons, absolute numbering continues across them.
INUYASHA_ALL = (
    [_ep(1, n, n) for n in range(1, 28)]  # S1: abs 1-27
    + [_ep(2, n, 27 + n) for n in range(1, 28)]  # S2: abs 28-54
    + [_ep(7, n, 167 + n) for n in range(1, 27)]  # S7 (Final Act): abs 168-193
)
INUYASHA_S1 = [e for e in INUYASHA_ALL if e["seasonNumber"] == 1]


class SceneNumberingTranslationTests(unittest.TestCase):
    def test_scene_absolute_in_season_one_translates(self):
        # The real case: Sonarr asks S01E168, which is TVDB S07E01.
        self.assertEqual(
            (7, 1),
            translate_scene_numbering(1, 168, INUYASHA_S1, INUYASHA_ALL),
        )

    def test_last_arc_episode_translates(self):
        self.assertEqual(
            (7, 26),
            translate_scene_numbering(1, 193, INUYASHA_S1, INUYASHA_ALL),
        )

    def test_existing_pair_is_untouched(self):
        # S01E05 exists -> must not be rewritten (would break normal searches).
        self.assertIsNone(
            translate_scene_numbering(1, 5, INUYASHA_S1, INUYASHA_ALL)
        )

    def test_valid_high_season_pair_untouched(self):
        # Bleach-style: scene season really is 17 and S17E14 exists.
        bleach_s17 = [_ep(17, n, 366 + n) for n in range(1, 30)]
        self.assertIsNone(
            translate_scene_numbering(17, 14, bleach_s17, bleach_s17)
        )

    def test_unknown_absolute_number_gives_up(self):
        self.assertIsNone(
            translate_scene_numbering(1, 999, INUYASHA_S1, INUYASHA_ALL)
        )

    def test_missing_numbers_are_ignored(self):
        self.assertIsNone(translate_scene_numbering(None, 168, [], INUYASHA_ALL))
        self.assertIsNone(translate_scene_numbering(1, None, [], INUYASHA_ALL))

    def test_non_integer_input_is_ignored(self):
        self.assertIsNone(
            translate_scene_numbering("abc", 168, INUYASHA_S1, INUYASHA_ALL)
        )

    def test_string_numbers_are_accepted(self):
        # Newznab params arrive as strings.
        self.assertEqual(
            (7, 1),
            translate_scene_numbering("1", "168", INUYASHA_S1, INUYASHA_ALL),
        )

    def test_episode_without_season_data_is_skipped(self):
        broken = [{"absoluteEpisodeNumber": 168}]
        self.assertIsNone(translate_scene_numbering(1, 168, [], broken))

    def test_identity_translation_is_reported_as_no_change(self):
        # An absolute number that resolves to the very pair asked for.
        same = [_ep(1, 168, 168)]
        self.assertIsNone(translate_scene_numbering(1, 168, [], same))


if __name__ == "__main__":
    unittest.main()
