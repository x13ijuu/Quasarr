# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import json
import os
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager

import requests
from bs4 import BeautifulSoup

from quasarr.constants import (
    DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    SESSION_REQUEST_TIMEOUT_SECONDS,
)
from quasarr.providers.log import debug, info
from quasarr.providers.utils import is_flaresolverr_available

_CLOUDFLARE_GATE_TTL_SECONDS = 24 * 60 * 60
_cloudflare_gate_expires_at = {}
_cloudflare_gate_lock = threading.Lock()

# --- Maja fork: concurrency cap for FlareSolverr solves ----------------------
#
# Every solve is a Chrome. A sessionless request spawns a fresh one per request
# (flaresolverr_service.py: `driver = utils.get_webdriver(...)`), a session keeps
# one alive. Nothing on either side bounds how many run at once: FlareSolverr's
# SessionsStorage has no cap and no TTL, and the search fan-out deliberately
# starts one worker PER SOURCE (search/__init__.py: "One worker per source"),
# with cache families running in parallel on top and Sonarr plus Radarr asking
# at the same time.
#
# Measured 2026-08-30 against three live targets: a single solve peaks at
# 320-380 MB over a ~540 MB idle baseline. Against the 2048 MB cgroup limit that
# leaves room for four concurrent solves — so the container did not "leak", it
# simply ran more browsers than fit, and hitting the ceiling put the cgroup into
# reclaim thrash: sessions.create then fails and the source drops out of the
# response after the fan-out deadline. Raising the limit only moves the ceiling
# (measured: capped at 1536 MB it pinned at 1536, at 2048 MB it pinned at 2048).
#
# Default 3 = (2048 - 540) / 380, rounded down, minus one solve of headroom.
FLARESOLVERR_MAX_CONCURRENT = int(os.environ.get("FLARESOLVERR_MAX_CONCURRENT", "3"))
# Upper bound on queueing, so a slot wait can never eat the whole fan-out budget:
# a solve takes ~12 s, so 30 s covers two waves of waiters.
FLARESOLVERR_SLOT_WAIT_SECONDS = int(
    os.environ.get("FLARESOLVERR_SLOT_WAIT", "30")
)
_solve_slots = threading.BoundedSemaphore(max(1, FLARESOLVERR_MAX_CONCURRENT))


@contextmanager
def solve_slot(timeout=None):
    """Hold one of the concurrent-solve slots, or refuse.

    Saturation behaves like an unreachable source (RuntimeError, which every
    caller already treats as "this host gave us nothing") rather than blocking:
    waiting past the fan-out deadline would trade a memory problem for a timeout
    problem, and Sonarr's 100 s limit is not configurable.
    """
    wait = FLARESOLVERR_SLOT_WAIT_SECONDS
    if timeout:
        wait = min(wait, timeout)
    if not _solve_slots.acquire(timeout=wait):
        # Logged, not silent: a cap set too low costs search hits, and that must
        # be visible as a cause rather than as unexplained missing releases.
        info(
            f"FlareSolverr busy: no free solve slot after {wait}s "
            f"(limit {FLARESOLVERR_MAX_CONCURRENT}) — skipping this request"
        )
        raise RuntimeError(
            f"FlareSolverr is saturated ({FLARESOLVERR_MAX_CONCURRENT} concurrent "
            f"solves); skipped after waiting {wait}s"
        )
    try:
        yield
    finally:
        _solve_slots.release()


def _cloudflare_gate_key(url):
    return urllib.parse.urlparse(url).netloc.casefold()


def _is_cloudflare_gated(url):
    key = _cloudflare_gate_key(url)
    if not key:
        return False

    now = time.monotonic()
    with _cloudflare_gate_lock:
        expires_at = _cloudflare_gate_expires_at.get(key)
        if expires_at is None:
            return False
        if expires_at <= now:
            _cloudflare_gate_expires_at.pop(key, None)
            return False
        return True


