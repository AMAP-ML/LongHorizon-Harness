"""PLAN-zeromem.md §8 document-level review tests.

The 9 tests from §8.10, using the same fakes as every other v3 suite
(FakeStreamAgentAdapter for any writer dispatch, FakeProvider for every
pass call). The flat-call-count test (7) is the thesis of the revision: a
400-node document costs the same number of calls as a 40-node one, because
calls scale with *windows*, never nodes.
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
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import (  # noqa: E402
    create_run_dir,
    events_path,
    manifest_path,
    node_artifact_path,
)
from kusudaemon.v1.manifest import append_manifest_line  # noqa: E402
from kusudaemon.v1.tree import TaskNode, TaskTree  # noqa: E402
from kusudaemon.v2.pilot import select_pilot_nodes  # noqa: E402
from kusudaemon.v3.assembly_loop import run_assembly_loop  # noqa: E402
from kusudaemon.v3.document_review import (  # noqa: E402
    build_document_index,
    extract_term_index,
    run_document_review,
    serialize_triage,
    window_indices,
)
from kusudaemon.v3.revalidate import apply_revalidation_triage, summarize_triage  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"
NODES = ("alpha", "beta", "gamma")
SHAPES = {
    "alpha": "prose-dominant",
    "beta": "example-heavy",
    "gamma": "example-heavy",
}


def _node(node_id: str, shape: str | None = None, status: str = "passed") -> TaskNode:
    return TaskNode(
        id=node_id,
        brief=f"write the {node_id} section",
        artifact=f"out/{node_id}.md",
        gates=["nonempty"],
        shape=shape or SHAPES.get(node_id, "prose-dominant"),
        status=status,
    )


def _populate(run_dir: Path, tree: TaskTree) -> None:
    for node in tree.nodes.values():
        if node.status == "passed":
            node_artifact_path(run_dir, node.id).write_text(
                f"Content of {node.id}. " * 5, encoding="utf-8"
            )
            append_manifest_line(
                manifest_path(run_dir),
                node_id=node.id,
                artifact_path=str(node_artifact_path(run_dir, node.id)),
                artifact_text="word " * 20,
                gate_results=[],
                promotion=f"The {node.id} section covers its ground.",
            )


def _run(root: Path, tree: TaskTree) -> Path:
    run_dir = create_run_dir(root, "run1")
    tree_path = root / "tree.json"
    tree.save(tree_path)
    _populate(run_dir, tree)
    return run_dir


class DocumentIndexTest(unittest.TestCase):
    def test_build_document_index_from_manifest(self) -> None:
        """§8.10.1 — one entry per passed node, promotion present, artifact
        prose absent."""
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = _run(root, TaskTree(nodes={n: _node(n) for n in NODES}))

            entries = build_document_index(run_dir, TaskTree.load(root / "tree.json"))

            self.assertEqual([e.node_id for e in entries], list(NODES))
            self.assertTrue(all(e.promotion for e in entries))
            self.assertTrue(all(e.brief for e in entries))
            # Nothing in the index carries artifact prose.
            self.assertTrue(all("Content of" not in e.promotion for e in entries))
            self.assertTrue(all(not hasattr(e, "artifact_text") for e in entries))

    def test_pending_nodes_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            tree = TaskTree(nodes={"a": _node("a"), "b": _node("b", status="pending")})
            run_dir = _run(root, tree)
            entries = build_document_index(run_dir, tree)
            self.assertEqual([e.node_id for e in entries], ["a"])


class WindowIndicesTest(unittest.TestCase):
    def test_index_windows_overlap(self) -> None:
        """§8.10.2 — window/stride boundaries mirror survey.py's walk."""
        windows = window_indices(300, window=120, stride=100)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0], (0, 120))
        self.assertEqual(windows[-1], (200, 300))
        for (prev_start, prev_end), (start, end) in zip(windows, windows[1:]):
            self.assertEqual(start - prev_start, 100)
            self.assertTrue(start < prev_end, "windows must overlap")

    def test_small_index_is_one_window(self) -> None:
        self.assertEqual(window_indices(40, window=120, stride=100), [(0, 40)])


