"""Provider token usage rollup: trajectory parsing and report aggregation."""

from __future__ import annotations

import json

from lh_harness.agent_logs import token_usage
from lh_harness.manager import _aggregate_token_usage, _round_token_usage
from lh_harness.types import EpisodeResult, ManagedRound


def test_token_usage_sums_opencode_step_finish_tokens() -> None:
    raw = "\n".join(
        json.dumps(record)
        for record in (
            {
                "type": "step_finish",
                "part": {
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 8}},
                    "cost": 0.25,
                },
            },
            {
                "type": "step_finish",
                "part": {
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {"input": 30, "output": 6, "cache": {"read": 2}},
                },
            },
        )
    )
    usage = token_usage(raw)
    assert usage["input_tokens"] == 130
    assert usage["output_tokens"] == 26
    assert usage["reasoning_tokens"] == 5
    assert usage["cached_input_tokens"] == 10
    assert usage["cost_usd"] == 0.25


def test_token_usage_understands_codex_usage_records() -> None:
    raw = json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 50, "cached_input_tokens": 4, "output_tokens": 9},
        }
    )
    usage = token_usage(raw)
    assert usage["input_tokens"] == 50
    assert usage["cached_input_tokens"] == 4
    assert usage["output_tokens"] == 9
    assert usage["cost_usd"] == 0


def test_token_usage_ignores_non_numeric_noise() -> None:
    raw = json.dumps({"type": "step_finish", "part": {"tokens": {"input": "many"}}})
    usage = token_usage(raw)
    assert usage["input_tokens"] == 0


def test_round_token_usage_merges_role_episodes() -> None:
    executor = EpisodeResult(
        status="done",
        metadata={"token_usage": {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.1}},
    )
    auditor = EpisodeResult(
        status="done",
        metadata={"token_usage": {"input_tokens": 40, "output_tokens": 4}},
    )
    merged = _round_token_usage(executor, auditor)
    assert merged["input_tokens"] == 140
    assert merged["output_tokens"] == 14
    assert merged["cost_usd"] == 0.1


def test_aggregate_token_usage_sums_rounds_and_skips_empty() -> None:
    rounds = [
        ManagedRound(round_index=1, next_step="cli", plan_text="p", token_usage={"input_tokens": 10, "output_tokens": 2}),
        ManagedRound(round_index=2, next_step="cli", plan_text="p", token_usage={"input_tokens": 5, "cost_usd": 0.3}),
        ManagedRound(round_index=3, next_step="cli", plan_text="p"),
    ]
    totals = _aggregate_token_usage(rounds)
    assert totals["input_tokens"] == 15
    assert totals["output_tokens"] == 2
    assert totals["cost_usd"] == 0.3
    # A run with zero recorded usage reports an empty block, not zeroes.
    assert _aggregate_token_usage([ManagedRound(round_index=1, next_step="cli", plan_text="p")]) == {}
