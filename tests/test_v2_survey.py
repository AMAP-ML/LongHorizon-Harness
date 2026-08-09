"""Survey tests (PLAN.md §4.2): mechanical chunking (no model), windowed
survey (FakeProvider — schema-validated canned boundary votes), and
harness-side spine assembly (vote merge + minimum-size floor). No network.
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
from kusudaemon.v0.run_dir import create_run_dir  # noqa: E402
from kusudaemon.v2.survey import (  # noqa: E402
    BoundaryVote,
    Chunk,
    assemble_spine,
    chunk_text,
    load_spine,
    save_spine,
    survey_chunks,
)


class ChunkTextTest(unittest.TestCase):
    def test_splits_on_markdown_headings(self) -> None:
        text = "## Intro\nsome intro text here.\n\n## Body\n" + ("word " * 60)
        chunks = chunk_text(text, min_chunk_tokens=5)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(chunks[0].text.startswith("## Intro"))

    def test_tiny_fragments_are_merged_into_neighbors(self) -> None:
        text = "## A\nx\n\n## B\n" + ("word " * 200)
        chunks = chunk_text(text, min_chunk_tokens=50)
        # "## A\nx\n" alone is far under the token floor and must not survive
        # as its own chunk.
        self.assertTrue(all(chunk.tokens >= 1 for chunk in chunks))
        self.assertNotIn("## A\nx\n\n", [chunk.text for chunk in chunks])

    def test_empty_text_yields_no_chunks(self) -> None:
        self.assertEqual(chunk_text("   \n  "), [])

    def test_chunks_are_indexed_in_order(self) -> None:
        text = "## A\n" + ("word " * 60) + "\n\n## B\n" + ("word " * 60) + "\n\n## C\n" + (
            "word " * 60
        )
        chunks = chunk_text(text, min_chunk_tokens=10)
        self.assertEqual([chunk.index for chunk in chunks], list(range(len(chunks))))


class SurveyChunksTest(unittest.TestCase):
    def _chunks(self, n: int) -> list[Chunk]:
        return [Chunk(index=i, text=f"chunk {i} words here", tokens=10) for i in range(n)]

    def test_single_window_covers_all_chunks(self) -> None:
        chunks = self._chunks(5)
        provider = FakeProvider(
            [{"boundaries": [{"boundary_after": 2, "label": "shift", "confidence": 0.9}]}]
        )
        votes = survey_chunks(chunks, provider, window_size=12, stride=8)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(votes, [BoundaryVote(boundary_after=2, label="shift", confidence=0.9)])

    def test_multiple_windows_convert_local_to_global_indices(self) -> None:
        chunks = self._chunks(20)
        provider = FakeProvider(
            [
                {"boundaries": [{"boundary_after": 5, "label": "first", "confidence": 0.8}]},
                {"boundaries": [{"boundary_after": 3, "label": "second", "confidence": 0.7}]},
                {"boundaries": []},
            ]
        )
        votes = survey_chunks(chunks, provider, window_size=8, stride=8)
        self.assertEqual(len(provider.calls), 3)
        global_indices = sorted(vote.boundary_after for vote in votes)
        # window 0 covers chunks 0-7 (local 5 -> global 5);
        # window 1 covers chunks 8-15 (local 3 -> global 11).
        self.assertEqual(global_indices, [5, 11])

    def test_fewer_than_two_chunks_makes_no_calls(self) -> None:
        provider = FakeProvider([])
        self.assertEqual(survey_chunks(self._chunks(1), provider), [])
        self.assertEqual(survey_chunks([], provider), [])


class AssembleSpineTest(unittest.TestCase):
    def _chunks(self, tokens_per_chunk: list[int]) -> list[Chunk]:
        return [
            Chunk(index=i, text=f"chunk {i}", tokens=tokens) for i, tokens in enumerate(tokens_per_chunk)
        ]

    def test_no_votes_yields_one_unit_covering_everything(self) -> None:
        chunks = self._chunks([100, 100, 100])
        units = assemble_spine(chunks, [])
        self.assertEqual(len(units), 1)
        self.assertEqual((units[0].start_chunk, units[0].end_chunk), (0, 2))
        self.assertEqual(units[0].tokens, 300)

    def test_confident_boundary_splits_into_two_units(self) -> None:
        chunks = self._chunks([1000, 1000, 1000, 1000])
        votes = [BoundaryVote(boundary_after=1, label="new topic", confidence=0.9)]
        units = assemble_spine(chunks, votes, min_unit_tokens=500)
        self.assertEqual(len(units), 2)
        self.assertEqual((units[0].start_chunk, units[0].end_chunk), (0, 1))
        self.assertEqual((units[1].start_chunk, units[1].end_chunk), (2, 3))
        self.assertEqual(units[1].label, "new topic")

    def test_low_confidence_boundary_is_dropped(self) -> None:
        chunks = self._chunks([1000, 1000, 1000])
        votes = [BoundaryVote(boundary_after=1, label="maybe", confidence=0.2)]
        units = assemble_spine(chunks, votes, confidence_floor=0.5)
        self.assertEqual(len(units), 1)

    def test_duplicate_boundary_votes_keep_the_highest_confidence(self) -> None:
        chunks = self._chunks([1000, 1000, 1000, 1000])
        votes = [
            BoundaryVote(boundary_after=1, label="weak", confidence=0.55),
            BoundaryVote(boundary_after=1, label="strong", confidence=0.95),
        ]
        units = assemble_spine(chunks, votes, min_unit_tokens=500)
        self.assertEqual(units[1].label, "strong")

    def test_undersized_unit_is_folded_into_its_neighbor(self) -> None:
        chunks = self._chunks([1000, 10, 1000])
        votes = [
            BoundaryVote(boundary_after=0, label="tiny", confidence=0.9),
            BoundaryVote(boundary_after=1, label="rest", confidence=0.9),
        ]
        units = assemble_spine(chunks, votes, min_unit_tokens=500)
        # The 10-token middle unit cannot stand alone; it must be folded into
        # a neighbor rather than surviving under the floor.
        self.assertTrue(all(unit.tokens >= 500 or len(units) == 1 for unit in units))
        total_tokens = sum(unit.tokens for unit in units)
        self.assertEqual(total_tokens, 2010)

    def test_unit_ids_are_sequential(self) -> None:
        chunks = self._chunks([1000, 1000, 1000])
        votes = [BoundaryVote(boundary_after=0, label="b", confidence=0.9)]
        units = assemble_spine(chunks, votes, min_unit_tokens=1)
        self.assertEqual([unit.id for unit in units], ["unit-01", "unit-02"])


class SpinePersistenceTest(unittest.TestCase):
    def test_save_and_load_round_trip(self) -> None:
        chunks = [Chunk(index=i, text=f"c{i}", tokens=1000) for i in range(3)]
        votes = [BoundaryVote(boundary_after=0, label="second", confidence=0.9)]
        units = assemble_spine(chunks, votes, min_unit_tokens=1)
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            save_spine(run_dir, units)
            loaded = load_spine(run_dir)
            self.assertEqual(loaded, units)

    def test_load_missing_spine_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            self.assertEqual(load_spine(run_dir), [])


if __name__ == "__main__":
    unittest.main()
