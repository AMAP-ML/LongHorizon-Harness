"""Cross-cutting checks tests (PLAN.md §4.6.2). Pure script, no network."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from waypoint.v0.run_dir import create_run_dir, manifest_path, node_artifact_path  # noqa: E402
from waypoint.v1.manifest import append_manifest_line  # noqa: E402
from waypoint.v1.gates import evaluate_gates  # noqa: E402
from waypoint.v1.tree import TaskNode, TaskTree  # noqa: E402
from waypoint.v3.checks import (  # noqa: E402
    check_all_nodes_passed,
    check_artifacts_exist_and_nonempty,
    check_manifest_recorded,
    check_no_gate_drift,
    run_cross_cutting_checks,
    write_checks_json,
)
from waypoint.v3.run_dir import assembly_checks_path  # noqa: E402


def _node(node_id: str, *, status: str = "passed", gates: list[str] | None = None) -> TaskNode:
    return TaskNode(
        id=node_id, brief=f"write {node_id}", artifact=f"out/{node_id}.md",
        gates=gates or ["nonempty"], status=status,
    )


class ChecksTest(unittest.TestCase):
    def test_all_nodes_passed_flags_incomplete_nodes(self) -> None:
        tree = TaskTree(nodes={n.id: n for n in [_node("a"), _node("b", status="pending")]})
        result = check_all_nodes_passed(tree)
        self.assertFalse(result.passed)
        self.assertIn("b", result.details[0])

    def test_artifacts_exist_and_nonempty_flags_missing_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            tree = TaskTree(nodes={n.id: n for n in [_node("a"), _node("b"), _node("c")]})
            node_artifact_path(run_dir, "a").write_text("content", encoding="utf-8")
            node_artifact_path(run_dir, "b").write_text("   ", encoding="utf-8")
            # "c" artifact never written -> missing

            result = check_artifacts_exist_and_nonempty(run_dir, tree)
            self.assertFalse(result.passed)
            joined = "\n".join(result.details)
            self.assertIn("b: artifact is empty", joined)
            self.assertIn("c: artifact missing", joined)
            self.assertNotIn("a:", joined)

    def test_no_gate_drift_flags_a_file_that_no_longer_passes_its_own_gates(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            node = _node("a", gates=["contains:REQUIRED"])
            tree = TaskTree(nodes={node.id: node})
            node_artifact_path(run_dir, "a").write_text("this no longer has the marker", encoding="utf-8")

            result = check_no_gate_drift(run_dir, tree)
            self.assertFalse(result.passed)
            self.assertIn("a", result.details[0])

    def test_manifest_recorded_flags_passed_node_with_no_manifest_line(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            m_path = manifest_path(run_dir)
            node_a = _node("a")
            node_b = _node("b")
            tree = TaskTree(nodes={"a": node_a, "b": node_b})
            text_a = "content a"
            node_artifact_path(run_dir, "a").write_text(text_a, encoding="utf-8")
            append_manifest_line(
                m_path, node_id="a", artifact_path=str(node_artifact_path(run_dir, "a")),
                artifact_text=text_a, gate_results=evaluate_gates(node_a.gates, text_a),
                promotion="done",
            )
            # "b" never got a manifest line

            result = check_manifest_recorded(run_dir, m_path, tree)
            self.assertFalse(result.passed)
            self.assertIn("b", result.details[0])

    def test_run_cross_cutting_checks_and_write_checks_json(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            node = _node("a")
            tree = TaskTree(nodes={"a": node})
            node_artifact_path(run_dir, "a").write_text("content", encoding="utf-8")

            results = run_cross_cutting_checks(run_dir, tree, manifest_path(run_dir))
            path = write_checks_json(run_dir, results)

            self.assertEqual(path, assembly_checks_path(run_dir))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("passed", payload)
            self.assertEqual(len(payload["checks"]), len(results))
            # "a" was never appended to manifest.jsonl in this test, so the
            # overall checks payload should report the run as not fully clean.
            self.assertFalse(payload["passed"])


if __name__ == "__main__":
    unittest.main()
