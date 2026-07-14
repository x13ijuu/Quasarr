# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import hashlib
import json
import os
import time

from quasarr.constants import (
    AUTO_DECRYPT_PATTERNS,
    CLIENT_DOWNLOAD_CATEGORY_FALLBACK_MAP,
    PROTECTED_PATTERNS,
)
from quasarr.downloads.linkcrypters.hide import decrypt_links_if_hide
from quasarr.downloads.mirror_filters import filter_final_download_urls
from quasarr.downloads.packages import get_packages
from quasarr.downloads.sources import get_sources as get_download_sources
from quasarr.providers.host_bans import (
    HostBannedError,
    is_banned,
    park_waiting,
    record_ban,
)
from quasarr.providers.hostname_issues import clear_hostname_issue, mark_hostname_issue
from quasarr.providers.log import info, warn
from quasarr.providers.notifications import (
    send_notification,
    send_tracked_notification,
    update_release_notification,
)
from quasarr.providers.notifications.helpers.notification_types import NotificationType
from quasarr.providers.statistics import StatsHelper
from quasarr.providers.utils import (
    download_package,
    extract_client_type,
    filter_offline_links,
    normalize_download_title,
)
from quasarr.storage.categories import (
    download_category_exists,
    get_download_category_from_package_id,
    get_download_category_mirrors,
)

# =============================================================================
# DETERMINISTIC PACKAGE ID GENERATION
# =============================================================================


def generate_deterministic_package_id(
    title, source_key, client_type, download_category, nonce=None
):
    """
    Generate a package ID from title, source, and client type.

    Without a nonce the ID is DETERMINISTIC — the same (title, source_key,
    client_type) always yields the same ID (used for internal lookups/tests).

    With a nonce it is UNIQUE per grab (Maja fork). This is required for the
    SABnzbd contract: real SAB mints a fresh nzo_id on every add, and Sonarr keys
    its download tracking by that id. A deterministic id that repeats across grab
    attempts collides with Sonarr's history — once a release was recorded as
    downloadFailed under id X, a LATER successful download reusing id X is treated
    as already-handled and never tracked/imported (proven: E137 2026-07-08). So the
    live grab path (enqueue_grab) always passes a per-grab nonce.

    Args:
        title: Release title (e.g., "Movie.Name.2024.1080p.BluRay")
        source_key: Source identifier/hostname shorthand
        client_type: Client type without version (e.g., "radarr", "sonarr")
        download_category: Optional download category override
        nonce: Optional per-grab uniqueness token. When set, the id is unique.

    Returns:
        Package ID in format: Quasarr_{download_category}_{hash32}
    """
    # Normalize inputs for consistency
    normalized_title = title.strip()
    normalized_source = source_key.lower().strip() if source_key else "unknown"
    normalized_client = client_type.lower().strip() if client_type else "unknown"

    # Determine download category
    if download_category and download_category_exists(download_category):
        final_download_category = download_category
    else:
        # Fallback to client type mapping
        final_download_category = CLIENT_DOWNLOAD_CATEGORY_FALLBACK_MAP.get(
            normalized_client, "tv"
        )

    # Create hash from combination using SHA256 (+ optional per-grab nonce)
    hash_input = f"{normalized_title}|{normalized_source}|{normalized_client}"
    if nonce is not None:
        hash_input = f"{hash_input}|{nonce}"
    hash_bytes = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    # Use first 32 characters for good collision resistance (128-bit)
    return f"Quasarr_{final_download_category}_{hash_bytes[:32]}"


# =============================================================================
# LINK CLASSIFICATION
# =============================================================================


def detect_crypter(url):
    """Returns (crypter_name, 'auto'|'protected') or (None, None)."""
    for name, pattern in AUTO_DECRYPT_PATTERNS.items():
        if pattern.search(url):
            return name, "auto"
    for name, pattern in PROTECTED_PATTERNS.items():
        if pattern.search(url):
            return name, "protected"
    return None, None


def _drop_filecrypt_if_disabled(shared_state, classified, title):
    """Drop filecrypt links from the protected bucket when the kill switch is off."""
    if shared_state.values.get("filecrypt_enabled", True):
        return classified

    filecrypt_re = PROTECTED_PATTERNS["filecrypt"]
    kept, dropped = [], 0
    for link in classified["protected"]:
        if filecrypt_re.search(link[0]):
            dropped += 1
        else:
            kept.append(link)

    if dropped:
        info(
            f"Filecrypt disabled - dropped <r>{dropped}</r> filecrypt link(s) for {title}"
        )
    classified["protected"] = kept
    return classified


