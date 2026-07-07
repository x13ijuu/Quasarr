# -*- coding: utf-8 -*-
# Quasarr — Maja fork (x13ijuu)
# Project by https://github.com/rix1337

"""
Adaptive host-ban handling (F3).

When a DDL host (e.g. anime-loads) rate-limits / IP-bans us after too many
CAPTCHA solves in a short window, upstream simply fails every grab — which makes
Sonarr blocklist releases and keeps hammering the banned host. Instead we:

  * detect the ban (al.py raises HostBannedError),
  * park the affected grab in a `waiting` table and show Sonarr a "Waiting for
    unban" queue slot (Queued -> not a failure, no blocklist, no import),
  * probe with a back-off timer that LEARNS the real ban duration per host, so a
    5-minute ban costs ~5-10 minutes of waiting instead of a flat 3 hours,
  * on success, drain the remaining waiting packages for that host.

State lives in two SQLite tables (same DataBase pattern as hostname_issues):
  host_bans : key=source_key, value=json ban state (learned duration, retry_after, …)
  waiting   : key=package_id, value=json full re-download context

All timing functions take an explicit `now` (epoch seconds) so the logic is
deterministic and unit-testable; callers pass time.time().
"""

import json
import random
import time

# Back-off without a learned duration: exponential from 5 min, capped at 3 h.
BASE_WAIT_S = 300
MAX_WAIT_S = 3 * 3600
# Once we have learned the ban duration, the real unban is near — probe tightly.
LEARNED_MAX_WAIT_S = 1800
EMA_ALPHA = 0.5
MAX_SAMPLES = 5
JITTER = 0.10
# Give up on a parked package after this long so Sonarr can eventually act itself.
WAITING_MAX_AGE_S = 48 * 3600

# Message fragments that mark a rate-limit / block (conservative: anything else
# after failed CAPTCHA solves is treated as a normal failure, never a ban).
BAN_MARKERS = (
    "noadblock",
    "slowdown",
    "slow down",
    "too many",
    "rate limit",
    "ratelimit",
    "banned",
    "blocked",
    "ip is",
    "429",
)


class HostBannedError(Exception):
    """Raised by a source when the host has rate-limited / banned our IP."""

    def __init__(self, source_key, message=""):
        self.source_key = (source_key or "unknown").lower()
        self.message = str(message)
        super().__init__(f"host banned: {self.source_key}: {self.message}")


def looks_like_ban(message):
    m = str(message).lower()
    return any(marker in m for marker in BAN_MARKERS)


def _db(table):
    from quasarr.storage.sqlite_database import DataBase

    return DataBase(table)


def _now(now):
    return time.time() if now is None else now


def _jitter(seconds):
    delta = seconds * JITTER
    return max(1, int(seconds + random.uniform(-delta, delta)))


# ---------------------------------------------------------------------------
# ban state
# ---------------------------------------------------------------------------

def _load(source_key):
    # Fail-safe: any DB/state problem means "we don't know of a ban" (fail-open,
    # like the external guard was) rather than breaking the download path.
    try:
        raw = _db("host_bans").retrieve(source_key.lower())
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _save(source_key, state):
    try:
        _db("host_bans").update_store(source_key.lower(), json.dumps(state))
    except Exception:
        pass


def _initial_retry_after(state, banned_since):
    """First probe time for a fresh ban, using a learned duration if we have one."""
    learned = state.get("learned_duration_s")
    if learned:
        return banned_since + max(120, _jitter(int(0.85 * learned)))
    return banned_since + _jitter(BASE_WAIT_S)


def record_ban(source_key, message="", now=None):
    """
    Mark a host as banned. Idempotent: an already-active ban keeps its
    banned_since / retry_after (a repeat ban mid-wait must not reset the clock).
    Returns the (possibly updated) state.
    """
    now = _now(now)
    key = source_key.lower()
    state = _load(key) or {}
    if state.get("banned_since"):
        # Already banned — just refresh the last message.
        state["last_message"] = str(message)[:300]
        state["updated_at"] = now
        _save(key, state)
        return state

    state["banned_since"] = now
    state["probe_attempts"] = 0
    state["last_message"] = str(message)[:300]
    state["updated_at"] = now
    state.setdefault("learned_duration_s", None)
    state.setdefault("duration_samples", [])
    state["retry_after"] = _initial_retry_after(state, now)
    _save(key, state)
    return state


