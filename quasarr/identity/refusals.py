# Quasarr — Maja fork (x13ijuu)
"""Refusal ledger: a claim the delivered files disproved is never offered again.

The problem, measured on 2026-08-24: AL advertised
``…S04E19.German.ML.GerSub.EngSub…`` and delivered
``…S04E19.Japanese.GerSub.EngSub…``. Sonarr imported the file, saw no German
audio, discarded it and searched again ~15 minutes later — **93 grabs of one
episode in 14 days**. Every single pass looked like a success: the grab
arrived, the import worked, the downloadId was bound.

The cause is not a bug in the parser. AL carries its language flags on the
release BLOCK, which spans a whole episode range; while a season is still
airing the German dub lags behind, so the block truthfully says "this release
has German" and the newest episode truthfully does not. The title is therefore
a CLAIM, and per-episode it is not verifiable up front.

So it is treated as a claim: once the delivered files disprove it, that release
is refused for good and never offered for that episode again. Nothing is
renamed, nothing is guessed — the ledger only ever records a contradiction that
already happened.

Deliberately conservative. A refusal is recorded ONLY when

  1. the advertised title claims German AUDIO (subtitle tokens stripped first —
     ``GerSub`` is a subtitle, never a rung on the audio ladder), and
  2. at least one piece of delivered evidence names a DIFFERENT audio language,
     and
  3. no delivered evidence shows German audio.

"Advertised" means the title the client grabbed, kept in the grab note. It is
NOT the package name: a source that re-guesses the title from its details page
hands JDownloader the CORRECTED name, and comparing that against itself can only
ever come out clean.

The delivered evidence is that re-guessed package name TOGETHER with the real
filenames. The files alone are not enough — they frequently name no audio
language at all, while the re-guess is the source's own reading of what the
release actually is.

Evidence that carries no language information proves nothing and is left alone.
The cost of a false refusal is an unobtainable release, which is worse than one
more wrong grab.

Two classes of refusal, deliberately kept apart
----------------------------------------------

``hard`` — the case above. A claim the delivered bytes disproved. It is a
statement about the release, not about a moment, so it stands for ``MAX_AGE_S``.

``soft`` — the release could not be RESOLVED at all: the source advertises it,
but its page carries no matching release ("no tabs", no release id). Measured
2026-08-31: One Piece E1176 was grabbed **29 times in seven hours**, and each
time this module's caller logged a refusal it never recorded, so the next feed
offered the release again unchanged. That is a different animal from a
disproved claim — "not there yet" is usually temporary, because a source
announces an episode before the release is posted. A permanent refusal would
turn a few hours of lag into six months of an unobtainable episode.

So a soft refusal backs OFF instead of banning: it hides the release for an
hour, then six, then a day, and after that leaves it alone for a week. It
expires on its own, and the episode returns the moment the source really has
it. Every attempt is counted, so a release that is genuinely gone stops costing
grabs quickly while one that was merely early comes back.

This module does NOT notify. Alerting is deterministic and lives on the Maja
side (ADR 0026, "one alerting brain"): ``tools/observer/media-regrab-watch.sh``
already reports the repeat-grab class with the same language comparison.
Quasarr enforces, the observer reports.

Table: ``refusals``. Key ``"<source_key>|<url>|<title>"``, value a JSON blob.
The title is part of the identity because ``url`` is the SERIES page on AL, not
the episode: the two-part key refused every episode of a show because one of
them lied. Found live on 2026-08-31 — a single mislabelled Bleach episode had
silenced the whole series until 2027. Two-part keys left over from that scheme
are ignored on read and pruned on sight.
"""

import json
import re
import time

from quasarr.providers.log import debug, info

