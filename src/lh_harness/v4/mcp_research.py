"""Research-tool plumbing for web search / current-docs retrieval (PLAN.md
§13 v4, §15.2, §15.7).

§15.2 is explicit about scope here: "Do not write web search or fetch. Use
MCP servers... MCP is the seam." This module supplies that seam, not a
search implementation:

- ``web_search`` needs no MCP server at all — Claude Code already ships
  ``WebSearch``/``WebFetch`` as built-in tools, so a research node just
  needs its per-node ``allowed_tools`` narrowed to them (§6: "tools is
  per-node... the single biggest token lever"), the exact mechanism v1
  already uses for everything else (``ClaudeCodeAdapter(allowed_tools=...)``).
- ``doc_retrieval`` (current, version-specific library docs) has one
  concrete named donor in the reuse inventory (§15.7): Context7. That one
  *does* need an MCP server wired in via
  ``ClaudeCodeAdapter(mcp_config=...)``, which already exists
  (``adapters/claude_code.py``) — this module only builds the config JSON
  and the matching tool allowlist, the same way ``adapters/claude_code.py``
  itself reads ``LH_HARNESS_CLAUDECODE_MCP_CONFIG`` rather than hardcoding
  a path.

No network call happens in this module. It writes a JSON file to disk;
whether the referenced MCP server actually exists on the machine running
``claude`` is an operational concern, not a harness one — same division
adapters/claude_code.py already draws for its own ``mcp_config`` parameter.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ResearchKind = Literal["web_search", "doc_retrieval"]


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        entry: dict = {"command": self.command, "args": list(self.args)}
        if self.env:
            entry["env"] = dict(self.env)
        return entry


def context7_server() -> McpServerSpec:
    """The one concrete doc-retrieval MCP server PLAN.md §15.7 names.

    Overridable via env because the exact package/version/API key is an
    operational choice, not a harness one — mirrors how
    ``adapters/claude_code.py`` itself reads ``LH_HARNESS_CLAUDECODE_MCP_CONFIG``
    instead of hardcoding a path.
    """
    command = os.getenv("LH_HARNESS_CONTEXT7_MCP_COMMAND", "npx")
    args_raw = os.getenv("LH_HARNESS_CONTEXT7_MCP_ARGS", "-y @upstash/context7-mcp")
    env: dict[str, str] = {}
    api_key = os.getenv("CONTEXT7_API_KEY")
    if api_key:
        env["CONTEXT7_API_KEY"] = api_key
    return McpServerSpec(name="context7", command=command, args=tuple(args_raw.split()), env=env)


# web_search has no single canonical MCP server (Brave/Tavily/Exa/etc. all
# differ) — deliberately no default here, unlike context7_server() above.
# A caller that wants a *different* search backend than Claude Code's
# built-in WebSearch/WebFetch tools passes its own mcp_config the same way
# any ClaudeCodeAdapter caller already can; this module doesn't guess a
# package name for it.
RESEARCH_TOOL_ALLOWLIST: dict[ResearchKind, tuple[str, ...]] = {
    "web_search": ("WebSearch", "WebFetch", "Write"),
    "doc_retrieval": (
        "mcp__context7__resolve-library-id",
        "mcp__context7__get-library-docs",
        "Write",
    ),
}


def allowed_tools_for(kind: ResearchKind) -> tuple[str, ...]:
    return RESEARCH_TOOL_ALLOWLIST[kind]


def write_mcp_config(path: str | Path, kinds: list[ResearchKind]) -> Path | None:
    """Write an ``--mcp-config`` JSON file covering every kind in ``kinds``
    that actually needs one. Returns ``None`` (writes nothing) when none
    do — e.g. an all-``web_search`` plan needs no MCP config at all, since
    those tools are built into Claude Code."""
    servers: dict[str, dict] = {}
    if "doc_retrieval" in kinds:
        spec = context7_server()
        servers[spec.name] = spec.to_json()
    if not servers:
        return None
    config_path = Path(path)
    config_path.write_text(
        json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return config_path