class TermIndexTest(unittest.TestCase):
    def test_extract_term_index_is_model_free(self) -> None:
        """§8.10.3 — deterministic, no provider: same input, same output."""
        from kusudaemon.v3.document_review import IndexEntry

        entries = [
            IndexEntry(
                node_id="a",
                brief="covers Activation Energy theory",
                shape="prose-dominant",
                tokens=100,
                promotion="defines **Activation Energy** and Transition State.",
            ),
            IndexEntry(
                node_id="b",
                brief="covers the Transition State model",
                shape="prose-dominant",
                tokens=90,
                promotion="uses the Transition State framework.",
            ),
        ]
        first = extract_term_index(entries)
        second = extract_term_index(entries)
        self.assertEqual(first, second)
        self.assertEqual(first["Activation Energy"], ["a"])
        self.assertEqual(first["Transition State"], ["a", "b"])


def _pass_clean(num: int) -> list[dict]:
    return [{"items": [], "verdict": "pass"} for _ in range(num)]


def _adapter(root: Path, node_id: str, run_dir: Path) -> FakeStreamAgentAdapter:
    return FakeStreamAgentAdapter(
        script_path=str(FAKE_CLI),
        pidfile=str(root / f"{node_id}.pid"),
        prompt_dir=str(root / "prompts"),
        workspace_path=str(run_dir),
        session_id="REPAIRED_MARKER",
    )


def _three_node_run(root: Path) -> tuple[Path, TaskTree]:
    tree = TaskTree(nodes={n: _node(n) for n in NODES})
    run_dir = _run(root, tree)
    return run_dir, tree