# Subtitle markers must never be read as audio. "German.ML.GerSub" claims German
# audio; "Japanese.GerSub" claims Japanese audio with a German subtitle, and the
# fork's own upgrade ladder makes the same distinction ("Die Leiter ist
# AUDIO-basiert. Ein deutscher Untertitel obendrauf ist Tiebreak, keine Sprosse").
#
# The marker also comes SPLIT, with the languages listed once and "Sub" trailing
# the whole list ("…Ger.Eng.Sub.AAC.1080p…"). Read one token at a time that says
# "German audio, English audio, something called Sub" — the exact opposite of
# what it means. Measured 2026-08-30: this alone kept the ledger silent through
# 74 grabs of one episode, because the delivered file "showed German" and rule 3
# cleared the package.
# Only the SHORT forms may be swallowed by a trailing "Sub". The long form
# ("German", "Japanese") is how these titles spell an audio track, and letting
# the pattern reach across it would erase the very language the comparison
# needs: "Japanese.Ger.Eng.Sub" is Japanese audio with two subtitle tracks, not
# a release with no audio at all.
_SUBTITLE_LANGUAGE = r"(?:ger|eng|jap|jpn|de|en|jp)"
_SUBTITLE_TOKEN = re.compile(
    r"(?i)\b" + _SUBTITLE_LANGUAGE + r"(?:[._\-\s]" + _SUBTITLE_LANGUAGE + r")*"
    r"[._\-\s]?sub(?:bed|s)?\b"
)

# Audio markers, deliberately narrow. Bare "ger" only counts when it stands as
# its own token (e.g. "[GER-JAP]"), never as the prefix of something else.
_AUDIO_MARKERS = {
    "German": re.compile(
        r"(?i)(?:^|[\W_])(?:german|deutsch|ger|dub-?ger|gerdub)(?:[\W_]|$)"
    ),
    "Japanese": re.compile(r"(?i)(?:^|[\W_])(?:japanese|japanisch|jap|jpn)(?:[\W_]|$)"),
    "English": re.compile(r"(?i)(?:^|[\W_])(?:english|englisch|eng)(?:[\W_]|$)"),
    "Italian": re.compile(r"(?i)(?:^|[\W_])(?:italian|italienisch|ita)(?:[\W_]|$)"),
}

_TABLE = "refusals"

# Sources whose language flags sit on the release BLOCK rather than the
# individual release — the whole reason a title here is a claim and not a fact
# (see the module docstring). Both are anime-only sites.
#
# The comparison is scoped to them on purpose. Everywhere else a title states
# the language of the thing it names, so a mismatch between title and filename
# is far more likely to be Dual-Audio, a sample file or a sloppy container name
# than a lie — and the penalty for reading one of those as a lie is a release
# that can never be obtained again. Before this guard the check ran on every
# finished package, films included.
BLOCK_LANGUAGE_SOURCES = frozenset({"al", "at"})


def advertises_block_language(source_key):
    """May a title from this source be treated as a language CLAIM?"""
    return (source_key or "").lower() in BLOCK_LANGUAGE_SOURCES

# A refusal is a statement about a release, not about a moment. It does not
# expire on its own — but a very old entry for a release nobody offers any more
# is dead weight, so the store is trimmed opportunistically.
MAX_AGE_S = 180 * 24 * 3600
MAX_ENTRIES = 2000

KIND_HARD = "hard"
KIND_SOFT = "soft"

# How long a release stays hidden after the n-th failure to resolve it. The
# last value repeats: once a release has failed four times it is almost
# certainly not coming, but "almost" is not "certainly", so it keeps getting a
# weekly look rather than a permanent ban.
SOFT_BACKOFF_S = (3600, 6 * 3600, 24 * 3600, 7 * 24 * 3600)


def _db(table):
    # Imported inside the function: a module-level import would tie this file
    # into the import graph at load time and a rebase could reorder it.
    from quasarr.storage.sqlite_database import DataBase

    return DataBase(table)


def _now(now=None):
    return time.time() if now is None else now


def normalize_title(title):
    """Fold a release title to the form both sides of the ledger agree on.

    The search result and the download payload carry the bare release title;
    the newznab layer prefixes "[AL] " on the way out and the client hands it
    back without one. Case and separator noise differ between the two as well,
    so neither side is trusted to spell it identically.
    """
    text = str(title or "").strip()
    if text.startswith("[") and "]" in text:
        text = text.split("]", 1)[1].strip()
    return re.sub(r"[\s._\-]+", ".", text).lower()


def refusal_key(source_key, url, title=None):
    """Stable identity of a release across grabs.

    NOT the package_id — ``enqueue_grab`` mints a fresh nonce per grab, so it
    identifies one attempt, not a release. ``(source_key, url, title)`` is what
    the search result and the payload agree on.

    The title is not decoration. On AL ``url`` is the series page, shared by
    every episode of a show, so a key without the title refuses the series.
    """
    return f"{(source_key or '').lower()}|{url or ''}|{normalize_title(title)}"


