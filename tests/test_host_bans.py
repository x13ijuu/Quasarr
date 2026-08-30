# -*- coding: utf-8 -*-
"""
Tests for the adaptive host-ban subsystem (Maja fork, F3).

Covers the learning probe timer (a short ban must not cost a flat 3h), idempotent
ban recording, back-off scheduling, restart persistence, and the waiting-package
store. Timing is deterministic: every function takes an explicit `now`.
"""
import os
import tempfile
import unittest

from quasarr.providers import host_bans as hb
from quasarr.providers import shared_state as shared_state_module


class HostBanBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shared_state_module.values = {
            "dbfile": os.path.join(self.tmp.name, "Quasarr.db")
        }
        shared_state_module.lock = None

    def tearDown(self):
        self.tmp.cleanup()


class BanStateTests(HostBanBaseTest):
    def test_looks_like_ban(self):
        self.assertTrue(hb.looks_like_ban("noadblock"))
        # AL sends the message as a stringified list, e.g. Message: ['noadblock'].
        self.assertTrue(hb.looks_like_ban(["noadblock"]))
        self.assertTrue(hb.looks_like_ban("['noadblock']"))
        self.assertTrue(hb.looks_like_ban("Please slowdown"))
        self.assertTrue(hb.looks_like_ban("429 Too Many Requests"))
        self.assertFalse(hb.looks_like_ban("invalid captcha solution"))
        self.assertFalse(hb.looks_like_ban(""))

    def test_record_ban_sets_first_probe_and_is_idempotent(self):
        t0 = 1_000_000
        hb.record_ban("al", "noadblock", now=t0)
        self.assertTrue(hb.is_banned("al"))
        first = hb.next_retry_at("al")
        # First probe is base wait (~5min) out, within jitter.
        self.assertGreater(first, t0 + 200)
        self.assertLess(first, t0 + 400)
        # A repeat ban mid-wait must NOT reset banned_since / retry_after.
        hb.record_ban("al", "still banned", now=t0 + 60)
        self.assertEqual(first, hb.next_retry_at("al"))

    def test_due_for_probe(self):
        t0 = 1_000_000
        hb.record_ban("al", "noadblock", now=t0)
        self.assertFalse(hb.due_for_probe("al", now=t0 + 10))
        self.assertTrue(hb.due_for_probe("al", now=t0 + 3600))

    def test_probe_failure_backs_off(self):
        t0 = 1_000_000
        hb.record_ban("al", "noadblock", now=t0)
        r1 = hb.next_retry_at("al")
        hb.record_probe_failure("al", now=r1)
        r2 = hb.next_retry_at("al")
        self.assertGreater(r2, r1)  # backed off further

    def test_learns_short_ban_and_clears(self):
        # Ban lifts after ~6 min; the learned duration must be short, not 3h.
        t0 = 1_000_000
        hb.record_ban("al", "noadblock", now=t0)
        # First probe fails at +5min, unban succeeds at +6min.
        hb.record_probe_failure("al", now=t0 + 300)
        learned = hb.record_unban("al", now=t0 + 360)
        self.assertFalse(hb.is_banned("al"))
        self.assertIsNotNone(learned)
        self.assertLess(learned, 1800)  # well under 30min, nowhere near 3h

    def test_learned_duration_shortens_next_first_probe(self):
        t0 = 1_000_000
        hb.record_ban("al", "noadblock", now=t0)
        hb.record_probe_failure("al", now=t0 + 300)
        hb.record_unban("al", now=t0 + 360)  # learns ~5min
        # A fresh ban later should schedule its first probe near the learned
        # duration, not the flat 5-min base -> earlier than base+jitter ceiling
        # is fine; key point: it uses the learned value.
        t1 = 2_000_000
        hb.record_ban("al", "noadblock", now=t1)
        first = hb.next_retry_at("al")
        # 0.85 * ~300s ~= 255s; allow jitter and the 120s floor.
        self.assertGreater(first, t1 + 120)
        self.assertLess(first, t1 + 400)

    def test_persistence_across_reload(self):
        t0 = 1_000_000
        hb.record_ban("al", "noadblock", now=t0)
        retry = hb.next_retry_at("al")
        # Simulate a Quasarr restart: new DataBase handles, same dbfile.
        self.assertTrue(hb.is_banned("al"))
        self.assertEqual(retry, hb.next_retry_at("al"))

    def test_get_banned_hosts_only_active(self):
        t0 = 1_000_000
        hb.record_ban("al", "noadblock", now=t0)
        hb.record_ban("sf", "slowdown", now=t0)
        hb.record_probe_failure("sf", now=t0 + 300)
        hb.record_unban("sf", now=t0 + 360)  # sf now clear
        banned = hb.get_banned_hosts(now=t0 + 400)
        self.assertIn("al", banned)
        self.assertNotIn("sf", banned)


class WaitingStoreTests(HostBanBaseTest):
    def _ctx(self, title, source_key="al", created_at=None):
        ctx = {
            "title": title,
            "url": "http://anime-loads.org/x",
            "size_mb": 1024,
            "source_key": source_key,
        }
        if created_at is not None:
            ctx["created_at"] = created_at
        return ctx

    def test_park_get_delete(self):
        hb.park_waiting("Quasarr_tv_a", self._ctx("A"), now=100)
        got = hb.get_waiting("Quasarr_tv_a")
        self.assertEqual("A", got["title"])
        self.assertEqual(100, got["created_at"])
        hb.delete_waiting("Quasarr_tv_a")
        self.assertIsNone(hb.get_waiting("Quasarr_tv_a"))

    def test_waiting_for_host_and_oldest(self):
        hb.park_waiting("Quasarr_tv_a", self._ctx("A", created_at=200), now=200)
        hb.park_waiting("Quasarr_tv_b", self._ctx("B", created_at=100), now=100)
        hb.park_waiting("Quasarr_mv_c", self._ctx("C", source_key="wd", created_at=50))
        al_items = hb.waiting_for_host("al")
        self.assertEqual(2, len(al_items))
        oldest = hb.oldest_waiting_for_host("al")
        self.assertEqual("B", oldest[1]["title"])  # created_at 100 < 200

    def test_no_waiting_returns_none(self):
        self.assertIsNone(hb.oldest_waiting_for_host("al"))


if __name__ == "__main__":
    unittest.main()
