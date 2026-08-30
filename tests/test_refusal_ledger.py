# -*- coding: utf-8 -*-
"""
Tests for the refusal ledger (Maja fork, maja.33).

AL advertised `…S04E19.German.ML.GerSub.EngSub…` and delivered
`…S04E19.Japanese.GerSub.EngSub…`. Sonarr imported the file, saw no German
audio, discarded it and searched again ~15 minutes later — 93 grabs of ONE
episode in 14 days. Every pass looked like a success, so nothing stopped it.

The ledger's job is narrow: once the delivered files disprove the title's audio
claim, that release is never offered again. Two properties matter more than
coverage:

  * it must fire on the real case, and
  * it must stay silent on everything it cannot prove — a false refusal makes a
    release permanently unobtainable, which is worse than one more wrong grab.

Subtitles are not audio. `GerSub` on a Japanese release is a German subtitle,
not a German dub; reading it as audio would refuse exactly the releases we want.
"""

import json
import os
import tempfile
import unittest

from quasarr.identity import refusals
from quasarr.providers import shared_state as shared_state_module
from quasarr.storage.sqlite_database import DataBase

SLIME_CLAIM = (
    "Meine.Wiedergeburt.als.Schleim.in.einer.anderen.Welt."
    "S04E19.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"
)
SLIME_DELIVERED = (
    "Meine.Wiedergeburt.als.Schleim.in.einer.anderen.Welt."
    "S04E19.Japanese.GerSub.EngSub.1080p.WEB-DL.x264-Puddingsama.mkv"
)


class LanguageComparisonTests(unittest.TestCase):
    """No DB — pure judgement about claim vs. delivery."""

    def test_the_real_case_is_a_contradiction(self):
        result = refusals.language_contradiction(SLIME_CLAIM, [SLIME_DELIVERED])
        self.assertIsNotNone(result, "the 93-grab loop must be detected")
        claimed, delivered = result
        self.assertIn("German", claimed)
        self.assertIn("Japanese", delivered)
        self.assertNotIn("German", delivered)

    def test_gersub_is_a_subtitle_not_a_dub(self):
        # A naive substring check reads "GerSub" as German and would clear the
        # very releases this ledger exists to catch.
        self.assertEqual(
            {"Japanese"},
            refusals.audio_languages("Show.S01E01.Japanese.GerSub.EngSub"),
            "subtitle tokens must not become audio languages",
        )
        self.assertEqual(
            {"German"},
            refusals.audio_languages("Show.S01E01.German.DL.GerSub.EngSub"),
            "a real German dub is still recognised next to its subtitles",
        )

    def test_honest_delivery_is_not_refused(self):
        self.assertIsNone(
            refusals.language_contradiction(
                SLIME_CLAIM, ["Slime.S04E19.German.DL.1080p.WEB-DL.x264.mkv"]
            )
        )

    def test_file_without_language_information_proves_nothing(self):
        # The dangerous silent case: refusing here would kill good releases.
        for name in ("episode17.mkv", "S04E19.mkv", "", "part1.rar"):
            with self.subTest(name=name):
                self.assertIsNone(refusals.language_contradiction(SLIME_CLAIM, [name]))

    def test_no_files_at_all_proves_nothing(self):
        self.assertIsNone(refusals.language_contradiction(SLIME_CLAIM, []))
        self.assertIsNone(refusals.language_contradiction(SLIME_CLAIM, None))

    def test_title_that_never_claimed_german_is_out_of_scope(self):
        self.assertIsNone(
            refusals.language_contradiction(
                "One.Piece.E1120.Japanese.EngSub.720p.WEB-DL.x264",
                ["One.Piece.E1120.Japanese.EngSub.720p.mkv"],
            )
        )

    def test_one_german_file_among_many_clears_the_package(self):
        self.assertIsNone(
            refusals.language_contradiction(
                SLIME_CLAIM,
                [
                    "Slime.S04E19.Japanese.GerSub.mkv",
                    "Slime.S04E19.German.DL.mkv",
                ],
            )
        )

    def test_bracket_notation_counts_as_german(self):
        # AL's other naming school: "650 - ONE PIECE [GER-JAP].mp4"
        self.assertIn(
            "German", refusals.audio_languages("650 - ONE PIECE [GER-JAP].mp4")
        )

    def test_split_subtitle_marker_is_not_audio(self):
        # The second naming school for the same statement: the languages are
        # listed once and "Sub" trails the whole list. Token-by-token this reads
        # as "German audio, English audio" — the exact opposite of what it says,
        # and it cleared rule 3 through 74 grabs of one episode before it was
        # found.
        for name in (
            "Show.Name.2018.S04E20.Ger.Eng.Sub.AAC.1080p.WebDL.x264-Group.mkv",
            "Show.Name.S04E20.Ger-Eng-Sub.1080p.mkv",
            "Show.Name.S04E20.Ger.Sub.1080p.mkv",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    set(),
                    refusals.audio_languages(name),
                    "a trailing 'Sub' governs every language in front of it",
                )

    def test_split_subtitle_marker_does_not_swallow_a_real_dub(self):
        # The guard rail for the rule above: an actual dub named next to its
        # subtitle list must survive, or the ledger starts refusing the very
        # releases it exists to protect.
        self.assertEqual(
            {"German"},
            refusals.audio_languages(
                "Show.Name.S04E20.German.DL.Ger.Eng.Sub.1080p.mkv"
            ),
        )
        self.assertEqual(
            {"Japanese"},
            refusals.audio_languages("Show.Name.S04E20.Japanese.Ger.Eng.Sub.1080p.mkv"),
        )


