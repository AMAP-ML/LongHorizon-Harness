"""A ``websearch`` tool for gptme, backed by a local SearXNG instance.

gptme has no built-in web-search tool (its browser tools drive a real
browser/lynx/Perplexity — see ``gptme/tools/browser.py`` — not a search
engine query). This module plugs that gap using SearXNG
(https://docs.searxng.org/), a self-hosted metasearch engine, so the
Writer's search results never leave the user's own infrastructure and no
third-party search API key is required.

This file is loaded by gptme itself, by path, via
``init_tools(["...", str(SEARXNG_TOOL_PATH)])`` ->
``gptme.tools.base.load_from_file`` — never imported as part of the
``lh_harness`` package (that loader uses
``importlib.util.spec_from_file_location`` on the raw path, independent of
any package context). Keep this module stdlib-only and self-contained for
exactly that reason: it runs however gptme's tool loader chooses to load
it, not through the harness's own import machinery.

Configuration is a single env var, ``LH_HARNESS_SEARXNG_URL`` (defaults to
``http://localhost:8080``, SearXNG's own default Docker port) — set in the
project's ``.env`` alongside every other environment-sourced setting (see
``provider_config.py``); this is not harness "provider" config, so it does
not go in ``provider.json``.

Requires SearXNG's JSON output format to be enabled (it is off by default):
``search.formats`` must include ``json`` in the instance's ``settings.yml``.
See the README's SearXNG setup section for the Docker + settings walkthrough.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any

SEARXNG_TOOL_PATH = Path(__file__).resolve()

DEFAULT_SEARXNG_URL = "http://localhost:8080"
DEFAULT_NUM_RESULTS = 5
MAX_NUM_RESULTS = 10
REQUEST_TIMEOUT_SECONDS = 15


def searxng_base_url() -> str:
    """The configured SearXNG instance, env-overridable like every other
    external endpoint this harness talks to."""
    return os.getenv("LH_HARNESS_SEARXNG_URL", DEFAULT_SEARXNG_URL).rstrip("/")


class SearxngSearchError(RuntimeError):
    pass


def search(query: str, *, base_url: str | None = None, num_results: int = DEFAULT_NUM_RESULTS) -> dict[str, Any]:
    """Query SearXNG's JSON API (``GET {base_url}/search?q=...&format=json``).

    Raises ``SearxngSearchError`` with a message aimed at a human debugging
    a local Docker setup (connection refused, JSON format disabled, etc.)
    rather than a raw ``URLError``/``HTTPError`` traceback.
    """
    url = base_url or searxng_base_url()
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    full_url = f"{url}/search?{params}"
    try:
        with urllib.request.urlopen(full_url, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise SearxngSearchError(
            f"SearXNG at {url} returned HTTP {exc.code} for a search request. "
            f"If this is 403, JSON output is likely disabled — add `json` to "
            f"`search.formats` in SearXNG's settings.yml and restart it. "
            f"Response body: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SearxngSearchError(
            f"Could not reach SearXNG at {url} ({exc.reason}). Is the "
            f"container running (`docker ps`)? Set LH_HARNESS_SEARXNG_URL "
            f"in .env if it's not on the default port."
        ) from exc
    except TimeoutError as exc:
        raise SearxngSearchError(f"SearXNG at {url} did not respond within {REQUEST_TIMEOUT_SECONDS}s") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SearxngSearchError(
            f"SearXNG at {url} did not return valid JSON — is `json` enabled "
            f"in `search.formats` in its settings.yml? Raw response starts: "
            f"{raw[:200]!r}"
        ) from exc

    data["results"] = (data.get("results") or [])[: max(1, min(num_results, MAX_NUM_RESULTS))]
    return data


def _format_results(query: str, data: dict[str, Any]) -> str:
    lines = [f"Search results for: {query}"]
    for answer in data.get("answers") or []:
        lines.append(f"\nAnswer: {answer}")
    results = data.get("results") or []
    if not results:
        lines.append("\n(no results)")
        return "\n".join(lines)
    for i, r in enumerate(results, start=1):
        title = r.get("title") or "(untitled)"
        link = r.get("url") or ""
        snippet = (r.get("content") or "").strip()
        lines.append(f"\n{i}. {title}\n   {link}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


def execute_websearch(
    code: str | None,
    args: list[str] | None,
    kwargs: dict[str, str] | None,
) -> Generator["Message", None, None]:
    from gptme.message import Message  # local import: only needed inside gptme's process

    query = ""
    if kwargs and kwargs.get("query"):
        query = kwargs["query"]
    elif args:
        query = " ".join(args)
    elif code and code.strip():
        query = code.strip()

    if not query:
        yield Message("system", "No search query provided")
        return

    num_results = DEFAULT_NUM_RESULTS
    if kwargs and kwargs.get("num_results"):
        try:
            num_results = int(kwargs["num_results"])
        except ValueError:
            pass

    try:
        data = search(query, num_results=num_results)
    except SearxngSearchError as exc:
        yield Message("system", f"websearch failed: {exc}")
        return

    yield Message("system", _format_results(query, data))


def examples(tool_format: str) -> str:
    from gptme.tools.base import ToolUse

    return f"""
> User: what's the latest stable version of Python?
> Assistant:
{ToolUse("websearch", [], "latest stable Python version").to_output(tool_format)}
> System: Search results for: latest stable Python version
>
> 1. Python Releases for macOS
>    https://www.python.org/downloads/
>    ...
""".strip()


def _build_tool():
    from gptme.tools.base import Parameter, ToolSpec

    return ToolSpec(
        name="websearch",
        desc="Search the web via a local SearXNG instance",
        instructions=(
            "Search the web through a self-hosted SearXNG metasearch instance. "
            "Put the query in the code block (or pass `query`). Returns titles, "
            "URLs, and snippets for the top results — fetch a URL with a "
            "reading tool if you need the full page content."
        ),
        instructions_format={
            "markdown": "Use a code block tagged `websearch` containing the query text.",
        },
        examples=examples,
        execute=execute_websearch,
        block_types=["websearch"],
        parameters=[
            Parameter(name="query", type="string", description="The search query", required=False),
            Parameter(
                name="num_results",
                type="integer",
                description=f"Max results to return (default {DEFAULT_NUM_RESULTS}, capped at {MAX_NUM_RESULTS})",
                required=False,
            ),
        ],
        available=True,
    )


try:
    # Only succeeds inside the gptme worker process, where gptme is
    # guaranteed importable (see module docstring). Guarded so the rest of
    # this module — search(), _format_results(), searxng_base_url() — stays
    # importable and unit-testable from the core test suite, which per
    # CLAUDE.md must stay gptme-free.
    tool = _build_tool()
except ImportError:
    tool = None
