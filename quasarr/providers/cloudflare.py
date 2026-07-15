# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import json
import urllib.parse
import uuid

import requests
from bs4 import BeautifulSoup

from quasarr.constants import (
    DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    SESSION_REQUEST_TIMEOUT_SECONDS,
)
from quasarr.providers.log import debug
from quasarr.providers.utils import is_flaresolverr_available


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

    def get(self, url, headers, timeout, request_get=requests.get):
        response = request_get(url, headers=headers, timeout=timeout)
        if response.status_code != 403 and not is_cloudflare_challenge(response.text):
            return response

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

        debug("Encountered Cloudflare protection. Retrying with FlareSolverr...")
        response = flaresolverr_get(
            self.shared_state,
            url,
            timeout=timeout,
            session_id=self.session_id,
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
        resp = requests.post(
            flaresolverr_url, headers=fs_headers, json=fs_payload, timeout=timeout + 10
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

    def __init__(self, url, status_code, headers, text):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or ""
        self.content = self.text.encode("utf-8")

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
):
    """
    Core function for performing a GET request via FlareSolverr only.
    Used internally by FlareSolverrSession.get()

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

    try:
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
        url=url, status_code=status_code, headers=fs_headers, text=html
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
    return None


def flaresolverr_destroy_session(shared_state, session_id):
    if not is_flaresolverr_available(shared_state):
        return

    flaresolverr_url = shared_state.values["config"]("FlareSolverr").get("url")
    payload = {"cmd": "sessions.destroy", "session": session_id}

    try:
        requests.post(
            flaresolverr_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=SESSION_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception:
        pass
