"""The v4 entrypoint (PLAN.md §13 v4): resolve every planned research query
before any Writer dispatch, folding capped findings into the target node's
``inputs`` (§6 — no new tree.json field, a finding is just another input).

Coverage:
- attach_finding: a passing finding's path lands in node.inputs, a failing
  one (empty retrieval) is skipped, and re-attaching the same path never
  duplicates it
- run_research_loop end to end: queries dispatch, findings land in
  node.inputs, and the mutation is durable on tree.json on disk
- an unknown node id in the plan fails loudly instead of silently no-oping
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

from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir  # noqa: E402
from kusudaemon.v1.gates import GateResult  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v4.research import ResearchFinding, ResearchQuery  # noqa: E402
from kusudaemon.v4.research_loop import attach_finding, run_research_loop  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _node(node_id: str) -> TaskNode:
    return TaskNode(id=node_id, brief=f"write {node_id}", artifact=f"out/{node_id}.md", gates=["nonempty"])


class AttachFindingTest(unittest.TestCase):
    def test_passing_finding_is_attached(self) -> None:
        node = _node("ch07")
        finding = ResearchFinding(
            node_id="ch07", slug="q1", kind="web_search", text="Gauss's law relates flux to enclosed charge.",
            finding_path=Path("/tmp/does-not-matter.md"), gate_results=[GateResult("nonempty", True)],
        )
        self.assertTrue(attach_finding(node, finding))
        self.assertEqual(node.inputs, [str(finding.finding_path)])

    def test_failing_finding_is_skipped(self) -> None:
        node = _node("ch07")
        finding = ResearchFinding(
            node_id="ch07", slug="q1", kind="web_search", text="",
            finding_path=Path("/tmp/does-not-matter.md"),
            gate_results=[GateResult("nonempty", False, "artifact is empty")],
        )
        self.assertFalse(attach_finding(node, finding))
        self.assertEqual(node.inputs, [])

    def test_attaching_twice_does_not_duplicate(self) -> None:
        node = _node("ch07")
        finding = ResearchFinding(
            node_id="ch07", slug="q1", kind="web_search", text="finding text",
            finding_path=Path("/tmp/does-not-matter.md"), gate_results=[GateResult("nonempty", True)],
        )
        attach_finding(node, finding)
        attach_finding(node, finding)
        self.assertEqual(node.inputs, [str(finding.finding_path)])


class RunResearchLoopTest(unittest.TestCase):
    def _adapter(self, root: Path, prompt_dir: Path, run_dir: Path, tag: str, session_id: str) -> FakeStreamAgentAdapter:
        return FakeStreamAgentAdapter(
            script_path=str(FAKE_CLI),
            pidfile=str(root / f"{tag}.pid"),
            prompt_dir=str(prompt_dir),
            workspace_path=str(run_dir),
            session_id=session_id,
        )

    def test_findings_land_in_node_inputs_and_persist_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            tree_path = root / "tree.json"
            node = _node("ch07")
            TaskTree(nodes={node.id: node}).save(tree_path)

            plan = {
                "ch07": [ResearchQuery(slug="gauss-law", kind="web_search", question="What is Gauss's law?")]
            }
            counter = {"n": 0}

            def adapter_factory(node: TaskNode, query: ResearchQuery) -> FakeStreamAgentAdapter:
                counter["n"] += 1
                return self._adapter(root, prompt_dir, run_dir, f"q{counter['n']}", "FOUND_IT")

            results = asyncio.run(
                run_research_loop(
                    run_dir, tree_path, plan, adapter_factory,
                    LocalEnvironment(tmp_dir=str(prompt_dir)), EpisodeBudget(max_duration_seconds=30),
                )
            )

            self.assertEqual(len(results["ch07"]), 1)
            finding = results["ch07"][0]
            self.assertIn("FOUND_IT", finding.text)

            reloaded = TaskTree.load(tree_path)
            self.assertEqual(reloaded.nodes["ch07"].inputs, [str(finding.finding_path)])

    def test_unknown_node_in_plan_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            tree_path = root / "tree.json"
            TaskTree(nodes={"ch07": _node("ch07")}).save(tree_path)

            plan = {
                "ch99": [ResearchQuery(slug="q", kind="web_search", question="?")]
            }

            def adapter_factory(node: TaskNode, query: ResearchQuery) -> FakeStreamAgentAdapter:
                raise AssertionError("should never be called for an unknown node")

            with self.assertRaises(KeyError):
                asyncio.run(
                    run_research_loop(
                        run_dir, tree_path, plan, adapter_factory,
                        LocalEnvironment(tmp_dir=str(prompt_dir)), EpisodeBudget(max_duration_seconds=30),
                    )
                )


if __name__ == "__main__":
    unittest.main()
