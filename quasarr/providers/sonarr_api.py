# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import time
from datetime import datetime, timezone

import requests

from quasarr.providers.log import error, trace, warn

_SHARED_STATE_KEY = "sonarr_client"


def get_client(shared_state):
    """Return the cached Sonarr client, or None when Sonarr is not configured."""
    return shared_state.values.get(_SHARED_STATE_KEY)


def set_client(shared_state, client):
    """Store the Sonarr client in shared state (pass None to clear)."""
    shared_state.update(_SHARED_STATE_KEY, client)


class SonarrAPIClient:
    """Minimal client for the Sonarr v3 HTTP API.

    See https://sonarr.tv/docs/api/ for the full specification.
    """

    def __init__(self, base_url, api_key, timeout=10):
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _get(self, path, params=None, timeout=None):
        # A caller timeout only ever tightens the client's own: it says how much
        # of its budget is left, not that this request may take longer.
        timeout = min(self._timeout, timeout) if timeout else self._timeout
        url = f"{self._base_url}/api/v3{path}"
        headers = {
            "X-Api-Key": self._api_key,
            "Accept": "application/json",
        }
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            warn(f"Sonarr API request to {url} failed: {e}")
            return None

    def series_lookup_imdb(self, imdb_id):
        """Look up a series on Sonarr by its IMDb ID.

        Sonarr's lookup endpoint takes a free-form term; prefixing with
        ``imdb:`` restricts the match to the given IMDb ID. Returns the first
        result whose ``imdbId`` matches, or ``None`` if no candidate was
        returned or the request failed.
        """
        if not imdb_id:
            return None
        results = self._get("/series/lookup", params={"term": f"imdb:{imdb_id}"})
        if not results:
            return None
        for series in results:
            if series.get("imdbId") == imdb_id:
                return series
        return None

    def series_lookup(self, term):
        """Return Sonarr series lookup candidates for a free-form title."""
        if not term:
            return []
        return self._get("/series/lookup", params={"term": term}) or []

    def series_list(self):
        """Return the series Sonarr actually has added (not a lookup)."""
        return self._get("/series") or []

    def episodes(self, series_id, season=None):
        """Return episodes of a series; pass ``season`` to fetch just one season."""
        params = {"seriesId": series_id}
        if season is not None:
            params["seasonNumber"] = season
        return self._get("/episode", params=params) or []

    def wanted(self, kind, page=1, page_size=50, timeout=None):
        """Return a wanted episodes page (``kind`` is ``missing`` or ``cutoff``);
        records include the series.

        ``None`` means the request failed. A caller walking pages must not read
        that as "no more pages".
        """
        return self._get(
            f"/wanted/{kind}",
            params={
                "page": page,
                "pageSize": page_size,
                "includeSeries": "true",
                "monitored": "true",
            },
            timeout=timeout,
        )


def get_tmdb_id(shared_state, imdb_id):
    """Return the tmdbId Sonarr resolves for the given IMDb ID, or None."""
    client = get_client(shared_state)
    if client is None:
        error("Sonarr metadata lookup skipped: Sonarr is not configured")
        return None

    series = client.series_lookup_imdb(imdb_id)
    if not series:
        return None

    tmdb_id = series.get("tmdbId")
    if not tmdb_id:
        warn(f"Sonarr response for {imdb_id} did not include a TMDB ID")
        return None

    trace(f"Resolved IMDb ID '{imdb_id}' to TMDB ID '{tmdb_id}'")

    return tmdb_id


def get_tvdb_id(shared_state, imdb_id):
    """Return the tvdbId Sonarr resolves for the given IMDb ID, or None."""
    client = get_client(shared_state)
    if client is None:
        error("Sonarr metadata lookup skipped: Sonarr is not configured")
        return None

    series = client.series_lookup_imdb(imdb_id)
    if not series:
        return None

    tvdb_id = series.get("tvdbId")
    if not tvdb_id:
        warn(f"Sonarr response for {imdb_id} did not include a TVDB ID")
        return None

    trace(f"Resolved IMDb ID '{imdb_id}' to TVDB ID '{tvdb_id}'")

    return tvdb_id