def _mark_cloudflare_gated(url):
    """Remember a challenged netloc and return True only for first detection."""
    key = _cloudflare_gate_key(url)
    if not key:
        return True

    now = time.monotonic()
    with _cloudflare_gate_lock:
        expires_at = _cloudflare_gate_expires_at.get(key)
        if expires_at is not None and expires_at > now:
            return False
        _cloudflare_gate_expires_at[key] = now + _CLOUDFLARE_GATE_TTL_SECONDS
        return True


def _clear_cloudflare_gate_cache():
    """Clear process-local gate state for hermetic tests."""
    with _cloudflare_gate_lock:
        _cloudflare_gate_expires_at.clear()


def is_cloudflare_challenge(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")

    # Check <title>
    title = (soup.title.string or "").strip().lower() if soup.title else ""
    if "just a moment" in title or "attention required" in title:
        return True

    # Check known Cloudflare elements
    if soup.find(id="challenge-form"):
        return True
    if soup.find("div", {"class": "cf-browser-verification"}):
        return True
    if soup.find("div", {"id": "cf-challenge-running"}):
        return True

    # Check scripts referencing Cloudflare
    for script in soup.find_all("script", src=True):
        if "cdn-cgi/challenge-platform" in script["src"]:
            return True

    # Optional: look for Cloudflare comment or beacon
    if "data-cf-beacon" in html or "<!-- cloudflare -->" in html.lower():
        return True

    return False


class LazyFlareSolverrSession:
    """Lazily create and reuse one FlareSolverr session for an operation."""

    def __init__(self, shared_state):
        self.shared_state = shared_state
        self.session_id = None

    def get(
        self,
        url,
        headers,
        timeout,
        request_get=requests.get,
        document_start_js=None,
        execute_js=None,
    ):
        if not _is_cloudflare_gated(url):
            response = request_get(url, headers=headers, timeout=timeout)
            if response.status_code != 403 and not is_cloudflare_challenge(
                response.text
            ):
                return response

            if _mark_cloudflare_gated(url):
                debug(
                    "Detected Cloudflare protection. Routing this host through "
                    "FlareSolverr for 24 hours."
                )

        if not is_flaresolverr_available(self.shared_state):
            raise requests.RequestException(
                "Cloudflare protection detected but FlareSolverr is not configured"
            )

        if self.session_id is None:
            requested_session_id = str(uuid.uuid4())
            self.session_id = flaresolverr_create_session(
                self.shared_state, requested_session_id
            )
            if not self.session_id:
                raise requests.RequestException(
                    "Could not create FlareSolverr session for Cloudflare bypass"
                )

        # Browser solves can take longer than a plain search request. Keep the
        # caller's HTTP budget for the initial request, but never give an
        # actual FlareSolverr solve less than the existing session budget.
        solver_timeout = max(timeout, SESSION_REQUEST_TIMEOUT_SECONDS)
        # Only forward the flaresolverr-next JS extras when a caller asked for them,
        # so existing call sites (and their test doubles) keep the original signature.
        extra_js = {}
        if document_start_js:
            extra_js["document_start_js"] = document_start_js
        if execute_js:
            extra_js["execute_js"] = execute_js
        response = flaresolverr_get(
            self.shared_state,
            url,
            timeout=solver_timeout,
            session_id=self.session_id,
            **extra_js,
        )
        if (
            response is None
            or response.status_code == 403
            or is_cloudflare_challenge(response.text)
        ):
            raise requests.RequestException(
                "Could not bypass Cloudflare protection with FlareSolverr"
            )
        if user_agent := self.shared_state.values.get("user_agent"):
            headers["User-Agent"] = user_agent
        return response

    def close(self):
        if self.session_id is None:
            return
        try:
            flaresolverr_destroy_session(self.shared_state, self.session_id)
        finally:
            self.session_id = None


def update_session_via_flaresolverr(
    info,
    shared_state,
    sess,
    target_url: str,
    timeout: int | None = None,
):
    if timeout is None:
        timeout = DOWNLOAD_REQUEST_TIMEOUT_SECONDS

    # Check if FlareSolverr is available
    if not is_flaresolverr_available(shared_state):
        info("FlareSolverr is not configured. Cannot bypass Cloudflare protection.")
        return False

    flaresolverr_url = shared_state.values["config"]("FlareSolverr").get("url")
    if not flaresolverr_url:
        info("Cannot proceed without FlareSolverr. Please configure it in the web UI!")
        return False

    fs_payload = {
        "cmd": "request.get",
        "url": target_url,
        "maxTimeout": timeout * 1000,
    }

    # Send the JSON request to FlareSolverr
    fs_headers = {"Content-Type": "application/json"}
    try:
        # Eigener Solve, also eigener Browser — muss durch dieselbe Drossel.
        with solve_slot(timeout):
            resp = requests.post(
                flaresolverr_url,
                headers=fs_headers,
                json=fs_payload,
                timeout=timeout + 10,
            )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        info(f"Could not reach FlareSolverr: {e}")
        return {
            "status_code": None,
            "headers": {},
            "json": None,
            "text": "",
            "cookies": [],
            "error": f"FlareSolverr request failed: {e}",
        }
    except Exception as e:
        raise RuntimeError(f"Could not reach FlareSolverr: {e}") from e

    fs_json = resp.json()
    if fs_json.get("status") != "ok" or "solution" not in fs_json:
        raise RuntimeError(
            f"FlareSolverr did not return a valid solution: {fs_json.get('message', '<no message>')}"
        )

    solution = fs_json["solution"]

    # Replace our requests.Session cookies with whatever FlareSolverr solved
    sess.cookies.clear()
    for ck in solution.get("cookies", []):
        sess.cookies.set(
            ck.get("name"),
            ck.get("value"),
            domain=ck.get("domain"),
            path=ck.get("path", "/"),
        )
    return {"session": sess, "user_agent": solution.get("userAgent", None)}


def ensure_session_cf_bypassed(
    info,
    shared_state,
    session,
    url,
    headers,
    timeout: int | None = None,
):
    """
    Performs a GET and, if Cloudflare challenge or 403 is present, tries FlareSolverr.
    Returns tuple: (session, headers, response) or (None, None, None) on failure.
    """
    if timeout is None:
        timeout = DOWNLOAD_REQUEST_TIMEOUT_SECONDS

    try:
        resp = session.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        info(f"Initial GET failed: {e}")
        return None, None, None

    # If page is protected, try FlareSolverr
    if resp.status_code == 403 or is_cloudflare_challenge(resp.text):
        # Check if FlareSolverr is available before attempting bypass
        if not is_flaresolverr_available(shared_state):
            info(
                "Cloudflare protection detected but FlareSolverr is not configured. "
                "Please configure FlareSolverr in the web UI to access this site."
            )
            return None, None, None

        debug(
            "Encountered Cloudflare protection. Solving challenge with FlareSolverr..."
        )
        flaresolverr_result = update_session_via_flaresolverr(
            info, shared_state, session, url, timeout=timeout
        )
        if not flaresolverr_result:
            info("FlareSolverr did not return a result.")
            return None, None, None

        # update session and possibly user-agent
        session = flaresolverr_result.get("session", session)
        user_agent = flaresolverr_result.get("user_agent")
        if user_agent and user_agent != shared_state.values.get("user_agent"):
            info("Updating User-Agent from FlareSolverr solution: " + user_agent)
            shared_state.update("user_agent", user_agent)
            headers = {"User-Agent": shared_state.values["user_agent"]}

        # re-fetch using the new session/headers
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            info(f"GET after FlareSolverr failed: {e}")
            return None, None, None

        if resp.status_code == 403 or is_cloudflare_challenge(resp.text):
            info("Could not bypass Cloudflare protection with FlareSolverr!")
            return None, None, None

    return session, headers, resp


class FlareSolverrResponse:
    """
    Minimal Response-like object so it behaves like requests.Response.
    """

    def __init__(self, url, status_code, headers, text, execute_js_result=None):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or ""
        self.content = self.text.encode("utf-8")
        # flaresolverr-next: result string of an optional executeJs snippet, or None
        # when none was requested / the FlareSolverr build does not support it.
        self.execute_js_result = execute_js_result

        # Cloudflare cookies are irrelevant here, but keep attribute for compatibility
        self.cookies = requests.cookies.RequestsCookieJar()

    def raise_for_status(self):
        if 400 <= self.status_code:
            raise requests.HTTPError(f"{self.status_code} Error at {self.url}")

    def json(self):
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            pre = BeautifulSoup(self.text, "html.parser").find("pre")
            if pre is None:
                raise
            return json.loads(pre.get_text())


def flaresolverr_get(
    shared_state,
    url,
    timeout=None,
    session_id=None,
    document_start_js=None,
    execute_js=None,
):
    """
    Core function for performing a GET request via FlareSolverr only.
    Used internally by FlareSolverrSession.get()

    ``document_start_js`` / ``execute_js`` are flaresolverr-next extras: JS run at
    document start on every (redirect) document, and JS run once on the solved page
    whose string result is returned as ``FlareSolverrResponse.execute_js_result``.
    FlareSolverr builds without support simply ignore them.

    Returns None if FlareSolverr is not available.
    """
    # Check if FlareSolverr is available
    if not is_flaresolverr_available(shared_state):
        raise RuntimeError(
            "FlareSolverr is not configured. Please configure it in the web UI."
        )

    if timeout is None:
        timeout = DOWNLOAD_REQUEST_TIMEOUT_SECONDS

    flaresolverr_url = shared_state.values["config"]("FlareSolverr").get("url")
    if not flaresolverr_url:
        raise RuntimeError("FlareSolverr URL not configured in shared_state.")

    payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}
    if session_id:
        payload["session"] = session_id
    if document_start_js:
        payload["documentStartJs"] = document_start_js
    if execute_js:
        payload["executeJs"] = execute_js

    try:
        # Maja fork: one of a bounded number of concurrent solves. Held only for
        # the request itself — a retained session keeps an idle browser between
        # solves, which is cheap; the 320-380 MB peak happens here, while the
        # page loads and renders.
        with solve_slot(timeout):
            resp = requests.post(
                flaresolverr_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout + 10,
            )
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Error communicating with FlareSolverr: {e}") from e

    data = resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr returned error: {data.get('message')}")

    solution = data.get("solution", {})
    html = solution.get("response", "")
    status_code = solution.get("status", 200)
    url = solution.get("url", url)

    # headers → convert list-of-keyvals to dict
    fs_headers = {h["name"]: h["value"] for h in solution.get("headers", [])}

    # Update global UA if provided
    user_agent = solution.get("userAgent")
    if user_agent and user_agent != shared_state.values.get("user_agent"):
        shared_state.update("user_agent", user_agent)

    return FlareSolverrResponse(
        url=url,
        status_code=status_code,
        headers=fs_headers,
        text=html,
        execute_js_result=solution.get("executeJsResult"),
    )


