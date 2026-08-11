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

import os
import tempfile
from pathlib import Path


def write_text_atomic(path: str | Path, text: str) -> None:
    """Truncate-proof write: temp file in the same directory, ``fsync``,
    ``os.replace`` (PLAN.md §11.6).

    ``Path.write_text`` truncates in place, so a kill -9 mid-write leaves a
    truncated tree.json / contract.md / phase.json / audit file — none of
    which has events.jsonl's torn-tail tolerance. ``os.replace`` is atomic
    on POSIX: readers see either the old file or the new one, never half a
    file. ``events.py`` documents the same reasoning for its fsync-per-append
    contract.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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


def spec_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "spec.md"


def events_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "events.jsonl"


def manifest_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "manifest.jsonl"


def node_scratch_dir(run_dir: str | Path, node_id: str) -> Path:
    """Pure getter — §11.10.14: no mkdir side effect. Read-only surfaces
    (dashboard, checks, assembler) must be able to resolve paths in runs
    they are only inspecting; writers call ``ensure_node_scratch_dir``."""
    return Path(run_dir) / "scratch" / node_id


def ensure_node_scratch_dir(run_dir: str | Path, node_id: str) -> Path:
    """``node_scratch_dir`` plus the mkdir — the variant writers call."""
    path = node_scratch_dir(run_dir, node_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def node_trace_path(run_dir: str | Path, node_id: str) -> Path:
    return node_scratch_dir(run_dir, node_id) / "trace.jsonl"


def ensure_node_trace_path(run_dir: str | Path, node_id: str) -> Path:
    """``node_trace_path`` plus the parent mkdir. The subprocess tees its
    live output into this file (v0/runner.py), so the directory must exist
    before the agent starts, even though the path is also read by the
    dashboard for its logdir discovery."""
    ensure_node_scratch_dir(run_dir, node_id)
    return node_trace_path(run_dir, node_id)


def node_artifact_path(run_dir: str | Path, node_id: str) -> Path:
    return Path(run_dir) / "out" / f"{node_id}.md"


def resolve_stored(run_dir: str | Path, ref: str) -> Path:
    """Turn a path *stored on disk* (``tree.json``'s ``inputs``/``artifact``,
    a manifest promotion path) into a real filesystem path.

    PLAN.md §D0b's rule, stated once: a path stored on disk is relative to
    ``run_dir`` so the run directory stays movable; a path already absolute
    (e.g. hand-authored, or a future workspace-mode input outside the run
    dir) is returned unchanged. Every reader of a stored path — prompt
    building, the dashboard's input-token/exists checks, document review,
    the assembler — must call this instead of re-deriving the rule ad hoc.
    Lives here (not in pipeline/run_dir.py) so v0-v3 modules can use it
    without inverting the dependency direction onto pipeline/.
    """
    ref_path = Path(ref)
    if ref_path.is_absolute():
        return ref_path
    return Path(run_dir) / ref_path
