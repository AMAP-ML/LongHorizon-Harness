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


def detect_runtime_signals(log: str) -> list[dict[str, str]]:
    runtime_log = tool_output_view(log)
    signals: list[dict[str, str]] = []
    for pattern in _CRASH_PATTERNS:
        match = pattern.search(runtime_log)
        if match:
            signals.append({"signal": match.group(0), "evidence": _near(runtime_log, match.start())})
    return signals


def _near(text: str, index: int, radius: int = 240) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + radius)
    return text[start:end]
