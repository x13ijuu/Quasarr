# -*- coding: utf-8 -*-

import time
import unittest
from unittest.mock import ANY, MagicMock, patch

from quasarr.downloads.sources.ff import Source as FfDownloadSource
from quasarr.providers.cloudflare import FlareSolverrResponse, LazyFlareSolverrSession
from quasarr.search.sources.ff import Source as FfSearchSource
from quasarr.search.sources.sf import Source as SfSearchSource


class FakeResponse:
    def __init__(self, url, text="", status_code=200, headers=None, json_data=None):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self._json_data = json_data

    def json(self):
        return self._json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"{self.status_code} error for url: {self.url}")


def _build_shared_state(hostnames):
    shared_state = MagicMock()
    stored = {}

    def _config(section):
        if section == "Hostnames":
            return hostnames
        return {}

    def _update(key, value):
        stored[key] = value

    shared_state.values = {
        "config": _config,
        "user_agent": "UnitTestAgent/1.0",
        "internal_address": "http://localhost:1234",
    }
    shared_state.update = _update
    return shared_state


class FfSourceTests(unittest.TestCase):
    def test_feed_cross_references_movie_api(self):
        host = "host-ff.invalid"
        feed_url = f"https://{host}/updates/2026-06-24#list"
        empty_feed_url = f"https://{host}/updates/2026-06-23#list"
        movie_url = f"https://{host}/example-movie"
        api_url = f"https://{host}/api/v1/token123?filter="
        release_url = f"{movie_url}/Example.Movie.2026.1080p.WEB-GROUP"
        requested_urls = []

        feed_html = """
        <div class="sra">
          <span class="lsf-icon timed">10:15</span>
          <a href="/example-movie"></a>
          <h2>Example Movie<i>(2026)</i><span>
            <a href="/example-movie/Example.Movie.2026.1080p.WEB-GROUP">
              Example.Movie.2026.1080p.WEB-GROUP
            </a>
          </span></h2>
        </div>
        """
        movie_html = """
        <a href="https://www.imdb.com/title/tt1234567/">IMDB</a>
        <script>initMovie('token123', '', '', '', '', '');</script>
        """
        api_html = """
        <div class="entry">
          <span class="morespec">Example.Movie.2026.1080p.WEB-GROUP</span>
          <span class="audiotag"><small>Größe:</small> 1.5 GB</span>
        </div>
        """

        def fake_get(url, headers=None, timeout=None):
            requested_urls.append(url)
            if url == feed_url:
                return FakeResponse(url, text=feed_html)
            if url == empty_feed_url:
                return FakeResponse(url, text="")
            if url == movie_url:
                return FakeResponse(url, text=movie_html)
            if url == api_url:
                return FakeResponse(url, json_data={"html": api_html})
            raise AssertionError(f"Unexpected URL requested: {url}")

        with (
            patch("quasarr.search.sources.ff.requests.get", side_effect=fake_get),
            patch(
                "quasarr.search.sources.ff.generate_download_link",
                side_effect=lambda *args: f"download://{args[1]}",
            ),
            patch("quasarr.search.sources.ff.clear_hostname_issue"),
            patch("quasarr.search.sources.ff.datetime") as fake_datetime,
        ):
            fake_datetime.now.return_value.strftime.return_value = "2026-06-24"
            fake_datetime.now.return_value = __import__("datetime").datetime(
                2026, 6, 24, 12, 0, 0
            )
            fake_datetime.strptime = __import__("datetime").datetime.strptime
            result = FfSearchSource().feed(
                _build_shared_state({"ff": host}), time.time(), 2000
            )

        self.assertEqual([feed_url, movie_url, api_url, empty_feed_url], requested_urls)
        self.assertEqual(1, len(result))
        details = result[0]["details"]
        self.assertEqual("Example.Movie.2026.1080p.WEB-GROUP", details["title"])
        self.assertEqual("tt1234567", details["imdb_id"])
        self.assertEqual(release_url, details["source"])
        self.assertEqual(1536 * 1024 * 1024, details["size"])

    def test_feed_stops_cross_reference_when_timeout_budget_expires(self):
        host = "host-ff.invalid"
        feed_url = f"https://{host}/updates/2026-06-24#list"
        requested_urls = []
        feed_html = """
        <div class="sra">
          <span class="lsf-icon timed">10:15</span>
          <a href="/example-movie"></a>
          <h2>Example Movie<i>(2026)</i><span>
            <a href="/example-movie/Example.Movie.2026.1080p.WEB-GROUP">
              Example.Movie.2026.1080p.WEB-GROUP
            </a>
          </span></h2>
        </div>
        """

        def fake_get(url, headers=None, timeout=None):
            requested_urls.append(url)
            if url == feed_url:
                return FakeResponse(url, text=feed_html)
            raise AssertionError(f"Unexpected URL requested: {url}")

        with (
            patch("quasarr.search.sources.ff.requests.get", side_effect=fake_get),
            patch("quasarr.search.sources.ff.FEED_REQUEST_TIMEOUT_SECONDS", 1),
            patch(
                "quasarr.search.sources.ff.time.time",
                side_effect=[0, 0, 1.1, 1.1, 1.1],
            ),
            patch("quasarr.search.sources.ff.datetime") as fake_datetime,
        ):
            fake_datetime.now.return_value.strftime.return_value = "2026-06-24"
            fake_datetime.now.return_value = __import__("datetime").datetime(
                2026, 6, 24, 12, 0, 0
            )
            result = FfSearchSource().feed(_build_shared_state({"ff": host}), 0, 2000)

        self.assertEqual([feed_url], requested_urls)
        self.assertEqual([], result)

    def test_download_resolves_external_without_requesting_final_destination(self):
        host = "host-ff.invalid"
        external_url = f"https://{host}/external/abc123"
        direct_url = "https://hoster.invalid/file/example"
        requested_urls = []

        class FakeSession:
            def get(self, url, allow_redirects=False, timeout=None, headers=None):
                requested_urls.append((url, allow_redirects))
                if url == external_url:
                    return FakeResponse(
                        url,
                        status_code=302,
                        headers={"Location": direct_url},
                    )
                raise AssertionError(f"Unexpected URL requested: {url}")

        with (
            patch(
                "quasarr.downloads.sources.ff.requests.Session",
                return_value=FakeSession(),
            ),
            patch(
                "quasarr.downloads.sources.ff.detect_crypter_type", return_value=None
            ),
        ):
            result = FfDownloadSource().get_download_links(
                _build_shared_state({"ff": host}),
                external_url,
                [],
                "Example.Movie.2026.1080p.WEB-GROUP",
                None,
            )

        self.assertEqual(
            {"links": [[direct_url, "hoster"]], "imdb_id": None},
            result,
        )
        self.assertEqual([(external_url, False)], requested_urls)