def classify_links(links):
    """
    Classify links into direct/auto/protected categories.
    Direct = anything that's not a known crypter or junkies link.
    Mirror names from source are preserved.
    """
    classified = {"direct": [], "auto": [], "protected": []}

    for link in links:
        url = link[0]
        mirror = link[1] if len(link) > 1 else ""

        if isinstance(mirror, str) and mirror.lower() == "junkies":
            classified["protected"].append(link)
            continue

        crypter, crypter_type = detect_crypter(url)
        if crypter_type == "auto":
            classified["auto"].append(link)
        elif crypter_type == "protected":
            classified["protected"].append(link)
        else:
            # Not a known crypter = direct hoster link
            classified["direct"].append(link)

    return classified


# =============================================================================
# LINK PROCESSING
# =============================================================================


def _persist_failed_package(
    shared_state, title, package_id, reason, remove_protected=False
):
    if remove_protected:
        try:
            shared_state.get_db("protected").delete(package_id)
        except Exception as e:
            info(f'Error removing protected package "{package_id}" before fail: {e}')
    fail(title, package_id, shared_state, reason=reason)
    return {"success": False, "persisted_failure": True, "reason": reason}


def _get_protected_release(shared_state, package_id):
    try:
        raw_data = shared_state.get_db("protected").retrieve(package_id)
        data = json.loads(raw_data) if raw_data else None
    except Exception as e:
        info(f'Error reading protected package "{package_id}" for notification: {e}')
        return None
    return data if isinstance(data, dict) else None


def _delete_protected_package(shared_state, package_id):
    try:
        shared_state.get_db("protected").delete(package_id)
    except Exception as e:
        info(f'Error removing protected package "{package_id}": {e}')


def _format_mirror_token_list(tokens):
    cleaned = [str(token) for token in sorted(tokens) if token]
    return ", ".join(cleaned) if cleaned else "unknown"


def submit_final_download_urls(
    shared_state,
    urls,
    title,
    password,
    package_id,
    remove_protected=False,
    notification_details=None,
):
    """
    Final mirror whitelist check before sending direct HTTP links to JDownloader.
    """
    protected_release = None
    if remove_protected:
        protected_release = _get_protected_release(shared_state, package_id) or {
            "title": title
        }
    category = get_download_category_from_package_id(package_id)
    mirrors = get_download_category_mirrors(category, lowercase=True)
    filtered = filter_final_download_urls(urls, mirrors)
    final_urls = filtered["urls"]
    dropped = filtered["dropped"]

    if mirrors and dropped:
        info(
            f"Final mirror-whitelist check kept <g>{len(final_urls)}</g> of <y>{len(urls)}</y> links "
            f'for "{title}" in category "{category}" '
            f"(allowed: {_format_mirror_token_list(filtered['allowed_tokens'])}, "
            f"dropped: {_format_mirror_token_list({item['token'] for item in dropped})})"
        )

    if mirrors and not final_urls:
        reason = (
            f'All final download links were rejected by the mirror-whitelist for category "{category}". '
            f"Allowed mirrors: {_format_mirror_token_list(filtered['allowed_tokens'])}. "
            f"Received mirrors: {_format_mirror_token_list({item['token'] for item in dropped})}."
        )
        result = _persist_failed_package(
            shared_state,
            title,
            package_id,
            reason,
            remove_protected=remove_protected,
        )
        if protected_release:
            update_release_notification(
                shared_state,
                protected_release,
                NotificationType.FAILED,
                details={"reason": reason},
            )
        return result

    info(f"Sending {len(final_urls)} direct download links for {title}")
    if download_package(final_urls, title, password, package_id, shared_state):
        if remove_protected:
            _delete_protected_package(shared_state, package_id)
            if protected_release:
                update_release_notification(
                    shared_state,
                    protected_release,
                    NotificationType.SOLVED,
                    details=notification_details,
                )
        return {"success": True, "links": final_urls}
    return {
        "success": False,
        "reason": f"Failed to add {len(final_urls)} links to linkgrabber",
    }


def handle_direct_links(shared_state, links, title, password, package_id):
    """Send direct hoster links to JDownloader."""
    urls = [link[0] for link in links]
    result = submit_final_download_urls(shared_state, urls, title, password, package_id)
    if result["success"]:
        StatsHelper(shared_state).increment_package_with_links(result["links"])
        return {"success": True}
    return result


