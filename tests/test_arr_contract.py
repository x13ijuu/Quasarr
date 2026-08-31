# -*- coding: utf-8 -*-
"""
Contract tests for what the *arr clients actually see (Maja fork).

Every regression this fork has shipped was found in production. The suite it
inherited tests parsers, matchers and helpers in isolation, and all of them
were green on 2026-08-31 while One Piece E1176 was grabbed **29 times in seven
hours** — because the defect was not in any single unit. It was in the contract
between Quasarr and Sonarr, which nothing tested:

  * Sonarr matches usenet releases against its blocklist on publication date.
    AL states age relatively ("vor 7 Stunden"), and turning that into an
    absolute timestamp per request produced a NEW date every poll. So Sonarr
    blocklisted the release 29 times and recognised it zero times.
  * The download path knew every single time that the release could not be
    resolved ("Refusing feed grab: no tabs"), and the next feed offered it
    again unchanged, because a failure to resolve was never written down.

Neither is visible from inside a unit. Both are obvious the moment you ask the
question a client asks: *poll twice and compare*.

These tests therefore assert properties of the feed ACROSS polls, and of the
search results a client would receive, with the clock deliberately moved in
between. The fake Sonarr is the same fake-client pattern the rest of the suite
uses — hermetic, no network, no JDownloader.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.identity import first_seen, refusals
from quasarr.providers import shared_state as shared_state_module
from quasarr.providers.sonarr_api import (
    resolve_absolute_candidates,
    resolve_scene_numbering,
)
from quasarr.search.sources import al as al_search

HOST = "al.invalid"
SERIES_URL = f"https://www.{HOST}/media/synthetic-show"
TITLE = "Synthetic.Show.E1176.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"


def _feed_html(relative_age="vor 7 Stunden"):
    """One "new episodes" row, the shape the AL feed parser expects."""
    return f"""
    <html><body>
      <div id="episodes_updates_list"><table><tbody>
        <tr>
          <p><a href="{SERIES_URL}" data-original-title="Synthetic Show">Synthetic Show</a></p>
          <div class="label-group"><a href="https://www.{HOST}/anime-series">Serie</a></div>
          <small class="text-muted">{relative_age}</small>
          <div class="mt10">
            <span>Episode 1176</span>
            <span>German, ML, GerSub, EngSub, 1080p, WEB-DL, x264</span>
          </div>
        </tr>
      </tbody></table></div>
    </body></html>
    """


class _ShiftedDatetime(datetime):
    """A clock the source module reads that is `_SHIFT` ahead of the real one."""

    _SHIFT = timedelta(0)

    @classmethod
    def utcnow(cls):
        return datetime.utcnow() + cls._SHIFT


def _clock_advanced_by(**delta):
    """Move the clock the AL date parser derives its timestamps from."""
    shifted = type("Shifted", (_ShiftedDatetime,), {"_SHIFT": timedelta(**delta)})
    return patch.object(al_search, "datetime", shifted)


class _FakeResponse:
    def __init__(self, html):
        self.content = html.encode("utf-8")
        self.text = html
        self.status_code = 200

    def raise_for_status(self):
        return None


class _StateBackedTest(unittest.TestCase):
    """Real sqlite in a temp dir — the anchor and the ledger are the subject."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_values = getattr(shared_state_module, "values", None)
        self._old_lock = getattr(shared_state_module, "lock", None)
        config_path = os.path.join(self.tmp.name, "Quasarr.ini")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write("[Hostnames]\nal = " + HOST + "\n")
        shared_state_module.values = {
            "dbfile": os.path.join(self.tmp.name, "Quasarr.db"),
            "configfile": config_path,
            "config": lambda _section: {"al": HOST},
            "internal_address": "http://quasarr.invalid:8080",
        }
        shared_state_module.lock = None

    def tearDown(self):
        shared_state_module.values = self._old_values
        shared_state_module.lock = self._old_lock

    def _poll_feed(self, html=None):
        response = _FakeResponse(html if html is not None else _feed_html())
        with patch.object(
            al_search, "fetch_via_requests_session", return_value=response
        ):
            return al_search.Source().feed(
                shared_state_module, start_time=0.0, search_category="5000"
            )


