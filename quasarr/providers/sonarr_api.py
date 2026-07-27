# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

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

    def _get(self, path, params=None):
        url = f"{self._base_url}/api/v3{path}"
        headers = {
            "X-Api-Key": self._api_key,
            "Accept": "application/json",
        }
        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=self._timeout
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

    def wanted(self, kind, page=1, page_size=50):
        """Return a wanted episodes page (``kind`` is ``missing`` or ``cutoff``);
        records include the series."""
        return (
            self._get(
                f"/wanted/{kind}",
                params={
                    "page": page,
                    "pageSize": page_size,
                    "includeSeries": "true",
                    "monitored": "true",
                },
            )
            or {}
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


def get_wanted_episodes(shared_state, limit=50):
    """Return aired monitored episodes Sonarr wants as ``[{imdb_id, season,
    episode}]``.

    Covers both missing episodes (no file) and cutoff-unmet ones (present but
    below the quality cutoff), missing first, capped at ``limit``. Episodes that
    have not aired yet are skipped, and pages are walked (bounded by
    ``_WANTED_MAX_PAGES``) so a backlog of unaired entries still yields aired
    ones. Empty when Sonarr is not configured or the request fails. Used to seed
    a show feed for sources that need a concrete season+episode per request.
    """
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
            records = client.wanted(kind, page=page, page_size=limit).get("records", [])
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
                    return episodes

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

    for candidate in all_episodes or []:
        if candidate.get("absoluteEpisodeNumber") == episode:
            target_season = candidate.get("seasonNumber")
            target_episode = candidate.get("episodeNumber")
            if target_season is None or target_episode is None:
                return None
            if (target_season, target_episode) == (season, episode):
                return None
            return target_season, target_episode

    return None


def resolve_scene_numbering(shared_state, imdb_id, season, episode):
    """Translate a scene-numbered search request via Sonarr's own episode data.

    Returns (season, episode) when a translation applies, else None. Fail-open:
    any missing client/series/episode data leaves the request unchanged.
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
