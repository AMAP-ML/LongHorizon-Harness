"""Research-tool plumbing for web search / current-docs retrieval (PLAN.md
§13 v4).

Per-research-kind tool allowlists, kept separate from tool *implementations*
the same way ``v1/gates.py`` separates gate evaluation from node content:
this module just says which tools a research episode of a given kind gets.

- ``web_search`` maps to exactly one tool: ``adapters/tools/searxng_search.py``,
  a self-hosted SearXNG metasearch query. It's referenced here by file path
  (``SEARXNG_TOOL_PATH``), not by name, because gptme's own
  ``init_tools()`` loads non-built-in tools that way (see that module's
  docstring for why). This used to be Claude Code's built-in
  ``WebSearch``/``WebFetch`` tools narrowed via ``allowed_tools`` — that
  adapter no longer exists, so this harness now ships its own search tool
  instead of relying on a host CLI's built-ins.
- ``doc_retrieval`` (current, version-specific library docs) has no gptme
  equivalent wired up yet. The previous design routed it through Claude
  Code's ``--mcp-config`` and Context7 (§15.7) — that config format is
  Claude-Code-specific and gptme doesn't read it. gptme does have its own
  native MCP tool support (``gptme.tools.mcp``), so this is a real gap to
  fill later, not a dead end — but wiring it up is out of scope here.
  ``allowed_tools_for("doc_retrieval")`` raises rather than returning a
  config nothing would actually honor.
"""

from __future__ import annotations

from typing import Literal

from ..adapters.tools.searxng_search import SEARXNG_TOOL_PATH

ResearchKind = Literal["web_search", "doc_retrieval"]

RESEARCH_TOOL_ALLOWLIST: dict[ResearchKind, tuple[str, ...]] = {
    "web_search": (str(SEARXNG_TOOL_PATH),),
}


def allowed_tools_for(kind: ResearchKind) -> tuple[str, ...]:
    allowlist = RESEARCH_TOOL_ALLOWLIST.get(kind)
    if allowlist is None:
        raise ValueError(
            f"research kind {kind!r} has no tool wired up for the gptme "
            "backend yet. Remove it from the research_plan, or implement "
            "one (see this module's docstring)."
        )
    return allowlist
