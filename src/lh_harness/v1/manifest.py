"""manifest.jsonl (PLAN.md §6) — one machine-written line per completed leaf.

Everything here is derived by the harness from the artifact — no model
involvement — except ``promotion``, which is the writer's own capped
handoff (PLAN.md §13 v1 scope: "Writer returns capped at ~400 tokens"). The
richer §6 fields that need node-type templates (``headers``, ``terms_defined``,
``refs_out``, ``problems``) are v2+ (they depend on the planner's type
system); this is the v1 subset that's derivable without one.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .gates import GateResult, estimate_tokens

PROMOTION_TOKEN_CAP = 400


def cap_promotion(text: str, limit: int = PROMOTION_TOKEN_CAP) -> str:
    text = text.strip()
    if estimate_tokens(text) <= limit:
        return text
    words = text.split()
    # estimate_tokens uses words/0.75 tokens-per-word; invert it to size the
    # truncation in words.
    approx_word_limit = max(1, int(limit * 0.75))
    return " ".join(words[:approx_word_limit]) + " …[truncated to fit promotion budget]"


def append_manifest_line(
    manifest_path: str | Path,
    *,
    node_id: str,
    artifact_path: str,
    artifact_text: str,
    gate_results: list[GateResult],
    promotion: str,
) -> dict[str, Any]:
    line = {
        "node": node_id,
        "artifact": str(artifact_path),
        "tokens": estimate_tokens(artifact_text),
        "gates": "pass" if all(result.passed for result in gate_results) else "fail",
        "unmet_gates": [
            {"gate": result.gate, "detail": result.detail}
            for result in gate_results
            if not result.passed
        ],
        "promotion": cap_promotion(promotion),
        "ts": time.time(),
    }
    with open(manifest_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
    return line


def read_manifest_tail(manifest_path: str | Path, n: int = 10) -> list[dict[str, Any]]:
    path = Path(manifest_path)
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    results: list[dict[str, Any]] = []
    for line in lines[-n:]:
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return results