class AdvertisedClaimTests(unittest.TestCase):
    """What the completion path feeds in (mirrors get_packages).

    A source may re-guess the title from its details page, in which case the
    name JDownloader carries is already CORRECTED. Comparing that against
    itself can only ever come out clean — so the claim comes from the grab
    note, and the corrected name joins the delivered evidence.
    """

    ADVERTISED = "Show.Name.S04E20.German.ML.GerSub.EngSub.1080p.WEB-DL.x264"
    REGUESSED = "Show.Name.S04E20.Japanese.GerSub.EngSub.1080p.WEB-DL.x264-Group"
    FILENAME = "Show.Name.2018.S04E20.Ger.Eng.Sub.AAC.1080p.WebDL.x264-Group.part1.rar"

    @classmethod
    def _evidence(cls, package_name, filenames, advertised=None):
        evidence = list(filenames or [])
        if package_name and package_name != (advertised or cls.ADVERTISED):
            evidence.insert(0, package_name)
        return evidence

    def test_reguessed_title_no_longer_hides_the_claim(self):
        # The whole loop in one assertion: advertised German, delivered
        # Japanese, and the only reason it went unnoticed for 74 grabs was that
        # the comparison used the corrected name on both sides.
        self.assertIsNone(
            refusals.language_contradiction(
                self.REGUESSED, self._evidence(self.REGUESSED, [self.FILENAME])
            ),
            "the old comparison could not see it — kept as the reason why",
        )
        result = refusals.language_contradiction(
            self.ADVERTISED, self._evidence(self.REGUESSED, [self.FILENAME])
        )
        self.assertIsNotNone(result, "advertised claim vs. corrected name is the proof")
        claimed, delivered = result
        self.assertEqual({"German"}, claimed)
        self.assertEqual({"Japanese"}, delivered)

    def test_files_alone_would_not_have_been_enough(self):
        # Why the corrected package name is weighed at all: the real files name
        # subtitles only, and evidence without an audio language proves nothing.
        self.assertIsNone(
            refusals.language_contradiction(self.ADVERTISED, [self.FILENAME])
        )

    def test_untouched_title_keeps_the_legacy_behaviour(self):
        # The unchanged path: no re-guess, so advertised == package name and the
        # verdict still rests on the filenames.
        honest = "Show.Name.S01E18.German.ML.GerSub.EngSub.1080p.WEB-DL.x264-Group"
        evidence = self._evidence(
            honest, ["Show.Name.S01E18.English.1080p.mkv"], advertised=honest
        )
        self.assertEqual(
            ["Show.Name.S01E18.English.1080p.mkv"],
            evidence,
            "an uncorrected name must not vouch for its own claim",
        )
        result = refusals.language_contradiction(honest, evidence)
        self.assertIsNotNone(result)
        claimed, delivered = result
        self.assertEqual({"German"}, claimed)
        self.assertEqual({"English"}, delivered)

    def test_a_correct_delivery_is_still_never_refused(self):
        self.assertIsNone(
            refusals.language_contradiction(
                self.ADVERTISED,
                self._evidence(
                    "Show.Name.S04E20.German.DL.1080p.WEB-DL.x264-Group",
                    ["Show.Name.S04E20.German.DL.1080p.mkv"],
                ),
            )
        )


class RefusalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_values = getattr(shared_state_module, "values", None)
        self._old_lock = getattr(shared_state_module, "lock", None)
        shared_state_module.values = {
            "dbfile": os.path.join(self.tmp.name, "Quasarr.db")
        }
        shared_state_module.lock = None

    def tearDown(self):
        shared_state_module.values = self._old_values
        shared_state_module.lock = self._old_lock

    def _release(self, hostname="al", source="https://al.invalid/media/slime"):
        return {
            "details": {
                "title": SLIME_CLAIM,
                "hostname": hostname,
                "source": source,
                "link": "http://internal/download/?payload=x",
            },
            "type": "protected",
        }

    def test_recorded_refusal_is_filtered_out_of_search(self):
        release = self._release()
        kept, dropped = refusals.filter_refused([release])
        self.assertEqual(1, len(kept), "nothing refused yet")
        self.assertEqual([], dropped)

        refusals.record_refusal(
            "al",
            "https://al.invalid/media/slime",
            SLIME_CLAIM,
            {"German"},
            {"Japanese"},
        )

        kept, dropped = refusals.filter_refused([release])
        self.assertEqual([], kept, "a disproved release must not be offered again")
        self.assertEqual(1, len(dropped))

    def test_refusal_is_scoped_to_the_release_not_the_host(self):
        refusals.record_refusal(
            "al",
            "https://al.invalid/media/slime",
            SLIME_CLAIM,
            {"German"},
            {"Japanese"},
        )
        other = self._release(source="https://al.invalid/media/one-piece")
        kept, dropped = refusals.filter_refused([other])
        self.assertEqual(1, len(kept), "one bad release must not ban the whole host")
        self.assertEqual([], dropped)

    def test_grab_note_round_trip(self):
        refusals.remember_grab("Quasarr_tv_abc", "AL", "https://al.invalid/x", "T")
        grab = refusals.recall_grab("Quasarr_tv_abc")
        self.assertEqual("al", grab["source_key"], "source key is normalised")
        self.assertEqual("https://al.invalid/x", grab["url"])

    def test_grab_notes_expire_refusals_do_not(self):
        refusals.remember_grab("old", "al", "u", "t", now=0)
        refusals.record_refusal("al", "u", "t", {"German"}, {"Japanese"}, now=0)

        later = refusals.GRAB_MAX_AGE_S + 1
        self.assertEqual(1, refusals.prune_grabs(now=later))
        self.assertIsNone(refusals.recall_grab("old"))
        self.assertTrue(
            refusals.is_refused("al", "u"),
            "scaffolding expires, the verdict does not",
        )

    def test_refusal_can_be_lifted(self):
        refusals.record_refusal("al", "u", "t", {"German"}, {"Japanese"})
        self.assertTrue(refusals.is_refused("al", "u"))
        refusals.delete_refusal("al", "u")
        self.assertFalse(refusals.is_refused("al", "u"))

    def test_broken_store_fails_open(self):
        # A search must never return zero because bookkeeping is unhappy.
        DataBase("refusals").store("garbage-key", "not json")
        release = self._release()
        kept, _dropped = refusals.filter_refused([release])
        self.assertEqual(1, len(kept))

    def test_recorded_blob_is_readable_evidence(self):
        refusals.record_refusal(
            "al", "https://al.invalid/x", SLIME_CLAIM, {"German"}, {"Japanese"}
        )
        blob = json.loads(
            DataBase("refusals").retrieve(
                refusals.refusal_key("al", "https://al.invalid/x")
            )
        )
        self.assertEqual(["German"], blob["claimed"])
        self.assertEqual(["Japanese"], blob["delivered"])
        self.assertEqual(SLIME_CLAIM, blob["title"])


if __name__ == "__main__":
    unittest.main()
