"""Survey — structure discovery (PLAN.md §4.2). Three stages, uniform
output regardless of input structure:

1. **Mechanical chunking** (``chunk_text``, no model) — split on whatever
   boundaries exist: headings, page breaks, blank-line runs. A textbook's
   TOC and a folder of unstructured lecture notes both go through the same
   function; the difference shows up only in how much work stage 2 has to
   do.
2. **Windowed survey** (``survey_chunks``, model, tiny outputs) — walk
   chunks in overlapping windows. Each call sees only that window (never
   the whole corpus, §8) and emits only candidate boundaries — never a
   summary, never content.
3. **Spine assembly** (``assemble_spine``, harness, no model) — merge
   boundary votes across overlapping windows, apply a minimum-size floor,
   produce ``spine.json``.
4. **Materialization** (``materialize_units``, no model, PLAN-zeromem.md
   §7) — write each unit's verbatim chunk range to ``spine/<unit-id>.md``,
   so a leaf's ``inputs`` entries (``unit_input_path``) resolve to a real
   file a Writer's own tools can open, instead of an opaque id nothing on
   disk answers to.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

from ..v0.run_dir import write_text_atomic
from ..v1.gates import estimate_tokens
from ..v1.provider import OpenAICompatibleProvider
from .run_dir import spine_path, spine_unit_path

DEFAULT_MIN_CHUNK_TOKENS = 50
DEFAULT_WINDOW_SIZE = 12
DEFAULT_WINDOW_STRIDE = 8
DEFAULT_MIN_UNIT_TOKENS = 800
DEFAULT_CONFIDENCE_FLOOR = 0.5

DEFAULT_BOUNDARY_PERCENTILE = 0.75  # keep the top quartile of dissimilarity
DEFAULT_SMOOTHING_WINDOW = 2
HEADING_BOOST = 0.25

_HEADING_RE = re.compile(
    r"(?m)^(?:#{1,6}[ \t]+\S.*"
    r"|(?:chapter|section|part)\s+\d+\b.*"
    r"|\d+(?:\.\d+)*[.)][ \t]+\S.*)$",
    re.IGNORECASE,
)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_PAGE_BREAK_RE = re.compile(r"\f")


@dataclass
class Chunk:
    index: int
    text: str
    tokens: int


def chunk_text(text: str, *, min_chunk_tokens: int = DEFAULT_MIN_CHUNK_TOKENS) -> list[Chunk]:
    """Deterministic, model-free split on headings / page breaks / blank-line
    runs, then folds any resulting fragment under ``min_chunk_tokens`` into
    its neighbor so stage 2 never sees a near-empty window entry."""
    if not text.strip():
        return []
    positions = {0, len(text)}
    for pattern in (_HEADING_RE, _PAGE_BREAK_RE, _BLANK_RUN_RE):
        for match in pattern.finditer(text):
            positions.add(match.start() if pattern is _HEADING_RE else match.end())
    ordered = sorted(positions)
    segments = [
        text[start:end] for start, end in zip(ordered, ordered[1:]) if text[start:end].strip()
    ]
    if not segments:
        segments = [text]
    merged = _merge_small_segments(segments, min_chunk_tokens)
    return [Chunk(index=i, text=segment, tokens=estimate_tokens(segment)) for i, segment in enumerate(merged)]


def _merge_small_segments(segments: list[str], min_tokens: int) -> list[str]:
    # §11.10.9: a running token count instead of re-tokenizing the growing
    # tail on every fold — the old loop was O(n²) in corpus size (it
    # re-estimated a string it was also concatenating in place).
    merged: list[str] = []
    running: list[int] = []
    for segment in segments:
        if merged and running[-1] < min_tokens:
            merged[-1] += segment
            running[-1] += estimate_tokens(segment)
        else:
            merged.append(segment)
            running.append(estimate_tokens(segment))
    if len(merged) > 1 and running[-1] < min_tokens:
        merged[-2] += merged[-1]
        merged.pop()
        running[-2] += running[-1]
        running.pop()
    return merged


@dataclass
class BoundaryVote:
    boundary_after: int
    label: str
    confidence: float


SURVEY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["boundaries"],
    "additionalProperties": False,
    "properties": {
        "boundaries": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["boundary_after", "label", "confidence"],
                "additionalProperties": False,
                "properties": {
                    "boundary_after": {"type": "integer"},
                    "label": {"type": "string", "maxLength": 120},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}

_SURVEY_SYSTEM_PROMPT = (
    "You are surveying a corpus for structural boundaries. You never "
    "summarize and never quote content back — you see a window of "
    "consecutive chunks, each shown only as its first few words, and each "
    "labeled by its index within this window. Emit only candidate "
    "boundaries: the local chunk index after which a new structural unit "
    "begins, a short label for what follows, and a confidence from 0 to 1. "
    "A window with no real boundary should return an empty list. Respond "
    "with a single JSON object only."
)


def _render_window(window: list[Chunk]) -> str:
    lines = []
    for local_index, chunk in enumerate(window):
        preview = " ".join(chunk.text.split()[:15])
        lines.append(f"{local_index}: {preview}")
    return "\n".join(lines)


def survey_chunks(
    chunks: list[Chunk],
    provider: OpenAICompatibleProvider,
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    stride: int = DEFAULT_WINDOW_STRIDE,
    on_reasoning: Callable[[str], None] | None = None,
) -> list[BoundaryVote]:
    """Walk ``chunks`` in overlapping windows of ``window_size`` (advancing
    by ``stride``), collecting one boundary-vote call per window. Each call's
    ``boundary_after`` is local to its window; this converts it back to a
    global chunk index before returning."""
    votes: list[BoundaryVote] = []
    if len(chunks) < 2:
        return votes
    start = 0
    while start < len(chunks):
        window = chunks[start : start + window_size]
        if len(window) < 2:
            break
        messages = [
            {"role": "system", "content": _SURVEY_SYSTEM_PROMPT},
            {"role": "user", "content": _render_window(window)},
        ]
        payload = provider.complete_json(messages, SURVEY_SCHEMA, on_reasoning=on_reasoning)
        for boundary in payload.get("boundaries", []):
            global_index = start + int(boundary["boundary_after"])
            if 0 <= global_index < len(chunks) - 1:
                votes.append(
                    BoundaryVote(
                        boundary_after=global_index,
                        label=str(boundary["label"])[:120],
                        confidence=float(boundary["confidence"]),
                    )
                )
        if start + window_size >= len(chunks):
            break
        start += stride
    return votes


def survey_chunks_deterministic(
    chunks: list[Chunk],
    *,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
    boundary_percentile: float = DEFAULT_BOUNDARY_PERCENTILE,
    smoothing_window: int = DEFAULT_SMOOTHING_WINDOW,
) -> list[BoundaryVote]:
    """Zero-token stage 2 (PLAN-zeromem.md §3.6): boundaries from
    embedding dissimilarity valleys plus the structural signals
    ``chunk_text`` already found, instead of one model call per window.

    Returns the same ``BoundaryVote`` list ``survey_chunks`` does, so
    ``assemble_spine`` — already model-free — is unchanged, including its
    confidence floor and min-unit folding.

    ``embed_fn`` is injectable so tests drive the algorithm with fake
    vectors and no optional dependency installed; it defaults to
    ``embeddings.embed_texts``, which raises ``EmbeddingsUnavailable`` (a
    loud, non-fatal fallback the driver logs) when the ``retrieval`` extra
    is missing.

    Boundary selection: a candidate is a **strict local maximum** of the
    unsmoothed dissimilarity series, kept only if it also clears the
    ``boundary_percentile`` quantile of the *smoothed, heading-boosted*
    series. Smoothing sets the contrast threshold against the corpus
    instead of picking the candidates — the two-series split is what makes
    a single stylistically odd chunk (a two-index plateau of high
    dissimilarity, with no strict peak) emit *no* vote: thresholding the
    plateau alone would keep it, since the top quartile by construction
    contains the top values. ``assemble_spine``'s highest-confidence-wins
    merge still runs and is still correct, just with nothing to merge."""
    if len(chunks) < 2:
        return []
    embed_fn = embed_fn or _default_embed
    vectors = embed_fn([chunk.text for chunk in chunks])
    dissimilarities = [
        1.0 - _cosine_of(vectors[i], vectors[i + 1])
        for i in range(len(chunks) - 1)
    ]

    smoothed = _smooth(dissimilarities, smoothing_window)
    for i in range(len(chunks) - 1):
        if _HEADING_RE.match(chunks[i + 1].text):
            smoothed[i] = min(1.0, smoothed[i] + HEADING_BOOST)

    threshold = _quantile(smoothed, boundary_percentile)
    n = len(dissimilarities)
    votes: list[BoundaryVote] = []
    for i, value in enumerate(dissimilarities):
        left = dissimilarities[i - 1] if i > 0 else -1.0
        right = dissimilarities[i + 1] if i + 1 < n else -1.0
        is_peak = value > left and value > right
        # An author-declared heading outranks any embedding gap (§3.6 step
        # 5): a boosted index is a candidate even on a corpus too uniform to
        # produce a raw dissimilarity peak.
        is_heading = _HEADING_RE.match(chunks[i + 1].text) is not None
        # Smoothing sets corpus-level contrast (threshold + confidence); a
        # genuine raw peak keeps its full height when clearing it.
        at_or_above = smoothed[i] >= threshold or value >= threshold
        if at_or_above and (is_peak or is_heading):
            votes.append(
                BoundaryVote(
                    boundary_after=i,
                    label=_label_for_chunk(chunks[i + 1]),
                    confidence=_min_max(smoothed[i], smoothed),
                )
            )
    return votes


def _label_for_chunk(chunk: Chunk) -> str:
    """Structural label, never a paraphrase (§3.2: the author's own words,
    with provenance): the first heading line in the chunk, stripped of
    leading ``#`` / numbering, else the first 8 words. Capped at 120 chars
    to match ``SURVEY_SCHEMA``'s ``maxLength``."""
    match = _HEADING_RE.match(chunk.text)
    if match is not None:
        label = _strip_heading_markers(match.group(0))
    else:
        label = " ".join(chunk.text.split()[:8])
    return label[:120]