def handle_auto_decrypt_links(shared_state, links, title, password, package_id):
    """Decrypt hide.cx links and send to JDownloader."""
    result = decrypt_links_if_hide(shared_state, links)

    if result.get("status") != "success":
        return {"success": False, "reason": "Auto-decrypt failed"}

    decrypted_urls = result.get("results", [])
    if not decrypted_urls:
        return {"success": False, "reason": "No links decrypted"}

    info(f"Decrypted <g>{len(decrypted_urls)}</g> download links for {title}")

    submit_result = submit_final_download_urls(
        shared_state, decrypted_urls, title, password, package_id
    )
    if submit_result["success"]:
        StatsHelper(shared_state).increment_package_with_links(submit_result["links"])
        return {"success": True}
    return submit_result


def store_protected_links(
    shared_state,
    links,
    title,
    password,
    package_id,
    size_mb=None,
    original_url=None,
    imdb_id=None,
    notifications=None,
):
    """Store protected links for CAPTCHA UI."""
    blob_data = {
        "title": title,
        "links": links,
        "password": password,
        "size_mb": size_mb,
    }
    if original_url:
        blob_data["original_url"] = original_url
    if imdb_id:
        blob_data["imdb_id"] = imdb_id
    if notifications:
        blob_data["notifications"] = notifications

    shared_state.values["database"]("protected").update_store(
        package_id, json.dumps(blob_data)
    )
    info(
        f'CAPTCHA-Solution required for <b>{title}</b> at: "{shared_state.values["external_address"]}/captcha"'
    )
    return {"success": True}


def process_links(
    shared_state,
    source_result,
    title,
    password,
    package_id,
    imdb_id,
    source_url,
    size_mb,
    label,
):
    """
    Central link processor with priority: direct → auto-decrypt → protected.
    If ANY direct links exist, use them and ignore crypted fallbacks.
    """
    if not source_result:
        return fail(
            title,
            package_id,
            shared_state,
            reason=f'Source returned no data for "{title}" on {label} - "{source_url}"',
        )

    links = source_result.get("links", [])
    password = source_result.get("password") or password
    imdb_id = imdb_id or source_result.get("imdb_id")
    title = normalize_download_title(source_result.get("title") or title)

    if not links:
        return fail(
            title,
            package_id,
            shared_state,
            reason=f'No links found for "{title}" on {label} - "{source_url}"',
        )

    # Filter out 404 links
    valid_links = [link for link in links if "/404.html" not in link[0]]
    if not valid_links:
        return fail(
            title,
            package_id,
            shared_state,
            reason=f'All links are offline or IP is banned for "{title}" on {label} - "{source_url}"',
        )
    links = valid_links

    # Filter out verifiably offline links
    links = filter_offline_links(links, shared_state=shared_state, log_func=info)
    if not links:
        return fail(
            title,
            package_id,
            shared_state,
            reason=f'All verifiable links are offline for "{title}" on {label} - "{source_url}"',
        )

    classified = classify_links(links)
    classified = _drop_filecrypt_if_disabled(shared_state, classified, title)

    # PRIORITY 1: Direct hoster links
    if classified["direct"]:
        info(
            f"Found <g>{len(classified['direct'])}</g> direct hoster links for {title}"
        )
        result = handle_direct_links(
            shared_state, classified["direct"], title, password, package_id
        )
        if result["success"]:
            send_notification(
                shared_state,
                title=title,
                case=NotificationType.UNPROTECTED,
                imdb_id=imdb_id,
                source=source_url,
            )
            return {"success": True, "title": title}
        if result.get("persisted_failure"):
            return {"success": True, "title": title, "failed": True}
        return fail(title, package_id, shared_state, reason=result.get("reason"))

    # PRIORITY 2: Auto-decryptable (hide.cx)
    if classified["auto"]:
        info(
            f"Found <g>{len(classified['auto'])}</g> auto-decryptable links for {title}"
        )
        result = handle_auto_decrypt_links(
            shared_state, classified["auto"], title, password, package_id
        )
        if result["success"]:
            send_notification(
                shared_state,
                title=title,
                case=NotificationType.UNPROTECTED,
                imdb_id=imdb_id,
                source=source_url,
            )
            return {"success": True, "title": title}
        if result.get("persisted_failure"):
            return {"success": True, "title": title, "failed": True}
        info(f"Auto-decrypt failed for {title}, falling back to manual CAPTCHA...")
        classified["protected"].extend(classified["auto"])

    # PRIORITY 3: Protected (filecrypt, tolink, keeplinks, junkies)
    if classified["protected"]:
        info(f"Found <g>{len(classified['protected'])}</g> protected links for {title}")
        notification_references = send_tracked_notification(
            shared_state,
            title=title,
            case=NotificationType.CAPTCHA,
            imdb_id=imdb_id,
            source=source_url,
        )
        store_protected_links(
            shared_state,
            classified["protected"],
            title,
            password,
            package_id,
            size_mb=size_mb,
            original_url=source_url,
            imdb_id=imdb_id,
            notifications=notification_references,
        )
        return {"success": True, "title": title}

    return fail(
        title,
        package_id,
        shared_state,
        reason=f'No usable links found for {title} on {label} - "{source_url}"',
    )


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def _park_banned_grab(
    package_id, source_key, request_from, download_category, title, url,
    size_mb, password, imdb_id, message="",
):
    """
    Record a host ban and park the full re-download context so the waiting_worker
    can retry it after the unban. Returns the SAB-style result for download().
    """
    record_ban(source_key, message)
    park_waiting(
        package_id,
        {
            "title": title,
            "url": url,
            "size_mb": size_mb,
            "password": password,
            "imdb_id": imdb_id,
            "source_key": source_key,
            "download_category": download_category,
            "request_from": request_from,
            "reason": str(message)[:300],
        },
    )
    info(
        f'Host "{source_key}" is banned — parked "{title}" to wait for the unban '
        f"(package {package_id})."
    )
    return {
        "success": True,
        "package_id": package_id,
        "title": title,
        "waiting": True,
    }