# Cap on wanted pages walked per kind so a backlog of unaired entries cannot
# turn one feed run into unbounded Sonarr paging.
_WANTED_MAX_PAGES = 5


def _has_aired(record, now):
    """True only when the episode has a known air date in the past.

    Unaired or undated episodes have no release to search for yet, so they are
    excluded from the feed seed (the show equivalent of skipping announced
    movies). cutoff-unmet entries can include not-yet-aired episodes, so the
    check applies to every wanted record.
    """
    air = record.get("airDateUtc")
    if not air:
        return False
    try:
        return datetime.fromisoformat(air.replace("Z", "+00:00")) <= now
    except ValueError:
        return False


def get_wanted_episodes(shared_state, limit=50, deadline=None, status=None):
    """Return aired monitored episodes Sonarr wants as ``[{imdb_id, season,
    episode}]``.

    Covers both missing episodes (no file) and cutoff-unmet ones (present but
    below the quality cutoff), missing first, capped at ``limit``. Episodes that
    have not aired yet are skipped, and pages are walked (bounded by
    ``_WANTED_MAX_PAGES``) so a backlog of unaired entries still yields aired
    ones. Empty when Sonarr is not configured or the request fails. Used to seed
    a show feed for sources that need a concrete season+episode per request.
    """
    if status is not None:
        # Callers that persist progress across runs need to know whether this is
        # the whole wanted list or as far as paging got.
        status["complete"] = False

    client = get_client(shared_state)
    if client is None:
        return []

    now = datetime.now(timezone.utc)
    episodes = []
    seen = set()
    for kind in ("missing", "cutoff"):
        for page in range(1, _WANTED_MAX_PAGES + 1):
            if len(episodes) >= limit:
                return episodes
            # Every page is its own Sonarr request, so a slow instance must not
            # spend a caller's whole budget before it gets any seeds - and the
            # last page before the deadline must not overrun it either.
            page_timeout = None
            if deadline is not None:
                page_timeout = deadline - time.time()
                if page_timeout <= 0:
                    return episodes
            page_data = client.wanted(
                kind, page=page, page_size=limit, timeout=page_timeout
            )
            if page_data is None:
                return episodes  # request failed: what we have is partial
            records = page_data.get("records", [])
            if not records:
                break  # no more pages for this kind
            for record in records:
                if not _has_aired(record, now):
                    continue
                series = record.get("series") or {}
                imdb_id = series.get("imdbId")
                season = record.get("seasonNumber")
                episode = record.get("episodeNumber")
                if not imdb_id or season is None or episode is None:
                    continue
                key = (imdb_id, season, episode)
                if key in seen:
                    continue
                seen.add(key)
                episodes.append(
                    {"imdb_id": imdb_id, "season": season, "episode": episode}
                )
                if len(episodes) >= limit:
                    if status is not None:
                        status["complete"] = True
                    return episodes

    if status is not None:
        status["complete"] = True
    return episodes


def translate_scene_numbering(season, episode, season_episodes, all_episodes):
    """Maja fork: map a scene-numbered request back to TVDB season/episode.

    Sonarr applies TheXEM scene mappings BEFORE it queries an indexer. For anime
    whose mapping collapses every season into scene season 1 with absolute
    numbering (InuYasha: TVDB S07E01 -> scene S01E168), the request that reaches
    us is "season 1, episode 168" — a pair that does not exist. DDL sites organise
    those shows by TVDB-ish seasons ("Staffel 7"), so the scene request finds
    nothing while the TVDB one finds the whole arc.

    Only fires when the requested pair is genuinely absent AND an episode with
    that absolute number exists. Valid pairs (Slime S01E02, Bleach S17E14) and
    absolute-only searches are left untouched.

    Args:
        season/episode: the requested numbers.
        season_episodes: Sonarr episodes of that season.
        all_episodes: every episode of the series.

    Returns:
        (season, episode) tuple when a translation applies, else None.
    """
    if season is None or episode is None:
        return None
    try:
        season = int(season)
        episode = int(episode)
    except (TypeError, ValueError):
        return None

    for candidate in season_episodes or []:
        if candidate.get("episodeNumber") == episode:
            return None  # requested pair exists as-is — nothing to translate

    # Scene first, TVDB second — same reasoning as resolve_absolute_candidates:
    # Sonarr numbers its request in scene terms whenever TheXEM has a mapping,
    # so the scene field is what the incoming number actually speaks.
    for field in ("sceneAbsoluteEpisodeNumber", "absoluteEpisodeNumber"):
        for candidate in all_episodes or []:
            if candidate.get(field) != episode:
                continue
            target_season = candidate.get("seasonNumber")
            target_episode = candidate.get("episodeNumber")
            if target_season is None or target_episode is None:
                return None
            if (target_season, target_episode) == (season, episode):
                return None
            return target_season, target_episode

    return None


