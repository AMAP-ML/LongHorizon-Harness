"""PLAN-zeromem.md §1: deterministic dispatch policy tests.

``decide_next_action_deterministic`` / ``decide_next_action_with_policy``
perform the orchestrator's halt/escalate arbitration in code and pick the
first ready node in document order, spending zero tokens. These tests
assert the arbitration branches match ``decide_next_action``'s, that the
``"model"`` policy still reaches the provider, and that the ``"document_order"``
policy never does (an empty ``FakeProvider`` queue raises if anything pops
it — the strictest way to prove zero calls).

Also covers §1.8: ``_compact_state`` is bounded by the ready-set size, not
the tree size.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402
from kusudaemon.v1.orchestrator import (  # noqa: E402
    decide_next_action_deterministic,
    decide_next_action_with_policy,
    _compact_state,
)
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402


def _tree(*nodes: TaskNode) -> TaskTree:
    return TaskTree(nodes={node.id: node for node in nodes})


def _node(node_id: str, *, status: str = "pending", **kwargs) -> TaskNode:
    return TaskNode(
        id=node_id,
        brief=f"brief for {node_id}",
        artifact=f"out/{node_id}.md",
        gates=["nonempty"],
        status=status,  # type: ignore[arg-type]
        **kwargs,
    )


def _ready_tree() -> TaskTree:
    return _tree(_node("a"), _node("b"), _node("c"))


class DocumentOrderPolicyTest(unittest.TestCase):
    def test_document_order_picks_first_ready(self) -> None:
        tree = _ready_tree()
        decision = decide_next_action_deterministic(tree, round_index=0)
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.node_id, "a")  # tree.json iteration order

    def test_document_order_respects_dependencies(self) -> None:
        # b depends_on a; a pending. ready_nodes() excludes b, so the policy
        # must pick a — dependency gating lives in the tree, not the model.
        tree = _tree(_node("a"), _node("b", depends_on=["a"]))
        decision = decide_next_action_deterministic(tree, round_index=0)
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.node_id, "a")

    def test_document_order_halts_when_complete(self) -> None:
        tree = _tree(_node("a", status="passed"), _node("b", status="passed"))
        decision = decide_next_action_deterministic(tree, round_index=1)
        self.assertEqual(decision.action, "halt")
        self.assertIsNone(decision.node_id)
        self.assertEqual(decision.reason, "all nodes passed")

    def test_document_order_escalates_when_blocked(self) -> None:
        tree = _tree(_node("a", status="blocked"))
        decision = decide_next_action_deterministic(tree, round_index=0)
        self.assertEqual(decision.action, "escalate")
        self.assertEqual(decision.reason, "no ready nodes and nothing in flight")

    def test_document_order_halts_when_in_flight(self) -> None:
        tree = _tree(_node("a", status="dispatched"))
        decision = decide_next_action_deterministic(tree, round_index=0)
        self.assertEqual(decision.action, "halt")
        self.assertEqual(decision.reason, "nodes in flight; nothing new to dispatch")


class PolicyDispatcherTest(unittest.TestCase):
    def test_policy_model_still_calls_provider(self) -> None:
        provider = FakeProvider(
            [{"action": "dispatch", "node_id": "b", "reason": "canned choice"}]
        )
        decision = decide_next_action_with_policy(
            _ready_tree(), "/nonexistent/manifest.jsonl", provider, round_index=0
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.node_id, "b")

    def test_policy_document_order_makes_zero_calls(self) -> None:
        provider = FakeProvider([])  # empty queue: any pop raises AssertionError
        decision = decide_next_action_with_policy(
            _ready_tree(), "/nonexistent/manifest.jsonl", provider,
            round_index=0, policy="document_order",
        )
        self.assertEqual(len(provider.calls), 0)
        self.assertEqual(decision.node_id, "a")

    def test_policy_model_requires_provider(self) -> None:
        with self.assertRaises(ValueError):
            decide_next_action_with_policy(
                _ready_tree(), "/nonexistent/manifest.jsonl", None, round_index=0
            )


class CompactStateBoundedTest(unittest.TestCase):
    def test_compact_state_bounded_by_ready_set(self) -> None:
        # 200-node tree with 3 ready nodes: the rendered state names all 3
        # ready nodes and stays small regardless of tree size (§1.8).
        nodes = {f"n{i:03d}": _node(f"n{i:03d}", status="passed") for i in range(200)}
        for i in range(3):
            nodes[f"n{i:03d}"].status = "pending"
        tree = TaskTree(nodes=nodes)
        ready = tree.ready_nodes()
        self.assertEqual(len(ready), 3)
        text = _compact_state(tree, ready, "/nonexistent/manifest.jsonl", 3)
        self.assertLess(len(text), 2_000)
        for node_id in ready:
            self.assertIn(node_id, text)


if __name__ == "__main__":
    unittest.main()