class FfSfCloudflareTests(unittest.TestCase):
    source_cases = (
        ("ff", FfSearchSource),
        ("sf", SfSearchSource),
    )

    def test_flaresolverr_response_supports_json_api_payloads(self):
        payload = '{"result": [{"url_id": "synthetic-id"}]}'
        for body in (payload, f"<html><body><pre>{payload}</pre></body></html>"):
            with self.subTest(wrapped=body.startswith("<html>")):
                response = FlareSolverrResponse(
                    "https://source.invalid/api",
                    200,
                    {"Content-Type": "application/json"},
                    body,
                )

                self.assertEqual(
                    {"result": [{"url_id": "synthetic-id"}]},
                    response.json(),
                )

    def test_search_keeps_plain_response_without_flaresolverr(self):
        for initials, source_class in self.source_cases:
            with self.subTest(source=initials):
                host = f"host-{initials}.invalid"
                response = FakeResponse(
                    f"https://{host}/api/v2/search",
                    json_data={"result": []},
                )
                with (
                    patch(
                        f"quasarr.search.sources.{initials}.requests.get",
                        return_value=response,
                    ) as plain_get,
                    patch(
                        "quasarr.providers.cloudflare.is_flaresolverr_available"
                    ) as is_available,
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_create_session"
                    ) as create_session,
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_get"
                    ) as flaresolverr_get,
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_destroy_session"
                    ) as destroy_session,
                ):
                    result = source_class().search(
                        _build_shared_state({initials: host}),
                        time.time(),
                        2000 if initials == "ff" else 5000,
                        "Synthetic Title",
                    )

                self.assertEqual([], result)
                plain_get.assert_called_once()
                is_available.assert_not_called()
                create_session.assert_not_called()
                flaresolverr_get.assert_not_called()
                destroy_session.assert_not_called()

    def test_search_retries_cloudflare_challenge_with_flaresolverr(self):
        challenge_html = """
        <html>
          <title>Just a moment...</title>
          <form id="challenge-form"></form>
        </html>
        """
        for initials, source_class in self.source_cases:
            with self.subTest(source=initials):
                host = f"host-{initials}.invalid"
                request_url = f"https://{host}/api/v2/search"
                challenged = FakeResponse(
                    request_url,
                    text=challenge_html,
                    status_code=403,
                )
                solved = FakeResponse(request_url, json_data={"result": []})
                with (
                    patch(
                        f"quasarr.search.sources.{initials}.requests.get",
                        return_value=challenged,
                    ) as plain_get,
                    patch(
                        "quasarr.providers.cloudflare.is_flaresolverr_available",
                        return_value=True,
                    ),
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_create_session",
                        return_value="test-session",
                    ) as create_session,
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_get",
                        return_value=solved,
                    ) as flaresolverr_get,
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_destroy_session"
                    ) as destroy_session,
                ):
                    result = source_class().search(
                        _build_shared_state({initials: host}),
                        time.time(),
                        2000 if initials == "ff" else 5000,
                        "Synthetic Title",
                    )

                self.assertEqual([], result)
                plain_get.assert_called_once()
                flaresolverr_get.assert_called_once()
                create_session.assert_called_once()
                self.assertEqual(
                    "test-session", flaresolverr_get.call_args.kwargs["session_id"]
                )
                destroy_session.assert_called_once_with(ANY, "test-session")

    def test_blocked_search_without_flaresolverr_reports_normal_failure(self):
        challenge_html = "<title>Just a moment...</title>"
        for initials, source_class in self.source_cases:
            with self.subTest(source=initials):
                host = f"host-{initials}.invalid"
                challenged = FakeResponse(
                    f"https://{host}/api/v2/search",
                    text=challenge_html,
                    status_code=403,
                )
                with (
                    patch(
                        f"quasarr.search.sources.{initials}.requests.get",
                        return_value=challenged,
                    ),
                    patch(
                        "quasarr.providers.cloudflare.is_flaresolverr_available",
                        return_value=False,
                    ),
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_create_session"
                    ) as create_session,
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_get"
                    ) as flaresolverr_get,
                    patch(
                        "quasarr.providers.cloudflare.flaresolverr_destroy_session"
                    ) as destroy_session,
                    patch(
                        f"quasarr.search.sources.{initials}.mark_hostname_issue"
                    ) as mark_issue,
                ):
                    result = source_class().search(
                        _build_shared_state({initials: host}),
                        time.time(),
                        2000 if initials == "ff" else 5000,
                        "Synthetic Title",
                    )

                self.assertEqual([], result)
                mark_issue.assert_called_once()
                create_session.assert_not_called()
                flaresolverr_get.assert_not_called()
                destroy_session.assert_not_called()

    def test_lazy_session_reuses_one_flaresolverr_session(self):
        shared_state = _build_shared_state({})
        headers = {"User-Agent": "UnitTestAgent/1.0"}
        challenged = FakeResponse(
            "https://source.invalid/page",
            text="<title>Just a moment...</title>",
            status_code=403,
        )
        solved = FakeResponse("https://source.invalid/page", text="<html>ok</html>")
        plain_get = MagicMock(return_value=challenged)

        with (
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="reused-session",
            ) as create_session,
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                return_value=solved,
            ) as flaresolverr_get,
            patch(
                "quasarr.providers.cloudflare.flaresolverr_destroy_session"
            ) as destroy_session,
        ):
            session = LazyFlareSolverrSession(shared_state)
            try:
                session.get(
                    "https://source.invalid/first",
                    headers,
                    10,
                    request_get=plain_get,
                )
                session.get(
                    "https://source.invalid/second",
                    headers,
                    10,
                    request_get=plain_get,
                )
            finally:
                session.close()

        create_session.assert_called_once()
        self.assertEqual(2, flaresolverr_get.call_count)
        self.assertEqual(
            ["reused-session", "reused-session"],
            [call.kwargs["session_id"] for call in flaresolverr_get.call_args_list],
        )
        destroy_session.assert_called_once_with(shared_state, "reused-session")

    def test_download_early_return_destroys_lazy_session(self):
        host = "host-ff.invalid"
        release_url = f"https://{host}/synthetic-release"
        challenged = FakeResponse(
            release_url,
            text="<title>Just a moment...</title>",
            status_code=403,
        )
        solved = FakeResponse(release_url, text="<html>no movie token</html>")

        with (
            patch(
                "quasarr.downloads.sources.ff.requests.get",
                return_value=challenged,
            ),
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="download-session",
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                return_value=solved,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_destroy_session"
            ) as destroy_session,
        ):
            result = FfDownloadSource().get_download_links(
                _build_shared_state({"ff": host}),
                release_url,
                [],
                "Synthetic.Release.1080p-GROUP",
                None,
            )

        self.assertEqual({"links": [], "imdb_id": None}, result)
        destroy_session.assert_called_once_with(ANY, "download-session")

    def test_search_exception_destroys_lazy_session(self):
        host = "host-sf.invalid"
        challenged = FakeResponse(
            f"https://{host}/api/v2/search",
            text="<title>Just a moment...</title>",
            status_code=403,
        )

        with (
            patch(
                "quasarr.search.sources.sf.requests.get",
                return_value=challenged,
            ),
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="failed-session",
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                side_effect=RuntimeError("synthetic solver failure"),
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_destroy_session"
            ) as destroy_session,
            patch("quasarr.search.sources.sf.mark_hostname_issue"),
        ):
            result = SfSearchSource().search(
                _build_shared_state({"sf": host}),
                time.time(),
                5000,
                "Synthetic Title",
            )

        self.assertEqual([], result)
        destroy_session.assert_called_once_with(ANY, "failed-session")


if __name__ == "__main__":
    unittest.main()
