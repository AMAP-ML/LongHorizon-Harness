"""Run-directory path helpers layered on top of v0/v1 (PLAN.md §5).

v0 owns ``spec.md``/``events.jsonl``/``manifest.jsonl``/``scratch``/``out``;
v1 adds ``tree.json``/``audit/``/``orchestrator/``. v2 adds the two paths
its own state needs: ``spine.json`` (discovered structure, §4.2) and
``contract.md`` (frozen after pilot, §4.4). Both are plain accessors — the
files are created by whichever v2 module actually writes them
(``survey.save_spine``, ``contract.freeze_contract``), not pre-touched here,
since unlike v0's single-node files they don't exist until survey/pilot
actually run.
"""

from __future__ import annotations

from pathlib import Path

from ..v0.run_dir import (  # noqa: F401 — re-exported for v2 callers
    create_run_dir,
    events_path,
    manifest_path,
    node_artifact_path,
    node_scratch_dir,
    node_trace_path,
    spec_path,
)
from ..v1.run_dir import (  # noqa: F401 — re-exported for v2 callers
    audit_dir,
    audit_path,
    orchestrator_dir,
    round_trace_path,
    tree_path,
)


def spine_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "spine.json"


def contract_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "contract.md"


def spine_units_dir(run_dir: str | Path) -> Path:
    """Materialized spine units (PLAN-zeromem.md §7) — verbatim slices of
    the source a leaf's ``inputs`` entries resolve to, so a Writer's ``read``
    tool has something real to open instead of an opaque ``unit-03`` id."""
    path = Path(run_dir) / "spine"
    path.mkdir(parents=True, exist_ok=True)
    return path


def spine_unit_path(run_dir: str | Path, unit_id: str) -> Path:
    return spine_units_dir(run_dir) / f"{unit_id}.md"
