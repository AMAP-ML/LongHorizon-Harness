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

In corpus mode (``kind="text"``, unchanged) the workspace is the run
directory itself: the writer sees ``source.txt``, ``contract.md``, ``out/``,
``scratch/``, and its own artifacts — the entire corpus a leaf needs — with
every other run directory path already out of its sight by construction. In
workspace mode (``kind="workspace"``, PLAN.md §A3/§B1) the workspace is the
real repo (``work.root``) instead, and the run directory — wherever it
happens to be nested, by default ``<root>/.kusudaemon/runs`` — is hidden as
one subtree rather than by individual filename (see
``_hidden_paths_and_exceptions_for``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter
from ..adapters.gptme_adapter import DEFAULT_TOOL_ALLOWLIST, GptmeAdapter
from ..v1.tree import TaskNode
from ..v4.mcp_research import allowed_tools_for
from ..v4.research import ResearchQuery, normalize_probe_kind

WRITER_BACKENDS = ("gptme",)
_RESEARCH_CAPABLE: tuple[str, ...] = ("gptme",)

# PLAN.md §D2: the run's own bookkeeping, off limits to every Writer's
# prompt — including "out/" and "scratch/" themselves, which hold every
# OTHER leaf's finished artifact and working notes. A Writer that can read
# out/ch03.md while writing ch04 produces exactly the correlated drift
# cross-agent isolation (§2 invariant 6) exists to prevent, and it's
# invisible because the artifact looks *more* coherent, not less.
_HIDDEN_RUN_PATHS: tuple[str, ...] = ("events.jsonl", "approvals.jsonl", "audit/", "scratch/", "out/")


def _hidden_paths_for(node: TaskNode) -> tuple[str, ...]:
    """The full hidden list, unfiltered. §D2 (fixing §11.8's inversion): the
    node's own carve-out is no longer expressed by dropping "out/"/"scratch/"
    from this list — that hid the whole directory from every node, always,
    since e.g. "out/a.md".startswith("out/") is trivially true for EVERY
    node's own path, not just a coincidence for one. See
    ``_hidden_path_exceptions_for`` for the actual carve-out."""
    return _HIDDEN_RUN_PATHS


def _hidden_path_exceptions_for(node: TaskNode) -> tuple[str, ...]:
    """The node's own two paths — its artifact and its scratch dir — which
    it is *supposed* to touch (writing out/<node>.md is the entire point of
    the episode). Rendered as an explicit exception alongside the hidden
    list (``cli_agent.py:_hidden_paths_notice``) rather than by removing the
    broader entry."""
    return (f"out/{node.id}.md", f"scratch/{node.id}")


def _hidden_run_dir_subtree_for_probe(run_dir: Path, workspace_root: Path) -> tuple[str, ...]:
    """PLAN.md §A6/§B4: the read-only-probe counterpart of
    ``_hidden_paths_and_exceptions_for``, without an exceptions half.
    ``workspace``/``corpus`` probes never get a write tool at all (see
    ``v4/research.py``'s module docstring — the finding is captured via
    the assistant-message fallback, same as today's web-kind probes), so
    there is no "its own artifact/scratch dir" to carve back out the way a
    Writer's does. This still has to hide the run directory's bookkeeping
    from a ``workspace``-kind probe's read/list/grep tools, or a probe
    reading "the whole repo" would also read every other node's finished
    artifact and scratch notes (§2 invariant 6) the instant the run
    directory happens to be nested inside the workspace (the default
    ``--workspace`` ``runs_root``)."""
    try:
        run_dir_resolved = run_dir.resolve()
        workspace_resolved = workspace_root.resolve()
    except OSError:
        return ()
    if run_dir_resolved == workspace_resolved:
        # Corpus-mode probes: workspace_path *is* run_dir (spine/ lives
        # there), so the per-file names apply exactly as they do for a
        # Writer. This is the "spine/ only" approximation the module
        # docstring in v4/mcp_research.py documents: everything but this
        # denylist stays readable, which includes spine/ itself.
        return _HIDDEN_RUN_PATHS
    try:
        run_dir_rel = run_dir_resolved.relative_to(workspace_resolved)
    except ValueError:
        return ()
    return (f"{run_dir_rel.as_posix()}/",)


def _hidden_paths_and_exceptions_for(
    node: TaskNode, run_dir: Path, workspace_root: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """PLAN.md §A3/§B1: in corpus mode ``workspace_path`` (the Writer's cwd)
    *is* ``run_dir``, so ``_hidden_paths_for``'s relative names ("out/",
    "scratch/", ...) already point at the right place — this branch
    reproduces that exactly, byte-for-byte, so a caller that never mentions
    workspace mode sees no behavior change.

    In workspace mode ``workspace_path`` is ``work.root``, a real repo the
    run dir merely happens to be nested inside (the default
    ``--workspace`` ``runs_root``, ``<root>/.kusudaemon/runs``). The
    corpus-mode per-file names would resolve to nonexistent paths relative
    to that cwd ("out/" isn't a directory in the repo root); worse, doing
    nothing would leave the ENTIRE run directory readable from the
    Writer's cwd — every other node's artifact, scratch notes, and audit
    verdicts, not just its own (§2 invariant 6). So this hides the run
    directory as one subtree, relative to the Writer's actual cwd, with the
    same two-path carve-out expressed the same way.

    A ``run_dir`` outside ``workspace_root`` entirely (a custom
    ``--runs-root`` that isn't nested in the workspace) needs no entry at
    all: nothing of the harness's bookkeeping sits inside the Writer's cwd
    to begin with.
    """
    if node is None:
        return (), ()
    try:
        run_dir_resolved = run_dir.resolve()
        workspace_resolved = workspace_root.resolve()
    except OSError:
        run_dir_resolved, workspace_resolved = run_dir, workspace_root
    if run_dir_resolved == workspace_resolved:
        return _hidden_paths_for(node), _hidden_path_exceptions_for(node)
    try:
        run_dir_rel = run_dir_resolved.relative_to(workspace_resolved)
    except ValueError:
        return (), ()
    hidden = (f"{run_dir_rel.as_posix()}/",)
    exceptions = (
        (run_dir_rel / "out" / f"{node.id}.md").as_posix(),
        (run_dir_rel / "scratch" / node.id).as_posix(),
    )
    return hidden, exceptions


def build_writer_adapter(
    backend: str,
    *,
    workspace_path: str | Path,
    prompt_dir: str | Path,
    node: TaskNode | None = None,
    model: str | None = None,
    mcp_config: str | None = None,
    run_dir: str | Path | None = None,
) -> AgentAdapter:
    """A Writer adapter for one node. ``node.tools`` narrows the tool set
    (the adapter's ``tool_allowlist``) the same way v1's round loop does —
    web search is layered on top of that narrowed (or default) set
    unconditionally, so even a node scoped down via ``node.tools`` keeps
    search access; only ``node.tools`` itself can narrow shell/read/save/
    patch.

    ``run_dir`` defaults to ``workspace_path`` — today's corpus-mode
    invariant, where the Writer's cwd *is* the run directory. A caller
    dispatching ``kind="workspace"`` (PLAN.md §A3) passes ``workspace_path``
    as ``work.root`` and ``run_dir`` as the actual run directory, which may
    live nested inside it; see ``_hidden_paths_and_exceptions_for``.
    """
    workspace = str(workspace_path)
    prompts = str(prompt_dir)
    run_dir_path = Path(run_dir) if run_dir is not None else Path(workspace_path)
    workspace_root_path = Path(workspace_path)
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
            # PLAN.md §D2/§A3: hide the run's own bookkeeping in the
            # episode prompt, with an explicit carve-out for the node's own
            # artifact/scratch paths (not a silent drop from the list).
            hidden, exceptions = _hidden_paths_and_exceptions_for(node, run_dir_path, workspace_root_path)
            kwargs["hidden_paths"] = hidden
            kwargs["hidden_path_exceptions"] = exceptions
        return GptmeAdapter(**kwargs)
    raise ValueError(f"unknown backend: {backend!r}")


def build_research_adapter(
    backend: str,
    *,
    workspace_path: str | Path,
    prompt_dir: str | Path,
    query: ResearchQuery,
    model: str | None = None,
    run_dir: str | Path | None = None,
) -> AgentAdapter:
    """Adapter for one v4/§B4 probe.

    ``web`` (legacy ``web_search``) gets a ``GptmeAdapter`` narrowed to
    exactly the SearXNG tool (``v4/mcp_research.py``'s ``allowed_tools_for``)
    — nothing else, so the episode can't drift into shell/file access it
    has no reason to need. ``doc_retrieval`` still raises there: it needed
    Claude Code's Context7 MCP wiring, which no longer exists in this
    gptme-only harness. Raise instead of silently giving a query full tool
    access: a research_plan that can't be honored should fail the run
    loudly rather than degrade it.

    ``workspace``/``corpus`` (PLAN.md §A6/§B4) additionally need
    ``hidden_paths`` set, the same "hide the run directory's own
    bookkeeping" guarantee ``build_writer_adapter`` already gives every
    Writer (§2 invariant 6) — a read/list/grep-capable probe pointed at a
    real repo must not incidentally read every other node's finished
    artifact just because the run directory happens to be nested inside
    the workspace it's exploring. ``run_dir`` is optional (``None`` skips
    this — today's ``web``/``doc_retrieval`` callers never pass it, since
    those kinds get no filesystem tools at all) so this stays a strict,
    additive change to the signature.
    """
    if backend not in _RESEARCH_CAPABLE:
        raise ValueError(
            f"research queries need a backend that can serve them; "
            f"backend {backend!r} cannot. Remove the research_plan or add "
            f"support for this backend."
        )
    kwargs: dict[str, Any] = dict(
        model=model,
        workspace_path=str(workspace_path),
        prompt_dir=str(prompt_dir),
        tool_allowlist=allowed_tools_for(query.kind),
    )
    kind = normalize_probe_kind(query.kind)
    if kind in ("workspace", "corpus") and run_dir is not None:
        kwargs["hidden_paths"] = _hidden_run_dir_subtree_for_probe(
            Path(run_dir), Path(workspace_path)
        )
    return GptmeAdapter(**kwargs)


# PLAN.md §B4: research_plan JSON (CLI/web) still ships the legacy spelling
# "web_search" (unchanged shipped contract); "web" is accepted directly too
# so a caller that already generalized to Probe's vocabulary isn't forced
# back to the old name. Both normalize to "web" inside Probe.__post_init__.
_VALID_PLAN_KINDS = ("web", "web_search", "workspace", "corpus", "doc_retrieval")


def parse_research_plan(raw: Any) -> dict[str, list[ResearchQuery]]:
    """Turn the web/CLI's loose ``research_plan`` JSON into v4's typed plan.

    Accepted shapes: a list of ``{node_id, slug, kind, question}`` objects,
    or a dict mapping ``node_id -> [same objects]`` (without node_id). Kind
    defaults to ``"web_search"`` (normalized to ``"web"`` by ``Probe``
    construction — see ``v4/research.py``).
    """
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid research_plan JSON: {exc}") from exc
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
        raise ValueError("research_plan must be a list, dict, or JSON string")

    plan: dict[str, list[ResearchQuery]] = {}
    for item in items:
        node_id = str(item.get("node_id") or "")
        if not node_id:
            continue
        kind = str(item.get("kind") or "web_search")
        if kind not in _VALID_PLAN_KINDS:
            raise ValueError(f"unknown research kind: {kind!r}")
        query = ResearchQuery(
            slug=str(item.get("slug") or f"q{len(plan.get(node_id, [])) + 1}"),
            kind=kind,  # type: ignore[arg-type]
            question=str(item.get("question") or ""),
        )
        plan.setdefault(node_id, []).append(query)
    return plan