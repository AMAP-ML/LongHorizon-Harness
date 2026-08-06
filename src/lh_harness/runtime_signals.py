from __future__ import annotations

import re

from .agent_logs import TURN_FAILED_SIGNAL, tool_output_view

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
