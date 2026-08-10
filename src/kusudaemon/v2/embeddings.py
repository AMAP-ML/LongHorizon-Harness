"""Optional embedding backend (``pip install "kusudaemon[retrieval]"``).

Isolated here so the rest of the harness never imports
``sentence_transformers`` at module scope, and so the test suite — which
per CLAUDE.md must run with no optional extras installed — can check
availability and skip. Mirrors the pattern
``adapters/tools/searxng_search.py`` uses for ``gptme``.

``cosine`` is pure stdlib and separately unit-testable with hand-written
vectors and no model installed — which is what lets the §3.7 algorithm
tests in ``test_v2_survey_deterministic.py`` drive
``survey_chunks_deterministic`` with injected fake vectors.
"""

from __future__ import annotations

import math
from typing import Callable

DEFAULT_EMBED_MODEL = "BAAI/bge-m3"  # the paper's dense encoder

_model_cache: dict[str, Callable[[list[str]], list[list[float]]]] = {}


class EmbeddingsUnavailable(RuntimeError):
    """Raised when a caller demanded embeddings without the extra installed."""


def embeddings_available() -> bool:
    """True if ``sentence_transformers`` imports. Never raises."""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def embed_texts(
    texts: list[str],
    *,
    model_name: str = DEFAULT_EMBED_MODEL,
    batch_size: int = 32,
) -> list[list[float]]:
    """L2-normalized embeddings, one per input. Raises
    ``EmbeddingsUnavailable`` if the extra is missing. Model instances are
    cached module-level by name — loading BGE-M3 takes seconds and a
    survey embeds every chunk in one pass."""
    if not embeddings_available():
        raise EmbeddingsUnavailable(
            "sentence-transformers is not installed — `pip install "
            '"kusudaemon[retrieval]"` enables embedding mode'
        )
    encoder = _model_cache.get(model_name)
    if encoder is None:
        from sentence_transformers import SentenceTransformer
        from sentence_transformers import util as _util

        model = SentenceTransformer(model_name)

        def encoder_batch(batch: list[str]) -> list[list[float]]:
            vectors = model.encode(
                batch, batch_size=batch_size, normalize_embeddings=True
            )
            return [list(vector) for vector in vectors]

        encoder = encoder_batch
        _model_cache[model_name] = encoder
    return encoder(list(texts))


def cosine(a: list[float], b: list[float]) -> float:
    """Plain dot product — inputs from ``embed_texts`` are already
    normalized. Pure stdlib, unit-testable with hand-written vectors and
    no model installed."""
    return sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        or 1.0
    )