def flaresolverr_post(
    shared_state,
    url,
    data=None,
    headers=None,
    timeout=None,
    session_id=None,
):
    """
    Core function for performing a POST request via FlareSolverr only.
    """
    if not is_flaresolverr_available(shared_state):
        raise RuntimeError(
            "FlareSolverr is not configured. Please configure it in the web UI."
        )

    if timeout is None:
        timeout = DOWNLOAD_REQUEST_TIMEOUT_SECONDS

    flaresolverr_url = shared_state.values["config"]("FlareSolverr").get("url")
    if not flaresolverr_url:
        raise RuntimeError("FlareSolverr URL not configured in shared_state.")

    if isinstance(data, dict):
        post_data = urllib.parse.urlencode(data)
    else:
        post_data = data or ""

    payload = {
        "cmd": "request.post",
        "url": url,
        "postData": post_data,
        "maxTimeout": timeout * 1000,
    }
    if session_id:
        payload["session"] = session_id

    if headers:
        payload["headers"] = headers

    try:
        # Maja fork: one of a bounded number of concurrent solves. Held only for
        # the request itself — a retained session keeps an idle browser between
        # solves, which is cheap; the 320-380 MB peak happens here, while the
        # page loads and renders.
        with solve_slot(timeout):
            resp = requests.post(
                flaresolverr_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout + 10,
            )
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Error communicating with FlareSolverr: {e}") from e

    data = resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr returned error: {data.get('message')}")

    solution = data.get("solution", {})
    html = solution.get("response", "")
    status_code = solution.get("status", 200)
    url = solution.get("url", url)

    fs_headers = {h["name"]: h["value"] for h in solution.get("headers", [])}

    user_agent = solution.get("userAgent")
    if user_agent and user_agent != shared_state.values.get("user_agent"):
        shared_state.update("user_agent", user_agent)

    return FlareSolverrResponse(
        url=url, status_code=status_code, headers=fs_headers, text=html
    )


