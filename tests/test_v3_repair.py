"""Scoped repair tests (PLAN.md §4.5, §4.6). Writer backend is the same
fake_stream_agent.py double v0/v1/v2 already use; Reviewer backend is
FakeProvider.

Coverage:
- repair dispatches a genuinely new episode under a derived id, not a
  no-op replay of the node's already-completed original episode (the
  thing v0's run_node would do if called again under the same node id)
- the repaired content lands on the real artifact path, and the pre-repair
  content is snapshotted to out/.versions/ first
- a repair that still fails its gates does not touch the live artifact,
  and increments attempts toward max_attempts -> "blocked"
- a node with judgment items routes the repaired artifact through the
  Reviewer before it's allowed back to "passed"
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
from waypoint.environment.local import LocalEnvironment  # noqa: E402
from waypoint.types import EpisodeBudget  # noqa: E402
from waypoint.v0.events import EventLog  # noqa: E402
from waypoint.v0.run_dir import create_run_dir, events_path, manifest_path, node_artifact_path  # noqa: E402
from waypoint.v1.manifest import append_manifest_line  # noqa: E402
from waypoint.v1.gates import evaluate_gates  # noqa: E402
from waypoint.v1.tree import TaskNode, TaskTree  # noqa: E402
from waypoint.v3.repair import repair_node_id, run_repair  # noqa: E402
from waypoint.v3.run_dir import version_snapshot_path  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _node(node_id: str, *, gates: list[str] | None = None, judgment: list[str] | None = None) -> TaskNode:
    return TaskNode(
        id=node_id, brief=f"write {node_id}", artifact=f"out/{node_id}.md",
        gates=gates or ["nonempty"], judgment=judgment or [], status="passed",
    )


class RepairTest(unittest.TestCase):
    def _adapter(self, root: Path, prompt_dir: Path, run_dir: Path, node_id: str, session_id: str) -> FakeStreamAgentAdapter:
        return FakeStreamAgentAdapter(
            script_path=str(FAKE_CLI),
            pidfile=str(root / f"{node_id}.pid"),
            prompt_dir=str(prompt_dir),
            workspace_path=str(run_dir),
            session_id=session_id,
        )

    def _seed(self, root: Path, node: TaskNode, original_text: str) -> tuple[Path, EventLog]:
        run_dir = create_run_dir(root, "run1")
        node_artifact_path(run_dir, node.id).write_text(original_text, encoding="utf-8")
        log = EventLog(events_path(run_dir))
        # A node reaching repair.py already has an episode_completed event
        # from its original (successful) dispatch -- forge that, since
        # that's the exact precondition run_repair exists to work around
        # (v0's run_node would no-op-replay a second call under this same
        # node id).
        log.append({
            "node_id": node.id, "role": "writer", "round": 0, "type": "node_dispatched",
        })
        log.append({
            "node_id": node.id, "role": "writer", "round": 0, "type": "episode_completed",
            "status": "done", "artifact_path": str(node_artifact_path(run_dir, node.id)),
            "error": None, "duration_ms": 1,
        })
        m_path = manifest_path(run_dir)
        append_manifest_line(
            m_path, node_id=node.id, artifact_path=str(node_artifact_path(run_dir, node.id)),
            artifact_text=original_text, gate_results=evaluate_gates(node.gates, original_text),
            promotion="original",
        )
        return run_dir, log

    def test_repair_dispatches_fresh_episode_and_overwrites_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            node = _node("ch01")
            run_dir, log = self._seed(root, node, "ORIGINAL CONTENT")
            tree_path = root / "tree.json"
            tree = TaskTree(nodes={node.id: node})
            tree.save(tree_path)
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            adapter = self._adapter(root, prompt_dir, run_dir, node.id, "REPAIRED_MARKER")
            provider = FakeProvider([])  # no judgment items -> reviewer auto-passes, no call

            outcome = asyncio.run(
                run_repair(
                    run_dir, node, tree, tree_path, manifest_path(run_dir),
                    "§1, missing marker", adapter,
                    LocalEnvironment(tmp_dir=str(prompt_dir)), EpisodeBudget(max_duration_seconds=30),
                    provider, log,
                )
            )

            self.assertTrue(outcome.passed)
            self.assertEqual(outcome.status, "passed")
            self.assertEqual(node.status, "passed")
            self.assertEqual(outcome.repair_id, repair_node_id("ch01", 1))

            # A genuinely new episode ran under the derived id -- not a
            # no-op replay of ch01's original completed event.
            self.assertIsNotNone(log.last_event(outcome.repair_id, "episode_completed"))

            live_text = node_artifact_path(run_dir, "ch01").read_text(encoding="utf-8")
            self.assertIn("REPAIRED_MARKER", live_text)
            self.assertNotIn("ORIGINAL CONTENT", live_text)

            snapshot_path = version_snapshot_path(run_dir, "ch01", outcome.repair_id)
            self.assertEqual(snapshot_path, outcome.snapshot_path)
            self.assertEqual(snapshot_path.read_text(encoding="utf-8"), "ORIGINAL CONTENT")

            # tree.json on disk reflects the transition too.
            reloaded = TaskTree.load(tree_path)
            self.assertEqual(reloaded.nodes["ch01"].status, "passed")

    def test_repair_that_still_fails_gates_leaves_live_artifact_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            node = _node("ch02", gates=["contains:UNOBTAINABLE_STRING"])
            run_dir, log = self._seed(root, node, "ORIGINAL")
            tree_path = root / "tree.json"
            tree = TaskTree(nodes={node.id: node})
            tree.save(tree_path)
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            adapter = self._adapter(root, prompt_dir, run_dir, node.id, "STILL_MISSING_IT")
            provider = FakeProvider([])

            outcome = asyncio.run(
                run_repair(
                    run_dir, node, tree, tree_path, manifest_path(run_dir),
                    "scoped defect", adapter,
                    LocalEnvironment(tmp_dir=str(prompt_dir)), EpisodeBudget(max_duration_seconds=30),
                    provider, log, max_attempts=3,
                )
            )

            self.assertFalse(outcome.passed)
            self.assertEqual(outcome.status, "stale")
            self.assertEqual(node.attempts, 1)
            self.assertEqual(
                node_artifact_path(run_dir, "ch02").read_text(encoding="utf-8"), "ORIGINAL"
            )

    def test_repeated_gate_failure_exhausts_attempts_to_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            node = _node("ch03", gates=["contains:UNOBTAINABLE_STRING"])
            run_dir, log = self._seed(root, node, "ORIGINAL")
            tree_path = root / "tree.json"
            tree = TaskTree(nodes={node.id: node})
            tree.save(tree_path)
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            provider = FakeProvider([])
            env = LocalEnvironment(tmp_dir=str(prompt_dir))
            budget = EpisodeBudget(max_duration_seconds=30)

            for attempt in range(1, 3):
                adapter = self._adapter(root, prompt_dir, run_dir, f"{node.id}-{attempt}", "still no marker")
                outcome = asyncio.run(
                    run_repair(
                        run_dir, node, tree, tree_path, manifest_path(run_dir),
                        "scoped defect", adapter, env, budget, provider, log, max_attempts=2,
                    )
                )

            self.assertFalse(outcome.passed)
            self.assertEqual(outcome.status, "blocked")
            self.assertEqual(node.attempts, 2)

    def test_repair_with_judgment_routes_through_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            node = _node("ch04", judgment=["R1"])
            node.rubric = {"R1": "must be thorough"}
            run_dir, log = self._seed(root, node, "ORIGINAL")
            tree_path = root / "tree.json"
            tree = TaskTree(nodes={node.id: node})
            tree.save(tree_path)
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            adapter = self._adapter(root, prompt_dir, run_dir, node.id, "REPAIRED_MARKER")
            provider = FakeProvider([{"items": [{"id": "R1", "pass": True}], "verdict": "pass"}])

            outcome = asyncio.run(
                run_repair(
                    run_dir, node, tree, tree_path, manifest_path(run_dir),
                    "scoped defect", adapter,
                    LocalEnvironment(tmp_dir=str(prompt_dir)), EpisodeBudget(max_duration_seconds=30),
                    provider, log,
                )
            )

            self.assertTrue(outcome.passed)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(outcome.verdict.verdict, "pass")


if __name__ == "__main__":
    unittest.main()
