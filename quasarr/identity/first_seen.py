# Quasarr — Maja fork (x13ijuu)
"""First-seen anchor: a release keeps the publication date it was first offered with.

The problem, measured on 2026-08-31: One Piece E1176 was grabbed **29 times in
seven hours**, once per RSS sync, and Sonarr's blocklist never recognised the
release it had just blocklisted 29 times.

The cause is not in Sonarr. AL states publication as a relative age ("vor 7
Stunden"), and the only way to turn that into the absolute timestamp a newznab
feed must carry is to subtract it from the current time. Do that per request
and the same release gets a NEW date every poll. Two feed polls seconds apart
proved it live: 8 of 18 AL releases came back with a different ``pubDate``,
shifted by exactly the elapsed time.

Sonarr matches usenet releases against its blocklist on publication date, with
indexer plus size as the fallback. A date that moves matches neither — so the
blocklist was structurally unable to hold anything from this source, and every
failed release came back on the next sync forever. The size fallback could not
save it either: the feed path reported no size at all.

The fix is to stop deriving the date from the clock. The first time a release
is offered, the derived timestamp is written down; every later poll serves that
same value. It is honest — the source really does not publish an absolute date,
and "when we first saw it" is the best fact available — and it is stable across
restarts, which is what the client needs.

Only dates that had to be DERIVED are anchored. A source that states a real
timestamp is already stable and is passed through untouched.

Table: ``release_first_seen``. Key ``"<source_key>|<url>|<title>"`` (the same
identity the refusal ledger uses), value a JSON blob.
"""

import json
import time

from quasarr.identity.refusals import normalize_title
from quasarr.providers.log import debug

_TABLE = "release_first_seen"

# An anchor for a release nobody offers any more is dead weight. This is not an
# expiry in any meaningful sense — a release that is still in a feed gets its
# anchor refreshed on every poll, so only genuinely gone entries age out.
MAX_AGE_S = 90 * 24 * 3600
MAX_ENTRIES = 20000


def _db():
    from quasarr.storage.sqlite_database import DataBase

    return DataBase(_TABLE)


def _now(now=None):
    return time.time() if now is None else now


def anchor_key(source_key, url, title):
    return f"{(source_key or '').lower()}|{url or ''}|{normalize_title(title)}"


def anchor(source_key, url, title, derived_date, now=None):
    """Return the date this release was first offered with.

    ``derived_date`` is the freshly computed RSS date string. On the first sight
    of a release it is stored and returned unchanged; afterwards the stored one
    wins, no matter how the clock has moved.

    Fail-open in both directions: a broken store gives back the derived value,
    which is exactly today's behaviour, so a feed never breaks over bookkeeping.
    """
    if not derived_date:
        return derived_date

    key = anchor_key(source_key, url, title)
    current = _now(now)

    try:
        raw = _db().retrieve(key)
    except Exception as e:
        debug(f"Could not read first-seen anchor for {key}: {e}")
        return derived_date

    if raw:
        try:
            blob = json.loads(raw)
            stored = blob.get("date")
            if stored:
                # Touch it so an actively offered release never ages out from
                # under a client that is still being shown it.
                if current - blob.get("seen_at", 0) > 24 * 3600:
                    blob["seen_at"] = current
                    try:
                        _db().update_store(key, json.dumps(blob))
                    except Exception:
                        pass
                return stored
        except Exception as e:
            debug(f"Unreadable first-seen anchor for {key}: {e}")

    try:
        _db().update_store(
            key,
            json.dumps(
                {
                    "source_key": (source_key or "").lower(),
                    "url": url or "",
                    "title": title or "",
                    "date": derived_date,
                    "created_at": current,
                    "seen_at": current,
                }
            ),
        )
    except Exception as e:
        debug(f"Could not store first-seen anchor for {key}: {e}")

    return derived_date


def all_anchors():
    try:
        rows = _db().retrieve_all_titles() or []
    except Exception as e:
        debug(f"Could not read first-seen anchors: {e}")
        return {}
    out = {}
    for key, value in rows:
        try:
            out[key] = json.loads(value)
        except Exception:
            continue
    return out


def prune(now=None):
    """Trim anchors nobody has been offered in a long time. Best-effort."""
    current = _now(now)
    entries = all_anchors()
    if not entries:
        return 0

    stale = [
        k
        for k, v in entries.items()
        if current - v.get("seen_at", v.get("created_at", 0)) > MAX_AGE_S
    ]
    if len(entries) - len(stale) > MAX_ENTRIES:
        fresh = sorted(
            ((k, v) for k, v in entries.items() if k not in stale),
            key=lambda kv: kv[1].get("seen_at", kv[1].get("created_at", 0)),
        )
        stale += [k for k, _v in fresh[: len(fresh) - MAX_ENTRIES]]

    removed = 0
    for key in stale:
        try:
            _db().delete(key)
            removed += 1
        except Exception as e:
            debug(f"Could not prune first-seen anchor {key}: {e}")
    return removed
