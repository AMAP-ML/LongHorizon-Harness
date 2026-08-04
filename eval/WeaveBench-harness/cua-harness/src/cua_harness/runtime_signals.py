from __future__ import annotations

import json
import re


_CRASH_PATTERNS = (
    re.compile(r"AGENT_EXIT=([1-9]\d*)"),
    re.compile(r"IMAGE_PROXY_FAILED[^\n]*"),
    re.compile(r"OSError: \[Errno 98\] Address already in use"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"Connection error\."),
    re.compile(r"response\.failed"),
)


def detect_runtime_signals(log: str) -> list[dict[str, str]]:
    runtime_log = _runtime_log_view(log)
    signals: list[dict[str, str]] = []
    for pattern in _CRASH_PATTERNS:
        match = pattern.search(runtime_log)
        if match:
            signals.append({"signal": match.group(0), "evidence": _near(runtime_log, match.start())})
    return signals


def _runtime_log_view(log: str) -> str:
    return _openclaw_jsonl_view(log, include_roles={"toolResult", "tool"}, keep_non_json=True)


def _openclaw_jsonl_view(log: str, *, include_roles: set[str], keep_non_json: bool) -> str:
    parts: list[str] = []
    for line in log.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            if keep_non_json:
                parts.append(line)
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            if keep_non_json:
                parts.append(line)
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            if keep_non_json and record.get("type") != "message":
                parts.append(line)
            continue
        if message.get("role") not in include_roles:
            continue
        text = _message_text(message)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(item, str):
                chunks.append(item)
        return "\n".join(chunks)
    return ""


def _near(text: str, index: int, radius: int = 240) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + radius)
    return text[start:end]
