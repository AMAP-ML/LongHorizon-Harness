"""Run-directory path helpers layered on top of v0/v1/v2/v3 (PLAN.md §5).

v4 adds the one thing research needs that nothing below it created: a
durable finding location under each node's own ``scratch/<node>/``, the
same place a Writer's own working notes and raw tool dumps already live
(§5) — never under ``out/``, so a research finding is never mistaken for
document content by ``v3/assemble.py``'s tree-order concatenation (it only
ever walks nodes actually present in ``tree.json``, and research ids never
are — see ``v4/research.py``).

Two paths per query, not one, because they have different writers and
different lifetimes:

- ``research_raw_finding_path`` — written once, by the agent itself, during
  its own episode. Durable regardless of when a crash lands relative to
  this module's own post-processing (§8/§10: resumability is layered, not
  reinvented per module).
- ``research_finding_path`` — written by the harness *after* the episode
  returns: the capped, canonical finding text every downstream node prompt
  actually reads. Its mere existence (nonempty) is what makes a repeated
  call to ``run_research_query`` a no-op, the same way v0's own
  ``episode_completed`` event makes a repeated ``run_node`` call a no-op.
"""

from __future__ import annotations

from pathlib import Path

from ..v0.run_dir import (  # noqa: F401 — re-exported for v4 callers
    create_run_dir,
    events_path,
    manifest_path,
    node_artifact_path,
    node_scratch_dir,
    node_trace_path,
    spec_path,
)
from ..v1.run_dir import (  # noqa: F401 — re-exported for v4 callers
    audit_dir,
    audit_path,
    orchestrator_dir,
    round_trace_path,
    tree_path,
)
from ..v2.run_dir import contract_path, spine_path  # noqa: F401 — re-exported for v4 callers
from ..v3.run_dir import (  # noqa: F401 — re-exported for v4 callers
    assembly_checks_path,
    assembly_dir,
    assembly_index_path,
    assembly_output_path,
    compile_log_path,
    revalidation_audit_path,
    revalidation_dir,
    version_snapshot_path,
    versions_dir,
)


def research_dir(run_dir: str | Path, node_id: str) -> Path:
    path = node_scratch_dir(run_dir, node_id) / "research"
    path.mkdir(parents=True, exist_ok=True)
    return path


def research_finding_path(run_dir: str | Path, node_id: str, slug: str) -> Path:
    return research_dir(run_dir, node_id) / f"{slug}.md"


def research_raw_finding_path(run_dir: str | Path, node_id: str, slug: str) -> Path:
    return research_dir(run_dir, node_id) / f"{slug}.raw.json"
