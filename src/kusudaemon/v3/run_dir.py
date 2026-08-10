"""Run-directory path helpers layered on top of v0/v1/v2 (PLAN.md §5).

v3 adds the two things assembly/repair need that nothing below it created:

- ``assembly/`` — ``index.md`` (deterministic concatenation order,
  §4.6.1), ``checks.json`` (cross-cutting checks, §4.6.2), a compiled
  output file, and ``compile.log`` (§4.6.3: "exit code and log are the
  gate").
- ``out/.versions/<node_id>/`` — pre-repair snapshots (§5: "pre-repair
  snapshots"; §4.6: "snapshot to out/.versions/ before any repair").

Like ``spine_path``/``contract_path`` in v2's ``run_dir.py``, none of these
are pre-touched by ``create_run_dir`` — they don't exist until assembly or
repair actually runs.
"""

from __future__ import annotations

from pathlib import Path

from ..v0.run_dir import (  # noqa: F401 — re-exported for v3 callers
    create_run_dir,
    ensure_node_scratch_dir,
    ensure_node_trace_path,
    events_path,
    manifest_path,
    node_artifact_path,
    node_scratch_dir,
    node_trace_path,
    spec_path,
)
from ..v1.run_dir import (  # noqa: F401 — re-exported for v3 callers
    audit_dir,
    audit_path,
    ensure_audit_dir,
    ensure_audit_path,
    ensure_orchestrator_dir,
    orchestrator_dir,
    round_trace_path,
    tree_path,
)
from ..v2.run_dir import contract_path, spine_path  # noqa: F401 — re-exported for v3 callers


def assembly_dir(run_dir: str | Path) -> Path:
    path = Path(run_dir) / "assembly"
    path.mkdir(parents=True, exist_ok=True)
    return path


def assembly_index_path(run_dir: str | Path) -> Path:
    return assembly_dir(run_dir) / "index.md"


def assembly_checks_path(run_dir: str | Path) -> Path:
    return assembly_dir(run_dir) / "checks.json"


def assembly_output_path(run_dir: str | Path, filename: str = "main.md") -> Path:
    return assembly_dir(run_dir) / filename


def compile_log_path(run_dir: str | Path) -> Path:
    return assembly_dir(run_dir) / "compile.log"


def revalidation_dir(run_dir: str | Path) -> Path:
    path = Path(run_dir) / "audit" / "revalidation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def revalidation_audit_path(run_dir: str | Path, node_id: str) -> Path:
    """Separate from v1's ``audit/<node>.json`` (the normal per-dispatch
    review verdict) so a re-validation pass never clobbers the record of
    the review that originally passed the node."""
    return revalidation_dir(run_dir) / f"{node_id}.json"


def versions_dir(run_dir: str | Path, node_id: str) -> Path:
    path = Path(run_dir) / "out" / ".versions" / node_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def version_snapshot_path(run_dir: str | Path, node_id: str, tag: str) -> Path:
    return versions_dir(run_dir, node_id) / f"{tag}.md"