class PublicationDateIsStableTests(_StateBackedTest):
    """The property Sonarr's blocklist depends on."""

    def test_the_same_release_keeps_its_date_across_polls(self):
        """The exact experiment that exposed the loop, as a test.

        Two polls of one unchanged release, with real time moving in between —
        which is all it took, because the date was "now minus seven hours"
        recomputed per request.
        """
        first = self._poll_feed()
        self.assertTrue(first, "fixture must yield a release, otherwise nothing is tested")

        with _clock_advanced_by(hours=2):
            second = self._poll_feed()

        self.assertEqual(
            first[0]["details"]["date"],
            second[0]["details"]["date"],
            "a release must not change its publication date between polls — "
            "Sonarr's blocklist keys on it, and a moving date makes the "
            "blocklist unable to hold the release at all",
        )

    def test_the_date_survives_the_source_restating_the_age(self):
        # AL rounds: the same release reads "vor 7 Stunden" and an hour later
        # "vor 8 Stunden". Both describe one publication, so both must render
        # the one date that was written down first.
        first = self._poll_feed(_feed_html("vor 7 Stunden"))
        second = self._poll_feed(_feed_html("vor 8 Stunden"))
        self.assertEqual(first[0]["details"]["date"], second[0]["details"]["date"])

    def test_a_stated_absolute_date_is_passed_through_untouched(self):
        # Anchoring exists for DERIVED dates. A source that states a real
        # timestamp is already stable and must not be second-guessed.
        html = _feed_html("13.07.2026 - 21:14")
        releases = self._poll_feed(html)
        self.assertTrue(releases)
        self.assertIn("13 Jul 2026", releases[0]["details"]["date"])

    def test_the_anchor_is_per_release_not_per_source(self):
        one = first_seen.anchor("al", SERIES_URL, "Show.E01", "Mon, 03 Aug 2026 10:00:00 +0000")
        two = first_seen.anchor("al", SERIES_URL, "Show.E02", "Tue, 04 Aug 2026 11:00:00 +0000")
        self.assertNotEqual(one, two, "two episodes are two releases, not one")


class UnresolvableReleaseIsHeldBackTests(_StateBackedTest):
    """The other half: a refusal nobody records is a refusal that repeats."""

    def _search_result(self, title=TITLE, source=SERIES_URL, hostname="al"):
        return {
            "details": {
                "title": title,
                "hostname": hostname,
                "source": source,
                "link": "http://quasarr.invalid/download/?payload=x",
            },
            "type": "protected",
        }

    def test_a_release_that_could_not_be_resolved_stops_being_offered(self):
        offered, dropped = refusals.filter_refused([self._search_result()])
        self.assertEqual(1, len(offered), "nothing is held back before it fails")

        refusals.record_unresolvable("al", SERIES_URL, TITLE, "no matching release on page")

        offered, dropped = refusals.filter_refused([self._search_result()])
        self.assertEqual(
            [], offered,
            "after the download path could not resolve it, the next feed must "
            "not offer it again — that repetition is the loop",
        )
        self.assertEqual(1, len(dropped))

    def test_the_hold_lapses_so_a_late_release_still_arrives(self):
        # "Not there yet" is usually temporary: a source announces an episode
        # before the release is posted. A permanent refusal would turn a few
        # hours of lag into an unobtainable episode.
        refusals.record_unresolvable("al", SERIES_URL, TITLE, "no tabs", now=0)

        offered, _ = refusals.filter_refused([self._search_result()], now=60)
        self.assertEqual([], offered, "held back while the backoff runs")

        offered, _ = refusals.filter_refused(
            [self._search_result()], now=refusals.SOFT_BACKOFF_S[0] + 1
        )
        self.assertEqual(1, len(offered), "and offered again once it lapses")

    def test_repeated_failures_back_off_further_each_time(self):
        holds = []
        for attempt in range(1, 5):
            refusals.record_unresolvable("al", SERIES_URL, TITLE, "no tabs", now=0)
            blob = refusals.all_refusals()[
                refusals.refusal_key("al", SERIES_URL, TITLE)
            ]
            holds.append(blob["expires_at"])
            self.assertEqual(attempt, blob["attempts"])
        self.assertEqual(sorted(holds), holds, "each failure must hold it longer")
        self.assertEqual(list(refusals.SOFT_BACKOFF_S), holds)

    def test_a_hold_covers_one_episode_not_the_whole_series(self):
        """The 2027 Bleach lockout, as a test.

        On AL the payload URL is the SERIES page, shared by every episode. A
        refusal keyed on it alone silenced a whole show for 180 days because
        one episode had lied about its language.
        """
        refusals.record_refusal(
            "al", SERIES_URL, TITLE, {"German"}, {"Japanese"}
        )
        sibling = self._search_result(
            title="Synthetic.Show.E1177.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"
        )
        offered, dropped = refusals.filter_refused([sibling])
        self.assertEqual(
            1, len(offered),
            "the next episode of the same series must still be offered",
        )
        self.assertEqual([], dropped)

    def test_a_proven_lie_outranks_a_missing_release(self):
        refusals.record_refusal("al", SERIES_URL, TITLE, {"German"}, {"Japanese"})
        self.assertFalse(
            refusals.record_unresolvable("al", SERIES_URL, TITLE, "no tabs"),
            "a hard refusal must not be downgraded to an expiring one",
        )
        offered, _ = refusals.filter_refused(
            [self._search_result()], now=refusals.SOFT_BACKOFF_S[-1] * 10
        )
        self.assertEqual([], offered, "and it must still hold long after any backoff")


