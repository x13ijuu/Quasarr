# -*- coding: utf-8 -*-
"""
Tests for the FlareSolverr concurrency cap (Maja fork, maja.35).

Every solve is a Chrome. A sessionless request spawns a fresh one per request,
and nothing on either side bounded how many run at once: FlareSolverr's
SessionsStorage has no cap and no TTL, and the search fan-out deliberately
starts one worker PER source, with cache families in parallel on top and Sonarr
plus Radarr asking simultaneously.

Measured 2026-08-30 against three live targets: one solve peaks at 320-380 MB
over a ~540 MB baseline, so against the 2048 MB limit only four fit. The
container never leaked — it ran more browsers than fit, pinned the cgroup, and
the resulting reclaim thrash made `sessions.create` fail. Raising the limit only
moved the ceiling (1536 -> pinned at 1536, 2048 -> pinned at 2048).

The cap must therefore hold three properties:
  * never let more than N solves run at once,
  * never block past the caller's budget — Sonarr's 100 s timeout is not
    configurable, so a saturated solver has to behave like an unreachable
    source rather than a slow one,
  * never lose a slot, not even when the solve raises.
"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.providers import cloudflare


def _shared_state():
    return SimpleNamespace(
        values={
            "config": lambda _section: {"url": "http://flaresolverr:8191/v1"},
            "user_agent": "test-agent",
        },
        update=lambda *_a, **_k: None,
    )


class _Resp:
    """Minimal stand-in for the requests.Response FlareSolverr returns."""

    def __init__(self):
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "status": "ok",
            "solution": {"response": "<html></html>", "status": 200, "headers": []},
        }


class FlareSolverrThrottleTests(unittest.TestCase):
    def setUp(self):
        # Each test gets its own semaphore so the cap can be varied and no test
        # inherits another's slot state.
        self._original = cloudflare._solve_slots
        self.addCleanup(lambda: setattr(cloudflare, "_solve_slots", self._original))

    def _set_cap(self, cap):
        cloudflare._solve_slots = threading.BoundedSemaphore(cap)

    def test_never_more_than_cap_concurrent_solves(self):
        self._set_cap(2)
        live = 0
        peak = 0
        guard = threading.Lock()
        release = threading.Event()

        def slow_post(*_a, **_k):
            nonlocal live, peak
            with guard:
                live += 1
                peak = max(peak, live)
            release.wait(timeout=5)
            with guard:
                live -= 1
            return _Resp()

        with patch.object(cloudflare.requests, "post", side_effect=slow_post), patch.object(
            cloudflare, "is_flaresolverr_available", return_value=True
        ):
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [
                    pool.submit(
                        cloudflare.flaresolverr_get,
                        _shared_state(),
                        "https://example.invalid/",
                        timeout=5,
                    )
                    for _ in range(5)
                ]
                # Give the workers time to pile up against the cap, then let go.
                threading.Timer(1.0, release.set).start()
                for future in futures:
                    future.result(timeout=30)

        self.assertLessEqual(peak, 2, f"cap breached: {peak} concurrent solves")
        self.assertGreater(peak, 0, "no solve ran at all — test is not exercising it")

    def test_saturation_refuses_instead_of_blocking(self):
        """A full solver must fail fast, not eat the fan-out budget."""
        self._set_cap(1)
        cloudflare._solve_slots.acquire()  # occupy the only slot
        self.addCleanup(cloudflare._solve_slots.release)

        with patch.object(cloudflare, "is_flaresolverr_available", return_value=True), patch.object(
            cloudflare.requests, "post"
        ) as post:
            with self.assertRaises(RuntimeError) as caught:
                cloudflare.flaresolverr_get(
                    _shared_state(), "https://example.invalid/", timeout=1
                )

        self.assertIn("saturated", str(caught.exception).lower())
        post.assert_not_called()

    def test_wait_is_capped_by_the_callers_timeout(self):
        self._set_cap(1)
        cloudflare._solve_slots.acquire()
        self.addCleanup(cloudflare._solve_slots.release)

        seen = {}

        def fake_acquire(timeout=None):
            seen["timeout"] = timeout
            return False

        with patch.object(cloudflare, "is_flaresolverr_available", return_value=True), patch.object(
            cloudflare._solve_slots, "acquire", side_effect=fake_acquire
        ):
            with self.assertRaises(RuntimeError):
                cloudflare.flaresolverr_get(
                    _shared_state(), "https://example.invalid/", timeout=3
                )

        self.assertEqual(
            3,
            seen["timeout"],
            "a short caller budget must shorten the slot wait, never extend it",
        )

    def test_slot_is_returned_when_the_solve_raises(self):
        self._set_cap(1)

        with patch.object(cloudflare, "is_flaresolverr_available", return_value=True), patch.object(
            cloudflare.requests, "post", side_effect=OSError("connection reset")
        ):
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    cloudflare.flaresolverr_get(
                        _shared_state(), "https://example.invalid/", timeout=1
                    )

        # Three failures in a row must not have drained the single slot.
        self.assertTrue(
            cloudflare._solve_slots.acquire(timeout=0),
            "a failed solve leaked its slot — the cap would starve itself",
        )
        cloudflare._solve_slots.release()

    def test_post_path_is_capped_too(self):
        """request.post solves cost the same browser as request.get."""
        self._set_cap(1)
        cloudflare._solve_slots.acquire()
        self.addCleanup(cloudflare._solve_slots.release)

        with patch.object(cloudflare, "is_flaresolverr_available", return_value=True), patch.object(
            cloudflare.requests, "post"
        ) as post:
            with self.assertRaises(RuntimeError):
                cloudflare.flaresolverr_post(
                    _shared_state(), "https://example.invalid/", {"a": "b"}, timeout=1
                )
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
