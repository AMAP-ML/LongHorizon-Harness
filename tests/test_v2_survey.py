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
from kusudaemon.v2.run_dir import spine_unit_path  # noqa: E402
from kusudaemon.v2.survey import (  # noqa: E402
    BoundaryVote,
    Chunk,
    assemble_spine,
    chunk_text,
    load_spine,
    materialize_units,
    save_spine,
    survey_chunks,
    unit_input_path,
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

    def test_on_reasoning_is_forwarded_to_each_window_call(self) -> None:
        # survey_chunks itself never inspects reasoning -- it just has to
        # pass the caller's hook through to every complete_json call
        # unchanged, so the driver's explore-01 pseudo-agent (§12) can
        # capture reasoning per window without survey.py knowing anything
        # about the dashboard.
        class _RecordingProvider:
            def __init__(self) -> None:
                self.on_reasoning_seen: list[object] = []

            def complete_json(self, messages, schema, *, temperature=0.0, retries=2, on_reasoning=None):
                self.on_reasoning_seen.append(on_reasoning)
                return {"boundaries": []}

        chunks = self._chunks(20)
        provider = _RecordingProvider()
        sentinel = lambda text: None  # noqa: E731
        survey_chunks(chunks, provider, window_size=12, stride=8, on_reasoning=sentinel)
        self.assertGreater(len(provider.on_reasoning_seen), 1)
        self.assertTrue(all(seen is sentinel for seen in provider.on_reasoning_seen))

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

    def test_load_spine_tolerates_legacy_records(self) -> None:
        # A spine.json written before materialization existed carries only
        # the original SpineUnit fields. load_spine must still construct it.
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            legacy = [
                {"id": "unit-01", "label": "Intro", "start_chunk": 0, "end_chunk": 1, "tokens": 900}
            ]
            spine_path_obj = run_dir / "spine.json"
            import json as _json

            spine_path_obj.write_text(_json.dumps(legacy), encoding="utf-8")
            loaded = load_spine(run_dir)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].id, "unit-01")

    def test_load_spine_drops_unknown_fields(self) -> None:
        # §11.11: a spine.json carrying a field added by a newer version is
        # exactly the legacy input the old comment promised to accept — and
        # SpineUnit(**item) raised TypeError on it. Unknown keys are dropped.
        import json as _json

        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            future = [
                {"id": "unit-01", "label": "Intro", "start_chunk": 0, "end_chunk": 1,
                 "tokens": 900, "weight": 1.0}
            ]
            (run_dir / "spine.json").write_text(_json.dumps(future), encoding="utf-8")
            loaded = load_spine(run_dir)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].id, "unit-01")
            self.assertEqual(loaded[0].tokens, 900)


class MaterializeUnitsTest(unittest.TestCase):
    def _source_and_units(self) -> tuple[list[Chunk], list["object"]]:
        text = "## Intro\n" + ("intro word " * 60) + "\n\n## Body\n" + ("body word " * 60)
        chunks = chunk_text(text, min_chunk_tokens=5)
        votes = [BoundaryVote(boundary_after=0, label="Body", confidence=0.9)]
        units = assemble_spine(chunks, votes, min_unit_tokens=1)
        return chunks, units

    def test_materialize_units_writes_one_file_per_unit(self) -> None:
        chunks, units = self._source_and_units()
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            materialize_units(run_dir, chunks, units)
            for unit in units:
                self.assertTrue(spine_unit_path(run_dir, unit.id).exists())

    def test_materialized_unit_text_matches_its_chunk_range(self) -> None:
        chunks, units = self._source_and_units()
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            materialize_units(run_dir, chunks, units)
            for unit in units:
                expected = "".join(c.text for c in chunks[unit.start_chunk : unit.end_chunk + 1])
                actual = spine_unit_path(run_dir, unit.id).read_text(encoding="utf-8")
                self.assertEqual(actual, expected)

    def test_materialize_is_idempotent(self) -> None:
        chunks, units = self._source_and_units()
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            materialize_units(run_dir, chunks, units)
            path = spine_unit_path(run_dir, units[0].id)
            first_mtime = path.stat().st_mtime_ns
            materialize_units(run_dir, chunks, units)
            self.assertEqual(path.stat().st_mtime_ns, first_mtime)

    def test_units_partition_the_source(self) -> None:
        chunks, units = self._source_and_units()
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            materialize_units(run_dir, chunks, units)
            reconstructed = "".join(
                spine_unit_path(run_dir, unit.id).read_text(encoding="utf-8") for unit in units
            )
            self.assertEqual(reconstructed, "".join(c.text for c in chunks))


class UnitInputPathTest(unittest.TestCase):
    def test_resolves_to_materialized_path_when_present(self) -> None:
        chunks = [Chunk(index=0, text="hello", tokens=10)]
        from kusudaemon.v2.survey import SpineUnit

        unit = SpineUnit(id="unit-01", label="x", start_chunk=0, end_chunk=0, tokens=10)
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            materialize_units(run_dir, chunks, [unit])
            resolved = unit_input_path(run_dir, unit)
            self.assertEqual(resolved, str(spine_unit_path(run_dir, unit.id).relative_to(run_dir)))

    def test_falls_back_to_unit_id_when_unmaterialized(self) -> None:
        from kusudaemon.v2.survey import SpineUnit

        unit = SpineUnit(id="unit-01", label="x", start_chunk=0, end_chunk=0, tokens=10)
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = create_run_dir(Path(root_str), "run1")
            self.assertEqual(unit_input_path(run_dir, unit), "unit-01")


if __name__ == "__main__":
    unittest.main()
