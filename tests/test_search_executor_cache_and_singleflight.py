# -*- coding: utf-8 -*-
"""
Tests for the executor behaviour this fork adds ON TOP of upstream's fan-out
deadline (v4.6.17, ``SEARCH_FANOUT_DEADLINE_SECONDS``).

Upstream now owns the timing itself — dropping a slow source from the response,
skipping a source whose deadline is already spent, and sizing the pool to one
worker per source. Those cases live in ``test_search_fanout_deadline.py`` and
were removed here when the fork rebased onto v4.6.17.

What upstream does NOT do, and this file covers:
  (1) A source dropped from the response keeps crawling and writes its result
      to the CACHE, so the next request serves it instantly. Without this a
      source that is reliably slower than the deadline (AL needs 79-90s for One
      Piece / Bleach) would be dropped on every request forever and never reach
      the client at all.
  (2) Single-flight: a second request for the same (source, action, category)
      adopts the in-flight crawl instead of duplicating it. The deadline makes
      *arr retries MORE likely, so this matters more after the rebase, not less
      (one observed crawl kept running 53 minutes after the client hung up).
  (3) The feed cache TTL honours FEED_CACHE_TTL (default 600s) instead of a
      hardcoded 60s that was shorter than one slow category pass.
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


class LateCacheWriterTests(unittest.TestCase):
    def setUp(self):
        search_cache.cache.clear()
        _inflight_futures.clear()

    def test_straggler_populates_cache_for_next_request(self):
        release_gate = threading.Event()

        def slow(*_a, **_k):
            release_gate.wait(10)
            return _release("late")

        ex = SearchExecutor(deadline=time.time() + 1)
        _add(ex, "sl", slow, category=5000)
        results, _, _, _ = ex.run_all()
        self.assertEqual([], results, "nothing finished inside the deadline")

        release_gate.set()
        # The late-cache done_callback fires from the worker thread.
        deadline = time.time() + 5
        while time.time() < deadline:
            ex2 = SearchExecutor()
            _add(ex2, "sl", lambda *_a, **_k: _release("recrawl"), category=5000)
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

        # The second request must reach the single-flight lookup BEFORE the first
        # crawl finishes, otherwise there is nothing left to adopt. This used to
        # be a `time.sleep(0.2)` and was the flakiest test in the suite: on a
        # loaded CI runner 0.2 s is not enough, thread 2 starts its own crawl and
        # the assertion below reports "1 != 2" — a red run with nothing broken.
        #
        # Make it observable instead of hoping. Both threads look the key up in
        # the in-flight registry; thread 1 finds nothing (it is the owner), thread
        # 2 finds the running future. A lookup that RETURNS something therefore is
        # the adoption, and waiting for it is deterministic.
        registered = threading.Event()
        adopted = threading.Event()

        class _WatchedRegistry(dict):
            """Makes the two moments that matter observable.

            The owner REGISTERS its future; a later request finds it and adopts.
            Waiting on `first_started` is not enough: that fires inside the crawl,
            which runs after executor.submit() but BEFORE the registration — so
            the second thread could look up an empty registry and legitimately
            start its own crawl.
            """

            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                registered.set()

            def get(self, key, default=None):
                value = super().get(key, default)
                if value is not None:
                    adopted.set()
                return value

        watched = _WatchedRegistry(search_module._inflight_futures)

        out1, out2 = [], []
        with patch.object(search_module, "_inflight_futures", watched):
            t1 = threading.Thread(target=run_one, args=(out1,))
            t1.start()
            self.assertTrue(first_started.wait(5), "erster Crawl ist nie angelaufen")
            self.assertTrue(
                registered.wait(5), "erster Request hat sein Future nie registriert"
            )
            t2 = threading.Thread(target=run_one, args=(out2,))
            t2.start()
            self.assertTrue(
                adopted.wait(10),
                "zweiter Request hat die Adoptionsstelle nie erreicht",
            )
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


if __name__ == "__main__":
    unittest.main()
