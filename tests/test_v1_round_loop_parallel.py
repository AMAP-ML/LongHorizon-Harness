"""§C2 parallel-dispatch round-loop tests (PLAN.md §C2).

- ``max_parallel=1`` is the byte-identical event sequence to the pre-§C2
  loop: no wave fill (one ``node_dispatch_decided`` per round, provider
  consulted once per round), no event interleaving.
- ``max_parallel>1`` fills the wave from the ready set by code with **no
  extra provider calls**, dispatches episodes concurrently (proven by
  wall-clock overlap of two real subprocess fakes with a work delay),
  and resumes a crashed in-flight scan gathered in chunks.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from fake_provider import FakeProvider  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v1.run_dir import create_run_dir, events_path, manifest_path, tree_path  # noqa: E402
from kusudaemon.v1.round_loop import run_round_loop  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _write_tree(path: Path, nodes: list[dict]) -> None:
    path.write_text(json.dumps(nodes), encoding="utf-8")


def _adapter_factory(root: Path, run_dir: Path, prompt_dir: Path, work_delay: float = 0.0):
    def factory(node):
        return FakeStreamAgentAdapter(
            script_path=str(FAKE_CLI),
            pidfile=str(root / f"{node.id}.pid"),
            prompt_dir=str(prompt_dir),
            workspace_path=str(run_dir),
            work_delay=work_delay,
        )

    return factory


def _independent_tree(run_dir: Path, ids: list[str]) -> None:
    _write_tree(
        tree_path(run_dir),
        [
            {"id": node_id, "brief": node_id, "artifact": f"out/{node_id}.md", "gates": ["nonempty"]}
            for node_id in ids
        ],
    )


class MaxParallelOneByteIdentityTest(unittest.TestCase):
    def test_single_round_one_node_no_wave_fill_no_interleave(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            _independent_tree(run_dir, ["a", "b"])

            # PLAN-AUDIT.md §E18: round 1's ready set is {a, b} (both
            # independent, no depends_on) -> a real model call, still
            # needed to pick between them. Round 2's ready set is {b}
            # alone (a has since passed) -> code-decided, zero calls — so
            # only one canned response is queued now, not two.
            provider = FakeProvider(
                [
                    {"action": "dispatch", "node_id": "a", "reason": "a first"},
                ]
            )
            tree = asyncio_run(
                run_dir, prompt_dir, root, provider, max_parallel=1
            )
            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")
            self.assertEqual(len(provider.calls), 1)

            # Byte-identity: a's whole lifecycle (dispatch decision ...
            # episode completed) precedes b's dispatch decision — no
            # wave interleaving, no extra "wave fill" events.
            events = EventLog(events_path(run_dir)).read_all()
            decisions = [
                (e["node_id"], e["type"]) for e in events if e["type"] == "node_dispatch_decided"
            ]
            self.assertEqual(decisions, [("a", "node_dispatch_decided"), ("b", "node_dispatch_decided")])
            a_completed = max(
                i for i, e in enumerate(events) if e.get("node_id") == "a" and e["type"] == "episode_completed"
            )
            b_decided_idx = next(
                i for i, e in enumerate(events) if e.get("node_id") == "b" and e["type"] == "node_dispatch_decided"
            )
            self.assertLess(a_completed, b_decided_idx)
            self.assertFalse(any("parallel wave" in str(e) for e in events))

    def test_wave_fill_requires_max_parallel_gt_one(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run2")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            _independent_tree(run_dir, ["a", "b", "c"])

            # Deterministic policy: zero provider calls; the loop's one
            # decision per round is code-derived.
            provider = FakeProvider([])
            tree = asyncio_run(
                run_dir, prompt_dir, root, provider, max_parallel=1,
                dispatch_policy="document_order",
            )
            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")
            self.assertEqual(tree.nodes["c"].status, "passed")
            events = EventLog(events_path(run_dir)).read_all()
            decisions = [e for e in events if e["type"] == "node_dispatch_decided"]
            # One round per node: no node shares a round with another.
            self.assertEqual(len(decisions), 3)
            self.assertEqual(len({e["round"] for e in decisions}), 3)


class ParallelWaveTest(unittest.TestCase):
    def test_wave_dispatches_concurrently_with_no_extra_orchestrator_calls(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run3")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            _independent_tree(run_dir, ["a", "b", "c"])

            # Model policy, but ONLY one canned decision: the wave must
            # pick b and c from the ready set by code, zero extra calls.
            provider = FakeProvider(
                [{"action": "dispatch", "node_id": "a", "reason": "model picked a"}]
            )
            started = time.monotonic()
            tree = asyncio_run(
                run_dir, prompt_dir, root, provider, max_parallel=3,
                work_delay=0.3,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")
            self.assertEqual(tree.nodes["c"].status, "passed")
            self.assertEqual(len(provider.calls), 1)

            # Real concurrency: three 0.3s subprocess episodes on one wave
            # finish well before the 0.9s they'd need sequentially.
            self.assertLess(elapsed, 0.8)

            # All three share one round; the wave fill is recorded as such.
            events = EventLog(events_path(run_dir)).read_all()
            decisions = [e for e in events if e["type"] == "node_dispatch_decided"]
            self.assertEqual(len(decisions), 3)
            self.assertEqual(len({e["round"] for e in decisions}), 1)
            reasons = {e["node_id"]: e["reason"] for e in decisions}
            self.assertEqual(reasons["a"], "model picked a")
            self.assertIn("parallel wave fill", reasons["b"])
            self.assertIn("parallel wave fill", reasons["c"])

    def test_resume_scan_gathers_crashed_in_flight_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run4")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            # A crash left both nodes "dispatched" with no completion.
            _write_tree(
                tree_path(run_dir),
                [
                    {
                        "id": "a",
                        "brief": "a",
                        "artifact": "out/a.md",
                        "gates": ["nonempty"],
                        "status": "dispatched",
                    },
                    {
                        "id": "b",
                        "brief": "b",
                        "artifact": "out/b.md",
                        "gates": ["nonempty"],
                        "status": "dispatched",
                    },
                ],
            )
            provider = FakeProvider([])
            tree = asyncio_run(
                run_dir, prompt_dir, root, provider, max_parallel=2,
                dispatch_policy="document_order",
            )
            self.assertEqual(tree.nodes["a"].status, "passed")
            self.assertEqual(tree.nodes["b"].status, "passed")
            # Resume completed them without any new dispatch decision.
            events = EventLog(events_path(run_dir)).read_all()
            self.assertEqual([e for e in events if e["type"] == "node_dispatch_decided"], [])


def asyncio_run(run_dir, prompt_dir, root, provider, *, max_parallel, work_delay=0.0, dispatch_policy="model"):
    import asyncio

    return asyncio.run(
        run_round_loop(
            run_dir,
            tree_path(run_dir),
            writer_adapter_factory=_adapter_factory(root, run_dir, prompt_dir, work_delay),
            env=LocalEnvironment(tmp_dir=str(prompt_dir)),
            provider=provider,
            prompt_for_node=lambda node: f"do {node.id}",
            writer_budget=EpisodeBudget(max_duration_seconds=60),
            max_parallel=max_parallel,
            dispatch_policy=dispatch_policy,
        )
    )


if __name__ == "__main__":
    unittest.main()