def _is_legacy_key(key):
    """True for the two-part keys written before the title joined the identity."""
    return str(key or "").count("|") < 2


def _is_active(blob, now=None):
    """Is this refusal still in force?

    A hard refusal has no expiry — it is a statement about the release. A soft
    one carries ``expires_at`` and lapses on its own; a blob without one is
    read as hard, which is how entries written before the split behave.
    """
    expires_at = (blob or {}).get("expires_at")
    if not expires_at:
        return True
    return _now(now) < expires_at


# --- grab identity ---------------------------------------------------------
#
# A finished JD package knows only its comment (= package_id), and that id
# carries a per-grab nonce, so it cannot lead back to the release. The grab path
# leaves a note here so the completion path can name what it refuses.

_GRABS = "grab_identity"
GRAB_MAX_AGE_S = 14 * 24 * 3600


def remember_grab(package_id, source_key, url, title, now=None):
    if not package_id:
        return False
    try:
        _db(_GRABS).update_store(
            package_id,
            json.dumps(
                {
                    "source_key": (source_key or "").lower(),
                    "url": url or "",
                    "title": title or "",
                    "created_at": _now(now),
                }
            ),
        )
        return True
    except Exception as e:
        debug(f"Could not remember grab {package_id}: {e}")
        return False


def recall_grab(package_id):
    if not package_id:
        return None
    try:
        raw = _db(_GRABS).retrieve(package_id)
        return json.loads(raw) if raw else None
    except Exception as e:
        debug(f"Could not recall grab {package_id}: {e}")
        return None


def forget_grab(package_id):
    try:
        _db(_GRABS).delete(package_id)
        return True
    except Exception:
        return False


def prune_grabs(now=None):
    """Grab notes are scaffolding, not history — drop them once they cannot matter."""
    current = _now(now)
    removed = 0
    try:
        rows = _db(_GRABS).retrieve_all_titles() or []
    except Exception as e:
        debug(f"Could not read grab notes: {e}")
        return 0
    for key, value in rows:
        try:
            if current - json.loads(value).get("created_at", 0) > GRAB_MAX_AGE_S:
                _db(_GRABS).delete(key)
                removed += 1
        except Exception:
            continue
    return removed


# --- language comparison ---------------------------------------------------


def audio_languages(text):
    """Audio languages named by a title or filename, subtitles removed first."""
    if not text:
        return set()
    without_subs = _SUBTITLE_TOKEN.sub(" ", str(text))
    return {
        language
        for language, pattern in _AUDIO_MARKERS.items()
        if pattern.search(without_subs)
    }


def language_contradiction(advertised_title, delivered_names):
    """Return ``(claimed, delivered)`` when the files disprove the title, else None.

    Both sides are audio-only. See the module docstring for why all three
    conditions are required — the silent middle case (a filename with no
    language information at all) must stay silent.
    """
    claimed = audio_languages(advertised_title)
    if "German" not in claimed:
        return None

    delivered = set()
    for name in delivered_names or []:
        delivered |= audio_languages(name)

    if not delivered:
        return None
    if "German" in delivered:
        return None

    return claimed, delivered


def record_refusal(
    source_key, url, title, claimed, delivered, package_id=None, now=None
):
    """Persist a disproved claim. Fail-open: a broken store never breaks a grab."""
    key = refusal_key(source_key, url, title)
    blob = {
        "kind": KIND_HARD,
        "source_key": (source_key or "").lower(),
        "url": url or "",
        "title": title or "",
        "claimed": sorted(claimed or []),
        "delivered": sorted(delivered or []),
        "package_id": package_id,
        "created_at": _now(now),
    }
    try:
        _db(_TABLE).update_store(key, json.dumps(blob))
        info(
            f"Refusing <r>{title}</r> from now on: claimed "
            f"{'/'.join(blob['claimed'])} audio, delivered "
            f"{'/'.join(blob['delivered'])}"
        )
        return True
    except Exception as e:
        debug(f"Could not record refusal for {key}: {e}")
        return False


