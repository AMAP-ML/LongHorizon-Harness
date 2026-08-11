"""Runtime recursive decomposition tests (PLAN.md §A8, §B5).

No network, no agent binary (CLAUDE.md Part III): every fake adapter here
writes directly to disk instead of shelling out. Covers §B5's own test
list:

- a proposal without measured overrun is rejected and the attempt is
  preserved (status/attempts untouched, a `split_rejected` event logged)
- a gapped/overlapping proposal is repaired, not trusted, with a harness
  event recording the repair
- a child failing `leaf_gate` rejects the *whole* proposal, not a partial
  accept
- depth/node caps refuse a structurally-valid proposal once either limit
  is hit
- a crash between graft and first child dispatch resumes correctly
- the parent's artifact equals the concatenation of its children once all
  children pass
- `check_split_parents_derived` catches a drifted parent artifact (see
  test_v3_checks.py)
- `v1/tree.py` accepts/rejects "split"/"parent" correctly (see
  test_v1_units.py)
- `v3/assemble.py` excludes split parents from top-level assembly (see
  test_v3_assemble.py)
- `TaskTree.is_complete()` treats "split" as complete (see test_v1_units.py)
- the `split_accepted` escalation trigger fires end to end for a T2 run
  (see test_v6_tiering.py)
- `v1/writer.py`'s prompt gains the split hint only when inputs exceed
  budget (see test_v1_units.py)

The ship-gate integration test below is the one PLAN.md §B5 itself
describes: "one real run where a leaf overruns, splits, and the final
artifact is complete."
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import (  # noqa: E402
    create_run_dir,
    ensure_node_scratch_dir,
    events_path,
    node_artifact_path,
)
from kusudaemon.v1.round_loop import run_round_loop  # noqa: E402
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree  # noqa: E402
from kusudaemon.v7.split import (  # noqa: E402
    SplitChildProposal,
    SplitProposal,
    evaluate_split,
    graft_split,
    handle_split_proposal,
    maybe_derive_split_parent,
    read_split_proposal,
)


def _node(node_id: str, **overrides) -> TaskNode:
    base = dict(
        id=node_id, brief=f"do {node_id}", artifact=f"out/{node_id}.md",
        gates=["nonempty"],
    )
    base.update(overrides)
    return TaskNode(**base)


def _write_split_json(run_dir: Path, node_id: str, reason: str, children: list[dict]) -> None:
    scratch = ensure_node_scratch_dir(run_dir, node_id)
    (scratch / "split.json").write_text(
        json.dumps({"reason": reason, "children": children}), encoding="utf-8"
    )


# ----------------------------------------------------------------------
# read_split_proposal
# ----------------------------------------------------------------------
class ReadSplitProposalTest(unittest.TestCase):
    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            self.assertIsNone(read_split_proposal(run_dir, "a"))

    def test_malformed_json_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            scratch = ensure_node_scratch_dir(run_dir, "a")
            (scratch / "split.json").write_text("not json{{{", encoding="utf-8")
            self.assertIsNone(read_split_proposal(run_dir, "a"))

    def test_missing_children_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            scratch = ensure_node_scratch_dir(run_dir, "a")
            (scratch / "split.json").write_text(json.dumps({"reason": "x"}), encoding="utf-8")
            self.assertIsNone(read_split_proposal(run_dir, "a"))

    def test_valid_proposal_parses(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            _write_split_json(
                run_dir, "a", "too big",
                [{"id": "x", "brief": "part x", "inputs": ["a.md"], "estimated_calls": 2}],
            )
            proposal = read_split_proposal(run_dir, "a")
            self.assertIsNotNone(proposal)
            self.assertEqual(proposal.reason, "too big")
            self.assertEqual(len(proposal.children), 1)
            self.assertEqual(proposal.children[0].id, "x")
            self.assertEqual(proposal.children[0].inputs, ("a.md",))


# ----------------------------------------------------------------------
# evaluate_split
# ----------------------------------------------------------------------
class EvaluateSplitTest(unittest.TestCase):
    def _big_node(self, **overrides) -> TaskNode:
        base = dict(
            id="big", brief="do big", artifact="out/big.md", gates=["nonempty"],
            inputs=["a.md", "b.md"], budget=NodeBudget(tokens=80, calls=15),
        )
        base.update(overrides)
        return TaskNode(**base)

    def _write_inputs(self, run_dir: Path, a_words: int = 40, b_words: int = 40) -> None:
        (run_dir / "a.md").write_text(" ".join(["word"] * a_words), encoding="utf-8")
        (run_dir / "b.md").write_text(" ".join(["word"] * b_words), encoding="utf-8")

    def test_no_measured_overrun_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            # Tiny inputs, generous budget -- no overrun.
            self._write_inputs(run_dir, a_words=2, b_words=2)
            node = self._big_node(budget=NodeBudget(tokens=24_000, calls=15))
            tree = TaskTree(nodes={node.id: node})
            proposal = SplitProposal(
                reason="x",
                children=(
                    SplitChildProposal(id="x", brief="part x", inputs=("a.md",), estimated_calls=2),
                    SplitChildProposal(id="y", brief="part y", inputs=("b.md",), estimated_calls=2),
                ),
            )
            decision = evaluate_split(run_dir, node, tree, proposal)
            self.assertFalse(decision.accepted)
            self.assertEqual(decision.reason, "no_measured_overrun")

    def test_overrun_via_prior_size_defect_even_with_small_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            self._write_inputs(run_dir, a_words=2, b_words=2)
            node = self._big_node(
                budget=NodeBudget(tokens=24_000, calls=15),
                last_defect="max_tokens:24000: ~30000 tokens, limit 24000",
            )
            tree = TaskTree(nodes={node.id: node})
            proposal = SplitProposal(
                reason="x",
                children=(
                    SplitChildProposal(id="x", brief="part x", inputs=("a.md",), estimated_calls=2),
                    SplitChildProposal(id="y", brief="part y", inputs=("b.md",), estimated_calls=2),
                ),
            )
            decision = evaluate_split(run_dir, node, tree, proposal)
            self.assertTrue(decision.accepted)

    def test_depth_cap_refuses_a_structurally_valid_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            self._write_inputs(run_dir)
            node = self._big_node(id="a.b.c", artifact="out/a.b.c.md")  # depth 2
            tree = TaskTree(nodes={node.id: node})
            proposal = SplitProposal(
                reason="x",
                children=(
                    SplitChildProposal(id="x", brief="part x", inputs=("a.md",), estimated_calls=2),
                    SplitChildProposal(id="y", brief="part y", inputs=("b.md",), estimated_calls=2),
                ),
            )
            decision = evaluate_split(run_dir, node, tree, proposal, depth_cap=2)
            self.assertFalse(decision.accepted)
            self.assertIn("depth_cap", decision.reason)

    def test_node_cap_refuses_a_structurally_valid_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            self._write_inputs(run_dir)
            node = self._big_node()
            other = _node("other")
            tree = TaskTree(nodes={node.id: node, other.id: other})
            proposal = SplitProposal(
                reason="x",
                children=(
                    SplitChildProposal(id="x", brief="part x", inputs=("a.md",), estimated_calls=2),
                    SplitChildProposal(id="y", brief="part y", inputs=("b.md",), estimated_calls=2),
                ),
            )
            decision = evaluate_split(run_dir, node, tree, proposal, node_cap=2)
            self.assertFalse(decision.accepted)
            self.assertIn("node_cap", decision.reason)

    def test_gapped_and_overlapping_proposal_is_repaired_to_tile_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            (run_dir / "a.md").write_text(" ".join(["word"] * 10), encoding="utf-8")
            (run_dir / "b.md").write_text(" ".join(["word"] * 10), encoding="utf-8")
            (run_dir / "c.md").write_text(" ".join(["word"] * 10), encoding="utf-8")
            # 30 words combined -> ~40 tokens > budget(30) -- overrun. Each
            # individual/paired file stays well within 30 once claimed by a
            # single child, so the repair below is what's under test here,
            # not leaf_gate.
            node = self._big_node(
                inputs=["a.md", "b.md", "c.md"], budget=NodeBudget(tokens=30, calls=15)
            )
            tree = TaskTree(nodes={node.id: node})
            # x claims a.md AND b.md (overlap-to-be with y); y also claims
            # b.md (duplicate -- first-claim-wins should drop it from y);
            # c.md is claimed by nobody (gap -- must be forced in).
            proposal = SplitProposal(
                reason="x",
                children=(
                    SplitChildProposal(id="x", brief="part x", inputs=("a.md", "b.md"), estimated_calls=2),
                    SplitChildProposal(id="y", brief="part y", inputs=("b.md",), estimated_calls=2),
                ),
            )
            decision = evaluate_split(run_dir, node, tree, proposal)
            self.assertTrue(decision.accepted, decision.reason)
            self.assertIsNotNone(decision.repair_detail)
            claimed: list[str] = []
            for child in decision.children:
                claimed.extend(child.inputs)
            # Every one of the parent's inputs claimed exactly once.
            self.assertEqual(sorted(claimed), ["a.md", "b.md", "c.md"])
            self.assertEqual(len(claimed), len(set(claimed)))

    def test_a_child_failing_leaf_gate_rejects_the_whole_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            (run_dir / "a.md").write_text(" ".join(["word"] * 500), encoding="utf-8")
            (run_dir / "b.md").write_text(" ".join(["word"] * 5), encoding="utf-8")
            # budget=80: b alone is fine, a alone blows the per-child ceiling.
            node = self._big_node(budget=NodeBudget(tokens=80, calls=15))
            tree = TaskTree(nodes={node.id: node})
            proposal = SplitProposal(
                reason="x",
                children=(
                    SplitChildProposal(id="x", brief="part x", inputs=("a.md",), estimated_calls=2),
                    SplitChildProposal(id="y", brief="part y", inputs=("b.md",), estimated_calls=2),
                ),
            )
            decision = evaluate_split(run_dir, node, tree, proposal)
            self.assertFalse(decision.accepted)
            self.assertIn("leaf_gate_failed", decision.reason)

    def test_child_count_out_of_bounds_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            # Tiny inputs, well within budget -- overrun is triggered via
            # the prior-size-defect path instead, so the single child below
            # trivially clears leaf_gate and this test isolates the count
            # check rather than accidentally re-testing leaf_gate.
            self._write_inputs(run_dir, a_words=2, b_words=2)
            node = self._big_node(
                inputs=["a.md", "b.md"], budget=NodeBudget(tokens=24_000, calls=15),
                last_defect="max_tokens:24000: ~30000 tokens, limit 24000",
            )
            tree = TaskTree(nodes={node.id: node})
            # One child claims every one of the parent's inputs -- a valid
            # tiling, but only 1 child: below SPLIT_MIN_CHILDREN=2.
            proposal = SplitProposal(
                reason="x",
                children=(
                    SplitChildProposal(
                        id="x", brief="part x", inputs=("a.md", "b.md"), estimated_calls=2
                    ),
                ),
            )
            decision = evaluate_split(run_dir, node, tree, proposal)
            self.assertFalse(decision.accepted)
            self.assertIn("child_count_out_of_bounds", decision.reason)

    def test_accepted_split_returns_the_repaired_children(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            self._write_inputs(run_dir)
            node = self._big_node()
            tree = TaskTree(nodes={node.id: node})
            proposal = SplitProposal(
                reason="too large for one episode",
                children=(
                    SplitChildProposal(id="a", brief="part a", inputs=("a.md",), estimated_calls=3),
                    SplitChildProposal(id="b", brief="part b", inputs=("b.md",), estimated_calls=3),
                ),
            )
            decision = evaluate_split(run_dir, node, tree, proposal)
            self.assertTrue(decision.accepted)
            self.assertEqual(len(decision.children), 2)


# ----------------------------------------------------------------------
# graft_split / handle_split_proposal
# ----------------------------------------------------------------------
class GraftSplitTest(unittest.TestCase):
    def test_children_get_dot_hierarchical_ids_and_copied_depends_on(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            dep = _node("dep", status="passed")
            node = _node(
                "big", inputs=["a.md", "b.md"], depends_on=["dep"],
                budget=NodeBudget(tokens=80, calls=15),
            )
            tree = TaskTree(nodes={dep.id: dep, node.id: node})
            tree_path = run_dir / "tree.json"
            tree.save(tree_path)
            log = EventLog(events_path(run_dir))
            children = [
                SplitChildProposal(id="a", brief="part a", inputs=("a.md",), estimated_calls=3),
                SplitChildProposal(id="b", brief="part b", inputs=("b.md",), estimated_calls=3),
            ]

            new_ids = graft_split(run_dir, node, tree, tree_path, children, log, reason="too big")

            self.assertEqual(new_ids, ["big.a", "big.b"])
            self.assertEqual(tree.nodes["big"].status, "split")
            for child_id in new_ids:
                child = tree.nodes[child_id]
                self.assertEqual(child.parent, "big")
                self.assertEqual(child.depends_on, ["dep"])
                self.assertEqual(child.status, "pending")
                self.assertEqual(child.artifact, f"out/{child_id}.md")

            events = log.read_all()
            split_events = [e for e in events if e["type"] == "node_split"]
            self.assertEqual(len(split_events), 1)
            self.assertEqual(split_events[0]["children"], new_ids)
            self.assertEqual(split_events[0]["reason"], "too big")

            reloaded = TaskTree.load(tree_path)
            self.assertEqual(reloaded.nodes["big"].status, "split")
            self.assertEqual(reloaded.nodes["big.a"].parent, "big")


class HandleSplitProposalTest(unittest.TestCase):
    def test_no_split_json_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            node = _node("a")
            tree = TaskTree(nodes={node.id: node})
            tree_path = run_dir / "tree.json"
            tree.save(tree_path)
            log = EventLog(events_path(run_dir))
            handled = handle_split_proposal(run_dir, node, tree, tree_path, log)
            self.assertFalse(handled)
            self.assertEqual(node.status, "pending")

    def test_rejected_proposal_preserves_attempts_and_reverts_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            (run_dir / "a.md").write_text("two words", encoding="utf-8")
            node = _node(
                "big", inputs=["a.md"], budget=NodeBudget(tokens=24_000, calls=15),
                status="dispatched", attempts=0,
            )
            tree = TaskTree(nodes={node.id: node})
            tree_path = run_dir / "tree.json"
            tree.save(tree_path)
            log = EventLog(events_path(run_dir))
            # No measured overrun -- must be rejected.
            _write_split_json(
                run_dir, "big", "feels big",
                [
                    {"id": "x", "brief": "part x", "inputs": ["a.md"], "estimated_calls": 2},
                    {"id": "y", "brief": "part y", "inputs": [], "estimated_calls": 2},
                ],
            )

            handled = handle_split_proposal(run_dir, node, tree, tree_path, log)

            self.assertTrue(handled)
            self.assertEqual(node.attempts, 0)
            self.assertEqual(node.status, "pending")
            events = log.read_all()
            rejected = [e for e in events if e["type"] == "split_rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["reason"], "no_measured_overrun")
            # Only the parent node exists -- nothing was grafted.
            self.assertEqual(set(tree.nodes.keys()), {"big"})

    def test_accepted_proposal_grafts_children(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            (run_dir / "a.md").write_text(" ".join(["word"] * 40), encoding="utf-8")
            (run_dir / "b.md").write_text(" ".join(["word"] * 40), encoding="utf-8")
            node = _node(
                "big", inputs=["a.md", "b.md"], budget=NodeBudget(tokens=80, calls=15),
                status="dispatched",
            )
            tree = TaskTree(nodes={node.id: node})
            tree_path = run_dir / "tree.json"
            tree.save(tree_path)
            log = EventLog(events_path(run_dir))
            _write_split_json(
                run_dir, "big", "too large for one episode",
                [
                    {"id": "a", "brief": "part a", "inputs": ["a.md"], "estimated_calls": 3},
                    {"id": "b", "brief": "part b", "inputs": ["b.md"], "estimated_calls": 3},
                ],
            )

            handled = handle_split_proposal(run_dir, node, tree, tree_path, log)

            self.assertTrue(handled)
            self.assertEqual(node.status, "split")
            self.assertEqual(set(tree.nodes.keys()), {"big", "big.a", "big.b"})


# ----------------------------------------------------------------------
# maybe_derive_split_parent
# ----------------------------------------------------------------------
class MaybeDeriveSplitParentTest(unittest.TestCase):
    def test_noop_when_node_has_no_parent(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            node = _node("a", status="passed")
            tree = TaskTree(nodes={node.id: node})
            log = EventLog(events_path(run_dir))
            maybe_derive_split_parent(run_dir, node, tree, run_dir / "tree.json", log)
            self.assertFalse(node_artifact_path(run_dir, "a").exists())

    def test_noop_until_every_sibling_has_passed(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            parent = _node("big", status="split")
            child_a = _node("big.a", status="passed", parent="big")
            child_b = _node("big.b", status="pending", parent="big")
            tree = TaskTree(nodes={n.id: n for n in [parent, child_a, child_b]})
            node_artifact_path(run_dir, "big.a").write_text("A body.", encoding="utf-8")
            log = EventLog(events_path(run_dir))

            maybe_derive_split_parent(run_dir, child_a, tree, run_dir / "tree.json", log)

            self.assertFalse(node_artifact_path(run_dir, "big").exists())

    def test_writes_derived_artifact_once_all_siblings_pass(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            parent = _node("big", status="split")
            child_a = _node("big.a", status="passed", parent="big")
            child_b = _node("big.b", status="passed", parent="big")
            tree = TaskTree(nodes={n.id: n for n in [parent, child_a, child_b]})
            node_artifact_path(run_dir, "big.a").write_text("A body.", encoding="utf-8")
            node_artifact_path(run_dir, "big.b").write_text("B body.", encoding="utf-8")
            log = EventLog(events_path(run_dir))

            maybe_derive_split_parent(run_dir, child_b, tree, run_dir / "tree.json", log)

            derived = node_artifact_path(run_dir, "big").read_text(encoding="utf-8")
            self.assertIn("A body.", derived)
            self.assertIn("B body.", derived)
            events = log.read_all()
            derived_events = [e for e in events if e["type"] == "split_parent_derived"]
            self.assertEqual(len(derived_events), 1)


# ----------------------------------------------------------------------
# Ship gate: a real leaf overruns, splits, and the final artifact is
# complete -- driven through run_round_loop end to end.
# ----------------------------------------------------------------------
class _SplitProposingAdapter:
    has_file_tools = True
    supports_session_resume = False

    def __init__(self, run_dir: Path, node_id: str, children: list[dict], reason: str) -> None:
        self._run_dir = run_dir
        self._node_id = node_id
        self._children = children
        self._reason = reason

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs) -> EpisodeResult:
        _write_split_json(self._run_dir, self._node_id, self._reason, self._children)
        return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})


class _ArtifactWritingAdapter:
    has_file_tools = True
    supports_session_resume = False

    def __init__(self, run_dir: Path, node_id: str, text: str) -> None:
        self._run_dir = run_dir
        self._node_id = node_id
        self._text = text

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs) -> EpisodeResult:
        node_artifact_path(self._run_dir, self._node_id).write_text(self._text, encoding="utf-8")
        return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})


def _ship_gate_adapter_factory(run_dir: Path):
    def factory(node: TaskNode):
        if node.id == "big":
            return _SplitProposingAdapter(
                run_dir=run_dir,
                node_id=node.id,
                reason="too large for one episode",
                children=[
                    {"id": "a", "brief": "handle part a", "inputs": ["part_a.md"], "estimated_calls": 3},
                    {"id": "b", "brief": "handle part b", "inputs": ["part_b.md"], "estimated_calls": 3},
                ],
            )
        return _ArtifactWritingAdapter(
            run_dir, node.id, f"# {node.id}\n\nReal generated content for {node.id}.\n"
        )

    return factory


class ShipGateSplitEndToEndTest(unittest.TestCase):
    def test_a_leaf_overruns_splits_and_the_final_artifact_is_complete(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = create_run_dir(Path(root_str), "run1")
                # 40 words each, ~53 tokens each (estimate_tokens); joined
                # ~106 tokens > budget.tokens=80 -- overrun. Each part alone
                # (~53 tokens) fits within the same 80-token ceiling every
                # grafted child inherits, so leaf_gate passes both.
                (run_dir / "part_a.md").write_text(" ".join(["word"] * 40), encoding="utf-8")
                (run_dir / "part_b.md").write_text(" ".join(["word"] * 40), encoding="utf-8")

                node = TaskNode(
                    id="big", brief="write the whole thing", artifact="out/big.md",
                    gates=["nonempty"], inputs=["part_a.md", "part_b.md"],
                    budget=NodeBudget(tokens=80, calls=15),
                )
                tree = TaskTree(nodes={node.id: node})
                tree_path = run_dir / "tree.json"
                tree.save(tree_path)

                result_tree = await run_round_loop(
                    run_dir,
                    tree_path,
                    writer_adapter_factory=_ship_gate_adapter_factory(run_dir),
                    env=LocalEnvironment(tmp_dir=str(run_dir / "tmp")),
                    provider=None,  # type: ignore[arg-type]
                    prompt_for_node=lambda n: f"do {n.id}",
                    dispatch_policy="document_order",
                    split_handler=handle_split_proposal,
                    on_node_passed=maybe_derive_split_parent,
                )

                self.assertEqual(result_tree.nodes["big"].status, "split")
                self.assertEqual(result_tree.nodes["big.a"].status, "passed")
                self.assertEqual(result_tree.nodes["big.b"].status, "passed")
                # Dot-hierarchical ids -- this is what makes the split
                # visible in the dashboard's task tree for free (it already
                # groups by dot-path, CLAUDE.md).
                self.assertTrue(result_tree.nodes["big.a"].id.startswith("big."))
                self.assertTrue(result_tree.nodes["big.b"].id.startswith("big."))

                derived = node_artifact_path(run_dir, "big").read_text(encoding="utf-8")
                child_a_text = node_artifact_path(run_dir, "big.a").read_text(encoding="utf-8")
                child_b_text = node_artifact_path(run_dir, "big.b").read_text(encoding="utf-8")
                self.assertIn(child_a_text.strip(), derived)
                self.assertIn(child_b_text.strip(), derived)

                self.assertTrue(result_tree.is_complete())
                self.assertFalse(result_tree.is_blocked())

        asyncio.run(scenario())


class CrashResumeAfterGraftTest(unittest.TestCase):
    """PLAN.md §B5: "a crash between graft and first child dispatch resumes
    correctly" -- write the post-graft, pre-child-dispatch state directly
    (parent already "split", children present but still "pending", exactly
    what a real crash would leave on disk) and confirm a fresh
    ``run_round_loop`` call picks the children up and completes normally,
    same resumability spirit as every other crash-resume test in this
    suite."""

    def test_resumes_and_completes_from_a_partially_grafted_tree(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = create_run_dir(Path(root_str), "run1")
                parent = TaskNode(
                    id="big", brief="write the whole thing", artifact="out/big.md",
                    gates=["nonempty"], status="split",
                    budget=NodeBudget(tokens=80, calls=15),
                )
                child_a = TaskNode(
                    id="big.a", brief="handle part a", artifact="out/big.a.md",
                    gates=["nonempty"], parent="big", status="pending",
                )
                child_b = TaskNode(
                    id="big.b", brief="handle part b", artifact="out/big.b.md",
                    gates=["nonempty"], parent="big", status="pending",
                )
                tree = TaskTree(nodes={n.id: n for n in [parent, child_a, child_b]})
                tree_path = run_dir / "tree.json"
                tree.save(tree_path)

                result_tree = await run_round_loop(
                    run_dir,
                    tree_path,
                    writer_adapter_factory=_ship_gate_adapter_factory(run_dir),
                    env=LocalEnvironment(tmp_dir=str(run_dir / "tmp")),
                    provider=None,  # type: ignore[arg-type]
                    prompt_for_node=lambda n: f"do {n.id}",
                    dispatch_policy="document_order",
                    split_handler=handle_split_proposal,
                    on_node_passed=maybe_derive_split_parent,
                )

                self.assertEqual(result_tree.nodes["big"].status, "split")
                self.assertEqual(result_tree.nodes["big.a"].status, "passed")
                self.assertEqual(result_tree.nodes["big.b"].status, "passed")
                derived = node_artifact_path(run_dir, "big").read_text(encoding="utf-8")
                self.assertIn("big.a", derived)
                self.assertIn("big.b", derived)
                self.assertTrue(result_tree.is_complete())

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
