from __future__ import annotations

import json

from lh_harness.runtime_signals import detect_runtime_signals, hard_signal_labels


def _jsonl(*records: dict[str, object]) -> str:
    return "\n".join(json.dumps(record) for record in records) + "\n"


def test_source_code_in_command_output_cannot_fake_a_runtime_failure() -> None:
    log = _jsonl(
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "command_execution",
                "status": "completed",
                "aggregated_output": (
                    'Result(status="error", actions_log=json.dumps('
                    '{"type": "turn.failed", "error": {"message": message}}), '
                    'metadata={"runtime_signals": [{"signal": "AGENT_TURN_FAILED"}]})\n'
                    "Connection error.\nresponse.failed\nAGENT_EXIT=9"
                ),
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "item-2", "type": "agent_message", "text": "Task completed."},
        },
        {"type": "turn.completed", "usage": {}},
    )

    assert hard_signal_labels(detect_runtime_signals(log)) == []


def test_top_level_turn_failed_event_is_a_hard_runtime_failure() -> None:
    log = _jsonl(
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.failed", "error": {"message": "model access denied"}},
    )

    signals = detect_runtime_signals(log)

    assert hard_signal_labels(signals) == ["AGENT_TURN_FAILED"]
    assert "model access denied" in signals[0]["evidence"]
