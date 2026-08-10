"""PLAN-zeromem.md §3.8 tests for the deterministic (embedding) survey.

All drive ``survey_chunks_deterministic`` with hand-built vectors injected
via ``embed_fn`` — no model, no network, no optional dependency installed.
``cosine`` comes from ``v2/embeddings.py``, which is pure stdlib.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import importlib  # noqa: E402

from kusudaemon.v2.embeddings import EmbeddingsUnavailable, cosine  # noqa: E402
from kusudaemon.v2.survey import (  # noqa: E402
    DEFAULT_CONFIDENCE_FLOOR,
    Chunk,
    assemble_spine,
    chunk_text,
    survey_chunks_deterministic,
)

embedding_module = importlib.import_module("kusudaemon.v2.embeddings")


def _chunks(texts: list[str]) -> list[Chunk]:
    return [Chunk(index=i, text=text, tokens=len(text.split())) for i, text in enumerate(texts)]


def _identity(chunks: list[Chunk]) -> Callable[[list[str]], list[list[float]]]:
    vectors = {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [0.9, 0.1]}
    return lambda texts: [vectors[t.strip()] for t in texts]


class SurveyDeterministicTest(unittest.TestCase):
    def test_fewer_than_two_chunks_returns_empty(self) -> None:
        self.assertEqual(survey_chunks_deterministic(_chunks(["only"])), [])
        self.assertEqual(survey_chunks_deterministic(_chunks([])), [])

    def test_clean_topic_shift_emits_boundary(self) -> None:
        chunks = _chunks(["a", "a", "a", "b", "b", "b"])
        votes = survey_chunks_deterministic(chunks, embed_fn=_identity(chunks))
        self.assertEqual([v.boundary_after for v in votes], [2])

    def test_uniform_corpus_emits_no_high_confidence_votes(self) -> None:
        chunks = _chunks(["a"] * 6)
        votes = survey_chunks_deterministic(
            chunks, embed_fn=_identity(chunks)
        )
        self.assertTrue(all(v.confidence < DEFAULT_CONFIDENCE_FLOOR for v in votes))
        units = assemble_spine(chunks, votes)
        self.assertEqual(len(units), 1)

    def test_heading_boost_promotes_weak_boundary(self) -> None:
        # Perfectly uniform embedding similarity (no raw peak at all) — only
        # the author-declared heading can promote a boundary here.
        chunks = _chunks(
            ["a", "a", "## Chapter 2 starts here with a few words", "a", "a", "a"]
        )

        def near_uniform(texts_in: list[str]) -> list[list[float]]:
            return [[0.999, 0.001] if "Chapter 2" in t else [1.0, 0.0] for t in texts_in]

        votes = survey_chunks_deterministic(chunks, embed_fn=near_uniform)
        self.assertTrue(votes, "heading boost must promote a weak boundary")
        self.assertEqual(votes[0].boundary_after, 1)

    def test_label_from_heading(self) -> None:
        from kusudaemon.v2.survey import _label_for_chunk

        label = _label_for_chunk(Chunk(index=0, text="## Photosynthesis\nbody text", tokens=3))
        self.assertEqual(label, "Photosynthesis")

    def test_label_fallback_to_first_words(self) -> None:
        from kusudaemon.v2.survey import _label_for_chunk

        chunk = Chunk(
            index=0, text="these are the first eight words used as the label here", tokens=12
        )
        self.assertEqual(
            _label_for_chunk(chunk), "these are the first eight words used as"
        )

    def test_label_truncated_to_120_chars(self) -> None:
        from kusudaemon.v2.survey import _label_for_chunk

        long_heading = "# " + "word." * 60  # > 120 chars after stripping "# "
        chunk = Chunk(index=0, text=long_heading + "\nbody text", tokens=62)
        label = _label_for_chunk(chunk)
        self.assertLessEqual(len(label), 120)
        self.assertEqual(label, ("word." * 60)[:120])

    def test_smoothing_suppresses_single_outlier(self) -> None:
        # a a a b a a a  — the odd chunk's two dissimilarity peaks form a
        # plateau with no strict local maximum: no boundary fires.
        chunks = _chunks(["a", "a", "a", "b", "a", "a", "a"])
        votes = survey_chunks_deterministic(
            chunks, embed_fn=_identity(chunks), smoothing_window=2
        )
        self.assertEqual(votes, [])

    def test_confidence_is_normalized_to_unit_range(self) -> None:
        texts = ["c", "c", "c", "c", "b", "b", "b", "b", "a", "a", "a", "a"]
        chunks = _chunks(texts)
        votes = survey_chunks_deterministic(chunks, embed_fn=_identity(chunks))
        self.assertTrue(votes)
        for vote in votes:
            self.assertGreaterEqual(vote.confidence, 0.0)
            self.assertLessEqual(vote.confidence, 1.0)

    def test_votes_feed_assemble_spine_unchanged(self) -> None:
        text = "\n\n".join(
            [
                "Introductory material about the topic and its early history",
                "Some more introductory material that continues the setup",
                "## Photosynthesis\nPhotosynthesis converts light into chemical energy over several paragraphs.",
                "More photosynthesis details and the calvin cycle and its products",
                "## Respiration\nRespiration extracts energy from glucose in the cell.",
                "The details of respiration and ATP production continue here.",
            ]
        )
        chunks = chunk_text(text)

        def binned_embed(texts_in: list[str]) -> list[list[float]]:
            return [
                [1.0, 0.0] if "Photosynthesis" in t or "photosynthesis" in t.lower() else
                [0.0, 1.0] if "Respiration" in t or "respiration" in t.lower() else
                [0.5, 0.5]
                for t in texts_in
            ]

        votes = survey_chunks_deterministic(chunks, embed_fn=binned_embed)
        units = assemble_spine(chunks, votes)
        self.assertTrue(units)
        seen: list[tuple[int, int]] = []
        for unit in units:
            self.assertEqual(unit.start_chunk, seen[-1][1] + 1 if seen else 0)
            seen.append((unit.start_chunk, unit.end_chunk))
        self.assertEqual(seen[0][0], 0)
        self.assertEqual(seen[-1][1], len(chunks) - 1)


class EmbeddingsTest(unittest.TestCase):
    def test_cosine_of_normalized_vectors(self) -> None:
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [-1.0, 0.0]), -1.0)

    def test_embeddings_unavailable_raises(self) -> None:
        original = embedding_module.embeddings_available
        embedding_module.embeddings_available = lambda: False
        try:
            from kusudaemon.v2.embeddings import embed_texts

            with self.assertRaises(EmbeddingsUnavailable):
                embed_texts(["hello"])
        finally:
            embedding_module.embeddings_available = original


if __name__ == "__main__":
    unittest.main()