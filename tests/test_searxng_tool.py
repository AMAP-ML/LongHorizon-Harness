"""SearXNG websearch tool (adapters/tools/searxng_search.py).

Only the pure-Python surface is tested here — search()/_format_results()/
searxng_base_url() — never execute_websearch(), which imports gptme.message
internally and only ever runs inside the gptme worker subprocess. Like
every other adapter's tests, this stays gptme-free: no real gptme install,
no network. urllib.request.urlopen is monkeypatched with a fake response.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from lh_harness.adapters.tools.searxng_search import (  # noqa: E402
    DEFAULT_SEARXNG_URL,
    SearxngSearchError,
    _format_results,
    search,
    searxng_base_url,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class SearxngBaseUrlTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.pop("LH_HARNESS_SEARXNG_URL", None)

    def tearDown(self) -> None:
        os.environ.pop("LH_HARNESS_SEARXNG_URL", None)
        if self._prev is not None:
            os.environ["LH_HARNESS_SEARXNG_URL"] = self._prev

    def test_default_url(self) -> None:
        self.assertEqual(searxng_base_url(), DEFAULT_SEARXNG_URL)

    def test_env_override_strips_trailing_slash(self) -> None:
        os.environ["LH_HARNESS_SEARXNG_URL"] = "http://example.com:8080/"
        self.assertEqual(searxng_base_url(), "http://example.com:8080")


class SearchTest(unittest.TestCase):
    def _fake_data(self, n: int) -> dict:
        return {
            "query": "q",
            "results": [
                {"title": f"Title {i}", "url": f"https://example.com/{i}", "content": f"snippet {i}"}
                for i in range(n)
            ],
        }

    def test_success_parses_and_caps_results(self) -> None:
        body = json.dumps(self._fake_data(12)).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)) as m:
            data = search("python releases", base_url="http://localhost:8080", num_results=3)
        self.assertEqual(len(data["results"]), 3)
        called_url = m.call_args[0][0]
        self.assertIn("q=python+releases", called_url)
        self.assertIn("format=json", called_url)

    def test_num_results_hard_cap(self) -> None:
        body = json.dumps(self._fake_data(12)).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_FakeResponse(body)):
            data = search("q", num_results=999)
        self.assertEqual(len(data["results"]), 10)

    def test_connection_refused_raises_clear_error(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            with self.assertRaises(SearxngSearchError) as ctx:
                search("q", base_url="http://localhost:8080")
        self.assertIn("Could not reach SearXNG", str(ctx.exception))
        self.assertIn("docker ps", str(ctx.exception))

    def test_http_error_mentions_json_format(self) -> None:
        err = urllib.error.HTTPError(
            "http://localhost:8080/search", 403, "Forbidden", hdrs=None, fp=io.BytesIO(b"blocked")
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SearxngSearchError) as ctx:
                search("q", base_url="http://localhost:8080")
        self.assertIn("403", str(ctx.exception))
        self.assertIn("search.formats", str(ctx.exception))

    def test_invalid_json_raises_clear_error(self) -> None:
        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"<html>not json</html>")):
            with self.assertRaises(SearxngSearchError) as ctx:
                search("q", base_url="http://localhost:8080")
        self.assertIn("valid JSON", str(ctx.exception))


class FormatResultsTest(unittest.TestCase):
    def test_formats_title_url_snippet(self) -> None:
        text = _format_results(
            "python",
            {"results": [{"title": "Python", "url": "https://python.org", "content": "lang"}]},
        )
        self.assertIn("Python", text)
        self.assertIn("https://python.org", text)
        self.assertIn("lang", text)

    def test_no_results_message(self) -> None:
        text = _format_results("q", {"results": []})
        self.assertIn("no results", text)

    def test_answers_surfaced(self) -> None:
        text = _format_results("q", {"results": [], "answers": ["42"]})
        self.assertIn("Answer: 42", text)


if __name__ == "__main__":
    unittest.main()
