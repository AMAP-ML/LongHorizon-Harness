"""Adapter factories for the pipeline (PLAN.md §11 control surface).

One module deciding which real agent backend a run's Writers talk to, so
the driver never constructs an adapter itself. The mapping is deliberately
tiny — this harness has exactly one backend:

- ``gptme`` — ``GptmeAdapter``: no agent CLI at all (drives gptme's tool
  loop against the user's configured OpenAI-compatible provider, see
  ``..provider_config``). This is the only Writer backend.

Research queries (v4) are refused loudly for now: gptme has no built-in
web search, and the WebSearch/WebFetch tools that used to serve them were
claude_code built-ins. Any existing research_plan therefore raises at
dispatch time instead of silently degrading to full tool access.

The workspace is always the run directory itself: the writer sees
``source.txt``, ``contract.md``, ``out/``, ``scratch/``, and its own
artifacts — the entire corpus a leaf needs — with every other run directory
path already out of its sight by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter
from ..adapters.gptme_adapter import GptmeAdapter
from ..v1.tree import TaskNode
from ..v4.research import ResearchQuery

WRITER_BACKENDS = ("gptme",)
_RESEARCH_CAPABLE: tuple[str, ...] = ()


def build_writer_adapter(
    backend: str,
    *,
    workspace_path: str | Path,
    prompt_dir: str | Path,
    node: TaskNode | None = None,
    model: str | None = None,
    mcp_config: str | None = None,
) -> AgentAdapter:
    """A Writer adapter for one node. ``node.tools`` narrows the tool set
    (the adapter's ``tool_allowlist``) the same way v1's round loop does."""
    workspace = str(workspace_path)
    prompts = str(prompt_dir)
    if backend == "gptme":
        kwargs: dict[str, Any] = dict(
            model=model, workspace_path=workspace, prompt_dir=prompts
        )
        if node and node.tools:
            kwargs["tool_allowlist"] = tuple(node.tools)
        return GptmeAdapter(**kwargs)
    raise ValueError(f"unknown backend: {backend!r}")


def build_research_adapter(
    backend: str,
    *,
    workspace_path: str | Path,
    prompt_dir: str | Path,
    query: ResearchQuery,
    model: str | None = None,
) -> AgentAdapter:
    """Adapter for one v4 research query.

    No backend can serve research queries in this gptme-only harness —
    gptme has no web search tool, and the WebSearch/WebFetch built-ins
    were claude_code-only. Raise instead of silently giving a query full
    tool access: a research_plan that can't be honored should fail the run
    loudly rather than degrade it.
    """
    if backend not in _RESEARCH_CAPABLE:
        raise ValueError(
            f"research queries need a backend with a built-in web search "
            f"(WebSearch/WebFetch); backend {backend!r} cannot serve them. "
            "Remove the research_plan or implement a search tool for gptme."
        )
    raise ValueError(f"unknown research backend: {backend!r}")


def parse_research_plan(raw: Any) -> dict[str, list[ResearchQuery]]:
    """Turn the web/CLI's loose ``research_plan`` JSON into v4's typed plan.

    Accepted shapes: a list of ``{node_id, slug, kind, question}`` objects,
    or a dict mapping ``node_id -> [same objects]`` (without node_id). Kind
    defaults to ``"web_search"``.
    """
    if not raw:
        return {}
    items: list[dict[str, Any]]
    if isinstance(raw, dict):
        items = []
        for node_id, queries in raw.items():
            if not isinstance(queries, list):
                continue
            for query in queries:
                if not isinstance(query, dict):
                    continue
                items.append({"node_id": node_id, **query})
    elif isinstance(raw, list):

        items = [item for item in raw if isinstance(item, dict)]
    else:
        raise ValueError("research_plan must be a list or dict")

    plan: dict[str, list[ResearchQuery]] = {}
    for item in items:
        node_id = str(item.get("node_id") or "")
        if not node_id:
            continue
        kind = str(item.get("kind") or "web_search")
        if kind not in ("web_search", "doc_retrieval"):
            raise ValueError(f"unknown research kind: {kind!r}")
        query = ResearchQuery(
            slug=str(item.get("slug") or f"q{len(plan.get(node_id, [])) + 1}"),
            kind=kind,  # type: ignore[arg-type]
            question=str(item.get("question") or ""),
        )
        plan.setdefault(node_id, []).append(query)
    return plan