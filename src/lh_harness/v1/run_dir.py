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
    path = Path(run_dir) / "audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def audit_path(run_dir: str | Path, node_id: str) -> Path:
    return audit_dir(run_dir) / f"{node_id}.json"


def orchestrator_dir(run_dir: str | Path) -> Path:
    path = Path(run_dir) / "orchestrator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def round_trace_path(run_dir: str | Path, round_index: int) -> Path:
    return orchestrator_dir(run_dir) / f"round-{round_index:03d}.jsonl"
