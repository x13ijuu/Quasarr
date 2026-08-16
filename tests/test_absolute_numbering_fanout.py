# -*- coding: utf-8 -*-
"""
Tests for absolute-numbered fan-out (Maja fork).

Sonarr sends absolute-numbered searches (``E1120``, no season) for
``seriesType=anime``. The fan-out dropped every source lacking
``supports_absolute_numbering`` — which is all of them except AL and AT. The
German sources that actually carry the content were therefore never asked: a
live count on the same episode showed SF returning MORE German-audio hits than
AL. With AL the only source ever queried, its 4.5-day outage in 08/2026 blinded
the entire anime pipeline.

The translation must:
  (1) hand non-absolute sources the (season, episode) pair they organise by,
  (2) leave AL/AT on the absolute form they expect,
  (3) cost ONE Sonarr call per search, not one per source,
  (4) fail open — an unresolvable number behaves exactly as before,
  (5) never touch ordinary season/episode searches.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.constants import SEARCH_CAT_SHOWS
from quasarr.search import get_search_results


class FakeSearchExecutor:
    latest = None

    def __init__(self):
        self.searches = []
        self.added = []
        FakeSearchExecutor.latest = self

    def add(self, source, args, kwargs, **options):
        self.searches.append(source.initials)
        self.added.append((source, args, kwargs, options))

    def run_all(self):
        return [], "", False, 0


def _sources_and_state():
    """'al' speaks absolute numbering, 'sf' does not — the real split."""
    absolute_source = SimpleNamespace(
        initials="al",
        supports_imdb=True,
        supports_absolute_numbering=True,
        supports_date_numbering=False,
        supported_categories=[SEARCH_CAT_SHOWS],
    )
    season_source = SimpleNamespace(
        initials="sf",
        supports_imdb=True,
        supports_absolute_numbering=False,
        supports_date_numbering=False,
        supported_categories=[SEARCH_CAT_SHOWS],
    )
    shared_state = SimpleNamespace(
        values={
            "sonarr_client": object(),
            "config": lambda _section: {
                "al": "al.invalid",
                "sf": "sf.invalid",
            },
        }
    )
    return absolute_source, season_source, shared_state


def _kwargs_by_source():
    return {
        source.initials: kwargs
        for source, _args, kwargs, _options in FakeSearchExecutor.latest.added
    }


class AbsoluteNumberingFanoutTests(unittest.TestCase):
    def _run(self, resolver, season=None, episode=None):
        absolute_source, season_source, shared_state = _sources_and_state()
        with (
            patch(
                "quasarr.search.get_sources",
                return_value={"al": absolute_source, "sf": season_source},
            ),
            patch("quasarr.search.get_imdb_metadata"),
            patch("quasarr.search.get_search_category_sources", return_value=[]),
            patch("quasarr.search.SearchExecutor", FakeSearchExecutor),
            patch(
                "quasarr.providers.sonarr_api.resolve_absolute_numbering",
                side_effect=resolver,
            ) as resolve_mock,
            patch("quasarr.providers.sonarr_api.resolve_scene_numbering",
                  return_value=None),
        ):
            results = get_search_results(
                shared_state,
                "sonarr",
                SEARCH_CAT_SHOWS,
                imdb_id="tt0388629",
                season=season,
                episode=episode,
            )
        return results, resolve_mock

    def test_resolvable_absolute_reaches_season_only_source_translated(self):
        _results, resolve_mock = self._run(lambda *_a, **_k: (22, 35), episode=1120)

        self.assertEqual(
            {"al", "sf"},
            set(FakeSearchExecutor.latest.searches),
            "the season-only source must no longer be skipped",
        )
        kwargs = _kwargs_by_source()
        self.assertEqual(
            {"search_string": "tt0388629", "season": 22, "episode": 35},
            kwargs["sf"],
            "season-only source gets the translated pair",
        )
        self.assertEqual(
            {"search_string": "tt0388629", "season": None, "episode": 1120},
            kwargs["al"],
            "absolute-capable source keeps the absolute form",
        )
        self.assertEqual(
            1,
            resolve_mock.call_count,
            "translation must cost one Sonarr call per search, not per source",
        )

    def test_unresolvable_absolute_keeps_todays_behaviour(self):
        _results, resolve_mock = self._run(lambda *_a, **_k: None, episode=1120)

        self.assertEqual(
            ["al"],
            FakeSearchExecutor.latest.searches,
            "fail-open: only absolute-capable sources run, exactly as before",
        )
        self.assertEqual(
            {"search_string": "tt0388629", "season": None, "episode": 1120},
            _kwargs_by_source()["al"],
        )
        self.assertEqual(1, resolve_mock.call_count)

    def test_ordinary_season_episode_search_is_untouched(self):
        _results, resolve_mock = self._run(
            lambda *_a, **_k: (99, 99), season=2, episode=3
        )

        self.assertEqual({"al", "sf"}, set(FakeSearchExecutor.latest.searches))
        for initials, kwargs in _kwargs_by_source().items():
            with self.subTest(source=initials):
                self.assertEqual(
                    {"search_string": "tt0388629", "season": 2, "episode": 3},
                    kwargs,
                    "a normal search must not be rewritten",
                )
        self.assertEqual(
            0,
            resolve_mock.call_count,
            "no Sonarr call when the request already carries a season",
        )

    def test_season_search_without_episode_is_untouched(self):
        _results, resolve_mock = self._run(lambda *_a, **_k: (9, 9), season=4)

        self.assertEqual({"al", "sf"}, set(FakeSearchExecutor.latest.searches))
        self.assertEqual(0, resolve_mock.call_count)


if __name__ == "__main__":
    unittest.main()
