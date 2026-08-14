"""Shared visible output parser for gptme and CLI adapters.

Extracts the last assistant message from a unified trace stream:
`{"type": "message", "role": "assistant", "content": ...}`.
"""

from __future__ import annotations

import json


def extract_visible_output(raw: str | list[str] | tuple[str, ...]) -> str:
    """Extract the final assistant message from structured trace JSON lines."""
    last_assistant_text = ""
    lines = raw.splitlines() if isinstance(raw, str) else list(raw)
    for line in lines:
        line = line.strip()

        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "message" and event.get("role") == "assistant":
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                last_assistant_text = content
        elif event.get("type") == "assistant":
            content = event.get("message") or event.get("content")
            if isinstance(content, str) and content.strip():
                last_assistant_text = content
    return last_assistant_text



# Backward compatibility alias
gptme_visible_output = extract_visible_output
