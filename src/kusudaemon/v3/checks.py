"""Cross-cutting checks (PLAN.md §4.6.2) — script, no model call, catches
cross-unit breakage that per-unit review can't see by construction (a
reviewer only ever sees one artifact, §3).

PLAN.md's example list — "do all refs_out resolve? is every used term in
glossary.json? duplicate definitions? continuous numbering? empty
sections?" — needs the node-type template system to have data to check
(``refs_out``, term extraction): v1's gates and v2's planner both document
that system as still unbuilt, and v3's Progress note repeats it. So this
module checks the cross-cutting properties that *are* derivable today from
``tree.json``, ``manifest.jsonl``, and the actual files in ``out/``:
missing/empty artifacts, nodes not yet passed (the "continuous" property
that's actually meaningful pre-template-system: nothing missing from the
sequence), gate drift (an artifact that passed its gates at dispatch time
but no longer does — someone or something touched ``out/`` since), and a
passed node with no matching manifest line. (An earlier draft also checked
for duplicate artifact paths across node ids; dropped because
``node_artifact_path`` derives the path purely from the node id and
``TaskTree.nodes`` is a dict keyed by id, so that collision is structurally
unreachable, not just unlikely.)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..v0.run_dir import node_artifact_path
from ..v1.gates import all_passed, evaluate_gates
from ..v1.tree import TaskTree
from .assemble import concatenate_artifacts
from .run_dir import assembly_checks_path


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: list[str] = field(default_factory=list)


def _read_artifact(run_dir: Path, node_id: str) -> str:
    path = node_artifact_path(run_dir, node_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _read_manifest_by_node(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.exists():
        return {}
    by_node: dict[str, dict[str, Any]] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        node_id = record.get("node")
        if node_id:
            by_node[node_id] = record  # last line for a node id wins
    return by_node


def check_all_nodes_passed(tree: TaskTree) -> CheckResult:
    """PLAN.md §A8/§B5: a "split" node never itself becomes "passed" (its
    children do), but it is a successful terminal outcome, not an
    incomplete one — excluded from this check the same way it's excluded
    from ``v3/assemble.py:require_complete``/``TaskTree.is_complete()``."""
    missing = [n.id for n in tree.nodes.values() if n.status not in ("passed", "split")]
    return CheckResult(
        name="all_nodes_passed",
        passed=not missing,
        details=[f"{node_id}: status={tree.nodes[node_id].status}" for node_id in missing],
    )


def check_artifacts_exist_and_nonempty(run_dir: str | Path, tree: TaskTree) -> CheckResult:
    run_dir = Path(run_dir)
    problems = []
    for node in tree.nodes.values():
        if node.status != "passed":
            continue
        path = node_artifact_path(run_dir, node.id)
        if not path.exists():
            problems.append(f"{node.id}: artifact missing at {path}")
        elif not path.read_text(encoding="utf-8").strip():
            problems.append(f"{node.id}: artifact is empty")
    return CheckResult(name="artifacts_exist_and_nonempty", passed=not problems, details=problems)


def check_no_gate_drift(run_dir: str | Path, tree: TaskTree) -> CheckResult:
    """A node that passed its gates when it was dispatched but whose current
    on-disk artifact no longer would — the file changed under us since
    (hand edit, bad repair, filesystem issue). Assembly should not silently
    include content that has drifted out of compliance."""
    run_dir = Path(run_dir)
    problems = []
    for node in tree.nodes.values():
        if node.status != "passed":
            continue
        text = _read_artifact(run_dir, node.id)
        results = evaluate_gates(node.gates, text)
        if not all_passed(results):
            unmet = [r.gate for r in results if not r.passed]
            problems.append(f"{node.id}: currently fails gates {unmet}")
    return CheckResult(name="no_gate_drift", passed=not problems, details=problems)


def check_manifest_recorded(run_dir: str | Path, manifest_path: str | Path, tree: TaskTree) -> CheckResult:
    by_node = _read_manifest_by_node(Path(manifest_path))
    missing = [
        node.id
        for node in tree.nodes.values()
        if node.status == "passed" and node.id not in by_node
    ]
    return CheckResult(
        name="manifest_recorded",
        passed=not missing,
        details=[f"{node_id}: passed but no manifest.jsonl line" for node_id in missing],
    )


def check_split_parents_derived(run_dir: str | Path, tree: TaskTree) -> CheckResult:
    """PLAN.md §A8.3: a split parent's artifact is *derived* — script
    concatenation of its children, never an integrator episode (the same
    read-only-assembler guardrail §4.6 states for the assembler generally).
    A parent whose on-disk artifact no longer equals a fresh concatenation
    of its children (a hand edit, a bad repair, a crash between the last
    child passing and ``v7/split.py:maybe_derive_split_parent``'s write) is
    a defect in the same spirit ``check_no_gate_drift`` treats a drifted
    leaf — something touched ``out/`` since — not a repair opportunity."""
    run_dir = Path(run_dir)
    problems = []
    for node in tree.nodes.values():
        if node.status != "split":
            continue
        child_ids = [n.id for n in tree.nodes.values() if n.parent == node.id]
        expected = concatenate_artifacts(run_dir, tree, node_ids=child_ids)
        actual = _read_artifact(run_dir, node.id)
        if actual.strip() != expected.strip():
            problems.append(f"{node.id}: derived artifact drifted from its children's concatenation")
    return CheckResult(name="split_parents_derived", passed=not problems, details=problems)


def run_cross_cutting_checks(
    run_dir: str | Path, tree: TaskTree, manifest_path: str | Path
) -> list[CheckResult]:
    return [
        check_all_nodes_passed(tree),
        check_artifacts_exist_and_nonempty(run_dir, tree),
        check_no_gate_drift(run_dir, tree),
        check_manifest_recorded(run_dir, manifest_path, tree),
        check_split_parents_derived(run_dir, tree),
    ]


def write_checks_json(run_dir: str | Path, results: list[CheckResult]) -> Path:
    payload = {
        "passed": all(r.passed for r in results),
        "checks": [asdict(r) for r in results],
    }
    path = assembly_checks_path(run_dir)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