def _strip_heading_markers(line: str) -> str:
    stripped = line.strip()
    stripped = stripped.lstrip("#").strip()
    stripped = re.sub(r"^(?:chapter|section|part)\s+\d+\b[.:]?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"^\d+(?:\.\d+)*[.)]\s*", "", stripped)
    return stripped.strip()


def _smooth(values: list[float], window: int) -> list[float]:
    if window <= 1:
        return list(values)
    smoothed: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - window)
        hi = min(len(values), i + window + 1)
        smoothed.append(sum(values[lo:hi]) / (hi - lo))
    return smoothed


def _quantile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 1.0
    index = min(len(ordered) - 1, max(0, int(percentile * len(ordered))))
    return ordered[index]


def _min_max(value: float, values: list[float]) -> float:
    lo, hi = min(values), max(values)
    if hi == lo:
        return 0.0
    return (value - lo) / (hi - lo)


def _cosine_of(a: list[float], b: list[float]) -> float:
    from .embeddings import cosine

    return cosine(a, b)


def _default_embed(texts: list[str]) -> list[list[float]]:
    from .embeddings import embed_texts

    return embed_texts(texts)


@dataclass
class SpineUnit:
    id: str
    label: str
    start_chunk: int
    end_chunk: int
    tokens: int


def assemble_spine(
    chunks: list[Chunk],
    votes: list[BoundaryVote],
    *,
    min_unit_tokens: int = DEFAULT_MIN_UNIT_TOKENS,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> list[SpineUnit]:
    """Harness-side merge (§4.2 stage 3, no model call): overlapping windows
    can vote on the same boundary more than once, so this keeps the
    highest-confidence vote per boundary index, drops anything below
    ``confidence_floor``, then folds any resulting unit under
    ``min_unit_tokens`` into a neighbor."""
    if not chunks:
        return []
    accepted: dict[int, BoundaryVote] = {}
    for vote in votes:
        if vote.confidence < confidence_floor:
            continue
        if not (0 <= vote.boundary_after < len(chunks) - 1):
            continue
        current = accepted.get(vote.boundary_after)
        if current is None or vote.confidence > current.confidence:
            accepted[vote.boundary_after] = vote

    boundary_indices = sorted(accepted)
    bounds = [-1, *boundary_indices, len(chunks) - 1]
    raw_units: list[list[Any]] = []
    for i in range(len(bounds) - 1):
        start_chunk = bounds[i] + 1
        end_chunk = bounds[i + 1]
        label = accepted[bounds[i]].label if bounds[i] in accepted else "Opening section"
        tokens = sum(chunk.tokens for chunk in chunks[start_chunk : end_chunk + 1])
        raw_units.append([start_chunk, end_chunk, label, tokens])

    merged = _apply_min_size_floor(raw_units, min_unit_tokens)
    return [
        SpineUnit(id=f"unit-{i + 1:02d}", label=label, start_chunk=start, end_chunk=end, tokens=tokens)
        for i, (start, end, label, tokens) in enumerate(merged)
    ]


def _apply_min_size_floor(raw_units: list[list[Any]], min_unit_tokens: int) -> list[list[Any]]:
    merged = [list(unit) for unit in raw_units]
    i = 0
    while i < len(merged) - 1:
        if merged[i][3] < min_unit_tokens:
            # Fold into the following unit; keep that unit's label (it's the
            # larger, more representative piece) and extend its start.
            merged[i + 1][0] = merged[i][0]
            merged[i + 1][3] += merged[i][3]
            merged.pop(i)
            continue
        i += 1
    if len(merged) > 1 and merged[-1][3] < min_unit_tokens:
        merged[-2][1] = merged[-1][1]
        merged[-2][3] += merged[-1][3]
        merged.pop()
    return merged


def save_spine(run_dir: str | Path, units: list[SpineUnit]) -> None:
    payload = [asdict(unit) for unit in units]
    write_text_atomic(spine_path(run_dir), json.dumps(payload, indent=2) + "\n")


def load_spine(run_dir: str | Path) -> list[SpineUnit]:
    path = spine_path(run_dir)
    if not path.exists():
        return []
    raw_text = path.read_text(encoding="utf-8").strip()
    raw = json.loads(raw_text) if raw_text else []
    # Records are constructed from the known SpineUnit field names only:
    # an unknown key (a spine.json written by a newer version) is dropped
    # instead of raising, and a field added later with a dataclass default
    # tolerates a legacy file that predates it — the tolerance the original
    # comment promised and SpineUnit(**item) never delivered (§11.11). A
    # record missing a *required* field still raises, exactly as the
    # dataclass demands. See materialize_units/unit_input_path
    # (PLAN-zeromem.md §7): a resumed pre-§7 run has a spine.json but no
    # materialized units, and unit_input_path already falls back to the
    # bare id in that case.
    known = {field.name for field in fields(SpineUnit)}
    return [
        SpineUnit(**{key: value for key, value in item.items() if key in known})
        for item in raw
    ]


def materialize_units(
    run_dir: str | Path, chunks: list[Chunk], units: list[SpineUnit]
) -> None:
    """Write ``spine/<unit-id>.md`` per unit — the verbatim concatenation of
    its chunk range (PLAN-zeromem.md §7) — so a leaf's ``inputs`` entries
    resolve to a real file instead of an opaque id nothing on disk answers
    to. Idempotent: a unit whose materialized file already holds the exact
    same text is left untouched rather than rewritten."""
    for unit in units:
        text = "".join(chunk.text for chunk in chunks[unit.start_chunk : unit.end_chunk + 1])
        path = spine_unit_path(run_dir, unit.id)
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        path.write_text(text, encoding="utf-8")


def unit_input_path(run_dir: str | Path, unit: SpineUnit) -> str:
    """Resolve a unit to its materialized file, relative to ``run_dir``, when
    ``materialize_units`` has actually written one for it; otherwise fall
    back to the bare unit id, today's behavior — the resolver a legacy or
    unmaterialized run degrades to (PLAN-zeromem.md §7)."""
    path = spine_unit_path(run_dir, unit.id)
    if path.exists() and path.stat().st_size > 0:
        return str(path.relative_to(Path(run_dir)))
    return unit.id
