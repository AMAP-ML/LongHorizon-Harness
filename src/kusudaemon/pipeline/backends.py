"""Adapter factories for the pipeline (PLAN.md §11 control surface).

One module deciding which real agent backend a run's Writers talk to, so
the driver never constructs an adapter itself. The mapping is deliberately
tiny — this harness has exactly one backend:

- ``gptme`` — ``GptmeAdapter``: no agent CLI at all (drives gptme's tool
  loop against the user's configured OpenAI-compatible provider, see
  ``..provider_config``). This is the only Writer backend.

Research queries (v4): ``web_search`` is served by a ``GptmeAdapter``
scoped to exactly one tool — ``adapters/tools/searxng_search.py``, a
self-hosted SearXNG metasearch query, loaded via gptme's own
file-path-allowlist mechanism (see that module's docstring). ``doc_retrieval``
(Context7) has no gptme equivalent wired up yet — it needed Claude Code's
MCP integration, which was removed along with that adapter — so it still
raises loudly rather than silently degrading.

**Every Writer also gets the same ``websearch`` tool directly** (added
2026-08-09, superseding v4's original "only an isolated research episode
may search" rule): a Writer can now call it mid-episode, at will, without
a caller having pre-planned a ``research_plan`` entry for that node. The
isolated v4 research phase (``v4/research_loop.py``) still exists
alongside this and is still useful — it *guarantees* a specific question
gets answered and capped to 300 tokens *before* the writer episode even
starts, which "the writer happened to think to search" doesn't — but it's
no longer the only way a node can reach the web. The tradeoff v4's design
note flagged (PLAN.md §8: raw search results are expensive, uncapped
context) is now the Writer's own token budget to manage
(``node.budget.tokens``, default 24k) rather than something the harness
prevents structurally.

The workspace is always the run directory itself: the writer sees
``source.txt``, ``contract.md``, ``out/``, ``scratch/``, and its own
artifacts — the entire corpus a leaf needs — with every other run directory
path already out of its sight by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter
from ..adapters.gptme_adapter import DEFAULT_TOOL_ALLOWLIST, GptmeAdapter
from ..v1.tree import TaskNode
from ..v4.mcp_research import allowed_tools_for
from ..v4.research import ResearchQuery

WRITER_BACKENDS = ("gptme",)
_RESEARCH_CAPABLE: tuple[str, ...] = ("gptme",)

# PLAN-zeromem.md §11.1: the run's own bookkeeping, off limits to every
# Writer's prompt. Each entry minus the node's own paths — its artifact and
# its scratch dir, both of which it is *supposed* to touch (writing
# out/<node>.md is the entire point of the episode).
_HIDDEN_RUN_PATHS: tuple[str, ...] = ("events.jsonl", "approvals.jsonl", "audit/", "scratch/", "out/")


def _hidden_paths_for(node: TaskNode) -> tuple[str, ...]:
    """§11.8: prefix-match, not exact-match. The previous filter compared
    each hidden entry against ``(out/<node>.md, scratch/<node>)`` exactly —
    but the entries are ``"out/"`` and ``"scratch/"``, so nothing was ever
    removed and every Writer was told to stay out of the directory it must
    write (its artifact and its promotion file). An entry is dropped when
    the node's own path equals it or lives beneath it."""
    own_paths = (f"out/{node.id}.md", f"scratch/{node.id}")
    return tuple(
        path
        for path in _HIDDEN_RUN_PATHS
        if not any(own.startswith(path) or path.startswith(own) for own in own_paths)
    )


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
    (the adapter's ``tool_allowlist``) the same way v1's round loop does —
    web search is layered on top of that narrowed (or default) set
    unconditionally, so even a node scoped down via ``node.tools`` keeps
    search access; only ``node.tools`` itself can narrow shell/read/save/
    patch."""
    workspace = str(workspace_path)
    prompts = str(prompt_dir)
    if backend == "gptme":
        kwargs: dict[str, Any] = dict(
            model=model, workspace_path=workspace, prompt_dir=prompts
        )
        base_tools = tuple(node.tools) if node and node.tools else DEFAULT_TOOL_ALLOWLIST
        web_search_tools = allowed_tools_for("web_search")
        kwargs["tool_allowlist"] = base_tools + tuple(
            tool for tool in web_search_tools if tool not in base_tools
        )
        if node is not None:
            # PLAN-zeromem.md §5.2c: node.budget.tokens (set by the planner,
            # validated by leaf_gate) becomes the episode's OpenAI-compatible
            # context length instead of the adapter's own default.
            kwargs["context_length"] = node.budget.tokens
            # PLAN-zeromem.md §11.1: hide the run's own bookkeeping (minus
            # the node's own artifact/scratch paths) in the episode prompt.
            kwargs["hidden_paths"] = _hidden_paths_for(node)
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

    ``web_search`` gets a ``GptmeAdapter`` narrowed to exactly the SearXNG
    tool (``v4/mcp_research.py``'s ``allowed_tools_for``) — nothing else, so
    the episode can't drift into shell/file access it has no reason to
    need. ``doc_retrieval`` still raises there: it needed Claude Code's
    Context7 MCP wiring, which no longer exists in this gptme-only harness.
    Raise instead of silently giving a query full tool access: a
    research_plan that can't be honored should fail the run loudly rather
    than degrade it.
    """
    if backend not in _RESEARCH_CAPABLE:
        raise ValueError(
            f"research queries need a backend that can serve them; "
            f"backend {backend!r} cannot. Remove the research_plan or add "
            f"support for this backend."
        )
    return GptmeAdapter(
        model=model,
        workspace_path=str(workspace_path),
        prompt_dir=str(prompt_dir),
        tool_allowlist=allowed_tools_for(query.kind),
    )


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