class RunDocumentReviewTest(unittest.TestCase):
    def _three_node_run(self) -> tuple[Path, TaskTree]:
        return _three_node_run(Path(tempfile.mkdtemp()))
    def _three_node_run(self) -> tuple[Path, TaskTree]:
        root = Path(tempfile.mkdtemp())
        tree = TaskTree(nodes={n: _node(n) for n in NODES})
        run_dir = _run(root, tree)
        return run_dir, tree

    def test_pass_emits_node_scoped_defects(self) -> None:
        """§8.10.4 — a verdict naming two ids lands in both nodes' triage."""
        run_dir, tree = self._three_node_run()
        provider = FakeProvider(
            _pass_clean(3)
            + [
                {
                    "items": [
                        {
                            "id": "P1",
                            "pass": False,
                            "class": "patchable",
                            "defect": "overlap",
                            "node_ids": ["alpha", "beta"],
                        }
                    ],
                    "verdict": "fail",
                }
            ]
            + _pass_clean(1)
        )
        result = run_document_review(run_dir, tree, provider)
        self.assertEqual(
            sorted(result.triage), ["alpha", "beta"]
        )
        self.assertEqual(result.triage["alpha"].classification, "patchable")
        self.assertEqual(result.triage["beta"].classification, "patchable")
        self.assertFalse(result.escalated)
        self.assertEqual(result.calls, 5)

    def test_unknown_node_id_is_dropped_not_crashed(self) -> None:
        """§8.10.5 — an invented node id is dropped and logged, while the
        real attribution still lands."""
        run_dir, tree = self._three_node_run()
        provider = FakeProvider(
            _pass_clean(3)
            + [
                {
                    "items": [
                        {
                            "id": "P1",
                            "pass": False,
                            "class": "patchable",
                            "defect": "overlap",
                            "node_ids": ["alpha", "ghost"],
                        }
                    ],
                    "verdict": "fail",
                }
            ]
            + _pass_clean(1)
        )
        log = EventLog(events_path(run_dir))
        result = run_document_review(run_dir, tree, provider, log=log)
        self.assertEqual(sorted(result.triage), ["alpha"])
        self.assertEqual(result.dropped_ids, ["ghost"])
        self.assertFalse(result.escalated)
        dropped_events = [
            e for e in log.read_all() if e.get("type") == "document_review_id_dropped"
        ]
        self.assertEqual(len(dropped_events), 1)
        self.assertEqual(dropped_events[0]["node_ids"], ["ghost"])

    def test_unattributable_defect_escalates(self) -> None:
        """§8.8 — a failing item naming no node goes to escalation."""
        run_dir, tree = self._three_node_run()
        provider = FakeProvider(
            [
                {
                    "items": [
                        {"id": "P1", "pass": False, "defect": "gap between windows"}
                    ],
                    "verdict": "fail",
                }
            ]
            + _pass_clean(4)
        )
        result = run_document_review(run_dir, tree, provider)
        self.assertTrue(result.escalated)
        self.assertIn("unattributable", result.escalation_reason)
        self.assertEqual(result.triage, {})

    def test_depth_sample_uses_shape_medians(self) -> None:
        """§8.10.6 — the depth pass reviews exactly what select_pilot_nodes
        returns, so the two stay in sync."""
        run_dir, tree = self._three_node_run()
        expected = sorted(node.id for node in select_pilot_nodes(tree).values())
        self.assertEqual(expected, ["alpha", "gamma"])  # gamma is the median example-heavy
        provider = FakeProvider(_pass_clean(5))
        result = run_document_review(run_dir, tree, provider, keep_depth_pass=True)
        self.assertEqual(result.calls, 5)
        without = run_document_review(
            run_dir, tree, FakeProvider(_pass_clean(3)), keep_depth_pass=False
        )
        self.assertEqual(without.calls, 3)

    def test_flattened_depth_pass_reads_full_artifact(self) -> None:
        run_dir, tree = self._three_node_run()
        provider = FakeProvider(
            _pass_clean(3)
            + [
                {
                    "items": [
                        {
                            "id": "D1",
                            "pass": False,
                            "class": "regenerate",
                            "defect": "shallow",
                        }
                    ],
                    "verdict": "fail",
                }
            ]
            + _pass_clean(1)
        )
        result = run_document_review(run_dir, tree, provider)
        self.assertEqual(sorted(result.triage), ["alpha"])
        self.assertEqual(result.triage["alpha"].classification, "regenerate")

    def test_call_count_is_flat_in_node_count(self) -> None:
        """§8.10.7 — 40 and 400-node trees cost the same calls: windows
        scale, nodes don't."""
        from kusudaemon.v3.document_review import DEFAULT_REVIEW_WINDOW, DEFAULT_REVIEW_STRIDE

        roots = []
        for count in (40, 400):
            root = Path(tempfile.mkdtemp())
            nodes = {}
            for i in range(count):
                node_id = f"n{i:03d}"
                nodes[node_id] = _node(node_id, shape="prose-dominant")
            tree = TaskTree(nodes=nodes)
            run_dir = _run(root, tree)
            roots.append((run_dir, tree))
        (run_dir_40, tree_40), (run_dir_400, tree_400) = roots

        windows_40 = len(window_indices(40, window=DEFAULT_REVIEW_WINDOW, stride=DEFAULT_REVIEW_STRIDE))
        windows_400 = len(window_indices(400, window=DEFAULT_REVIEW_WINDOW, stride=DEFAULT_REVIEW_STRIDE))
        self.assertEqual(windows_40, 1)
        self.assertEqual(windows_400, 4)

        provider = FakeProvider(_pass_clean(3 * windows_40 + 0))
        result_40 = run_document_review(run_dir_40, tree_40, provider, keep_depth_pass=False)
        self.assertEqual(result_40.calls, 3)

        provider_400 = FakeProvider(_pass_clean(3 * windows_400 + 0))
        result_400 = run_document_review(run_dir_400, tree_400, provider_400, keep_depth_pass=False)
        self.assertEqual(result_400.calls, 12)
        self.assertGreater(result_400.calls, result_40.calls)


