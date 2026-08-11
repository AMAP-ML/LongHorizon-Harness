"""Concatenation + index tests (PLAN.md §4.6.1). Pure script, no network,
no adapter, no provider — artifacts are written directly to disk.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v0.run_dir import create_run_dir, node_artifact_path  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v3.assemble import (  # noqa: E402
    AssemblyNotReadyError,
    assemble,
    concatenate_artifacts,
    ordered_node_ids,
    require_complete,
)


def _node(node_id: str, *, status: str = "passed") -> TaskNode:
    return TaskNode(
        id=node_id, brief=f"write {node_id}", artifact=f"out/{node_id}.md",
        gates=["nonempty"], status=status,
    )


class OrderTest(unittest.TestCase):
    def test_ordered_node_ids_matches_tree_json_array_order(self) -> None:
        nodes = {n.id: n for n in [_node("c"), _node("a"), _node("b")]}
        tree = TaskTree(nodes=nodes)
        # dict insertion order == the order the caller built it in == the
        # order tree.json's array will round-trip through (see module
        # docstring: the planner writes candidates left-to-right).
        self.assertEqual(ordered_node_ids(tree), ["c", "a", "b"])


class AssembleTest(unittest.TestCase):
    def test_concatenates_in_tree_order_and_writes_index(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            nodes = {n.id: n for n in [_node("ch01"), _node("ch02")]}
            tree = TaskTree(nodes=nodes)
            node_artifact_path(run_dir, "ch01").write_text("First chapter body.", encoding="utf-8")
            node_artifact_path(run_dir, "ch02").write_text("Second chapter body.", encoding="utf-8")

            output = assemble(run_dir, tree)

            self.assertTrue(output.output_path.exists())
            text = output.output_path.read_text(encoding="utf-8")
            self.assertLess(text.index("First chapter body."), text.index("Second chapter body."))
            self.assertIn("ch01", text)
            self.assertIn("ch02", text)

            index_text = output.index_path.read_text(encoding="utf-8")
            self.assertIn("ch01", index_text)
            self.assertIn("ch02", index_text)
            self.assertEqual([e.node_id for e in output.index], ["ch01", "ch02"])

    def test_raises_when_a_node_has_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            nodes = {
                "ch01": _node("ch01", status="passed"),
                "ch02": _node("ch02", status="pending"),
            }
            tree = TaskTree(nodes=nodes)
            node_artifact_path(run_dir, "ch01").write_text("done", encoding="utf-8")

            with self.assertRaises(AssemblyNotReadyError) as ctx:
                assemble(run_dir, tree)
            self.assertIn("ch02", str(ctx.exception))

    def test_split_parent_excluded_from_top_level_assembly_children_included(self) -> None:
        # PLAN.md §A8.3/§B5: a split parent's own artifact is a derived
        # concatenation of its already-grafted children -- including it too
        # at the top level would duplicate that content in main.md.
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            parent = TaskNode(
                id="big", brief="a large node", artifact="out/big.md",
                gates=["nonempty"], status="split",
            )
            child_a = TaskNode(
                id="big.a", brief="first half", artifact="out/big.a.md",
                gates=["nonempty"], status="passed", parent="big",
            )
            child_b = TaskNode(
                id="big.b", brief="second half", artifact="out/big.b.md",
                gates=["nonempty"], status="passed", parent="big",
            )
            other = _node("other")
            tree = TaskTree(nodes={n.id: n for n in [parent, child_a, child_b, other]})
            node_artifact_path(run_dir, "big.a").write_text("First half body.", encoding="utf-8")
            node_artifact_path(run_dir, "big.b").write_text("Second half body.", encoding="utf-8")
            node_artifact_path(run_dir, "big").write_text(
                "Derived text a lazy hand-editor left behind -- must not appear twice.",
                encoding="utf-8",
            )
            node_artifact_path(run_dir, "other").write_text("Other body.", encoding="utf-8")

            self.assertEqual(ordered_node_ids(tree), ["big.a", "big.b", "other"])
            require_complete(tree)  # must not raise -- "split" counts as done

            output = assemble(run_dir, tree)
            text = output.output_path.read_text(encoding="utf-8")
            self.assertIn("First half body.", text)
            self.assertIn("Second half body.", text)
            self.assertIn("Other body.", text)
            # The parent's own on-disk artifact never appears in the
            # top-level document at all -- and neither does its heading
            # (ordered_node_ids never yields "big", so _default_render is
            # never called on it).
            self.assertNotIn("hand-editor", text)
            self.assertNotIn("## big\n", text)

    def test_concatenate_artifacts_node_ids_override_scopes_to_a_subset(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            nodes = {n.id: n for n in [_node("ch01"), _node("ch02"), _node("ch03")]}
            tree = TaskTree(nodes=nodes)
            node_artifact_path(run_dir, "ch01").write_text("First.", encoding="utf-8")
            node_artifact_path(run_dir, "ch02").write_text("Second.", encoding="utf-8")
            node_artifact_path(run_dir, "ch03").write_text("Third.", encoding="utf-8")

            text = concatenate_artifacts(run_dir, tree, node_ids=["ch03", "ch01"])
            self.assertIn("Third.", text)
            self.assertIn("First.", text)
            self.assertNotIn("Second.", text)
            self.assertLess(text.index("Third."), text.index("First."))


if __name__ == "__main__":
    unittest.main()
