"""End-to-end assembly-loop tests (PLAN.md §4.6): assemble -> checks ->
compile -> (on failure) scoped repair -> reassemble -> recompile.

No LaTeX toolchain: the "compiler" is a shell one-liner that greps the
assembled output for a marker string and, on failure, echoes the offending
node's artifact filename (exactly the shape a real compiler's error log
would take -- "here is the file that broke") so
``assembly_loop.find_offending_nodes`` can attribute the failure. The
Writer backend for the repair dispatch is the same fake_stream_agent.py
double used everywhere else; its ``--session-id`` becomes the literal
marker string, giving a deterministic way to prove the repaired content
actually made it through assemble -> compile.
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
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, manifest_path, node_artifact_path  # noqa: E402
from kusudaemon.v1.gates import evaluate_gates  # noqa: E402
from kusudaemon.v1.manifest import append_manifest_line  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v3.assembly_loop import find_offending_nodes, run_assembly_loop  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _node(node_id: str, *, gates: list[str] | None = None, status: str = "passed") -> TaskNode:
    return TaskNode(
        id=node_id, brief=f"write {node_id}", artifact=f"out/{node_id}.md",
        gates=gates or ["nonempty"], status=status,
    )


class FindOffendingNodesTest(unittest.TestCase):
    def test_matches_by_artifact_filename_in_document_order(self) -> None:
        tree = TaskTree(nodes={n.id: n for n in [_node("a"), _node("b"), _node("c")]})
        log_text = "error in out/c.md: undefined ref\nsee also out/a.md"
        self.assertEqual(find_offending_nodes(tree, log_text), ["a", "c"])

    def test_no_match_returns_empty(self) -> None:
        tree = TaskTree(nodes={n.id: n for n in [_node("a")]})
        self.assertEqual(find_offending_nodes(tree, "generic linker error"), [])


class AssemblyLoopTest(unittest.TestCase):
    def _seed_run(self, root: Path, node: TaskNode, text: str) -> Path:
        run_dir = create_run_dir(root, "run1")
        node_artifact_path(run_dir, node.id).write_text(text, encoding="utf-8")
        append_manifest_line(
            manifest_path(run_dir), node_id=node.id,
            artifact_path=str(node_artifact_path(run_dir, node.id)),
            artifact_text=text, gate_results=evaluate_gates(node.gates, text),
            promotion="seed",
        )
        return run_dir

    def test_escalates_when_checks_fail_before_compiling(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            node = _node("a")
            run_dir = self._seed_run(root, node, "content")
            tree = TaskTree(nodes={"a": node, "b": _node("b", status="pending")})
            tree_path = root / "tree.json"
            tree.save(tree_path)

            result = asyncio.run(
                run_assembly_loop(
                    run_dir, tree_path, manifest_path(run_dir),
                    writer_adapter_factory=lambda n: (_ for _ in ()).throw(AssertionError("no dispatch expected")),
                    env=LocalEnvironment(), provider=FakeProvider([]),
                )
            )

            self.assertTrue(result.escalated)
            self.assertIn("checks failed", result.escalation_reason)
            self.assertIsNone(result.assembly)

    def test_compile_failure_repairs_offending_node_then_recompiles_clean(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            node = _node("n1")
            run_dir = self._seed_run(root, node, "content missing the marker")
            tree = TaskTree(nodes={"n1": node})
            tree_path = root / "tree.json"
            tree.save(tree_path)
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            compile_command = (
                "grep -q PATCHED_CONTENT main.md "
                "|| (echo 'error: n1.md missing marker' && exit 1)"
            )

            def adapter_factory(repair_node: TaskNode) -> FakeStreamAgentAdapter:
                return FakeStreamAgentAdapter(
                    script_path=str(FAKE_CLI),
                    pidfile=str(root / f"{repair_node.id}.pid"),
                    prompt_dir=str(prompt_dir),
                    workspace_path=str(run_dir),
                    session_id="PATCHED_CONTENT",
                )

            result = asyncio.run(
                run_assembly_loop(
                    run_dir, tree_path, manifest_path(run_dir),
                    writer_adapter_factory=adapter_factory,
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=FakeProvider([]),
                    compile_command=compile_command,
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )

            self.assertFalse(result.escalated)
            self.assertTrue(result.compile_result.passed)
            self.assertEqual(len(result.repairs), 1)
            self.assertTrue(result.repairs[0].passed)

            final_text = node_artifact_path(run_dir, "n1").read_text(encoding="utf-8")
            self.assertIn("PATCHED_CONTENT", final_text)

            reloaded = TaskTree.load(tree_path)
            self.assertEqual(reloaded.nodes["n1"].status, "passed")

    def test_compile_failure_unattributable_escalates_without_repairing(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            node = _node("n1")
            run_dir = self._seed_run(root, node, "content")
            tree = TaskTree(nodes={"n1": node})
            tree_path = root / "tree.json"
            tree.save(tree_path)

            result = asyncio.run(
                run_assembly_loop(
                    run_dir, tree_path, manifest_path(run_dir),
                    writer_adapter_factory=lambda n: (_ for _ in ()).throw(AssertionError("no dispatch expected")),
                    env=LocalEnvironment(),
                    provider=FakeProvider([]),
                    compile_command="echo 'totally generic linker error' && exit 1",
                )
            )

            self.assertTrue(result.escalated)
            self.assertEqual(len(result.repairs), 0)
            self.assertIn("could not be attributed", result.escalation_reason)

    # §11.7: a repair whose dispatch fails leaves the node stale/blocked, so
    # the re-assemble *raises* AssemblyNotReadyError — it must escalate like
    # every other not-ready state instead of escaping the loop as an
    # uncaught exception.

    def test_failed_repair_escalates_on_reassemble_not_raises(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            node = _node("n1")
            run_dir = self._seed_run(root, node, "content missing the marker")
            tree = TaskTree(nodes={"n1": node})
            tree_path = root / "tree.json"
            tree.save(tree_path)

            def broken_factory(repair_node: TaskNode) -> FakeStreamAgentAdapter:
                return FakeStreamAgentAdapter(
                    script_path=str(root / "no-such-agent.py"),
                    pidfile=str(root / f"{repair_node.id}.pid"),
                    prompt_dir=str(root / "prompts"),
                    workspace_path=str(run_dir),
                )

            result = asyncio.run(
                run_assembly_loop(
                    run_dir, tree_path, manifest_path(run_dir),
                    writer_adapter_factory=broken_factory,
                    env=LocalEnvironment(),
                    provider=FakeProvider([]),
                    compile_command=(
                        "grep -q PATCHED_CONTENT main.md "
                        "|| (echo 'error: n1.md missing marker' && exit 1)"
                    ),
                    max_repairs=1,
                    max_attempts=1,
                )
            )

            self.assertTrue(result.escalated)
            self.assertIn("not yet passed", result.escalation_reason)

            reloaded = TaskTree.load(tree_path)
            self.assertNotEqual(reloaded.nodes["n1"].status, "passed")


if __name__ == "__main__":
    unittest.main()