def find_existing_package(shared_state, package_id):
    """
    Locate where a package_id already exists, if anywhere.

    Returns the source as a string so callers can distinguish a genuine duplicate
    (currently downloading / already in history / awaiting CAPTCHA) from a stale
    "failed" marker. A failed marker is NOT a duplicate: it means a previous attempt
    of this release failed and the client (Sonarr/Radarr) is re-grabbing it — that is
    a retry request, not a reason to skip.

    Returns one of: "protected", "failed", "queue", "history", or None.
    """
    if shared_state.get_db("protected").retrieve(package_id):
        return "protected"
    if shared_state.get_db("failed").retrieve(package_id):
        return "failed"

    # F3 (Maja fork): already parked waiting for a host unban -> genuine duplicate.
    from quasarr.providers.host_bans import get_waiting

    if get_waiting(package_id):
        return "waiting"

    data = (
        shared_state.run_device_request(
            "load package state for duplicate detection",
            lambda _device: get_packages(shared_state),
            default={},
        )
        or {}
    )

    for section in ("queue", "history"):
        for pkg in data.get(section, []) or []:
            if pkg.get("nzo_id") == package_id:
                return section

    return None


def package_id_exists(shared_state, package_id):
    """Backwards-compatible boolean wrapper around find_existing_package()."""
    return find_existing_package(shared_state, package_id) is not None


def enqueue_grab(
    shared_state,
    request_from,
    download_category,
    title,
    url,
    size_mb,
    password,
    imdb_id,
    source_key,
):
    """
    Async accept path (Maja fork): the SABnzbd contract expects addurl to return
    a nzo_id INSTANTLY (just queue it). Upstream/our download() instead scrapes +
    solves the CAPTCHA + adds to JD inline (15-40s) before answering — so a burst
    of grabs blocks Sonarr's client requests, Sonarr times out / marks the client
    unavailable and DROPS the tracking, while Quasarr finishes in the background →
    orphaned completed downloads that never import. (Root cause of Rufus' 'hängt /
    verschwindet / kein Import' under load; RSS with 1 grab at a time never hit it.)

    enqueue_grab() persists the grab and returns the deterministic nzo_id at once.
    The waiting_worker processes it (calls download()) in the background, and
    get_packages() renders it as a 'Fetching'/Queued slot from the first poll on, so
    Sonarr tracks it reliably the whole time. Dedup and delete reuse the existing
    waiting-table machinery.
    """
    from quasarr.providers.host_bans import park_waiting

    title = normalize_download_title(title)
    client_type = extract_client_type(request_from)
    src = (source_key or "").lower().strip() or "unknown"

    # UNIQUE id per grab (SABnzbd contract). Real SAB mints a fresh nzo_id on every
    # add; Sonarr keys its download tracking by it. A deterministic id that repeats
    # across grab attempts collides with Sonarr's history: once a release was recorded
    # as downloadFailed under id X, a later successful download reusing X is treated as
    # already-handled and never imported (proven live: E137, 2026-07-08). The per-grab
    # nonce makes every grab a fresh, never-before-seen nzo -> Sonarr tracks it cleanly
    # through to import. The id stays stable for THIS grab's whole lifecycle (parked
    # here, reused verbatim by the worker's download()), so all internal keying holds.
    nonce = f"{time.time():.6f}-{os.urandom(4).hex()}"
    package_id = generate_deterministic_package_id(
        title, src, client_type, download_category, nonce=nonce
    )

    park_waiting(
        package_id,
        {
            "title": title,
            "url": url,
            "size_mb": size_mb,
            "password": password,
            "imdb_id": imdb_id,
            "source_key": source_key,
            "download_category": download_category,
            "request_from": request_from,
            "package_id": package_id,
            "pending": True,
        },
    )
    info(f'Queued "{title}" for background download (package {package_id}).')
    return {"success": True, "package_id": package_id, "title": title, "queued": True}


