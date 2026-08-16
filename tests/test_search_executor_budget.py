# -*- coding: utf-8 -*-
"""
Tests for the executor-level stability fixes (Maja fork, maja.23).

Background: Sonarr/Radarr abort HTTP requests after a hard, non-configurable
100s. Measured on the live host, 24% of Quasarr responses exceeded that —
every miss raises the *arr backoff escalation ("indexer down"), the client
retries, and without single-flight the retry doubled the crawl load (one
observed crawl kept running for 53 minutes after the client had hung up).

The fixes under test:
  (1) run_all returns PARTIAL results once SEARCH_RESPONSE_BUDGET_SECONDS is
      spent — a partial answer inside the client's limit beats a complete
      answer the client never waits for. Stragglers keep crawling and cache
      their result for the next request.
  (2) Single-flight: a second request for the same (source, action, category)
      adopts the in-flight crawl instead of duplicating it.
  (3) The feed cache TTL honours FEED_CACHE_TTL (default 600s) instead of a
      hardcoded 60s that was shorter than one slow category pass.
  (4) The executor sizes itself to the number of sources (default pool of 12
      left 4 of 16 sources queued for no reason).
"""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import quasarr.search as search_module
from quasarr.search import (
    FEED_CACHE_TTL_SECONDS,
    SearchExecutor,
    _inflight_futures,
    search_cache,
)


def _release(name):
    return [{"details": {"title": name, "link": f"https://x/{name}"}}]


def _source(initials, action_impl):
    return SimpleNamespace(initials=initials, feed=action_impl)


def _add(executor, initials, impl, category=2000, use_cache=True):
    executor.add(
        _source(initials, impl),
        (None, None, category),
        {},
        use_cache=use_cache,
        ttl=600,
        action="feed",
        cache_category=category,
    )


class ResponseBudgetTests(unittest.TestCase):
    def setUp(self):
        search_cache.cache.clear()
        _inflight_futures.clear()

    def test_partial_results_within_budget(self):
        release_gate = threading.Event()

        def fast(*_a, **_k):
            return _release("fast")

        def slow(*_a, **_k):
            release_gate.wait(10)
            return _release("slow")

        ex = SearchExecutor()
        _add(ex, "fa", fast, category=2000)
        _add(ex, "sl", slow, category=5000)

        with patch.object(search_module, "SEARCH_RESPONSE_BUDGET_SECONDS", 1):
            started = time.time()
            results, badges, all_cached, _ = ex.run_all()
            elapsed = time.time() - started

        try:
            self.assertLess(elapsed, 5, "must return at the budget, not at the straggler")
            titles = [r["details"]["title"] for r in results]
            self.assertEqual(["fast"], titles, "partial results: fast source only")
            self.assertIn("FA", badges)
            self.assertIn("SL", badges, "straggler must still appear in the status bar")
            self.assertFalse(all_cached)
        finally:
            release_gate.set()

    def test_straggler_populates_cache_for_next_request(self):
        release_gate = threading.Event()

        def slow(*_a, **_k):
            release_gate.wait(10)
            return _release("late")

        ex = SearchExecutor()
        _add(ex, "sl", slow, category=5000)
        with patch.object(search_module, "SEARCH_RESPONSE_BUDGET_SECONDS", 1):
            results, _, _, _ = ex.run_all()
        self.assertEqual([], results, "nothing finished inside the budget")

        release_gate.set()
        # The late-cache done_callback fires from the worker thread.
        deadline = time.time() + 5
        while time.time() < deadline:
            ex2 = SearchExecutor()
            _add(ex2, "sl", lambda *_a, **_k: _release("relcrawl"), category=5000)
            results2, _, all_cached2, _ = ex2.run_all()
            if all_cached2:
                break
            time.sleep(0.05)
        self.assertTrue(all_cached2, "second request must be served from cache")
        self.assertEqual(
            "late",
            results2[0]["details"]["title"],
            "cache holds the straggler's result, no re-crawl happened",
        )

    def test_zero_budget_disables_deadline(self):
        def slowish(*_a, **_k):
            time.sleep(0.3)
            return _release("done")

        ex = SearchExecutor()
        _add(ex, "sl", slowish, category=5000)
        with patch.object(search_module, "SEARCH_RESPONSE_BUDGET_SECONDS", 0):
            results, _, _, _ = ex.run_all()
        self.assertEqual(1, len(results))


class SingleFlightTests(unittest.TestCase):
    def setUp(self):
        search_cache.cache.clear()
        _inflight_futures.clear()

    def test_concurrent_identical_requests_crawl_once(self):
        calls = []
        first_started = threading.Event()
        release_gate = threading.Event()

        def crawl(*_a, **_k):
            calls.append(1)
            first_started.set()
            release_gate.wait(10)
            return _release("shared")

        def run_one(out):
            ex = SearchExecutor()
            _add(ex, "sl", crawl, category=5000)
            out.append(ex.run_all())

        out1, out2 = [], []
        t1 = threading.Thread(target=run_one, args=(out1,))
        t1.start()
        self.assertTrue(first_started.wait(5))
        t2 = threading.Thread(target=run_one, args=(out2,))
        t2.start()
        time.sleep(0.2)  # let the second request adopt the in-flight future
        release_gate.set()
        t1.join(10)
        t2.join(10)

        self.assertEqual(1, len(calls), "second request must adopt, not re-crawl")
        for out in (out1, out2):
            results, _, _, _ = out[0]
            self.assertEqual("shared", results[0]["details"]["title"])

    def test_registry_is_cleaned_after_completion(self):
        ex = SearchExecutor()
        _add(ex, "fa", lambda *_a, **_k: _release("x"), category=2000)
        ex.run_all()
        deadline = time.time() + 2
        while _inflight_futures and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual({}, _inflight_futures)


class FeedCacheTtlTests(unittest.TestCase):
    def setUp(self):
        search_cache.cache.clear()

    def test_default_ttl_is_600_not_60(self):
        # 60s was shorter than one slow category pass (~160s measured), which
        # silently disabled the cache-family optimization under load.
        self.assertEqual(600, FEED_CACHE_TTL_SECONDS)

    def test_cache_respects_long_ttl(self):
        search_cache.set("k", _release("v"), ttl=600)
        with patch("quasarr.search.time.time", return_value=time.time() + 599):
            val, _ = search_cache.get("k")
        self.assertIsNotNone(val, "hit at t+599")
        with patch("quasarr.search.time.time", return_value=time.time() + 601):
            val, _ = search_cache.get("k")
        self.assertIsNone(val, "miss after expiry")


class ExecutorSizingTests(unittest.TestCase):
    def test_all_sources_run_concurrently(self):
        # 16 sources, each blocking until ALL 16 have started: only possible if
        # the pool holds at least one thread per source (default pool = 12).
        barrier = threading.Barrier(16, timeout=5)

        def crawl(*_a, **_k):
            barrier.wait()
            return _release("x")

        ex = SearchExecutor()
        for i in range(16):
            _add(ex, f"s{i:02d}", crawl, category=2000 + i, use_cache=False)
        results, _, _, _ = ex.run_all()
        self.assertEqual(16, len(results))


if __name__ == "__main__":
    unittest.main()