def is_anime(series_record):
    """Is this Sonarr series record an anime?

    The scene-numbering collapse this module translates is a property of
    TheXEM's anime mappings. Reading the type keeps anime handling off the path
    of an ordinary series instead of relying on the translation to no-op there.
    """
    return str((series_record or {}).get("seriesType") or "").lower() == "anime"


def resolve_scene_numbering(shared_state, imdb_id, season, episode):
    """Translate a scene-numbered search request via Sonarr's own episode data.

    Returns (season, episode) when a translation applies, else None. Fail-open:
    any missing client/series/episode data leaves the request unchanged.

    Anime only. The translation used to run on EVERY tv search and only decline
    later, which cost two Sonarr round trips against the 95s fan-out deadline
    for every ordinary series - and left its rewrite branch reachable for one,
    because that branch fires whenever a requested pair is missing from its
    season, which also happens on plain metadata lag.
    """
    if not imdb_id or season is None or episode is None:
        return None

    client = get_client(shared_state)
    if client is None:
        return None

    try:
        wanted_imdb = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
        series_id = None
        for series in client.series_list():
            if series.get("imdbId") == wanted_imdb:
                if not is_anime(series):
                    trace(
                        f"scene numbering skipped for {wanted_imdb}: "
                        "not an anime series"
                    )
                    return None
                series_id = series.get("id")
                break
        if series_id is None:
            return None

        # Cheap path first: one season's episodes answer the common case, where
        # the requested pair exists and nothing needs translating. Only a miss
        # justifies pulling the full episode list (1000+ entries on long anime).
        season_episodes = client.episodes(series_id, season=season)
        if any(
            candidate.get("episodeNumber") == int(episode)
            for candidate in season_episodes or []
        ):
            return None

        translated = translate_scene_numbering(
            season, episode, season_episodes, client.episodes(series_id)
        )
        if translated:
            trace(
                f"scene numbering translated for {wanted_imdb}: "
                f"S{season}E{episode} -> S{translated[0]}E{translated[1]}"
            )
        return translated
    except Exception as e:
        trace(f"scene numbering translation failed for {imdb_id}: {e}")
        return None


def _episode_pair(candidate):
    """Return ``(season, episode)`` of a Sonarr episode record, or ``None``."""
    season = candidate.get("seasonNumber")
    episode = candidate.get("episodeNumber")
    if season is None or episode is None:
        return None
    return season, episode