def download(
    shared_state,
    request_from,
    download_category,
    title,
    url,
    size_mb,
    password,
    imdb_id,
    source_key,
    package_id=None,
):
    """
    Main download entry point.

    package_id (Maja fork): when supplied (by the background worker draining a
    pending/waiting grab), it is used verbatim so the id the worker processes is the
    exact unique id enqueue_grab already handed to Sonarr — the id must not be
    regenerated mid-lifecycle. When None (legacy/direct call), a deterministic id is
    generated as before.

    Args:
        shared_state: Application shared state
        request_from: User-Agent string (e.g., "Radarr/6.0.4.10291")
        download_category: Download category (e.g., "movies", "tv", "docs")
        title: Release title
        url: Source URL
        size_mb: Size in MB
        password: Archive password
        imdb_id: IMDb ID (optional)
        source_key: Hostname shorthand from search. If not provided,
                    will be derived from URL matching against configured hostnames.
    """
    try:
        if imdb_id and imdb_id.lower() == "none":
            imdb_id = None

        title = normalize_download_title(title)
        config = shared_state.values["config"]("Hostnames")

        # Extract client type (without version) for deterministic hashing
        client_type = extract_client_type(request_from)

        # Find matching source - all getters have unified signature
        source_result = None
        label = None
        detected_source_key = None

        mirrors = get_download_category_mirrors(download_category, lowercase=True)
        download_sources = get_download_sources()

        normalized_source_key = None
        if source_key and isinstance(source_key, str):
            normalized_source_key = source_key.lower().strip()

        # F3 (Maja fork): if the authoritative source is a currently-banned host, do NOT
        # hit it again — park the grab straight away so we never hammer a banned host
        # (this is what makes the external hoster-breaker guard unnecessary).
        if normalized_source_key and is_banned(normalized_source_key):
            package_id = package_id or generate_deterministic_package_id(
                title, normalized_source_key, client_type, download_category
            )
            if not find_existing_package(shared_state, package_id):
                return {
                    "package_id": package_id,
                    **_park_banned_grab(
                        package_id, normalized_source_key, request_from,
                        download_category, title, url, size_mb, password, imdb_id,
                        "host still banned",
                    ),
                }

        source_candidates = []
        if normalized_source_key and normalized_source_key in download_sources:
            source_candidates.append(
                (normalized_source_key, download_sources[normalized_source_key], True)
            )

        for key, source in download_sources.items():
            if normalized_source_key and key == normalized_source_key:
                continue
            source_candidates.append((key, source, False))

        for key, source, from_source_key in source_candidates:
            hostname = config.get(key)
            if not from_source_key and not (
                hostname and hostname.lower() in url.lower()
            ):
                continue

            try:
                # Mirrors are download-category-driven and passed to each source getter.
                candidate_result = source.get_download_links(
                    shared_state, url, mirrors, title, password
                )
                if candidate_result and candidate_result.get("links"):
                    clear_hostname_issue(key)
                    source_result = candidate_result
                    label = key.upper()
                    detected_source_key = key
                    break
            except HostBannedError as e:
                # F3 (Maja fork): host rate-limited/banned us. Park the grab and let the
                # waiting_worker retry after the unban instead of failing it (which would
                # make the client blocklist the release and keep hammering the host).
                banned_key = e.source_key or key
                package_id = package_id or generate_deterministic_package_id(
                    title, banned_key, client_type, download_category
                )
                return {
                    "package_id": package_id,
                    **_park_banned_grab(
                        package_id, banned_key, request_from, download_category,
                        title, url, size_mb, password, imdb_id, e.message,
                    ),
                }
            except Exception as e:
                info(f"Error getting download links from {key.upper()}: {e}")
                if not from_source_key or (
                    hostname and hostname.lower() in url.lower()
                ):
                    mark_hostname_issue(key, "download", str(e))

        # No source matched - check if URL is a known crypter directly
        if source_result is None:
            crypter, crypter_type = detect_crypter(url)
            if crypter_type:
                # For direct crypter URLs, we only know the crypter type, not the hoster inside
                source_result = {"links": [[url, crypter]]}
                label = crypter.upper()
                detected_source_key = crypter

        # Use provided source_key if available, otherwise use detected one
        # This ensures we use the authoritative source from the search results
        final_source_key = source_key if source_key else detected_source_key

        # Use the pre-minted per-grab id (worker path) or generate one (direct call)
        package_id = package_id or generate_deterministic_package_id(
            title, final_source_key, client_type, download_category
        )

        # Decide based on WHERE the package already exists (if anywhere).
        existing = find_existing_package(shared_state, package_id)
        if existing == "failed":
            # Root-cause fix (Maja fork): a "failed" marker is NOT a duplicate. The client
            # is re-grabbing a release whose previous attempt failed. Upstream skipped here
            # AND returned success -> the grab was silently swallowed (JD never got it, the
            # episode stayed missing forever). Clear the stale marker and actually retry.
            # nzo_id stays deterministic; clients with removeFailedDownloads already dropped
            # the old failed record, so reusing the id is safe.
            shared_state.get_db("failed").delete(package_id)
            info(f"Retrying previously-failed package {package_id} (re-grabbed by client).")
        elif existing:
            # Genuine duplicate: currently downloading, already in history, or awaiting a
            # CAPTCHA solve. Do not add a second copy.
            info(f"Package {package_id} already exists ({existing}). Skipping duplicate.")
            return {
                "success": True,
                "package_id": package_id,
                "title": title,
                "duplicate": True,
            }

        if source_result is None:
            result = fail(
                title,
                package_id,
                shared_state,
                reason=f'Could not find matching source for "{title}" - "{url}"',
            )
            return {"package_id": package_id, **result}

        result = process_links(
            shared_state,
            source_result,
            title,
            password,
            package_id,
            imdb_id,
            url,
            size_mb,
            label,
        )
        return {"package_id": package_id, **result}

    except Exception as e:
        if not package_id:
            # Fallback generation if we crashed early
            try:
                client_type = extract_client_type(request_from)
            except Exception:
                client_type = "unknown"

            final_source_key = source_key if source_key else "unknown"

            package_id = package_id or generate_deterministic_package_id(
                title, final_source_key, client_type, download_category
            )

        result = fail(title, package_id, shared_state, reason=f"Unexpected error: {e}")
        return {"package_id": package_id, **result}


