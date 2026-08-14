"""Adapter factories for the pipeline (PLAN.md §11 control surface).

One module deciding which real agent backend a run's Writers talk to, so
the driver never constructs an adapter itself. Four backends are supported:

- ``gptme`` — ``GptmeAdapter``: drives gptme's tool loop against the
  configured OpenAI-compatible provider (see ``..provider_config``).
- ``claude`` — ``ClaudeCodeAdapter``: Anthropic's ``claude --print`` CLI
  (session resume + per-node tool deny-lists via ``--disallowedTools``).
- ``codex`` — ``CodexAdapter``: OpenAI's ``codex exec`` CLI (sandbox bypass,
  config overrides).
- ``opencode`` — ``OpenCodeAdapter``: OpenCode's ``opencode run`` CLI
  (session resume + fine-grained tool permissions).

All four run their stdout through the trace translator in
``adapters/_agent_worker.py`` (``claude``/``codex``/``opencode``) or gptme's own
JSON output, so the dashboard's consumers see one vocabulary either way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter
from ..adapters.capabilities import (
    emit_capability_event,
    translate_tools_to_claude_disallowed,
    translate_tools_to_opencode_permissions,
)
from ..adapters.claude_code import ClaudeCodeAdapter
from ..adapters.codex import CodexAdapter
from ..adapters.gptme_adapter import DEFAULT_TOOL_ALLOWLIST, GptmeAdapter
from ..adapters.opencode import OpenCodeAdapter
from ..provider_config import read_backend_config
from ..v1.tree import TaskNode
from ..v4.mcp_research import allowed_tools_for
from ..v4.research import ResearchQuery, normalize_probe_kind, research_raw_finding_path

BACKENDS = WRITER_BACKENDS = ("gptme", "claude", "codex", "opencode")

# PLAN.md §D2: the run's own bookkeeping, off limits to every Writer's
# prompt — including "out/" and "scratch/" themselves, which hold every
# OTHER leaf's finished artifact and working notes. A Writer that can read
# out/ch03.md while writing ch04 produces exactly the correlated drift
# cross-agent isolation (§2 invariant 6) exists to prevent, and it's
# invisible because the artifact looks *more* coherent, not less.
_HIDDEN_RUN_PATHS: tuple[str, ...] = ("events.jsonl", "approvals.jsonl", "audit/", "scratch/", "out/")


def _hidden_paths_for(node: TaskNode) -> tuple[str, ...]:
    """The full hidden list, unfiltered."""
    return _HIDDEN_RUN_PATHS


def _hidden_path_exceptions_for(node: TaskNode) -> tuple[str, ...]:
    """The node's own two paths — its artifact and its scratch dir."""
    return (f"out/{node.id}.md", f"scratch/{node.id}")


def _hidden_run_dir_subtree_for_probe(run_dir: Path, workspace_root: Path) -> tuple[str, ...]:
    """PLAN.md §A6/§B4: the read-only-probe counterpart of
    ``_hidden_paths_and_exceptions_for``."""
    try:
        run_dir_resolved = run_dir.resolve()
        workspace_resolved = workspace_root.resolve()
    except OSError:
        return ()
    if run_dir_resolved == workspace_resolved:
        return _HIDDEN_RUN_PATHS
    try:
        run_dir_rel = run_dir_resolved.relative_to(workspace_resolved)
    except ValueError:
        return ()
    return (f"{run_dir_rel.as_posix()}/",)


