# -*- coding: utf-8 -*-
"""
Episode membership reads AL's row labels first (Maja fork, merge of 4.7.3).

Upstream 4.7.3 picks the link by row label ("003 Episode 080-081: Title") for
the download (#449) and filters search results by the labeled range (#453).
The fork's membership check (_tab_offers_episode, used by search, the audio
tab resolver and the download refusal guard) only knew position arithmetic:
episode N lives at data-loop N-1. That is wrong for a release that does not
start at episode 1 and for merged double episodes, so the merge makes the
check read the same labels - and keeps the arithmetic as the fallback for
rows without a usable label (tests/test_al_episode_link_membership.py).
"""
import unittest

from bs4 import BeautifulSoup

from quasarr.downloads.sources.al import (
    _episode_row_ranges,
    _resolve_episode_selection,
    _tab_offers_episode,
)


def _html(labels, with_cnl=True):
    """One tab; row i carries data-loop i and the label text AL renders."""
    links = '<a data-loop="cnl">Click\'n\'Load</a>' if with_cnl else ""
    links += "".join(
        f'<a data-loop="{i}">{i + 1:03d} {label}</a>' for i, label in enumerate(labels)
    )
    return (
        '<div class="tab-pane" id="download_1">'
        f'<div class="episodes">{links}</div></div>'
    )


def _tab(labels, with_cnl=True):
    return BeautifulSoup(_html(labels, with_cnl), "html.parser").find(
        "div", class_="tab-pane"
    )


class RangedReleaseTests(unittest.TestCase):
    """A release covering E080-E120: loops 0..40, labels 80..120."""

    def setUp(self):
        self.labels = [f"Episode {n:03d}: Title {n}" for n in range(80, 121)]
        self.tab = _tab(self.labels)

    def test_episode_inside_the_range_is_offered(self):
        self.assertTrue(_tab_offers_episode(self.tab, 85))
        self.assertTrue(_tab_offers_episode(self.tab, 120))

    def test_episode_before_the_range_is_not_offered(self):
        # The arithmetic alone said yes: loop 29 exists - and holds E109.
        self.assertFalse(_tab_offers_episode(self.tab, 30))

    def test_download_selection_agrees_with_membership(self):
        # E85 is row 6 (loop 5), not loop 84.
        self.assertEqual(5, _resolve_episode_selection(_html(self.labels), 1, 85))

    def test_ranges_are_parsed(self):
        ranges = _episode_row_ranges(self.tab)
        self.assertEqual((80, 80), ranges[0])
        self.assertEqual((120, 120), ranges[-1])


class MergedDoubleEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.labels = ["Episode 001-002: Pilot", "Episode 003: Next"]
        self.tab = _tab(self.labels)

    def test_second_half_of_a_merged_row_is_offered(self):
        self.assertTrue(_tab_offers_episode(self.tab, 2))

    def test_episode_after_a_merged_row_is_offered(self):
        # Arithmetic: E3 -> loop 2, which does not exist (only loops 0 and 1).
        self.assertTrue(_tab_offers_episode(self.tab, 3))

    def test_episode_beyond_the_labels_is_not_offered(self):
        self.assertFalse(_tab_offers_episode(self.tab, 4))


class FallbackTests(unittest.TestCase):
    def test_unlabeled_rows_fall_back_to_arithmetic(self):
        tab = _tab([f"Folge {n}" for n in range(1, 4)])
        self.assertEqual([], _episode_row_ranges(tab))
        self.assertTrue(_tab_offers_episode(tab, 3))
        self.assertFalse(_tab_offers_episode(tab, 4))

    def test_partly_labeled_rows_do_not_refuse_on_labels_alone(self):
        # Row 2 has no usable label - it might hold E2, so arithmetic decides.
        tab = _tab(["Episode 001: A", "Bonus", "Episode 003: C"])
        self.assertTrue(_tab_offers_episode(tab, 2))

    def test_episode_title_mentioning_an_episode_is_not_a_label(self):
        # Anchored like upstream: "Chikara - Episode 1" is a title, not a label.
        tab = _tab(["Chikara - Episode 1", "Chikara - Episode 2"])
        self.assertEqual([], _episode_row_ranges(tab))

    def test_tab_without_episode_list_returns_none(self):
        tab = BeautifulSoup(
            '<div class="tab-pane" id="download_1"></div>', "html.parser"
        ).find("div", class_="tab-pane")
        self.assertIsNone(_episode_row_ranges(tab))
        self.assertIsNone(_tab_offers_episode(tab, 1))


if __name__ == "__main__":
    unittest.main()