def flaresolverr_create_session(shared_state, session_id=None):
    if not is_flaresolverr_available(shared_state):
        return None

    flaresolverr_url = shared_state.values["config"]("FlareSolverr").get("url")
    payload = {"cmd": "sessions.create"}
    if session_id:
        payload["session"] = session_id

    try:
        # sessions.create startet sofort einen Browser, nicht erst der erste
        # Solve darin — also ebenfalls durch die Drossel. destroy bleibt bewusst
        # ungedeckelt: Aufraeumen darf nie an einer vollen Drossel haengen.
        with solve_slot(SESSION_REQUEST_TIMEOUT_SECONDS):
            resp = requests.post(
                flaresolverr_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=SESSION_REQUEST_TIMEOUT_SECONDS,
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "ok":
            return data.get("session")
    except Exception:
        pass

    # The create request failed from our point of view — but FlareSolverr may
    # still be busy starting the browser and will register the session once it
    # finishes (its SessionsStorage has no TTL, so nobody would ever clean it
    # up). Since we chose the session id, we can destroy the potential orphan;
    # destroy is a no-op on the server if the session never materialized.
    if session_id:
        _schedule_orphan_session_cleanup(shared_state, session_id)
    return None


def _schedule_orphan_session_cleanup(shared_state, session_id, delays=(0, 60, 240)):
    """Fire-and-forget destroys for a session whose create call failed client-side.

    The server-side browser start can outlive our request timeout by minutes, so a
    single immediate destroy would race the late registration — retry on a schedule
    that covers slow starts.
    """

    def _run():
        for delay in delays:
            if delay:
                time.sleep(delay)
            flaresolverr_destroy_session(shared_state, session_id, attempts=1)

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"fs-orphan-cleanup-{session_id[:8]}",
    ).start()


def flaresolverr_destroy_session(shared_state, session_id, attempts=2):
    if not is_flaresolverr_available(shared_state):
        return False

    flaresolverr_url = shared_state.values["config"]("FlareSolverr").get("url")
    payload = {"cmd": "sessions.destroy", "session": session_id}

    last_error = None
    for attempt in range(attempts):
        try:
            requests.post(
                flaresolverr_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                # A busy FlareSolverr needs longer to answer the destroy than the
                # create default — give retries more headroom instead of leaking.
                timeout=SESSION_REQUEST_TIMEOUT_SECONDS * (attempt + 1),
            )
            return True
        except Exception as e:
            last_error = e
    info(
        f"Could not destroy FlareSolverr session {session_id} "
        f"after {attempts} attempt(s): {last_error}"
    )
    return False
