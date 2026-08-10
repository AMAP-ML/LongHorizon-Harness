"""PLAN-zeromem.md §4.6 tests for v2/retrieval.py.

BM25 is stdlib; dense fusion is exercised via the injected ``dense`` seam
(fake vectors) — nothing here needs `kusudaemon[retrieval]` installed. The
run dirs are hand-built: ``chunks.jsonl`` via ``build_chunk_index``, plus a
``spine.json`` so unit scoping resolves.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v1.tree import TaskNode  # noqa: E402
from kusudaemon.v2.retrieval import (  # noqa: E402
    build_chunk_index,
    retrieve_spans,
)
from kusudaemon.v2.run_dir import chunk_index_path  # noqa: E402
from kusudaemon.v2.survey import Chunk, SpineUnit, save_spine  # noqa: E402

_TEXT_BLOCKS = [
    "python experts discuss the history of python language design",
    "python syntax evolved through several versions",
    "python semantics shaped by the community",
    "selenium is a web automation tool written for python",
    "selenium drives browsers through the webdriver protocol",
    "selenium tests run headless in containers",
    "pandas is a data analysis library",
    "pandas dataframes store tabular data",
    "pandas integrates with matplotlib for plotting",
]

_UNITS = [
    SpineUnit(id="unit-01", label="Python", start_chunk=0, end_chunk=2, tokens=30),
    SpineUnit(id="unit-02", label="Selenium", start_chunk=3, end_chunk=5, tokens=30),
    SpineUnit(id="unit-03", label="Pandas", start_chunk=6, end_chunk=8, tokens=30),
]


def _build_run(root: Path) -> Path:
    run_dir = root / "run"
    run_dir.mkdir()
    chunks = [
        Chunk(index=i, text=text, tokens=len(text.split()))
        for i, text in enumerate(_TEXT_BLOCKS)
    ]
    build_chunk_index(run_dir, chunks, _UNITS)
    save_spine(run_dir, _UNITS)
    return run_dir


def _node(unit: str, inputs: list[str] | None = None) -> TaskNode:
    return TaskNode(
        id="a",
        brief="Write about the topic.",
        artifact="out/a.md",
        gates=["nonempty"],
        inputs=inputs or [unit],
    )


class RetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = _build_run(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bm25_ranks_exact_term_match_first(self) -> None:
        spans = retrieve_spans(self.run_dir, _node("unit-01"), "python language", top_k=3)
        self.assertTrue(spans)
        top = max(spans, key=lambda s: s.score)
        self.assertEqual(top.chunk_index, 0)
        self.assertEqual(top.reason, "bm25")

    def test_bm25_idf_downweights_ubiquitous_terms(self) -> None:
        spans = retrieve_spans(self.run_dir, _node("unit-01"), "python language", top_k=3)
        ranked = sorted(spans, key=lambda s: -s.score)
        self.assertEqual(ranked[0].chunk_index, 0)
        low = spans[-1].score
        self.assertGreater(ranked[0].score, low)

    def test_candidates_restricted_to_node_units(self) -> None:
        spans = retrieve_spans(self.run_dir, _node("unit-01"), "selenium webdriver", top_k=8)
        self.assertTrue(spans)
        self.assertTrue(all(s.unit_id == "unit-01" for s in spans))
        self.assertFalse(any(s.chunk_index in (3, 4, 5) for s in spans))

    def test_closure_pulls_adjacent_chunks(self) -> None:
        spans = retrieve_spans(self.run_dir, _node("unit-02"), "selenium headless", top_k=1)
        self.assertEqual([s.chunk_index for s in spans], [4, 5])
        self.assertEqual(spans[0].reason, "closure")
        self.assertEqual(spans[1].reason, "bm25")

    def test_results_returned_in_document_order(self) -> None:
        spans = retrieve_spans(self.run_dir, _node("unit-01"), "python", top_k=8)
        indices = [s.chunk_index for s in spans]
        self.assertEqual(indices, sorted(indices))
        self.assertEqual(len(indices), len(set(indices)))

    def test_dense_fusion_changes_ranking(self) -> None:
        def fake_dense(chunk_indices: list[int], query: str) -> list[float]:
            return [1.0 if i == 1 else 0.0 for i in chunk_indices]

        spans = retrieve_spans(
            self.run_dir, _node("unit-01"), "python semantics", top_k=3, dense=fake_dense
        )
        ranked = sorted(spans, key=lambda s: -s.score)
        self.assertEqual(ranked[0].chunk_index, 1)
        self.assertEqual(ranked[0].reason, "fused")

    def test_no_embeddings_degrades_to_bm25(self) -> None:
        self.assertFalse((self.run_dir / "chunks.emb.npy").exists())
        spans = retrieve_spans(self.run_dir, _node("unit-03"), "pandas dataframes")
        self.assertTrue(spans)
        self.assertTrue(all(s.reason == "bm25" for s in spans))

    def test_build_index_is_idempotent(self) -> None:
        chunks = [
            Chunk(index=i, text=text, tokens=len(text.split()))
            for i, text in enumerate(_TEXT_BLOCKS)
        ]
        mtime_before = chunk_index_path(self.run_dir).stat().st_mtime_ns
        wrote = build_chunk_index(self.run_dir, chunks, _UNITS)
        mtime_after = chunk_index_path(self.run_dir).stat().st_mtime_ns
        self.assertFalse(wrote)
        self.assertEqual(mtime_before, mtime_after)

    def test_index_roundtrips_provenance(self) -> None:
        lines = [
            json.loads(line)
            for line in chunk_index_path(self.run_dir).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(lines), len(_TEXT_BLOCKS))
        for i, line in enumerate(lines):
            self.assertEqual(line["index"], i)
            self.assertIn(line["unit_id"], ("unit-01", "unit-02", "unit-03"))
            self.assertIn("text", line)
            self.assertIn("tokens", line)

    def test_materialized_path_inputs_resolve_to_units(self) -> None:
        node = _node("unit-01", inputs=["spine/unit-01.md", "scratch/a/finding-1.md"])
        spans = retrieve_spans(self.run_dir, node, "spine", top_k=3)
        self.assertEqual([s.chunk_index for s in spans], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()