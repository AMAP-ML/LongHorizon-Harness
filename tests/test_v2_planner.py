"""Planner tests (PLAN.md §4.3): the leaf gate is harness-checked code, the
recursive level-at-a-time call structure, and the depth/node caps that force
a leaf without ever asking the model whether it has hit a limit. No network
— FakeProvider validates every canned partition against PARTITION_SCHEMA.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402
from waypoint.v2.planner import Candidate, build_tree, leaf_gate  # noqa: E402
from waypoint.v2.survey import SpineUnit  # noqa: E402


def _units(n: int, tokens: int = 1000) -> list[SpineUnit]:
    return [
        SpineUnit(id=f"unit-{i + 1:02d}", label=f"unit {i}", start_chunk=i, end_chunk=i, tokens=tokens)
        for i in range(n)
    ]


class LeafGateTest(unittest.TestCase):
    def test_passes_when_all_conditions_hold(self) -> None:
        candidate = Candidate(
            id="a", brief="write a summary", shape="prose-dominant",
            unit_start=0, unit_end=0, estimated_calls=5, tokens=1000,
        )
        is_leaf, reasons = leaf_gate(candidate, token_budget=24000, tool_call_cap=15)
        self.assertTrue(is_leaf)
        self.assertEqual(reasons, [])

    def test_fails_when_inputs_exceed_token_budget(self) -> None:
        candidate = Candidate(
            id="a", brief="write a summary", shape="prose-dominant",
            unit_start=0, unit_end=0, estimated_calls=5, tokens=50000,
        )
        is_leaf, reasons = leaf_gate(candidate, token_budget=24000, tool_call_cap=15)
        self.assertFalse(is_leaf)
        self.assertTrue(any("exceed budget" in reason for reason in reasons))

    def test_fails_when_estimated_calls_exceed_cap(self) -> None:
        candidate = Candidate(
            id="a", brief="write a summary", shape="prose-dominant",
            unit_start=0, unit_end=0, estimated_calls=99, tokens=1000,
        )
        is_leaf, reasons = leaf_gate(candidate, token_budget=24000, tool_call_cap=15)
        self.assertFalse(is_leaf)
        self.assertTrue(any("exceed cap" in reason for reason in reasons))

    def test_fails_when_brief_is_blank(self) -> None:
        candidate = Candidate(
            id="a", brief="   ", shape="prose-dominant",
            unit_start=0, unit_end=0, estimated_calls=5, tokens=1000,
        )
        is_leaf, reasons = leaf_gate(candidate)
        self.assertFalse(is_leaf)
        self.assertTrue(any("done-condition" in reason for reason in reasons))


class BuildTreeTest(unittest.TestCase):
    def test_flat_partition_where_every_child_passes_the_leaf_gate(self) -> None:
        units = _units(4, tokens=1000)
        provider = FakeProvider(
            [
                {
                    "children": [
                        {
                            "id": f"c{i}",
                            "brief": f"write unit {i}",
                            "unit_start": i,
                            "unit_end": i,
                            "estimated_calls": 3,
                            "shape": "prose-dominant",
                        }
                        for i in range(4)
                    ]
                }
            ]
        )
        tree = build_tree(units, provider, depth_cap=4, node_cap=100)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(tree.nodes), 4)
        for node in tree.nodes.values():
            self.assertEqual(node.depends_on, [])
            self.assertIn("nonempty", node.gates)
            self.assertTrue(any(gate.startswith("max_tokens:") for gate in node.gates))

    def test_child_failing_leaf_gate_recurses_into_its_own_call(self) -> None:
        units = _units(6, tokens=1000)
        top_level = {
            "children": [
                {
                    "id": "big",
                    "brief": "write a huge section",
                    "unit_start": 0,
                    "unit_end": 5,
                    "estimated_calls": 99,  # fails leaf gate: exceeds tool_call_cap
                    "shape": "prose-dominant",
                },
            ]
        }
        second_level = {
            "children": [
                {
                    "id": "small1",
                    "brief": "write part 1",
                    "unit_start": 0,
                    "unit_end": 2,
                    "estimated_calls": 3,
                    "shape": "prose-dominant",
                },
                {
                    "id": "small2",
                    "brief": "write part 2",
                    "unit_start": 3,
                    "unit_end": 5,
                    "estimated_calls": 3,
                    "shape": "prose-dominant",
                },
            ]
        }
        provider = FakeProvider([top_level, second_level])
        tree = build_tree(units, provider, depth_cap=4, node_cap=100, tool_call_cap=15)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(tree.nodes), 2)
        self.assertIn("big.small1", tree.nodes)
        self.assertIn("big.small2", tree.nodes)

    def test_depth_cap_forces_a_leaf_without_a_model_call(self) -> None:
        units = _units(3, tokens=1000)
        # depth_cap=0 means even the top-level slice is forced to a leaf
        # before the planner is ever called.
        provider = FakeProvider([])
        tree = build_tree(units, provider, depth_cap=0, node_cap=100)
        self.assertEqual(len(provider.calls), 0)
        self.assertEqual(len(tree.nodes), 1)

    def test_single_unit_slice_is_always_a_leaf(self) -> None:
        units = _units(1, tokens=1000)
        provider = FakeProvider([])
        tree = build_tree(units, provider, depth_cap=4, node_cap=100)
        self.assertEqual(len(provider.calls), 0)
        self.assertEqual(len(tree.nodes), 1)

    def test_node_cap_stops_recursion_early(self) -> None:
        units = _units(5, tokens=1000)
        provider = FakeProvider(
            [
                {
                    "children": [
                        {
                            "id": f"c{i}",
                            "brief": f"write unit {i}",
                            "unit_start": i,
                            "unit_end": i,
                            "estimated_calls": 3,
                            "shape": "prose-dominant",
                        }
                        for i in range(5)
                    ]
                }
            ]
        )
        tree = build_tree(units, provider, depth_cap=4, node_cap=2)
        self.assertLessEqual(len(tree.nodes), 2)

    def test_resulting_tree_is_a_valid_taskree_round_trip(self) -> None:
        import json
        import tempfile

        from waypoint.v1.tree import TaskTree

        units = _units(3, tokens=1000)
        provider = FakeProvider(
            [
                {
                    "children": [
                        {
                            "id": f"c{i}",
                            "brief": f"write unit {i}",
                            "unit_start": i,
                            "unit_end": i,
                            "estimated_calls": 3,
                            "shape": "prose-dominant",
                        }
                        for i in range(3)
                    ]
                }
            ]
        )
        tree = build_tree(units, provider, depth_cap=4, node_cap=100)
        with tempfile.TemporaryDirectory() as root_str:
            path = Path(root_str) / "tree.json"
            tree.save(path)
            reloaded = TaskTree.load(path)
            self.assertEqual(set(reloaded.nodes), set(tree.nodes))
            self.assertEqual(len(reloaded.ready_nodes()), 3)


if __name__ == "__main__":
    unittest.main()
