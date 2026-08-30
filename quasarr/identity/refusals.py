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

This module does NOT notify. Alerting is deterministic and lives on the Maja
side (ADR 0026, "one alerting brain"): ``tools/observer/media-regrab-watch.sh``
already reports the repeat-grab class with the same language comparison.
Quasarr enforces, the observer reports.

Table: ``refusals``. Key ``"<source_key>|<url>"``, value a JSON blob.
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

# A refusal is a statement about a release, not about a moment. It does not
# expire on its own — but a very old entry for a release nobody offers any more
# is dead weight, so the store is trimmed opportunistically.
MAX_AGE_S = 180 * 24 * 3600
MAX_ENTRIES = 2000


def _db(table):
    # Imported inside the function: a module-level import would tie this file
    # into the import graph at load time and a rebase could reorder it.
    from quasarr.storage.sqlite_database import DataBase

    return DataBase(table)


def _now(now=None):
    return time.time() if now is None else now


def refusal_key(source_key, url):
    """Stable identity of a release across grabs.

    NOT the package_id — ``enqueue_grab`` mints a fresh nonce per grab, so it
    identifies one attempt, not a release. ``(source_key, url)`` is what the
    search result and the payload agree on.
    """
    return f"{(source_key or '').lower()}|{url or ''}"


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
    key = refusal_key(source_key, url)
    blob = {
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


def is_refused(source_key, url):
    try:
        return _db(_TABLE).retrieve(refusal_key(source_key, url)) is not None
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


def delete_refusal(source_key, url):
    try:
        _db(_TABLE).delete(refusal_key(source_key, url))
        return True
    except Exception as e:
        debug(f"Could not delete refusal for {source_key}: {e}")
        return False


def filter_refused(releases):
    """Drop refused releases from a search result list.

    Returns ``(kept, dropped)``. One store read per call, not one per release —
    this sits in the hot path of every search.
    """
    if not releases:
        return releases, []

    # Keys only — this runs on every search, and the JSON bodies are evidence
    # for a human, not something the filter needs.
    try:
        rows = _db(_TABLE).retrieve_all_titles() or []
    except Exception as e:
        debug(f"Refusal filter could not read the ledger: {e}")
        return releases, []

    refused = {key for key, _value in rows}
    if not refused:
        return releases, []

    kept, dropped = [], []
    for release in releases:
        details = release.get("details", {}) or {}
        key = refusal_key(details.get("hostname"), details.get("source"))
        (dropped if key in refused else kept).append(release)
    return kept, dropped


def prune(now=None):
    """Trim entries that are too old or beyond the cap. Best-effort."""
    current = _now(now)
    entries = all_refusals()
    if not entries:
        return 0

    stale = [
        k for k, v in entries.items() if current - v.get("created_at", 0) > MAX_AGE_S
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
