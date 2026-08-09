"""Runtime crash-signal detection for agent logs (gptme JSONL only).

gptme's worker emits one JSON object per message
(``{"type": "message", "role": ..., "content": ...}``); everything not JSON is
free text. Crash detection must skip the assistant's own prose (a script
raising inside the task legitimately prints a Traceback into tool output) and
hunt for the markers that mean the agent *runtime* itself died — non-zero
command exits, gptme's own failed-turn marker, connection failures.
"""

from __future__ import annotations

import json
import re

# Harness-normalized label for a gptme turn that died before answering, so it
# joins the same runtime-signal path as the `AGENT_EXIT=` convention.
TURN_FAILED_SIGNAL = "AGENT_TURN_FAILED"

_CRASH_PATTERNS = (
    re.compile(r"AGENT_EXIT=([1-9]\d*)"),
    re.compile(re.escape(TURN_FAILED_SIGNAL)),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"Connection error\."),
    re.compile(r"response\.failed"),
)

# A Traceback can legitimately appear in tool output (an agent running a script
# that raises), so only signals that mean the agent runtime itself died count as
# hard failures.
_HARD_SIGNAL_PREFIXES = ("AGENT_EXIT=", TURN_FAILED_SIGNAL)
_HARD_SIGNAL_VALUES = frozenset({"Connection error.", "response.failed"})


def tool_output_view(raw: str) -> str:
    """Isolate tool output from assistant/user prose, for crash detection.

    Keeps every non-JSON line plus any JSON record that is not a plain
    assistant/user message (tool results, system notes, gptme metadata),
    which is where crashes actually surface.
    """
    parts: list[str] = []
    for line in str(raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("{"):
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                parts.append(line)
                continue
            if isinstance(record, dict) and record.get("type") == "message" and record.get("role") in ("assistant", "user"):
                continue
            parts.append(line)
            continue
        parts.append(line)
    return "\n".join(part for part in parts if part)


def detect_runtime_signals(log: str) -> list[dict[str, str]]:
    runtime_log = tool_output_view(log)
    signals: list[dict[str, str]] = []
    for pattern in _CRASH_PATTERNS:
        match = pattern.search(runtime_log)
        if match:
            signals.append({"signal": match.group(0), "evidence": _near(runtime_log, match.start())})
    return signals


def _signal_labels(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    for item in raw:
        signal = item.get("signal") if isinstance(item, dict) else item
        if isinstance(signal, str) and signal.strip():
            labels.append(signal.strip())
    return labels


def hard_signal_labels(raw: object) -> list[str]:
    """Labels for signals that mean the agent runtime failed, not the task."""
    return [
        label
        for label in _signal_labels(raw)
        if label.startswith(_HARD_SIGNAL_PREFIXES) or label in _HARD_SIGNAL_VALUES
    ]


def _near(text: str, index: int, radius: int = 240) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + radius)
    return text[start:end]