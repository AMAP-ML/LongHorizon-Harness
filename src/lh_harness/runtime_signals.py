from __future__ import annotations

import re

from .agent_logs import TURN_FAILED_SIGNAL, runtime_event_view, tool_output_view

_HARD_RUNTIME_PATTERNS = (
    re.compile(r"AGENT_EXIT=([1-9]\d*)"),
    re.compile(re.escape(TURN_FAILED_SIGNAL)),
    re.compile(r"Connection error\."),
    re.compile(r"response\.failed"),
)
_DIAGNOSTIC_TOOL_PATTERNS = (re.compile(r"Traceback \(most recent call last\)"),)

# A Traceback can legitimately appear in tool output (an agent running a script
# that raises), so only signals that mean the agent runtime itself died count as
# hard failures.
_HARD_SIGNAL_PREFIXES = ("AGENT_EXIT=", TURN_FAILED_SIGNAL)
_HARD_SIGNAL_VALUES = frozenset({"Connection error.", "response.failed"})


def detect_runtime_signals(log: str) -> list[dict[str, str]]:
    runtime_events = runtime_event_view(log)
    tool_output = tool_output_view(log)
    signals: list[dict[str, str]] = []
    for pattern in _HARD_RUNTIME_PATTERNS:
        match = pattern.search(runtime_events)
        if match:
            signals.append({"signal": match.group(0), "evidence": _near(runtime_events, match.start())})
    for pattern in _DIAGNOSTIC_TOOL_PATTERNS:
        match = pattern.search(tool_output)
        if match:
            signals.append({"signal": match.group(0), "evidence": _near(tool_output, match.start())})
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