class AnimeHandlingStaysOffOrdinarySeriesTests(unittest.TestCase):
    """Rufus' question: does the anime work still let normal series through?

    These paths run on searches for every series. The guards make the answer a
    property of the code rather than something to re-investigate per incident.
    """

    IMDB = "tt1234567"

    class _FakeSonarr:
        def __init__(self, series_type):
            self._series_type = series_type
            self.episode_calls = 0

        def series_list(self):
            return [
                {
                    "imdbId": AnimeHandlingStaysOffOrdinarySeriesTests.IMDB,
                    "id": 7,
                    "seriesType": self._series_type,
                }
            ]

        def episodes(self, _series_id, season=None):
            self.episode_calls += 1
            return [
                {
                    "seasonNumber": 5,
                    "episodeNumber": 10,
                    "absoluteEpisodeNumber": 60,
                    "sceneAbsoluteEpisodeNumber": 10,
                    "hasFile": False,
                    "monitored": True,
                }
            ]

    def _run(self, func, series_type, *args):
        client = self._FakeSonarr(series_type)
        with patch("quasarr.providers.sonarr_api.get_client", return_value=client):
            return func(SimpleNamespace(values={}), self.IMDB, *args), client

    def test_scene_translation_leaves_a_standard_series_alone(self):
        result, client = self._run(resolve_scene_numbering, "standard", 5, 99)
        self.assertIsNone(result)
        self.assertEqual(
            0, client.episode_calls,
            "and it costs no Sonarr round trip against the fan-out deadline",
        )

    def test_absolute_candidates_are_not_built_for_a_standard_series(self):
        result, client = self._run(resolve_absolute_candidates, "standard", 10)
        self.assertEqual([], result)
        self.assertEqual(0, client.episode_calls)

    def test_anime_still_gets_both(self):
        # The guard must not be a way of switching the feature off.
        result, _ = self._run(resolve_absolute_candidates, "anime", 10)
        self.assertEqual([(5, 10)], result, "anime keeps its candidate resolution")

    def test_the_language_claim_check_is_scoped_to_block_labelling_sources(self):
        # A title is only a CLAIM where the source labels by release block.
        # Elsewhere a mismatch against a filename is far more likely to be
        # Dual-Audio or a sample than a lie, and the penalty for reading one of
        # those as a lie is a release that can never be obtained again.
        self.assertTrue(refusals.advertises_block_language("al"))
        self.assertTrue(refusals.advertises_block_language("AT"))
        for source_key in ("wx", "nx", "sf", "dl", "", None):
            with self.subTest(source=source_key):
                self.assertFalse(refusals.advertises_block_language(source_key))


if __name__ == "__main__":
    unittest.main()
