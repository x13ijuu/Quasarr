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

maja.31: the resolution is a LIST. Sonarr sends TheXEM's scene absolute number,
which restarts per season on many anime — "Slime E17" is S01E17 through S04E17.
Sources that filter locally get every candidate; sources that bake season and
episode into their query get the best one.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.constants import SEARCH_CAT_SHOWS
from quasarr.search import get_search_results


class FakeSearchExecutor:
    latest = None

    def __init__(self, deadline=None):
        self.searches = []
        self.added = []
        self.deadline = deadline
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
        supports_candidate_pairs=False,
        supported_categories=[SEARCH_CAT_SHOWS],
    )
    season_source = SimpleNamespace(
        initials="sf",
        supports_imdb=True,
        supports_absolute_numbering=False,
        supports_date_numbering=False,
        supports_candidate_pairs=True,
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
                "quasarr.providers.sonarr_api.resolve_absolute_candidates",
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
        _results, resolve_mock = self._run(lambda *_a, **_k: [(22, 35)], episode=1120)

        self.assertEqual(
            {"al", "sf"},
            set(FakeSearchExecutor.latest.searches),
            "the season-only source must no longer be skipped",
        )
        kwargs = _kwargs_by_source()
        self.assertEqual(
            {
                "search_string": "tt0388629",
                "season": 22,
                "episode": 35,
                "accepted_pairs": ((22, 35),),
            },
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
        _results, resolve_mock = self._run(lambda *_a, **_k: [], episode=1120)

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

    def test_ambiguous_scene_absolute_hands_every_candidate_to_local_filters(self):
        """Slime: scene absolute 17 means S01E17..S04E17.

        The best candidate (newest season with something still wanted) drives
        the query, but a source that filters locally must be allowed to accept
        any of them — otherwise season 1 wins again, just one layer down.
        """
        candidates = [(4, 17), (3, 17), (2, 17), (1, 17)]
        _results, _resolve_mock = self._run(
            lambda *_a, **_k: list(candidates), episode=17
        )

        kwargs = _kwargs_by_source()
        self.assertEqual(4, kwargs["sf"]["season"], "best candidate drives the query")
        self.assertEqual(17, kwargs["sf"]["episode"])
        self.assertEqual(
            tuple(candidates),
            kwargs["sf"]["accepted_pairs"],
            "every candidate stays acceptable for a locally filtering source",
        )
        self.assertIsInstance(
            kwargs["sf"]["accepted_pairs"],
            tuple,
            "kwargs land im Cache-Schluessel und muessen hashbar sein",
        )
        self.assertEqual(
            None,
            kwargs["al"]["season"],
            "absolute-capable source keeps the absolute form",
        )
        self.assertEqual(17, kwargs["al"]["episode"])
        self.assertNotIn(
            "accepted_pairs",
            kwargs["al"],
            "al does not advertise supports_candidate_pairs in this fixture",
        )

    def test_candidate_kwargs_survive_the_cache_key(self):
        """Regression (live 2026-08-24): accepted_pairs kam als Liste an.

        Der Cache-Schluessel ist ``frozenset(kwargs.items())`` — eine Liste darin
        wirft TypeError im Executor, und eine Suche, die wirft, liefert Sonarr
        NULL Releases. Das sieht aus wie "die Quelle hat nichts" und ist damit
        genau die stille Falschheit, gegen die dieser Fork gebaut wurde. Betroffen
        war jede absolut nummerierte Anime-Suche, auch One Piece.
        """
        from quasarr.search import SearchExecutor, _hashable

        candidates = [(4, 17), (3, 17)]
        self._run(lambda *_a, **_k: list(candidates), episode=17)
        self.assertIsInstance(
            _kwargs_by_source()["sf"]["accepted_pairs"],
            tuple,
            "der Fan-out muss hashbare kwargs bauen",
        )

        # Der ECHTE Executor, nicht die Attrappe: genau hier flog live der
        # TypeError. Ein list-wertiger kwarg darf ihn nicht mehr umwerfen.
        source = SimpleNamespace(initials="sf")
        executor = SearchExecutor()
        executor.add(
            source,
            (None, 0.0, 5000),
            {"search_string": "tt9054364", "accepted_pairs": [(4, 17), (3, 17)]},
        )
        self.assertEqual(1, len(executor.searches))

        for value in ([1, 2], {"a": [1]}, {1, 2}, (1, [2])):
            with self.subTest(value=value):
                hash(_hashable(value))

    def test_ordinary_season_episode_search_is_untouched(self):
        _results, resolve_mock = self._run(
            lambda *_a, **_k: [(99, 99)], season=2, episode=3
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
