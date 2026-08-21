# -*- coding: utf-8 -*-
"""
Tests for choosing between AL's release-notes title and a synthesised one.

AL sometimes files a whole COLLECTION under one tab and names it accordingly:

    One.Piece.E001-206.GerSub.480p.AAC.Web-DL.x264-Tanuki
    One.Piece.207-1100.720p.AAC.Web-DL.x264-Tanuki
    One.Piece.E1101-XXX.Ger.Sub.AAC.720p.CR.WEB.h264-DRiFTKiNG

...concatenated into a single string. Passing that through verbatim gives Sonarr
nothing to map: it answered "Unknown Series" and rejected the release. Observed
live on 2026-08-20 — the episode-link fix had already made the release appear
again, but it arrived unusable.

The synthesised alternative carries the requested numbering and parses by
construction:

    One.Piece.E1174.Japanese.GerSub.AAC.720p.WEB-DL.x264-TanukiDRiFTKiNG

Rule: keep the notes title only when it NAMES the requested episode.
"""
import unittest

from quasarr.downloads.sources.al import notes_title_identifies_episode

MEGA = (
    "One.Piece.E001-206.GerSub.480p.AAC.Web-DL.x264-TanukiOne.Piece."
    "207-1100.720p.AAC.Web-DL.x264-TanukiOne.Piece.E1101-XXX.Ger.Sub."
    "AAC.720p.CR.WEB.h264-DRiFTKiNG"
)


class NotesTitleChoiceTests(unittest.TestCase):
    def test_collection_title_does_not_identify_the_episode(self):
        # The live failure: ranges, not an episode. Must be replaced.
        self.assertFalse(notes_title_identifies_episode(MEGA, 1174))

    def test_absolute_episode_title_is_kept(self):
        self.assertTrue(
            notes_title_identifies_episode("One.Piece.E1174.German.ML.1080p", 1174)
        )

    def test_season_episode_title_is_kept(self):
        self.assertTrue(
            notes_title_identifies_episode("Bleach.S17E14.German.DL", 14)
        )

    def test_title_naming_a_different_episode_is_replaced(self):
        # Guards against silently shipping the wrong episode under a real-looking
        # name — the maja.17 damage pattern.
        self.assertFalse(
            notes_title_identifies_episode("One.Piece.E1174.German.ML", 1175)
        )

    def test_no_episode_requested_keeps_the_notes_title(self):
        # Season packs and feed entries have no requested episode; the notes
        # title is the best information available and must not be discarded.
        for title in ("One.Piece.S17.German.DL", MEGA, ""):
            with self.subTest(title=title[:30]):
                self.assertTrue(notes_title_identifies_episode(title, None))

    def test_empty_notes_title_with_requested_episode_is_replaced(self):
        self.assertFalse(notes_title_identifies_episode("", 1174))
        self.assertFalse(notes_title_identifies_episode(None, 1174))

    def test_unparseable_episode_argument_keeps_the_notes_title(self):
        # Never let a bad argument silently rewrite a good title.
        self.assertTrue(notes_title_identifies_episode("One.Piece.E5.German", "abc"))


if __name__ == "__main__":
    unittest.main()
