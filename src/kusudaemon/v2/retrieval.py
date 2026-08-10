"""Span retrieval over a run's own chunked source (PLAN-zeromem.md §4).

Zero-Mem's read path, narrowed: candidates are restricted to the chunks in
the node's own spine slice, because ``v2/planner.py`` already decided scope.
BM25 is stdlib and always available; dense scoring needs
``kusudaemon[retrieval]`` and is fused with it Zero-Mem-style when present.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..v1.tree import TaskNode
from .embeddings import DEFAULT_EMBED_MODEL
from .run_dir import (
    chunk_embeddings_meta_path,
    chunk_embeddings_path,
    chunk_index_path,
)
from .survey import Chunk, SpineUnit, load_spine

DEFAULT_TOP_K = 8
DEFAULT_RHO = 0.6  # the paper's dual-view fusion weight
DEFAULT_NEIGHBOR_RADIUS = 1  # Zero-Mem's hierarchy closure

_TERM_RE = re.compile(r"[a-z0-9]+")
_K1 = 1.5
_B = 0.75
_EMB_DTYPE = "float32"

DenseScorer = Callable[[list[int], str], list[float]]


@dataclass
class RetrievedSpan:
    chunk_index: int
    unit_id: str
    text: str
    score: float
    reason: str  # "bm25" | "dense" | "fused" | "closure"


def _tokenize(text: str) -> list[str]:
    return _TERM_RE.findall(text.lower())


def _idf_over(term: str, df: dict[str, int], n_docs: int) -> float:
    doc_freq = df.get(term, 0)
    return math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))


def _bm25_score(
    terms: list[str],
    text: str,
    doc_len: int,
    avgdl: float,
    df: dict[str, int],
    n_docs: int,
) -> float:
    tf_counter: dict[str, int] = {}
    for term in _tokenize(text):
        tf_counter[term] = tf_counter.get(term, 0) + 1
    score = 0.0
    for term in set(terms):
        tf = tf_counter.get(term, 0)
        if not tf:
            continue
        idf = _idf_over(term, df, n_docs)
        score += idf * (tf * (_K1 + 1.0)) / (tf + _K1 * (1.0 - _B + _B * doc_len / avgdl))
    return score


def _min_max_normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _resolve_unit_ids(node: TaskNode, units: dict[str, SpineUnit]) -> set[str]:
    """Map ``node.inputs`` entries to spine unit ids (§4.4): an entry that is
    a bare unit id (legacy runs) or the materialized unit's file path
    (``spine/unit-03.md``, Phase 0b) resolves to that unit; anything else —
    e.g. a v4 research finding path — is not a unit and is passed through."""

    known = set(units)
    resolved: set[str] = set()
    for entry in node.inputs:
        if entry in known:
            resolved.add(entry)
            continue
        candidate = Path(entry).name
        if candidate.endswith(".md"):
            candidate = candidate[:-3]
        if candidate in known:
            resolved.add(candidate)
    return resolved


def build_chunk_index(run_dir: str | Path, chunks: list[Chunk], units: list[SpineUnit]) -> bool:
    """Write ``chunks.jsonl`` — one provenance-bearing line per chunk
    ``{index, unit_id, tokens, text}`` — plus ``chunks.emb.npy`` when
    embeddings are available. Idempotent: a complete index is not rebuilt.
    Returns True if the index was (re)written, False when it already was."""

    run_dir = Path(run_dir)
    index_path = chunk_index_path(run_dir)
    if index_path.exists():
        with index_path.open(encoding="utf-8") as fh:
            existing = sum(1 for _ in fh)
        if existing == len(chunks):
            return False
    unit_of: dict[int, str] = {}
    for unit in units:
        for index in range(unit.start_chunk, unit.end_chunk + 1):
            unit_of[index] = unit.id
    lines = [
        json.dumps(
            {"index": chunk.index, "unit_id": unit_of.get(chunk.index, ""), "tokens": chunk.tokens, "text": chunk.text}
        )
        for chunk in chunks
    ]
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        from .embeddings import embed_texts, embeddings_available

        if embeddings_available() and chunks:
            vectors = embed_texts([chunk.text for chunk in chunks])
            import numpy as np

            np.save(chunk_embeddings_path(run_dir), np.asarray(vectors, dtype=np.float32))
            chunk_embeddings_meta_path(run_dir).write_text(
                json.dumps({"model": DEFAULT_EMBED_MODEL}) + "\n", encoding="utf-8"
            )
    except ImportError:
        pass
    return True


def _load_index(run_dir: Path) -> list[dict[str, Any]]:
    path = chunk_index_path(run_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# §11.10.10: dense retrieval must not re-load and de-vectorize the whole
# embedding matrix per node prompt. Keyed on (path, mtime, size) so an
# index rebuilt mid-run is picked up while a static one is read exactly
# once per process.
_DENSE_CACHE: dict[tuple[str, int, int], Any] = {}


def _default_dense_scorer(run_dir: Path) -> DenseScorer | None:
    emb_path = chunk_embeddings_path(run_dir)
    meta_path = chunk_embeddings_meta_path(run_dir)
    if not (emb_path.exists() and meta_path.exists()):
        return None
    try:
        import numpy as np

        stat = emb_path.stat()
        cache_key = (str(emb_path), stat.st_mtime_ns, stat.st_size)
        vectors = _DENSE_CACHE.get(cache_key)
        if vectors is None:
            vectors = np.load(emb_path)
            _DENSE_CACHE.clear()
            _DENSE_CACHE[cache_key] = vectors
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        model_name = meta.get("model") or DEFAULT_EMBED_MODEL

        from ..v2.embeddings import embed_texts

        def score(chunk_indices: list[int], query: str) -> list[float]:
            query_vec = embed_texts([query], model_name=model_name)[0]
            rows = vectors[list(chunk_indices)]
            if rows.size == 0:
                return []
            q_norm = float(np.linalg.norm(query_vec)) or 1.0
            norms = np.linalg.norm(rows, axis=1)
            sims = (rows @ np.asarray(query_vec, dtype=rows.dtype)) / (
                q_norm * np.maximum(norms, 1e-12)
            )
            return [float(s) for s in sims]

        return score
    except (ImportError, OSError, ValueError):
        return None


def retrieve_spans(
    run_dir: str | Path,
    node: TaskNode,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    rho: float = DEFAULT_RHO,
    neighbor_radius: int = DEFAULT_NEIGHBOR_RADIUS,
    dense: DenseScorer | None = None,
) -> list[RetrievedSpan]:
    """Candidates := chunks whose ``unit_id`` is in ``node.inputs``.

    1. BM25 over candidates (stdlib implementation, ~40 lines).
    2. Dense cosine over candidates, if the index has embeddings.
    3. Min-max normalize each view, fuse ``rho * dense + (1-rho) * bm25``.
       BM25 alone when no embeddings -- degradation, not failure.
    4. Closure: pull in +/- ``neighbor_radius`` adjacent chunks of each
       winner (Zero-Mem's hierarchy closure -- a retrieved paragraph whose
       antecedent sentence is in the previous chunk is worse than useless).
    5. Dedupe by chunk index, return in **ascending chunk order**, not
       score order: a Writer reading source material needs document order.

    ``dense`` is an injection seam for tests (fake vectors): callable over
    candidate chunk indices returning their cosine-similarity scores.
    """

    run_dir = Path(run_dir)
    records = _load_index(run_dir)
    units = {unit.id: unit for unit in load_spine(run_dir)}
    if not records:
        return []
    wanted = _resolve_unit_ids(node, units)
    if not wanted:
        return []
    candidates = [record for record in records if record.get("unit_id") in wanted]
    if not candidates:
        return []

    full_texts = [str(record.get("text") or "") for record in records]
    n_docs = len(full_texts)
    df: dict[str, int] = {}
    for text in full_texts:
        for term in set(_tokenize(text)):
            df[term] = df.get(term, 0) + 1
    avgdl = sum(len(t.split()) for t in full_texts) / max(1, n_docs)

    query_terms = _tokenize(query)
    bm25_raw = [
        _bm25_score(query_terms, str(record.get("text") or ""), len(text.split()), avgdl, df, n_docs)
        for record, text in zip(candidates, [str(c.get("text") or "") for c in candidates])
    ]
    bm25_norm = _min_max_normalize(bm25_raw)

    dense_scores: list[float] | None = None
    if dense is not None:
        dense_scores = list(dense([int(c["index"]) for c in candidates], query))
    else:
        default_scorer = _default_dense_scorer(run_dir)
        if default_scorer is not None:
            dense_scores = list(default_scorer([int(c["index"]) for c in candidates], query))
    if dense_scores is not None:
        dense_norm = _min_max_normalize(list(dense_scores))
        fused = [rho * d + (1.0 - rho) * b for d, b in zip(dense_norm, bm25_norm)]
        ranked = sorted(
            range(len(candidates)),
            key=lambda idx: (-fused[idx], int(candidates[idx]["index"])),
        )
        chosen = ranked[:top_k]
        winner_units: dict[int, list[Any]] = {
            idx: {"score": fused[idx], "reason": "fused"} for idx in chosen
        }
    else:
        ranked = sorted(
            range(len(candidates)),
            key=lambda idx: (-bm25_raw[idx], int(candidates[idx]["index"])),
        )
        chosen = ranked[:top_k]
        winner_units = {
            idx: {"score": bm25_raw[idx], "reason": "bm25"} for idx in chosen
        }

    spans: dict[int, RetrievedSpan] = {}
    for idx in chosen:
        record = candidates[idx]
        spans[int(record["index"])] = RetrievedSpan(
            chunk_index=int(record["index"]),
            unit_id=str(record["unit_id"]),
            text=str(record["text"]),
            score=float(winner_units[idx]["score"]),
            reason=str(winner_units[idx]["reason"]),
        )

    if neighbor_radius > 0:
        for idx in chosen:
            record = candidates[idx]
            unit = units.get(str(record["unit_id"]))
            if unit is None:
                continue
            chunk_index = int(record["index"])
            for delta in range(-neighbor_radius, neighbor_radius + 1):
                if delta == 0:
                    continue
                neighbor = chunk_index + delta
                if not (unit.start_chunk <= neighbor <= unit.end_chunk):
                    continue
                if neighbor in spans:
                    continue
                for other in records:
                    if int(other["index"]) == neighbor:
                        spans[neighbor] = RetrievedSpan(
                            chunk_index=neighbor,
                            unit_id=str(other["unit_id"]),
                            text=str(other["text"]),
                            score=float(winner_units[idx]["score"]),
                            reason="closure",
                        )
                        break

    return [spans[index] for index in sorted(spans)]