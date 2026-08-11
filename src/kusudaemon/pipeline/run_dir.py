"""v5 run-directory path helpers (PLAN.md §5 extension).

Layer on top of v0-v4's helpers without touching them. Every path here is
plainly named after the file it points at; only ``source_path`` and
``run_spec_path`` are pre-touched (by the driver itself), the rest are
created by whichever module writes them, following v2's stated pattern of
never leaving a misleading empty file behind.

Files introduced by the driver you will not find in PLAN.md §5's layout:

* ``source.txt``     — the corpus the whole run decomposes (§4.2 feeds on
                       it). Written once at run start; nothing else touches it.
* ``phase.json``     — ``{"phase", "status", "detail", "ts"}``, the pipeline's
                       durable progress marker. Skipping a phase on resume is
                       decided by *artifact* existence (§10: markdown artifacts
                       are authoritative), not by this file's word, but it is
                       the fast path any watcher (CLI ``status``, the web app)
                       reads.
* ``approvals.jsonl`` — every human-in-the-loop checkpoint (intake answers,
                       pilot edits, amend/triage/reopen gates), append-only,
                       latest record per ``approval_id`` wins. Cross-process:
                       the driver polls it, the web app and the CLI append to
                       it, so any surface can resolve what any other surface
                       asked (§11: the web app "can be attached from anywhere").
* ``halt.flag``       — §10 "halt": stop after the current phase. The driver
                       checks it at phase boundaries only, never mid-turn.
* ``run.spec.json``   — the immutable ``{goal, source, backend, ...}`` record
                       of what the run was asked to do, written once at first
                       dispatch so a detached web app (or ``pipeline resume``)
                       can rebuild the environment (provider, adapter factory)
                       without the original command line.
"""

from __future__ import annotations

from pathlib import Path

from ..v0.run_dir import (  # noqa: F401
    create_run_dir,
    events_path,
    glossary_path,
    manifest_path,
    node_artifact_path,
    node_scratch_dir,
    node_trace_path,
    resolve_stored,
    spec_path,
)
from ..v1.run_dir import (  # noqa: F401
    audit_dir,
    audit_path,
    orchestrator_dir,
    round_trace_path,
    tree_path,
)
from ..v2.run_dir import contract_path, spine_path  # noqa: F401
from ..v3.run_dir import (  # noqa: F401
    assembly_checks_path,
    assembly_dir,
    assembly_index_path,
    assembly_output_path,
    compile_log_path,
    revalidation_audit_path,
    versions_dir,
    version_snapshot_path,
)


def source_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "source.txt"


def phase_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "phase.json"


def tier_path(run_dir: str | Path) -> Path:
    """PLAN.md §A4/§B2: ``{tier, signals, estimate, needs_intake,
    needs_explore, override, ts}`` — the durable record of the classify
    phase's decision. ``_phase_done("classify")`` keys off this file's
    existence exactly the way ``_phase_done("plan")`` keys off
    ``tree.json``."""
    return Path(run_dir) / "tier.json"


def approvals_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "approvals.jsonl"


def halt_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "halt.flag"


def run_spec_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "run.spec.json"


def jobs_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "jobs.jsonl"


def driver_pid_path(run_dir: str | Path) -> Path:
    """PLAN.md §D0c: which process (if any) currently owns this run. A
    ``phase.json`` reading ``in_progress`` forever is indistinguishable
    from a run genuinely mid-call unless something records who was
    supposed to be making progress — this is that record, written once at
    driver construction. Deliberately not ``jobs.jsonl``: that file already
    has its own schema (dashboard-triggered background jobs, keyed by
    ``job_id``) unrelated to driver-process liveness."""
    return Path(run_dir) / "driver.pid.json"


def resolve_runs_root(root: str | Path) -> Path:
    """The one place a ``--runs-root`` string turns into an absolute path
    (PLAN.md §D0b). Every CLI command and the dashboard server must route
    through this rather than doing a bare ``Path(root)``: unresolved, the
    same ``--runs-root`` value silently addresses a different directory
    depending on the shell's cwd at invocation time, which is the root
    cause behind "a run I started now shows empty" reports — a second,
    empty run directory gets created in a sister folder and nothing errors.
    """
    return Path(root).expanduser().resolve()


def resolve_run_dir(root: str | Path, run_id: str) -> Path:
    return resolve_runs_root(root) / run_id