def _hidden_paths_and_exceptions_for_probe(
    run_dir: Path, workspace_root: Path, raw_finding_path: Path | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Carve out the probe's own raw finding path from the hidden run directory subtree."""
    hidden = _hidden_run_dir_subtree_for_probe(run_dir, workspace_root)
    exceptions: tuple[str, ...] = ()
    if raw_finding_path is not None:
        try:
            run_dir_resolved = run_dir.resolve()
            workspace_resolved = workspace_root.resolve()
            raw_resolved = raw_finding_path.resolve()
            if run_dir_resolved == workspace_resolved:
                exceptions = (raw_resolved.relative_to(run_dir_resolved).as_posix(),)
            else:
                exceptions = (raw_resolved.relative_to(workspace_resolved).as_posix(),)
        except (ValueError, OSError):
            pass
    return hidden, exceptions


def _hidden_paths_and_exceptions_for(
    node: TaskNode, run_dir: Path, workspace_root: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
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


def hidden_paths_for_node(
    node: TaskNode, run_dir: str | Path, workspace_root: str | Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return _hidden_paths_and_exceptions_for(node, Path(run_dir), Path(workspace_root))


def build_writer_adapter(
    backend: str,
    *,
    workspace_path: str | Path,
    prompt_dir: str | Path,
    node: TaskNode | None = None,
    model: str | None = None,
    mcp_config: str | None = None,
    run_dir: str | Path | None = None,
    provider: str | None = None,
) -> AgentAdapter:
    """A Writer adapter for one node. ``node.tools`` narrows the tool set
    (the adapter's ``tool_allowlist``) the same way v1's round loop does —
    web search is layered on top of that narrowed (or default) set
    unconditionally, so even a node scoped down via ``node.tools`` keeps
    search access.

    ``provider`` names the provider.json provider the gptme writer backend
    should use (the run's own selection from the new-run modal); the
    claude/codex/opencode backends have their own auth and ignore it.
    """
    workspace = str(workspace_path)
    prompts = str(prompt_dir)
    run_dir_path = Path(run_dir) if run_dir is not None else Path(workspace_path)
    workspace_root_path = Path(workspace_path)

    settings = read_backend_config(
        backend,
        run_dir=run_dir_path if run_dir is not None else None,
        model=model,
        provider=provider,
        extra={"mcp_config": mcp_config} if mcp_config else None,
    )

    base_tools = tuple(node.tools) if node and node.tools else DEFAULT_TOOL_ALLOWLIST
    web_search_tools = allowed_tools_for("web_search")
    all_writer_tools = base_tools + tuple(
        tool for tool in web_search_tools if tool not in base_tools
    )

    hidden: tuple[str, ...] = ()
    exceptions: tuple[str, ...] = ()
    if node is not None:
        hidden, exceptions = _hidden_paths_and_exceptions_for(node, run_dir_path, workspace_root_path)

    node_id = node.id if node else "-"

    if backend == "gptme":
        kwargs: dict[str, Any] = dict(
            model=settings.model,
            api_key=settings.api_key or None,
            base_url=settings.base_url or None,
            workspace_path=workspace,
            prompt_dir=prompts,
            tool_allowlist=all_writer_tools,
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )
        if node is not None:
            kwargs["context_length"] = node.budget.tokens
        return GptmeAdapter(**kwargs)

    if backend == "claude":
        if node is not None and node.budget.tokens:
            emit_capability_event(
                run_dir,
                node_id,
                "claude",
                "context_length",
                "Claude Code does not support context window restriction",
            )
        resolved_mcp = mcp_config or (str(settings.extra.get("mcp_config")) if settings.extra.get("mcp_config") else None)
        return ClaudeCodeAdapter(
            model=settings.model,
            api_key=settings.api_key or None,
            base_url=settings.base_url or None,
            workspace_path=workspace,
            prompt_dir=prompts,
            mcp_config=resolved_mcp,
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )

    if backend == "codex":
        if node is not None and node.budget.tokens:
            emit_capability_event(
                run_dir,
                node_id,
                "codex",
                "context_length",
                "Codex does not support context window restriction",
            )
        if node and node.tools and tuple(node.tools) != DEFAULT_TOOL_ALLOWLIST:
            emit_capability_event(
                run_dir,
                node_id,
                "codex",
                "tool_allowlist",
                "Codex does not support tool allowlists",
            )
        if hidden:
            emit_capability_event(
                run_dir,
                node_id,
                "codex",
                "path_deny",
                "Codex does not support filesystem path deny rules",
            )
        resolved_mcp = mcp_config or (str(settings.extra.get("mcp_config")) if settings.extra.get("mcp_config") else None)
        wire_api = str(settings.extra.get("wire_api") or "responses")
        return CodexAdapter(
            model=settings.model,
            api_key=settings.api_key or None,
            base_url=settings.base_url or None,
            wire_api=wire_api,
            workspace_path=workspace,
            prompt_dir=prompts,
            mcp_config=resolved_mcp,
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )

    if backend == "opencode":
        if node is not None and node.budget.tokens:
            emit_capability_event(
                run_dir,
                node_id,
                "opencode",
                "context_length",
                "OpenCode does not support context window restriction",
            )
        resolved_mcp = mcp_config or (str(settings.extra.get("mcp_config")) if settings.extra.get("mcp_config") else None)
        perms = translate_tools_to_opencode_permissions(all_writer_tools, include_web_search=True, hidden_paths=hidden)
        if isinstance(settings.extra.get("permissions"), dict):
            perms.update(settings.extra["permissions"])  # type: ignore[arg-type]
        return OpenCodeAdapter(
            model=settings.model,
            api_key=settings.api_key or None,
            base_url=settings.base_url or None,
            workspace_path=workspace,
            prompt_dir=prompts,
            mcp_config=resolved_mcp,
            permissions=perms,
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )

    raise ValueError(f"unknown backend: {backend!r}")


def build_research_adapter(
    backend: str,
    *,
    workspace_path: str | Path,
    prompt_dir: str | Path,
    query: ResearchQuery,
    model: str | None = None,
    run_dir: str | Path | None = None,
    node_id: str | None = None,
    raw_finding_path: str | Path | None = None,
) -> AgentAdapter:
    """Adapter for one v4/§B4 probe.

    Supports all backends (gptme, claude, codex, opencode). ``doc_retrieval``
    raises loudly rather than silently degrading: it needed Claude Code's Context7
    MCP wiring, which is not available for generic probes.
    """
    workspace = str(workspace_path)
    prompts = str(prompt_dir)
    kind = normalize_probe_kind(query.kind)
    tools = allowed_tools_for(query.kind)

    settings = read_backend_config(
        backend,
        run_dir=Path(run_dir) if run_dir is not None else None,
        model=model,
    )

    hidden: tuple[str, ...] = ()
    exceptions: tuple[str, ...] = ()
    if kind in ("workspace", "corpus") and run_dir is not None:
        target_raw = Path(raw_finding_path) if raw_finding_path is not None else research_raw_finding_path(
            Path(run_dir), node_id or query.slug, query.slug
        )
        hidden, exceptions = _hidden_paths_and_exceptions_for_probe(
            Path(run_dir), Path(workspace_path), target_raw
        )

    if backend == "gptme":
        kwargs: dict[str, Any] = dict(
            model=settings.model,
            api_key=settings.api_key or None,
            base_url=settings.base_url or None,
            workspace_path=workspace,
            prompt_dir=prompts,
            tool_allowlist=tools,
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )
        return GptmeAdapter(**kwargs)

    if backend == "claude":
        disallowed = translate_tools_to_claude_disallowed(tools, include_web_search=(kind == "web"))
        return ClaudeCodeAdapter(
            model=settings.model,
            api_key=settings.api_key or None,
            base_url=settings.base_url or None,
            workspace_path=workspace,
            prompt_dir=prompts,
            disallowed_tools=disallowed,
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )

    if backend == "codex":
        emit_capability_event(
            run_dir,
            query.slug,
            "codex",
            "tool_allowlist",
            "Codex does not support tool restrictions for research probes",
            role="research",
        )
        if hidden:
            emit_capability_event(
                run_dir,
                query.slug,
                "codex",
                "path_deny",
                "Codex does not support filesystem path deny rules",
                role="research",
            )
        wire_api = str(settings.extra.get("wire_api") or "responses")
        return CodexAdapter(
            model=settings.model,
            api_key=settings.api_key or None,
            base_url=settings.base_url or None,
            wire_api=wire_api,
            workspace_path=workspace,
            prompt_dir=prompts,
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )

    if backend == "opencode":
        perms = translate_tools_to_opencode_permissions(tools, include_web_search=(kind == "web"), hidden_paths=hidden)
        if isinstance(settings.extra.get("permissions"), dict):
            perms.update(settings.extra["permissions"])  # type: ignore[arg-type]
        return OpenCodeAdapter(
            model=settings.model,
            api_key=settings.api_key or None,
            base_url=settings.base_url or None,
            workspace_path=workspace,
            prompt_dir=prompts,
            permissions=perms,
            hidden_paths=hidden,
            hidden_path_exceptions=exceptions,
        )

    raise ValueError(f"unknown backend: {backend!r}")


_VALID_PLAN_KINDS = ("web", "web_search", "workspace", "corpus", "doc_retrieval")


def parse_research_plan(raw: Any) -> dict[str, list[ResearchQuery]]:
    """Turn the web/CLI's loose ``research_plan`` JSON into v4's typed plan."""
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