"""Probe-tool plumbing (PLAN.md §13 v4; generalized to probes by §A6/§B4).

Per-probe-kind tool allowlists, kept separate from tool *implementations*
the same way ``v1/gates.py`` separates gate evaluation from node content:
this module just says which tools a probe episode of a given kind gets.

- ``web`` (legacy spelling ``web_search``, normalized by
  ``research.normalize_probe_kind``) maps to exactly one tool:
  ``adapters/tools/searxng_search.py``, a self-hosted SearXNG metasearch
  query. It's referenced here by file path (``SEARXNG_TOOL_PATH``), not by
  name, because gptme's own ``init_tools()`` loads non-built-in tools that
  way (see that module's docstring for why). This used to be Claude Code's
  built-in ``WebSearch``/``WebFetch`` tools narrowed via ``allowed_tools``
  — that adapter no longer exists, so this harness now ships its own
  search tool instead of relying on a host CLI's built-ins.
- ``workspace`` (PLAN.md §A6's table: "read + list + grep, no write, no
  shell mutation") gets gptme's built-in ``read`` tool plus
  ``adapters/tools/workspace_read.py`` — a new, stdlib-only, read-only
  list+grep tool (this harness's existing pattern for "gptme has no
  built-in tool for this," same as the SearXNG tool above; see that
  module's own docstring). No ``shell``/``save``/``patch`` in the
  allowlist at all — "no write, no shell mutation" is enforced by omission,
  the same model ``pipeline/backends.py``'s ``hidden_paths``/
  ``tool_allowlist`` already use elsewhere in this codebase: a prompt-text
  and tool-availability guarantee, not a filesystem sandbox. Nothing here
  stops a determined agent from trying to invoke an un-allowlisted tool
  name; gptme simply has nothing registered under that name to call.
- ``corpus`` ("read over spine/ only", §A6's table) gets just gptme's
  built-in ``read`` tool — a corpus probe's targets are already indexed via
  ``spine.json``/the calling node's own ``inputs``, so there is nothing to
  list or grep for. Confinement to "spine/ only" is approximated, not
  literal: ``pipeline/backends.py``'s ``build_research_adapter`` hides the
  same run-directory bookkeeping (``events.jsonl``, ``approvals.jsonl``,
  ``audit/``, ``scratch/``, ``out/``) a Writer's ``hidden_paths`` already
  hides, leaving ``spine/``, ``spine.json``, ``contract.md``, and
  ``source.txt`` all readable — a coarser boundary than "spine/ only," but
  consistent with every other confinement in this codebase being a
  denylist over run-directory bookkeeping, not a per-directory allowlist
  (which does not exist anywhere in this harness today).
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

from ..adapters.tools.searxng_search import SEARXNG_TOOL_PATH
from ..adapters.tools.workspace_read import WORKSPACE_READ_TOOL_PATH
from .research import ProbeKind, ResearchKind, normalize_probe_kind  # noqa: F401 — re-exported for callers

RESEARCH_TOOL_ALLOWLIST: dict[ProbeKind, tuple[str, ...]] = {
    "web": (str(SEARXNG_TOOL_PATH),),
    "workspace": ("read", str(WORKSPACE_READ_TOOL_PATH)),
    "corpus": ("read",),
}


def allowed_tools_for(kind: str) -> tuple[str, ...]:
    kind = normalize_probe_kind(kind)  # type: ignore[assignment]
    allowlist = RESEARCH_TOOL_ALLOWLIST.get(kind)  # type: ignore[arg-type]
    if allowlist is None:
        raise ValueError(
            f"probe kind {kind!r} has no tool wired up for the gptme "
            "backend yet. Remove it from the research_plan, or implement "
            "one (see this module's docstring)."
        )
    return allowlist
