"""On-disk run directory for a single-node v0 run — a slice of PLAN.md §5's
full layout, just the pieces a single Writer node needs:

    <root>/<run-id>/
      spec.md
      events.jsonl
      manifest.jsonl
      scratch/<node_id>/trace.jsonl
      out/<node_id>.md
"""

from __future__ import annotations

from pathlib import Path


def create_run_dir(root: str | Path, run_id: str) -> Path:
    """Idempotent: safe to call again on resume, never wipes existing state."""
    run_dir = Path(root) / run_id
    (run_dir / "scratch").mkdir(parents=True, exist_ok=True)
    (run_dir / "out").mkdir(parents=True, exist_ok=True)
    for name in ("spec.md", "events.jsonl", "manifest.jsonl"):
        path = run_dir / name
        if not path.exists():
            path.touch()
    return run_dir


def events_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "events.jsonl"


def manifest_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "manifest.jsonl"


def node_scratch_dir(run_dir: str | Path, node_id: str) -> Path:
    path = Path(run_dir) / "scratch" / node_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def node_trace_path(run_dir: str | Path, node_id: str) -> Path:
    return node_scratch_dir(run_dir, node_id) / "trace.jsonl"


def node_artifact_path(run_dir: str | Path, node_id: str) -> Path:
    path = Path(run_dir) / "out"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{node_id}.md"
