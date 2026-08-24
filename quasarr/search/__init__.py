# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import timezone
from email.utils import parsedate_to_datetime
from threading import Lock

from quasarr.constants import (
    SEARCH_CAT_BOOKS,
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_MUSIC,
    SEARCH_CAT_SHOWS,
    SEARCH_FANOUT_DEADLINE_SECONDS,
)
from quasarr.providers.imdb_metadata import get_imdb_metadata
from quasarr.providers.log import (
    debug,
    error,
    get_source_logger,
    info,
    trace,
    warn,
)
from quasarr.search.sources import get_sources
from quasarr.search.sources.helpers.search_source import AbstractSearchSource
from quasarr.storage.categories import get_search_category_sources

# Feed results are cached per (source, action, owner category). The previous hardcoded
# 60s TTL was shorter than a single slow category pass (measured up to 160s), so the
# cache-family optimization ("2040/2045 served from the 2000 crawl") never fired under
# load and every *arr RSS sync re-crawled all sources. 600s is safe staleness for RSS
# data that clients poll every 15-60 minutes.
FEED_CACHE_TTL_SECONDS = int(os.environ.get("FEED_CACHE_TTL", "600"))


def get_search_results(
    shared_state,
    request_from,
    search_category,
    imdb_id="",
    search_phrase="",
    season=None,
    episode=None,
    offset=0,
    limit=1000,
    deadline=None,
):
    from quasarr.providers.utils import (
        determine_search_category,
        get_base_search_category_id,
        get_search_behavior_category,
        get_search_cache_owner_category,
        get_search_capability_category,
        normalize_optional_int,
        parse_episode_date,
        release_matches_search_category,
    )

    sources = get_sources()

    if imdb_id and not imdb_id.startswith("tt"):
        imdb_id = f"tt{imdb_id}"

    episode_date = parse_episode_date(season, episode)
    if episode_date:
        season = None
        episode = None

    # Maja fork: Sonarr rewrites searches into TheXEM SCENE numbering before it
    # reaches us. For anime whose mapping collapses all seasons into scene season
    # 1 (InuYasha: TVDB S07E01 -> scene S01E168) the requested pair does not
    # exist, and the DDL sites — which organise by TVDB seasons ("Staffel 7") —
    # return nothing. Translate back via Sonarr's own episode data; no-op for
    # valid pairs and absolute-only searches, fail-open when Sonarr is unset.
    # bottle hands absent query parameters through as "" rather than None, so a
    # bare `is not None` fired this on EVERY tv search and spent a Sonarr round
    # trip on a request that could not translate anything.
    if (
        imdb_id
        and normalize_optional_int(season) is not None
        and normalize_optional_int(episode) is not None
    ):
        from quasarr.providers.sonarr_api import resolve_scene_numbering

        translated = resolve_scene_numbering(shared_state, imdb_id, season, episode)
        if translated:
            season, episode = translated

    # Determine search category if not provided
    if not search_category:
        search_category = determine_search_category(request_from)

    # Resolve base category for logic (Movies, TV, etc.).
    base_search_category = (
        get_base_search_category_id(search_category) or search_category
    )
    behavior_search_category = (
        get_search_behavior_category(search_category) or search_category
    )

    if base_search_category == SEARCH_CAT_MOVIES:
        from quasarr.providers.radarr_api import get_client as get_radarr_client

        if get_radarr_client(shared_state) is None:
            error("Movie search unavailable: Radarr is not configured")
            return []
    elif base_search_category == SEARCH_CAT_SHOWS:
        from quasarr.providers.sonarr_api import get_client as get_sonarr_client

        if get_sonarr_client(shared_state) is None:
            error("TV search unavailable: Sonarr is not configured")
            return []

    # Anchored before metadata warming: sources derive their own budget from this,
    # so starting the clock after the warming would let a source outlive the
    # deadline by however long the warming took.
    start_time = time.time()

    if imdb_id and (deadline is None or time.time() < deadline):
        # A failed refresh is not cached, so every category of a multi-category
        # request would otherwise pay the Arr client timeout again, past the
        # ceiling the deadline exists to hold.
        get_imdb_metadata(shared_state, imdb_id, base_search_category)

    capability_category = get_search_capability_category(search_category)
    is_custom_search_category = False
    try:
        is_custom_search_category = int(search_category) >= 100000
    except (TypeError, ValueError):
        pass
    # Cache keys are shared at the cache-family owner level.
    # Multi-category callers should execute same-family categories in ascending order
    # so the lowest category populates cache before stricter siblings run.
    cache_key_category = (
        search_category
        if is_custom_search_category
        else get_search_cache_owner_category(behavior_search_category)
    )

    # Filter out sources that are not in the search category's whitelist
    # We use the original search_category ID here to get the specific whitelist
    whitelisted_sources = get_search_category_sources(search_category)

    if whitelisted_sources:
        debug(
            f"Using whitelist for category <g>{search_category}</g>: {', '.join([s.upper() for s in whitelisted_sources])}"
        )

    search_executor = SearchExecutor(deadline=deadline)

    # Config retrieval
    config = shared_state.values["config"]("Hostnames")

    use_pagination = True

    # Use base_search_category for logic branching
    if imdb_id:
        stype = f"IMDb-ID <y>{imdb_id}</y>"

        if season:
            stype += f" <g>S{season}</g>"
        if episode:
            stype += f"{'' if season else ' '}<e>E{episode}</e>"
        if episode_date:
            stype += f" <g>{episode_date:%Y}</g>-<e>{episode_date:%m}</e>-<y>{episode_date:%d}</y>"

        if base_search_category in [SEARCH_CAT_MOVIES, SEARCH_CAT_SHOWS]:
            args = (shared_state, start_time, behavior_search_category)

            # Maja fork: an absolute-numbered request (episode, no season) is
            # only answerable by AL/AT, so every other source was dropped below.
            # The German sources that actually carry the content never got asked
            # — SF returns more German-audio hits than AL on the same episode.
            # Resolve ONCE here and hand the result to those sources.
            #
            # maja.31: the resolution is a LIST. Sonarr sends TheXEM's scene
            # absolute number, which restarts per season on many anime — Slime's
            # "17" is S01E17 through S04E17. Picking one silently made season 1
            # the answer to every such request. Sources that fetch whole series
            # and filter locally get the full set and can accept any of them;
            # sources that bake season/episode into their query get the best
            # single candidate. Fail-open: an empty list is the old behaviour.
            absolute_candidates = []
            if imdb_id and episode and not season:
                from quasarr.providers.sonarr_api import resolve_absolute_candidates

                absolute_candidates = resolve_absolute_candidates(
                    shared_state, imdb_id, episode
                )

            for source in sources.values():
                source_logger = get_source_logger(source.initials)

                if not config.get(source.initials):
                    source_logger.trace("Hostname missing in config")
                    continue

                if capability_category not in source.supported_categories:
                    source_logger.trace(
                        f"Category <g>{capability_category}</g> not supported"
                    )
                    continue

                if whitelisted_sources and source.initials not in whitelisted_sources:
                    source_logger.trace(
                        f"Category <g>{search_category}</g> not whitelisted"
                    )
                    continue

                if not source.supports_imdb:
                    source_logger.warn("IMDb ID unsupported")
                    continue

                source_season, source_episode = season, episode
                source_pairs = None

                if episode and not season:
                    if absolute_candidates:
                        # Every source that can weigh alternatives gets them —
                        # AL/AT included, so their season-specific search
                        # variants ("Staffel 4") exist at all. Without a season
                        # they fall back to the plain title, which is the show's
                        # main page and therefore season 1.
                        if getattr(source, "supports_candidate_pairs", False):
                            source_pairs = list(absolute_candidates)
                    if not source.supports_absolute_numbering:
                        if not absolute_candidates:
                            source_logger.trace(
                                "Search with absolute EP number unsupported"
                            )
                            continue
                        source_season, source_episode = absolute_candidates[0]
                        source_logger.trace(
                            f"Absolute E{episode} translated to "
                            f"S{source_season}E{source_episode}"
                            + (
                                f" (of {len(absolute_candidates)} candidates)"
                                if len(absolute_candidates) > 1
                                else ""
                            )
                        )

                kwargs = {
                    "search_string": imdb_id,
                    "season": source_season,
                    "episode": source_episode,
                }

                if source_pairs:
                    kwargs["accepted_pairs"] = source_pairs

                if episode_date:
                    if not source.supports_date_numbering:
                        source_logger.trace("Search with date unsupported")
                        continue

                    kwargs["episode_date"] = episode_date

                search_executor.add(
                    source,
                    args,
                    kwargs,
                    use_cache=True,
                    cache_category=cache_key_category,
                )
        else:
            warn(
                f"{stype} is not supported for <d>{request_from}</d>, category: {search_category} (Base: {base_search_category})"
            )

    elif search_phrase:
        stype = f"Search-Phrase <b>{search_phrase}</b>"
        if base_search_category in [SEARCH_CAT_BOOKS, SEARCH_CAT_MUSIC]:
            args = (shared_state, start_time, behavior_search_category)
            kwargs = {"search_string": search_phrase}
            for source in sources.values():
                source_logger = get_source_logger(source.initials)

                if not config.get(source.initials):
                    source_logger.trace("Hostname missing in config")
                    continue

                if capability_category not in source.supported_categories:
                    source_logger.trace(
                        f"Category <g>{capability_category}</g> not supported"
                    )
                    continue

                if whitelisted_sources and source.initials not in whitelisted_sources:
                    source_logger.trace(
                        f"Category <g>{search_category}</g> not whitelisted"
                    )
                    continue

                if not source.supports_phrase:
                    source_logger.warn("Search phrase unsupported")
                    continue

                search_executor.add(
                    source,
                    args,
                    kwargs,
                    use_cache=True,
                    cache_category=cache_key_category,
                )
        else:
            warn(
                f"{stype} is not supported for <d>{request_from}</d>, category: {search_category} (Base: {base_search_category})"
            )

    else:
        stype = "<b>Feed</b> search"
        args = (shared_state, start_time, behavior_search_category)
        kwargs = {}
        use_pagination = False
        for source in sources.values():
            source_logger = get_source_logger(source.initials)

            if not config.get(source.initials):
                source_logger.trace("Hostname missing in config")
                continue

            if capability_category not in source.supported_categories:
                source_logger.trace(
                    f"Category <g>{capability_category}</g> not supported"
                )
                continue

            if whitelisted_sources and source.initials not in whitelisted_sources:
                source_logger.trace(
                    f"Category <g>{search_category}</g> not whitelisted"
                )
                continue

            search_executor.add(
                source,
                args,
                kwargs,
                use_cache=True,
                ttl=FEED_CACHE_TTL_SECONDS,
                action="feed",
                cache_category=cache_key_category,
            )

    debug(f"Starting <g>{len(search_executor.searches)}</g> searches for {stype}")

    # Unpack the new return values (all_cached, min_ttl)
    results, status_bar, all_cached, min_ttl = search_executor.run_all()

    elapsed_time = time.time() - start_time

    # Sort results by date (newest first)
    def get_date(item):
        try:
            dt = parsedate_to_datetime(item.get("details", {}).get("date", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return parsedate_to_datetime("Thu, 01 Jan 1970 00:00:00 +0000")

    results.sort(key=get_date, reverse=True)

    filtered_results = [
        release
        for release in results
        if release_matches_search_category(
            search_category,
            release.get("details", {}).get("title", ""),
        )
    ]
    filtered_out_count = len(results) - len(filtered_results)
    if filtered_out_count > 0:
        debug(
            f"Filtered out <r>{filtered_out_count}</r> releases by title rules for category <g>{search_category}</g>"
        )
    results = filtered_results

    # Calculate pagination for logging and return
    total_count = len(results)

    # Slicing
    if use_pagination:
        sliced_results = results[offset : offset + limit]
    else:
        sliced_results = results

    if sliced_results:
        trace(f"First {len(sliced_results)} results sorted by date:")
        for i, res in enumerate(sliced_results):
            details = res.get("details", {})
            trace(f"{i + 1}. {details.get('date')} | {details.get('title')}")

    # Formatting for log (1-based index for humans)
    log_start = min(offset + 1, total_count) if total_count > 0 else 0
    log_end = min(offset + limit, total_count) if use_pagination else total_count

    # Logic to switch between "Time taken" and "from cache"
    if all_cached:
        time_info = f"from cache ({int(min_ttl)}s left)"
    else:
        time_info = f"Time taken: {elapsed_time:.2f} seconds"

    info(
        f"Providing releases <g>{log_start}-{log_end}</g> of <g>{total_count}</g> to <d>{request_from}</d> "
        f"for {stype}{status_bar} <blue>{time_info}</blue>"
    )

    return sliced_results


class SearchExecutor:
    def __init__(self, deadline=None):
        self.searches = []
        # Absolute time this fan-out must be answered by. Callers that run several
        # executors for one *arr request pass their own so the runs share a single
        # deadline instead of each starting a fresh one.
        self.deadline = (
            deadline
            if deadline is not None
            else time.time() + SEARCH_FANOUT_DEADLINE_SECONDS
        )

    def add(
        self,
        source: AbstractSearchSource,
        args,
        kwargs,
        use_cache=False,
        ttl=300,
        action="search",
        cache_category=None,
    ):
        key_args = list(args)
        key_args[1] = None
        if cache_category is not None and len(key_args) >= 3:
            key_args[2] = cache_category
        key_args = tuple(key_args)
        key = hash((source.initials, action, key_args, frozenset(kwargs.items())))
        self.searches.append(
            (
                key,
                lambda: getattr(source, action)(*args, **kwargs),
                use_cache,
                ttl,
                source.initials,
            )
        )

    def run_all(self):
        results = []
        future_to_meta = {}

        # Track cache state
        all_cached = len(self.searches) > 0
        min_ttl = float("inf")
        bar_str = ""  # Initialize to prevent UnboundLocalError on full cache

        deadline = self.deadline
        # One worker per source: the default pool is sized from the CPU count, so
        # on a small host the last sources would queue behind the first ones and
        # burn the deadline without ever having started.
        # Not a context manager on purpose: its __exit__ joins every worker, which
        # would re-introduce the very wait the deadline exists to prevent.
        executor = ThreadPoolExecutor(max_workers=max(1, len(self.searches)))
        try:
            current_index = 0
            pending_futures = []
            skipped_badges = []

            for key, func, use_cache, ttl, source_name in self.searches:
                cached_result = None
                exp = 0

                if use_cache:
                    # Get both result and expiry
                    cached_result, exp = search_cache.get(key)

                if cached_result is not None:
                    get_source_logger(source_name).debug(
                        f"Using cached result with cache_key '{key}'"
                    )
                    results.extend(cached_result)

                    # Calculate TTL for this cached item
                    ttl_left = exp - time.time()
                    if ttl_left < min_ttl:
                        min_ttl = ttl_left
                else:
                    all_cached = False
                    if time.time() >= deadline:
                        # Nothing left to spend. Starting the work anyway would
                        # only detach a worker whose result this response can no
                        # longer use, and hit the source a second time for it.
                        skipped_badges.append(
                            f"<bg yellow><black>{source_name.upper()}</black></bg yellow>"
                        )
                        get_source_logger(source_name).warn(
                            "Not started, this request is already out of time"
                        )
                        continue

                    # Single-flight: if another request is already crawling this exact
                    # (source, action, category) key, adopt its future instead of
                    # crawling everything a second time. Overlapping *arr retries used
                    # to double the load on every slow feed — and the deadline above
                    # makes those retries MORE likely, not less, because a source that
                    # misses the deadline is absent from the response the client sees.
                    future = None
                    adopted = False
                    if use_cache:
                        with _inflight_lock:
                            running = _inflight_futures.get(key)
                            if running is not None and not running.done():
                                future = running
                                adopted = True
                    if adopted:
                        get_source_logger(source_name).debug(
                            "Adopting in-flight crawl instead of starting a duplicate"
                        )
                    else:
                        future = executor.submit(func)
                        if use_cache:
                            with _inflight_lock:
                                _inflight_futures[key] = future
                            future.add_done_callback(_make_inflight_cleanup(key))

                    # The crawl owner writes the cache; adopters only read the result.
                    cache_meta = (key, ttl) if (use_cache and not adopted) else None
                    future_to_meta[future] = (current_index, cache_meta, source_name)
                    pending_futures.append(future)
                    current_index += 1

            results_badges = [""] * len(pending_futures)
            if pending_futures:
                collected = set()

                def collect(future):
                    collected.add(future)
                    index, cache_meta, source_name = future_to_meta[future]
                    try:
                        res = future.result()
                        if res and len(res) > 0:
                            badge = f"<bg green><black>{source_name.upper()}</black></bg green>"
                        else:
                            get_source_logger(source_name).debug(
                                "❌ No results returned"
                            )
                            badge = f"<bg black><white>{source_name.upper()}</white></bg black>"

                        results_badges[index] = badge
                        results.extend(res)
                        if cache_meta:
                            cache_key, cache_ttl = cache_meta
                            search_cache.set(cache_key, res, ttl=cache_ttl)
                    except Exception as e:
                        results_badges[index] = (
                            f"<bg red><white>{source_name.upper()}</white></bg red>"
                        )
                        get_source_logger(source_name).warn(f"Search error: {e}")

                try:
                    for future in as_completed(
                        pending_futures, timeout=max(0.1, deadline - time.time())
                    ):
                        collect(future)
                except FutureTimeoutError:
                    # Radarr and Sonarr drop an indexer that outlives their own
                    # request timeout, so answer with whatever is ready instead of
                    # waiting for the straggler.
                    for future in pending_futures:
                        if future in collected:
                            continue
                        if future.done():
                            collect(future)
                            continue

                        index, cache_meta, source_name = future_to_meta[future]
                        results_badges[index] = (
                            f"<bg yellow><black>{source_name.upper()}</black></bg yellow>"
                        )
                        get_source_logger(source_name).warn(
                            f"Dropped from this response after "
                            f"{SEARCH_FANOUT_DEADLINE_SECONDS}s"
                        )
                        # Dropped from THIS response, but not thrown away: the crawl
                        # keeps running and writes its result to the cache, so the
                        # next request serves it instantly. Without this a source
                        # that is reliably slower than the deadline (AL: 66-73s vs
                        # 60s) would never reach the client at all — it would be
                        # dropped on every single request, forever.
                        if cache_meta:
                            future.add_done_callback(
                                _make_late_cache_writer(cache_meta, source_name)
                            )

            if results_badges or skipped_badges:
                bar_str = f" [{' '.join(results_badges + skipped_badges)}]"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return results, bar_str, all_cached, min_ttl


# In-flight registry for single-flight crawls, keyed like the search cache.
_inflight_lock = Lock()
_inflight_futures = {}


def _make_inflight_cleanup(key):
    def _cleanup(future):
        with _inflight_lock:
            if _inflight_futures.get(key) is future:
                del _inflight_futures[key]

    return _cleanup


def _make_late_cache_writer(cache_meta, source_name):
    cache_key, cache_ttl = cache_meta

    def _write(future):
        try:
            res = future.result()
        except Exception as e:
            get_source_logger(source_name).warn(f"Late search error: {e}")
            return
        search_cache.set(cache_key, res, ttl=cache_ttl)
        get_source_logger(source_name).debug(
            "Late result cached after response budget"
        )

    return _write


class SearchCache:
    def __init__(self):
        self.last_cleaned = time.time()
        self.cache = {}
        self._lock = Lock()

    def clean(self, now):
        with self._lock:
            self._clean_locked(now)

    def _clean_locked(self, now):
        if now - self.last_cleaned < 60:
            return
        keys_to_delete = [k for k, (_, exp) in self.cache.items() if now >= exp]
        for k in keys_to_delete:
            del self.cache[k]
        self.last_cleaned = now

    def get(self, key):
        now = time.time()
        with self._lock:
            val, exp = self.cache.get(key, (None, 0))
            if now < exp:
                return (val, exp)

            # Clean up stale key opportunistically.
            if key in self.cache:
                del self.cache[key]
            return (None, 0)

    def set(self, key, value, ttl=300):
        now = time.time()
        with self._lock:
            self.cache[key] = (value, now + ttl)
            self._clean_locked(now)


search_cache = SearchCache()
