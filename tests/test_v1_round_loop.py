"""v1 round-loop tests (PLAN.md §13 v1: "the round loop").

No network, no real `claude` binary, no API key: the Writer backend is
tests/fixtures/fake_stream_agent.py (same fixture v0's resumability proof
uses) and the Orchestrator/Reviewer backend is FakeProvider, a scripted
double that still validates every canned response against the schema it
was asked for.

Coverage:
- a two-node dependency chain runs end to end, in order, to "passed"
- a node whose gate can never pass exhausts retries and lands on "blocked",
  which escalates the run instead of looping forever
- calling run_round_loop twice resumes from tree.json: a "passed" node is
  never re-dispatched, and a node still "dispatched" from a simulated crash
  is resumed via v0's session-resume path directly, before the orchestrator
  is asked anything
- per-node tool restriction narrows the Writer's gptme tool allowlist
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
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402
from kusudaemon.adapters.gptme_adapter import GptmeAdapter  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v1.run_dir import create_run_dir, events_path, manifest_path, tree_path  # noqa: E402
from kusudaemon.v1.round_loop import run_round_loop  # noqa: E402
from kusudaemon.v1.tree import TaskTree  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _write_tree(path: Path, nodes: list[dict]) -> None:
    path.write_text(json.dumps(nodes), encoding="utf-8")


def _adapter_factory(root: Path, run_dir: Path, prompt_dir: Path):
    def factory(node):
        return FakeStreamAgentAdapter(
            script_path=str(FAKE_CLI),
            pidfile=str(root / f"{node.id}.pid"),
            prompt_dir=str(prompt_dir),
            workspace_path=str(run_dir),
        )

    return factory


class LinearChainRoundLoopTest(unittest.TestCase):
    def test_two_node_chain_runs_to_completion_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {"id": "a", "brief": "first", "artifact": "out/a.md", "gates": ["nonempty"]},
                    {
                        "id": "b",
                        "brief": "second",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )

            provider = FakeProvider(
                [
                    {"action": "dispatch", "node_id": "a", "reason": "a has no deps"},
                    {"action": "dispatch", "node_id": "b", "reason": "a passed"},
                ]
            )

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )

            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")
            # Both canned decisions were consumed and none left unused —
            # the loop halted on tree.is_complete() without a third call.
            self.assertEqual(len(provider.calls), 2)

            manifest_lines = manifest_path(run_dir).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(manifest_lines), 2)

            reloaded = TaskTree.load(tree_path(run_dir))
            self.assertEqual(reloaded.nodes["a"].status, "passed")
            self.assertEqual(reloaded.nodes["b"].status, "passed")


class GateFailureEscalatesTest(unittest.TestCase):
    def test_unsatisfiable_gate_blocks_then_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run2")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "impossible",
                        "artifact": "out/a.md",
                        "gates": ["len:9999-99999"],
                    }
                ],
            )

            provider = FakeProvider(
                [
                    {"action": "dispatch", "node_id": "a", "reason": "attempt 1"},
                    {"action": "dispatch", "node_id": "a", "reason": "attempt 2"},
                ]
            )

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    max_attempts=2,
                )
            )

            self.assertEqual(tree.nodes["a"].status, "blocked")
            self.assertEqual(tree.nodes["a"].attempts, 2)

            log = EventLog(events_path(run_dir))
            events = log.read_all()
            self.assertTrue(any(e["type"] == "run_escalated" for e in events))
            gate_failures = [e for e in events if e["type"] == "node_gate_failed"]
            self.assertEqual(len(gate_failures), 2)


class ResumeSkipsPassedNodesTest(unittest.TestCase):
    def test_second_call_does_not_redispatch_passed_node(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run3")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            _write_tree(
                tree_path(run_dir),
                [
                    {"id": "a", "brief": "first", "artifact": "out/a.md", "gates": ["nonempty"]},
                    {
                        "id": "b",
                        "brief": "second",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )

            adapter_factory = _adapter_factory(root, run_dir, prompt_dir)
            env = LocalEnvironment(tmp_dir=str(prompt_dir))
            budget = EpisodeBudget(max_duration_seconds=30)

            first_provider = FakeProvider(
                [
                    {"action": "dispatch", "node_id": "a", "reason": "start"},
                    # b just became ready but we halt anyway, standing in for
                    # a crash boundary right after a's completion.
                    {"action": "halt", "reason": "pretend crash boundary"},
                ]
            )
            asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=adapter_factory,
                    env=env,
                    provider=first_provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=budget,
                )
            )

            mid_tree = TaskTree.load(tree_path(run_dir))
            self.assertEqual(mid_tree.nodes["a"].status, "passed")
            self.assertEqual(mid_tree.nodes["b"].status, "pending")
            self.assertFalse((root / "b.pid").exists(), "node b's writer must not have run yet")

            log = EventLog(events_path(run_dir))
            completed_before = [
                e
                for e in log.read_all()
                if e.get("node_id") == "a" and e["type"] == "episode_completed"
            ]
            self.assertEqual(len(completed_before), 1)

            second_provider = FakeProvider(
                [{"action": "dispatch", "node_id": "b", "reason": "resume"}]
            )
            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=adapter_factory,
                    env=env,
                    provider=second_provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=budget,
                )
            )

            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")

            completed_after = [
                e
                for e in log.read_all()
                if e.get("node_id") == "a" and e["type"] == "episode_completed"
            ]
            self.assertEqual(
                len(completed_after), 1, "resume must not re-execute an already-passed node"
            )


class ResumeInFlightWriterNodeTest(unittest.TestCase):
    def test_dispatched_node_resumes_via_session_before_orchestrator_is_asked(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run4")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            # Simulate a crash that landed after node "a" was dispatched and
            # its session captured, but before the episode finished — the
            # same window v0's ResumeAfterSessionCrashTest proves survives a
            # real kill -9. Forging the same events.jsonl state here checks
            # that the *round loop* wires that recovery in correctly, not v0
            # itself (already proven in tests/test_v0_resume.py).
            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "first",
                        "artifact": "out/a.md",
                        "gates": ["nonempty"],
                        "status": "dispatched",
                    },
                    {
                        "id": "b",
                        "brief": "second",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "depends_on": ["a"],
                    },
                ],
            )
            log = EventLog(events_path(run_dir))
            log.append({"node_id": "a", "role": "writer", "round": 0, "type": "node_dispatched"})
            log.append(
                {
                    "node_id": "a",
                    "role": "writer",
                    "round": 0,
                    "type": "session_captured",
                    "session_id": "forged-session-1",
                }
            )

            provider = FakeProvider([{"action": "dispatch", "node_id": "b", "reason": "only b"}])

            tree = asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_path(run_dir),
                    writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir),
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                )
            )

            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")

            # The orchestrator was only ever asked about node b — node a's
            # recovery bypassed it entirely, as documented in round_loop.py.
            self.assertEqual(len(provider.calls), 1)

            all_events = [e for e in log.read_all() if e.get("node_id") == "a"]
            completed = [e for e in all_events if e["type"] == "episode_completed"]
            self.assertEqual(len(completed), 1)
            redispatches = [e for e in all_events if e["type"] == "node_redispatched"]
            self.assertTrue(any(e.get("reason") == "resumed_session" for e in redispatches))

            artifact_text = (run_dir / "out" / "a.md").read_text(encoding="utf-8")
            self.assertIn("resume_acknowledged", artifact_text)
            self.assertIn("forged-session-1", artifact_text)


class PerNodeToolRestrictionTest(unittest.TestCase):
    def test_tool_allowlist_reaches_the_command_line(self) -> None:
        adapter = GptmeAdapter(api_key="k", tool_allowlist=("shell", "read"))
        self.assertIn("--tool-allowlist", adapter.command_template)
        self.assertIn("shell", adapter.command_template)
        self.assertIn("read", adapter.command_template)

    def test_default_allowlist_used_when_not_narrowed(self) -> None:
        adapter = GptmeAdapter(api_key="k")
        self.assertIn("--tool-allowlist", adapter.command_template)
        self.assertIn("shell,read,save,patch", adapter.command_template)


if __name__ == "__main__":
    unittest.main()