def resolve_absolute_candidates(shared_state, imdb_id, absolute_episode):
    """Every (season, episode) pair an absolute-numbered request can mean.

    Maja fork: Sonarr sends the **scene** absolute number, not the TVDB one.
    For shows whose TheXEM mapping restarts absolute numbering per season, that
    number is ambiguous — Slime S01E17..S04E17 all carry
    ``sceneAbsoluteEpisodeNumber == 17`` while their ``absoluteEpisodeNumber``
    is 17/41/65/89. Matching only the TVDB field (the pre-maja.31 behaviour)
    always resolved such a request to season 1, so every source was asked for
    the wrong season and correct hits were dropped as season mismatches.

    Scene hits win whenever there are any: Sonarr uses scene numbering as soon
    as a TheXEM mapping exists. Without them the TVDB absolute is the truth,
    which keeps genuinely absolute-numbered shows (One Piece) unchanged.

    Ordering matters — the caller hands the FIRST pair to sources that bake
    season/episode into their query and cannot take a set. Episodes Sonarr is
    still missing come first, then the newest season, because a resetting scene
    number is nearly always chasing a recent episode. Sonarr's cutoff-unmet
    list would be the sharper signal but costs a paged walk per search; the
    candidate set makes that precision unnecessary for set-aware sources.

    Fail-open: any missing client/series/episode data returns ``[]`` and the
    caller then behaves exactly as before.
    """
    if not imdb_id or absolute_episode is None:
        return []

    try:
        absolute_episode = int(absolute_episode)
    except (TypeError, ValueError):
        return []

    client = get_client(shared_state)
    if client is None:
        return []

    try:
        wanted_imdb = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
        series_id = None
        for series in client.series_list():
            if series.get("imdbId") == wanted_imdb:
                if not is_anime(series):
                    # Absolute numbering, TheXEM scene mappings and the
                    # candidate-pair machinery downstream are anime concepts.
                    # An ordinary series reaching here means the request was
                    # malformed, not that it needs translating - and handing a
                    # candidate list to the shared matcher for one would let
                    # anime handling decide a search it has no business in.
                    trace(
                        f"absolute candidates skipped for {wanted_imdb}: "
                        "not an anime series"
                    )
                    return []
                series_id = series.get("id")
                break
        if series_id is None:
            return []

        scene_hits = []
        absolute_hits = []
        for candidate in client.episodes(series_id) or []:
            pair = _episode_pair(candidate)
            if pair is None:
                continue
            if candidate.get("sceneAbsoluteEpisodeNumber") == absolute_episode:
                scene_hits.append((candidate, pair))
            elif candidate.get("absoluteEpisodeNumber") == absolute_episode:
                absolute_hits.append((candidate, pair))

        hits = scene_hits or absolute_hits
        if not hits:
            return []

        # Specials and unmonitored seasons are never what an absolute request
        # means — but only drop them while something else survives.
        real = [(c, p) for c, p in hits if p[0] > 0 and c.get("monitored")]
        hits = real or hits

        hits.sort(key=lambda item: (bool(item[0].get("hasFile")), -item[1][0]))
        candidates = [pair for _, pair in hits]

        trace(
            f"absolute numbering resolved for {wanted_imdb}: E{absolute_episode} -> "
            + ", ".join(f"S{s}E{e}" for s, e in candidates)
            + (" (scene)" if scene_hits else " (tvdb)")
        )
        return candidates
    except Exception as e:
        trace(f"absolute numbering resolution failed for {imdb_id}: {e}")
        return []


def resolve_absolute_numbering(shared_state, imdb_id, absolute_episode):
    """Translate an absolute episode number into (season, episode) via Sonarr.

    Maja fork: Sonarr sends absolute-numbered searches (``E1120``, no season)
    for ``seriesType=anime``. Quasarr's fan-out drops every source that lacks
    ``supports_absolute_numbering`` — which is all of them except AL and AT.
    The German sources that DO carry the content (SF proven: more German-audio
    hits than AL on the same episode) are therefore never even asked, leaving AL
    a single point of failure. Its 4.5-day outage in 08/2026 blinded the whole
    anime pipeline.

    Translating once per search lets those sources answer with the season/episode
    pair they organise by, without changing what AL/AT receive.

    Since maja.31 this is the single-answer view of
    :func:`resolve_absolute_candidates` — an ambiguous scene number yields the
    best candidate rather than whatever episode happened to come first.

    Fail-open: any missing client/series/episode data returns None, and the
    caller then behaves exactly as before.

    Returns:
        (season, episode) tuple, else None.
    """
    candidates = resolve_absolute_candidates(shared_state, imdb_id, absolute_episode)
    return candidates[0] if candidates else None
