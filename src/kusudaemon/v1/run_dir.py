"""Run-directory path helpers layered on top of v0's slice (PLAN.md §5).

v0 already owns ``spec.md``/``events.jsonl``/``manifest.jsonl``/``scratch``/
``out``. v1 adds the paths its own state needs: ``tree.json`` (task state —
§13: "task state in JSON"), ``audit/<node>.json`` (reviewer verdicts), and
``orchestrator/round-NN.jsonl`` (per-round traces, §5: "naturally chunked —
stateless rounds").
"""

from __future__ import annotations

from pathlib import Path

from ..v0.run_dir import (  # noqa: F401 — re-exported for v1 callers
    create_run_dir,
    events_path,
    manifest_path,
    node_artifact_path,
    node_scratch_dir,
    node_trace_path,
)


def tree_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "tree.json"


def audit_dir(run_dir: str | Path) -> Path:
    """Pure getter — §11.10.14: no mkdir side effect (see v0's
    ``node_scratch_dir`` for the split's rationale; readers far outnumber
    writers here)."""
    return Path(run_dir) / "audit"


def ensure_audit_dir(run_dir: str | Path) -> Path:
    """``audit_dir`` plus the mkdir — the variant writers call."""
    path = audit_dir(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def audit_path(run_dir: str | Path, node_id: str) -> Path:
    return audit_dir(run_dir) / f"{node_id}.json"


def ensure_audit_path(run_dir: str | Path, node_id: str) -> Path:
    """Pure ``audit_path`` plus the parent mkdir — what writers actually
    want, without calling readers through a mutation."""
    ensure_audit_dir(run_dir)
    return audit_path(run_dir, node_id)


def orchestrator_dir(run_dir: str | Path) -> Path:
    """Pure getter (see ``audit_dir``)."""
    return Path(run_dir) / "orchestrator"


def ensure_orchestrator_dir(run_dir: str | Path) -> Path:
    """``orchestrator_dir`` plus the mkdir."""
    path = orchestrator_dir(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def round_trace_path(run_dir: str | Path, round_index: int) -> Path:
    return orchestrator_dir(run_dir) / f"round-{round_index:03d}.jsonl"