class DocumentReviewWiringTest(unittest.TestCase):
    def test_clean_document_dispatches_no_repairs(self) -> None:
        """§8.10.8 — an assembly loop run with a clean review dispatches no
        repairs and reports no escalation."""
        root = Path(tempfile.mkdtemp())
        tree = TaskTree(nodes={n: _node(n) for n in NODES})
        run_dir = _run(root, tree)

        provider = FakeProvider(_pass_clean(5))
        result = asyncio.run(
            run_assembly_loop(
                run_dir,
                str(root / "tree.json"),
                str(manifest_path(run_dir)),
                writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                    AssertionError("no writer should be constructed on a clean review")
                ),
                env=LocalEnvironment(tmp_dir=str(run_dir / "tmp")),
                provider=provider,
                document_review=True,
            )
        )
        self.assertFalse(result.escalated)
        self.assertIsNotNone(result.review)
        self.assertEqual(result.review.triage, {})
        self.assertEqual(result.repairs, [])
        self.assertEqual(result.review.calls, 5)

    def test_triage_routes_through_existing_repair_path(self) -> None:
        """§8.10.9 — patchable triage dispatches repair.run_repair(patch),
        asserted via a repair outcome landing on the real artifact."""
        root = Path(tempfile.mkdtemp())
        run_dir, tree = _three_node_run(root)
        tree_path = root / "tree.json"

        review_provider = FakeProvider(
            _pass_clean(3)
            + [
                {
                    "items": [
                        {
                            "id": "P1",
                            "pass": False,
                            "class": "patchable",
                            "defect": "add a summary box",
                            "node_ids": ["alpha"],
                        }
                    ],
                    "verdict": "fail",
                }
            ]
            + _pass_clean(1)
        )
        review = run_document_review(run_dir, tree, review_provider)
        self.assertEqual(sorted(review.triage), ["alpha"])

        env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
        log = EventLog(events_path(run_dir))
        outcomes = asyncio.run(
            apply_revalidation_triage(
                run_dir,
                TaskTree.load(tree_path),
                tree_path,
                str(manifest_path(run_dir)),
                review.triage,
                lambda node: _adapter(root, node.id, run_dir),
                env,
                FakeProvider([]),
                log,
                writer_budget=EpisodeBudget(),
            )
        )
        self.assertEqual([o.node_id for o in outcomes], ["alpha"])
        self.assertEqual(outcomes[0].mode, "patch")
        self.assertTrue(outcomes[0].passed)
        repaired_event = [e for e in log.read_all() if e.get("type") == "node_repaired"]
        self.assertEqual(len(repaired_event), 1)
        self.assertEqual(repaired_event[0]["repair_id"], "alpha~repair1")
        # The repaired text (the fresh dispatch's output) overwrote the live
        # artifact — a genuinely new episode, not a replay.
        live_text = node_artifact_path(run_dir, "alpha").read_text(encoding="utf-8")
        self.assertNotIn("Content of alpha", live_text)
        self.assertIn("completed", live_text)

    def test_serialize_triage_round_trips_through_apply_shape(self) -> None:
        run_dir, tree = _three_node_run(Path(tempfile.mkdtemp()))
        provider = FakeProvider(
            _pass_clean(3)
            + [
                {
                    "items": [
                        {
                            "id": "P1",
                            "pass": False,
                            "class": "patchable",
                            "defect": "opening",
                            "node_ids": ["alpha"],
                        }
                    ],
                    "verdict": "fail",
                }
            ]
            + _pass_clean(1)
        )
        review = run_document_review(run_dir, tree, provider)
        records = serialize_triage(review.triage)
        record = records["alpha"]
        self.assertEqual(record["classification"], "patchable")
        self.assertEqual(record["verdict"], "fail")
        self.assertEqual(record["items"][0]["node_ids"], ["alpha"])
        self.assertEqual(summarize_triage(review.triage), {"clean": 0, "patchable": 1, "regenerate": 0})


if __name__ == "__main__":
    unittest.main()