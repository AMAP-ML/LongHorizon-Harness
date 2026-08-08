"""Contract-amendment re-validation tests (PLAN.md §10).

Coverage:
- classify_verdict buckets clean/patchable/regenerate correctly, defaulting
  to the stricter "regenerate" when a failing item's class is missing or
  mixed with a regenerate item
- estimate_revalidation_cost counts only passed nodes and scales with
  contract + rubric + artifact size, with zero model calls
- run_revalidation_pass is read-only (no writer dispatch, asserted via
  FakeStreamAgentAdapter never being constructed), classifies via
  FakeProvider, and marks non-clean nodes "stale"
- apply_revalidation_triage executes patchable/regenerate through the same
  repair.run_repair path already covered in test_v3_repair.py, and leaves
  "clean" nodes untouched
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402
from lh_harness.environment.local import LocalEnvironment  # noqa: E402
from lh_harness.types import EpisodeBudget  # noqa: E402
from lh_harness.v0.events import EventLog  # noqa: E402
from lh_harness.v0.run_dir import create_run_dir, events_path, manifest_path, node_artifact_path  # noqa: E402
from lh_harness.v1.gates import evaluate_gates  # noqa: E402
from lh_harness.v1.manifest import append_manifest_line  # noqa: E402
from lh_harness.v1.reviewer import ReviewVerdict  # noqa: E402
from lh_harness.v1.tree import TaskNode, TaskTree  # noqa: E402
from lh_harness.v3.revalidate import (  # noqa: E402
    Triage,
    apply_revalidation_triage,
    classify_verdict,
    estimate_revalidation_cost,
    run_revalidation_pass,
    summarize_triage,
)
from lh_harness.v3.run_dir import revalidation_audit_path  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _node(node_id: str, *, judgment: list[str] | None = None, status: str = "passed") -> TaskNode:
    node = TaskNode(
        id=node_id, brief=f"write {node_id}", artifact=f"out/{node_id}.md",
        gates=["nonempty"], judgment=judgment or [], status=status,
    )
    if judgment:
        node.rubric = {j: f"rubric text for {j}" for j in judgment}
    return node


class ClassifyVerdictTest(unittest.TestCase):
    def test_pass_verdict_is_clean(self) -> None:
        verdict = ReviewVerdict(node_id="a", items=[{"id": "R1", "pass": True}], verdict="pass")
        self.assertEqual(classify_verdict(verdict), "clean")

    def test_all_patchable_failures_is_patchable(self) -> None:
        verdict = ReviewVerdict(
            node_id="a",
            items=[{"id": "R1", "pass": False, "class": "patchable", "defect": "missing box"}],
            verdict="fail",
        )
        self.assertEqual(classify_verdict(verdict), "patchable")

    def test_failure_with_no_class_defaults_to_regenerate(self) -> None:
        verdict = ReviewVerdict(node_id="a", items=[{"id": "R1", "pass": False}], verdict="fail")
        self.assertEqual(classify_verdict(verdict), "regenerate")

    def test_mixed_patchable_and_regenerate_is_regenerate(self) -> None:
        verdict = ReviewVerdict(
            node_id="a",
            items=[
                {"id": "R1", "pass": False, "class": "patchable"},
                {"id": "R2", "pass": False, "class": "regenerate"},
            ],
            verdict="fail",
        )
        self.assertEqual(classify_verdict(verdict), "regenerate")


class EstimateCostTest(unittest.TestCase):
    def test_counts_only_passed_nodes_and_scales_with_size(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            passed = _node("a")
            pending = _node("b", status="pending")
            node_artifact_path(run_dir, "a").write_text("word " * 100, encoding="utf-8")
            tree = TaskTree(nodes={"a": passed, "b": pending})

            small = estimate_revalidation_cost(run_dir, tree, "short contract")
            large = estimate_revalidation_cost(run_dir, tree, "much longer contract " * 50)

            self.assertEqual(small.node_count, 1)
            self.assertGreater(large.estimated_tokens, small.estimated_tokens)


class RevalidationPassTest(unittest.TestCase):
    def test_read_only_pass_classifies_and_marks_stale(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            clean_node = _node("clean1")
            patch_node = _node("patch1")
            node_artifact_path(run_dir, "clean1").write_text("fine as is", encoding="utf-8")
            node_artifact_path(run_dir, "patch1").write_text("needs a small edit", encoding="utf-8")
            tree = TaskTree(nodes={"clean1": clean_node, "patch1": patch_node})
            tree_path = Path(root_str) / "tree.json"
            tree.save(tree_path)

            provider = FakeProvider([
                {"items": [], "verdict": "pass"},
                {
                    "items": [{"id": "C1", "pass": False, "class": "patchable", "defect": "add a box"}],
                    "verdict": "fail",
                },
            ])

            triage = run_revalidation_pass(
                run_dir, tree, tree_path, "every unit needs a summary box", provider
            )

            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(summarize_triage(triage), {"clean": 1, "patchable": 1, "regenerate": 0})
            self.assertEqual(tree.nodes["clean1"].status, "passed")
            self.assertEqual(tree.nodes["patch1"].status, "stale")

            reloaded = TaskTree.load(tree_path)
            self.assertEqual(reloaded.nodes["patch1"].status, "stale")

            self.assertTrue(revalidation_audit_path(run_dir, "clean1").exists())
            self.assertTrue(revalidation_audit_path(run_dir, "patch1").exists())


class ApplyTriageTest(unittest.TestCase):
    def test_clean_untouched_patchable_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            clean_node = _node("clean1")
            patch_node = _node("patch1")
            for node, text in ((clean_node, "clean content"), (patch_node, "needs a patch")):
                node_artifact_path(run_dir, node.id).write_text(text, encoding="utf-8")
                m = manifest_path(run_dir)
                append_manifest_line(
                    m, node_id=node.id, artifact_path=str(node_artifact_path(run_dir, node.id)),
                    artifact_text=text, gate_results=evaluate_gates(node.gates, text),
                    promotion="seed",
                )
            log = EventLog(events_path(run_dir))
            for node in (clean_node, patch_node):
                log.append({"node_id": node.id, "role": "writer", "round": 0, "type": "node_dispatched"})
                log.append({
                    "node_id": node.id, "role": "writer", "round": 0, "type": "episode_completed",
                    "status": "done", "artifact_path": str(node_artifact_path(run_dir, node.id)),
                    "error": None, "duration_ms": 1,
                })

            tree = TaskTree(nodes={"clean1": clean_node, "patch1": patch_node})
            tree_path = root / "tree.json"
            tree.save(tree_path)

            triage = {
                "clean1": _fake_triage("clean1", "clean"),
                "patch1": _fake_triage("patch1", "patchable", defect="C1: add a box"),
            }

            calls_made: list[str] = []

            def adapter_factory(repair_node: TaskNode) -> FakeStreamAgentAdapter:
                calls_made.append(repair_node.id)
                return FakeStreamAgentAdapter(
                    script_path=str(FAKE_CLI),
                    pidfile=str(root / f"{repair_node.id}.pid"),
                    prompt_dir=str(root / "prompts"),
                    workspace_path=str(run_dir),
                    session_id="AMENDED_BOX_ADDED",
                )

            (root / "prompts").mkdir(parents=True, exist_ok=True)
            outcomes = asyncio.run(
                apply_revalidation_triage(
                    run_dir, tree, tree_path, manifest_path(run_dir), triage,
                    adapter_factory, LocalEnvironment(tmp_dir=str(root / "prompts")),
                    FakeProvider([]), log,
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )

            # Only patch1 was dispatched -- clean1 is left alone entirely.
            self.assertEqual(calls_made, ["patch1"])
            self.assertEqual(len(outcomes), 1)
            self.assertTrue(outcomes[0].passed)
            self.assertEqual(outcomes[0].mode, "patch")
            self.assertEqual(tree.nodes["patch1"].status, "passed")
            self.assertIn(
                "AMENDED_BOX_ADDED",
                node_artifact_path(run_dir, "patch1").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                node_artifact_path(run_dir, "clean1").read_text(encoding="utf-8"), "clean content"
            )


def _fake_triage(node_id: str, classification: str, *, defect: str = "") -> Triage:
    items = [] if classification == "clean" else [
        {"id": "C1", "pass": False, "class": classification, "defect": defect}
    ]
    verdict = ReviewVerdict(
        node_id=node_id, items=items, verdict="pass" if classification == "clean" else "fail"
    )
    return Triage(node_id=node_id, classification=classification, verdict=verdict)


if __name__ == "__main__":
    unittest.main()
