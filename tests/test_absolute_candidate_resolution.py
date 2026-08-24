# -*- coding: utf-8 -*-
"""
Tests for scene-aware absolute resolution (Maja fork, maja.31).

Sonarr does not send the TVDB absolute number — it sends TheXEM's SCENE
absolute number. For anime whose mapping restarts absolute numbering per season
those are different numbers, and the scene one is ambiguous:

    Slime S01E17  absolute 17   scene absolute 17
    Slime S02E17  absolute 41   scene absolute 17
    Slime S03E17  absolute 65   scene absolute 17
    Slime S04E17  absolute 89   scene absolute 17

``resolve_absolute_numbering`` matched only ``absoluteEpisodeNumber``, so a
request for S04E17 arrived as "17", resolved to S01E17, and every source was
asked for season 1. Measured live on 2026-08-24: nine season-1 releases came
back and SF's correct ``S04E17.German.DL`` was discarded as a season mismatch.

The resolver must:
  (1) prefer scene hits — that is the numbering Sonarr speaks,
  (2) return EVERY candidate, best first, instead of silently picking one,
  (3) rank what Sonarr still wants first, then the newest season,
  (4) leave genuinely absolute-numbered shows (One Piece) untouched,
  (5) drop specials and unmonitored seasons, but never to the point of
      returning nothing.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.providers.sonarr_api import (
    resolve_absolute_candidates,
    resolve_absolute_numbering,
)

IMDB = "tt9054364"


def _ep(season, episode, absolute, scene_absolute, has_file=True, monitored=True):
    return {
        "seasonNumber": season,
        "episodeNumber": episode,
        "absoluteEpisodeNumber": absolute,
        "sceneAbsoluteEpisodeNumber": scene_absolute,
        "hasFile": has_file,
        "monitored": monitored,
    }


# Slime: four seasons of 24, scene absolute restarts every season.
SLIME = []
for _season in range(1, 5):
    for _number in range(1, 25):
        SLIME.append(
            _ep(
                _season,
                _number,
                (_season - 1) * 24 + _number,
                _number,
                has_file=True,
            )
        )
# Specials exist and are unmonitored, exactly as in the live instance.
SLIME += [_ep(0, n, None, n, has_file=False, monitored=False) for n in range(1, 15)]

# One Piece: one long season, absolute == scene absolute (no XEM reset).
ONE_PIECE = [_ep(21, n, 1000 + n, 1000 + n) for n in range(1, 200)]


class _FakeClient:
    def __init__(self, episodes):
        self._episodes = episodes
        self.episode_calls = 0

    def series_list(self):
        return [{"imdbId": IMDB, "id": 22}]

    def episodes(self, _series_id, season=None):
        self.episode_calls += 1
        if season is None:
            return self._episodes
        return [e for e in self._episodes if e["seasonNumber"] == season]


def _resolve(episodes, number, func=resolve_absolute_candidates):
    client = _FakeClient(episodes)
    with patch("quasarr.providers.sonarr_api.get_client", return_value=client):
        return func(SimpleNamespace(values={}), IMDB, number)


class AbsoluteCandidateResolutionTests(unittest.TestCase):
    def test_scene_absolute_yields_every_season_newest_first(self):
        self.assertEqual(
            [(4, 17), (3, 17), (2, 17), (1, 17)],
            _resolve(SLIME, 17),
            "all four seasons are possible; the newest is the best guess",
        )

    def test_best_candidate_is_not_season_one(self):
        # The whole bug in one assertion.
        self.assertEqual((4, 17), _resolve(SLIME, 17, resolve_absolute_numbering))

    def test_missing_episode_outranks_present_ones(self):
        episodes = [dict(e) for e in SLIME]
        for episode in episodes:
            if (episode["seasonNumber"], episode["episodeNumber"]) == (2, 17):
                episode["hasFile"] = False
        self.assertEqual(
            (2, 17),
            _resolve(episodes, 17)[0],
            "what Sonarr is still missing beats the newest season",
        )

    def test_specials_and_unmonitored_are_dropped(self):
        for season, _episode in _resolve(SLIME, 3):
            self.assertGreater(season, 0, "season 0 is never what an absolute means")

    def test_true_absolute_numbering_is_unchanged(self):
        self.assertEqual([(21, 74)], _resolve(ONE_PIECE, 1074))
        self.assertEqual((21, 74), _resolve(ONE_PIECE, 1074, resolve_absolute_numbering))

    def test_tvdb_absolute_still_resolves_when_no_scene_mapping_exists(self):
        episodes = [dict(e, sceneAbsoluteEpisodeNumber=None) for e in SLIME]
        self.assertEqual(
            [(4, 17)],
            _resolve(episodes, 89),
            "without scene numbers the TVDB absolute is the truth",
        )

    def test_unknown_number_fails_open(self):
        self.assertEqual([], _resolve(SLIME, 9999))
        self.assertIsNone(_resolve(SLIME, 9999, resolve_absolute_numbering))

    def test_resolution_costs_one_episode_call(self):
        client = _FakeClient(SLIME)
        with patch("quasarr.providers.sonarr_api.get_client", return_value=client):
            resolve_absolute_candidates(SimpleNamespace(values={}), IMDB, 17)
        self.assertEqual(
            1, client.episode_calls, "one Sonarr call per search, not per candidate"
        )

    def test_no_sonarr_client_fails_open(self):
        with patch("quasarr.providers.sonarr_api.get_client", return_value=None):
            self.assertEqual(
                [], resolve_absolute_candidates(SimpleNamespace(values={}), IMDB, 17)
            )


if __name__ == "__main__":
    unittest.main()