def retry_waiting_package(shared_state, package_id):
    """
    Re-attempt a package parked while its host was banned. Removes the waiting row
    first so the dedupe check doesn't treat it as a duplicate, then runs the normal
    download() with the stored context. Returns the download() result (which may
    park it again if the host is still banned).
    """
    from quasarr.providers.host_bans import delete_waiting, get_waiting

    ctx = get_waiting(package_id)
    if not ctx:
        return None
    delete_waiting(package_id)
    return download(
        shared_state,
        ctx.get("request_from", ""),
        ctx.get("download_category"),
        ctx.get("title"),
        ctx.get("url"),
        ctx.get("size_mb"),
        ctx.get("password"),
        ctx.get("imdb_id"),
        ctx.get("source_key"),
        # reuse the exact unique id enqueue_grab minted (never regenerate mid-lifecycle)
        package_id=ctx.get("package_id") or package_id,
    )


def fail(title, package_id, shared_state, reason="Unknown error"):
    """Mark download as failed."""
    try:
        info(f"Reason for failure: {reason}")
        StatsHelper(shared_state).increment_failed_downloads()
        blob = json.dumps({"title": title, "error": reason})
        shared_state.get_db("failed").store(package_id, json.dumps(blob))
        info(f'Package "{title}" marked as failed!')
    except Exception as e:
        info(f'Error marking package "{package_id}" as failed: {e}')
    return {"success": True, "title": title, "failed": True}