def is_banned(source_key, now=None):
    state = _load(source_key)
    return bool(state and state.get("banned_since"))


def next_retry_at(source_key):
    state = _load(source_key)
    return state.get("retry_after") if state else None


def due_for_probe(source_key, now=None):
    now = _now(now)
    state = _load(source_key)
    if not state or not state.get("banned_since"):
        return False
    return now >= state.get("retry_after", 0)


def record_probe_failure(source_key, now=None):
    """A probe of a banned host failed again: back off and schedule the next probe."""
    now = _now(now)
    key = source_key.lower()
    state = _load(key)
    if not state:
        return
    state["probe_attempts"] = int(state.get("probe_attempts", 0)) + 1
    state["last_probe_at"] = now
    attempts = state["probe_attempts"]
    if state.get("learned_duration_s"):
        # Real unban is near — tight exponential, capped low.
        interval = min(BASE_WAIT_S * (2 ** (attempts - 1)), LEARNED_MAX_WAIT_S)
    else:
        interval = min(BASE_WAIT_S * (2 ** attempts), MAX_WAIT_S)
    state["retry_after"] = now + _jitter(interval)
    state["updated_at"] = now
    _save(key, state)


def record_unban(source_key, now=None):
    """
    A probe succeeded: learn the observed ban duration (EMA over recent samples)
    and clear the ban. Returns the learned duration for logging.
    """
    now = _now(now)
    key = source_key.lower()
    state = _load(key)
    if not state:
        return None
    banned_since = state.get("banned_since", now)
    last_failed = state.get("last_probe_at")
    # Tighter estimate: midpoint between the last failed probe and now. If we
    # unbanned on the very first probe, use the full elapsed time (upper bound).
    if last_failed and last_failed > banned_since:
        observed = (last_failed + now) / 2 - banned_since
    else:
        observed = now - banned_since
    observed = max(60, observed)

    samples = list(state.get("duration_samples") or [])
    samples.append(int(observed))
    samples = samples[-MAX_SAMPLES:]
    prev = state.get("learned_duration_s")
    if prev:
        learned = int(EMA_ALPHA * observed + (1 - EMA_ALPHA) * prev)
    else:
        learned = int(observed)

    # Persist the learning across bans; drop the active-ban fields.
    _db("host_bans").update_store(
        key,
        json.dumps(
            {
                "banned_since": None,
                "retry_after": None,
                "probe_attempts": 0,
                "learned_duration_s": learned,
                "duration_samples": samples,
                "last_message": "",
                "updated_at": now,
            }
        ),
    )
    return learned


def get_banned_hosts(now=None):
    """Return {source_key: state} for hosts with an active ban."""
    now = _now(now)
    try:
        rows = _db("host_bans").retrieve_all_titles() or []
    except Exception:
        return {}
    out = {}
    for key, raw in rows:
        try:
            state = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if state.get("banned_since"):
            out[key] = state
    return out


# ---------------------------------------------------------------------------
# waiting packages
# ---------------------------------------------------------------------------

def park_waiting(package_id, context, now=None):
    """Persist the full re-download context of a grab that hit a banned host."""
    now = _now(now)
    blob = dict(context)
    blob.setdefault("created_at", now)
    _db("waiting").update_store(package_id, json.dumps(blob))


def get_waiting(package_id):
    try:
        raw = _db("waiting").retrieve(package_id)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def delete_waiting(package_id):
    _db("waiting").delete(package_id)


def all_waiting():
    """Return list of (package_id, context) for every parked package."""
    try:
        rows = _db("waiting").retrieve_all_titles() or []
    except Exception:
        return []
    out = []
    for pid, raw in rows:
        try:
            out.append((pid, json.loads(raw)))
        except (ValueError, TypeError):
            continue
    return out


def waiting_for_host(source_key):
    key = source_key.lower()
    return [
        (pid, ctx)
        for pid, ctx in all_waiting()
        if (ctx.get("source_key") or "").lower() == key
    ]


def oldest_waiting_for_host(source_key):
    items = waiting_for_host(source_key)
    if not items:
        return None
    items.sort(key=lambda kv: kv[1].get("created_at", 0))
    return items[0]
