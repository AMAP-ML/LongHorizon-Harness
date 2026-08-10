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
        "gates": (
            "pass"
            if gate_results and all(result.passed for result in gate_results)
            else "fail"
        ),
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


def read_all_manifest_entries(manifest_path: str | Path) -> list[dict[str, Any]]:
    """Last line per node, in first-seen document order (§11.10.6).

    Every dispatch appends a line (``round_loop.dispatch`` and
    ``repair.run_repair`` both append unconditionally), so a thrice-retried
    node contributes three rows; readers that iterate this list (document
    review's index, ``prompts._promotions_of``, checks) would carry one
    index row per attempt. The terminal line is the record of the node's
    current state — the earlier rows are history, exactly like the
    approvals reader's latest-record-wins merge. Unlike
    ``read_manifest_tail``, not capped to the last ``n`` — PLAN-zeromem.md
    §8.4 builds this whole index (promotions + tokens) because it's already
    a token-capped summary of every completed leaf, paid for once at
    dispatch time rather than re-derived from full artifact text."""
    path = Path(manifest_path)
    if not path.exists():
        return []
    merged: list[dict[str, Any]] = []
    by_node: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        node = str(entry.get("node", ""))
        if node in by_node:
            merged[by_node[node]] = entry
        else:
            by_node[node] = len(merged)
            merged.append(entry)
    return merged


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