def record_unresolvable(source_key, url, title, reason, now=None):
    """Back a release off after its source could not resolve it.

    Called where the download path already knew it had nothing to grab and, up
    to maja.38, only logged it. Each call escalates the hold along
    ``SOFT_BACKOFF_S``; nothing here is permanent, so an episode that was merely
    announced early returns by itself.

    Never overrides a hard refusal — a disproved claim outranks a missing one.
    """
    key = refusal_key(source_key, url, title)
    current = _now(now)

    attempts = 1
    try:
        raw = _db(_TABLE).retrieve(key)
        if raw:
            previous = json.loads(raw)
            if previous.get("kind") == KIND_HARD or not previous.get("expires_at"):
                return False
            attempts = int(previous.get("attempts", 0)) + 1
    except Exception as e:
        debug(f"Could not read previous refusal for {key}: {e}")

    hold = SOFT_BACKOFF_S[min(attempts, len(SOFT_BACKOFF_S)) - 1]
    blob = {
        "kind": KIND_SOFT,
        "source_key": (source_key or "").lower(),
        "url": url or "",
        "title": title or "",
        "reason": str(reason or "")[:200],
        "attempts": attempts,
        "created_at": current,
        "expires_at": current + hold,
    }
    try:
        _db(_TABLE).update_store(key, json.dumps(blob))
        info(
            f"Holding back <r>{title}</r> for {hold // 3600}h "
            f"(attempt {attempts}, {blob['reason']})"
        )
        return True
    except Exception as e:
        debug(f"Could not record unresolvable refusal for {key}: {e}")
        return False


def is_refused(source_key, url, title=None, now=None):
    try:
        raw = _db(_TABLE).retrieve(refusal_key(source_key, url, title))
        if not raw:
            return False
        return _is_active(json.loads(raw), now)
    except Exception as e:
        debug(f"Refusal lookup failed for {source_key}: {e}")
        return False


def all_refusals():
    try:
        rows = _db(_TABLE).retrieve_all_titles() or []
    except Exception as e:
        debug(f"Could not read refusals: {e}")
        return {}
    out = {}
    for key, value in rows:
        try:
            out[key] = json.loads(value)
        except Exception:
            continue
    return out


def delete_refusal(source_key, url, title=None):
    try:
        _db(_TABLE).delete(refusal_key(source_key, url, title))
        return True
    except Exception as e:
        debug(f"Could not delete refusal for {source_key}: {e}")
        return False


def filter_refused(releases, now=None):
    """Drop refused releases from a search result list.

    Returns ``(kept, dropped)``. One store read per call, not one per release —
    this sits in the hot path of every search.

    Soft refusals expire, so a matching key is not on its own a reason to drop;
    the blob decides. Only the blobs of releases that actually matched are
    parsed, which keeps the cost proportional to the hits rather than to the
    size of the ledger.
    """
    if not releases:
        return releases, []

    try:
        rows = _db(_TABLE).retrieve_all_titles() or []
    except Exception as e:
        debug(f"Refusal filter could not read the ledger: {e}")
        return releases, []

    refused = {key: value for key, value in rows if not _is_legacy_key(key)}
    if not refused:
        return releases, []

    kept, dropped = [], []
    for release in releases:
        details = release.get("details", {}) or {}
        key = refusal_key(
            details.get("hostname"), details.get("source"), details.get("title")
        )
        raw = refused.get(key)
        if raw is None:
            kept.append(release)
            continue
        try:
            active = _is_active(json.loads(raw), now)
        except Exception:
            # An unreadable blob is not evidence. Offering a release one time
            # too many is recoverable; hiding it on a parse error is not.
            active = False
        (dropped if active else kept).append(release)
    return kept, dropped


def prune(now=None):
    """Trim entries that are too old or beyond the cap. Best-effort."""
    current = _now(now)
    entries = all_refusals()
    if not entries:
        return 0

    stale = [
        k
        for k, v in entries.items()
        if _is_legacy_key(k)
        or not _is_active(v, current)
        or current - v.get("created_at", 0) > MAX_AGE_S
    ]
    if len(entries) - len(stale) > MAX_ENTRIES:
        fresh = sorted(
            ((k, v) for k, v in entries.items() if k not in stale),
            key=lambda kv: kv[1].get("created_at", 0),
        )
        overflow = len(fresh) - MAX_ENTRIES
        stale += [k for k, _v in fresh[:overflow]]

    removed = 0
    for key in stale:
        try:
            _db(_TABLE).delete(key)
            removed += 1
        except Exception as e:
            debug(f"Could not prune refusal {key}: {e}")
    return removed
