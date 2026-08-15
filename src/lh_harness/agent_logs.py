"""Normalized reads of the machine logs the agent CLIs print on stdout.

Each backend speaks a different format: Claude Code emits
``--output-format stream-json`` while ``codex exec --json`` emits thread events.
The harness only ever needs three views of either: the assistant-visible text,
an ordered step list for the dashboard, and a tool/command output view for crash
detection, so format handling lives here instead of being re-derived at every
call site.
"""

from __future__ import annotations

from collections import deque
import io
import json
import re
from typing import Any, Iterator

CLAUDE_STREAM_JSON = "claude_stream_json"
CODEX_EXEC_JSON = "codex_exec_json"
DEEPSEEK_HARNESS_JSONL = "deepseek_harness_jsonl"
OPENCODE_RUN_JSON = "opencode_run_json"
# Untyped chat transcripts (one `{"role": ..., "content": ...}` object per line,
# optionally wrapped in `message`). Neither CLI emits this directly, but saved
# chat.jsonl transcripts and older runs use it.
CHAT_JSONL = "chat_jsonl"
UNKNOWN = ""

# Harness-normalized label for a Codex turn that died before answering, so it
# joins the same runtime-signal path as the `AGENT_EXIT=` convention.
TURN_FAILED_SIGNAL = "AGENT_TURN_FAILED"

_CODEX_EVENTS = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "item.started",
    "item.updated",
    "item.completed",
}
_CLAUDE_EVENTS = {"system", "assistant", "user", "result"}
# OpenCode `run --format json` emits one JSON event per line.  "error" is
# shared with Codex's `--json` protocol, so it is only matched after the Codex
# and Claude event families, which real logs open with before any error record.
_OPENCODE_EVENTS = {"step_start", "step_finish", "tool_use", "text", "error"}
# Codex items that record an action rather than assistant prose.
_CODEX_TOOL_ITEMS = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "dynamic_tool_call",
    "collab_tool_call",
    "web_search",
    "todo_list",
}


def detect_format(raw: str) -> str:
    saw_chat_role = False
    for record in _json_records(raw):
        record_type = record.get("type")
        if record_type == "dsh.result":
            return DEEPSEEK_HARNESS_JSONL
        if record_type in _CODEX_EVENTS:
            return CODEX_EXEC_JSON
        if record_type in _CLAUDE_EVENTS:
            return CLAUDE_STREAM_JSON
        if record_type in _OPENCODE_EVENTS:
            return OPENCODE_RUN_JSON
        if _chat_message(record) is not None:
            saw_chat_role = True
    return CHAT_JSONL if saw_chat_role else UNKNOWN


def visible_output(raw: str) -> str:
    """Return the final assistant-visible text, or "" for an unknown format."""
    log_format = detect_format(raw)
    if log_format == DEEPSEEK_HARNESS_JSONL:
        texts = _deepseek_assistant_texts(raw)
        return texts[-1].strip() if texts else ""
    if log_format == CODEX_EXEC_JSON:
        texts = _codex_assistant_texts(raw)
        return texts[-1].strip() if texts else ""
    if log_format == CLAUDE_STREAM_JSON:
        result_text, assistant_texts = _claude_texts(raw)
        if result_text.strip():
            return result_text.strip()
        return "\n\n".join(assistant_texts).strip()
    if log_format == OPENCODE_RUN_JSON:
        texts = _opencode_texts(raw)
        return texts[-1].strip() if texts else ""
    if log_format == CHAT_JSONL:
        texts = _chat_assistant_texts(raw)
        return texts[-1].strip() if texts else ""
    return ""


def assistant_texts(raw: str) -> list[str]:
    """Return every assistant text block in order, oldest first."""
    log_format = detect_format(raw)
    if log_format == DEEPSEEK_HARNESS_JSONL:
        return _deepseek_assistant_texts(raw)
    if log_format == CODEX_EXEC_JSON:
        return _codex_assistant_texts(raw)
    if log_format == CLAUDE_STREAM_JSON:
        result_text, texts = _claude_texts(raw)
        if result_text.strip() and (not texts or texts[-1].strip() != result_text.strip()):
            texts.append(result_text)
        return texts
    if log_format == OPENCODE_RUN_JSON:
        return _opencode_texts(raw)
    if log_format == CHAT_JSONL:
        return _chat_assistant_texts(raw)
    return []


def tool_output_view(raw: str) -> str:
    """Return tool/command output plus non-JSON lines, for crash detection."""
    log_format = detect_format(raw)
    parts: list[str] = []
    for line in str(raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            parts.append(line)
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            parts.append(line)
            continue
        if not isinstance(record, dict):
            continue
        if log_format == DEEPSEEK_HARNESS_JSONL:
            if record.get("type") != "dsh.result" or not record.get("is_error"):
                continue
            text = record.get("error") or record.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        elif log_format == CODEX_EXEC_JSON:
            parts.extend(_codex_tool_output(record))
        elif log_format == CLAUDE_STREAM_JSON:
            parts.extend(_claude_tool_output(record))
        elif log_format == OPENCODE_RUN_JSON:
            parts.extend(_opencode_tool_output(record))
    return "\n".join(part for part in parts if part)


def runtime_event_view(raw: str) -> str:
    """Return provider/runtime failures without including task tool output.

    Command output is untrusted task data and may legitimately contain source
    examples such as ``{"type": "turn.failed"}`` or ``AGENT_TURN_FAILED``.
    Only top-level CLI protocol events (plus exact legacy wrapper markers) may
    become hard runtime signals.
    """

    log_format = detect_format(raw)
    parts: list[str] = []
    for line in str(raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            if re.fullmatch(r"AGENT_EXIT=[1-9]\d*", stripped):
                parts.append(stripped)
            elif stripped in {"Connection error.", "response.failed"}:
                parts.append(stripped)
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record_type = str(record.get("type") or "")
        if log_format == DEEPSEEK_HARNESS_JSONL:
            if record_type != "dsh.result" or not record.get("is_error"):
                continue
            detail = record.get("error") or record.get("text") or "DeepSeek Harness failed"
            parts.append(f"response.failed: {detail}")
        elif log_format == CODEX_EXEC_JSON:
            if record_type == "turn.failed":
                error = record.get("error")
                message = error.get("message") if isinstance(error, dict) else error
                parts.append(f"{TURN_FAILED_SIGNAL}: {message or 'codex turn failed'}")
            elif record_type == "error":
                message = record.get("message") or record.get("error")
                if message:
                    parts.append(str(message))
        elif log_format == CLAUDE_STREAM_JSON and record_type == "result" and record.get("is_error"):
            message = record.get("result") or record.get("error") or record.get("subtype") or "claude response failed"
            parts.append(f"response.failed: {message}")
        elif log_format == OPENCODE_RUN_JSON and record_type == "error":
            parts.append(f"response.failed: {_opencode_error_message(record)}")
    return "\n".join(parts)


def parse_trajectory(raw: str, *, max_steps: int | None = None) -> list[dict[str, Any]]:
    """Turn a raw agent log into ordered, UI-friendly steps.

    Step kinds are shared across backends (``session``, ``thinking``, ``text``,
    ``tool_use``, ``tool_result``, ``result``) so the dashboard renders any agent
    without knowing which CLI produced the log.
    """
    bounded_steps = None if max_steps is None else max(1, int(max_steps))
    log_format = detect_format(raw)
    if log_format == DEEPSEEK_HARNESS_JSONL:
        return _deepseek_trajectory(raw, max_steps=bounded_steps)
    if log_format == CODEX_EXEC_JSON:
        return _codex_trajectory(raw, max_steps=bounded_steps)
    if log_format == CLAUDE_STREAM_JSON:
        return _claude_trajectory(raw, max_steps=bounded_steps)
    if log_format == OPENCODE_RUN_JSON:
        return _opencode_trajectory(raw, max_steps=bounded_steps)
    return []


# ----------------------------------------------------------------------------
# DeepSeek Harness (`dsh --profile headless` through deepseek_runner)
# ----------------------------------------------------------------------------


def _deepseek_assistant_texts(raw: str) -> list[str]:
    texts: list[str] = []
    for record in _json_records(raw):
        if record.get("type") != "dsh.result":
            continue
        text = record.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts


def _deepseek_trajectory(
    raw: str,
    *,
    max_steps: int | None = None,
) -> list[dict[str, Any]]:
    steps: deque[dict[str, Any]] = deque(maxlen=max_steps)
    for record in _json_records(raw):
        if record.get("type") != "dsh.result":
            continue
        text = record.get("text")
        error = record.get("error")
        step: dict[str, Any] = {
            "kind": "result",
            "text": text.strip() if isinstance(text, str) else "",
            "is_error": bool(record.get("is_error")),
        }
        if isinstance(record.get("exit_code"), int):
            step["exit_code"] = record["exit_code"]
        if isinstance(error, str) and error.strip():
            step["error"] = error.strip()
        steps.append(step)
    return list(steps)


# ----------------------------------------------------------------------------
# Codex (`codex exec --json`)
# ----------------------------------------------------------------------------


def _codex_assistant_texts(raw: str) -> list[str]:
    texts: list[str] = []
    for record in _json_records(raw):
        if record.get("type") != "item.completed":
            continue
        item = record.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts


def _codex_tool_output(record: dict[str, Any]) -> list[str]:
    record_type = record.get("type")
    if record_type == "turn.failed":
        error = record.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return [f"{TURN_FAILED_SIGNAL}: {message or 'codex turn failed'}"]
    if record_type == "error":
        message = record.get("message")
        return [str(message)] if message else []
    if record_type not in {"item.completed", "item.updated"}:
        return []
    item = record.get("item")
    if not isinstance(item, dict):
        return []
    item_type = item.get("type")
    if item_type == "command_execution":
        output = item.get("aggregated_output")
        return [str(output)] if output else []
    if item_type == "mcp_tool_call":
        text, _ = _content_blocks_to_text(_codex_mcp_content(item))
        return [text] if text else []
    if item_type == "error":
        message = item.get("message")
        return [str(message)] if message else []
    return []


def _codex_trajectory(raw: str, *, max_steps: int | None = None) -> list[dict[str, Any]]:
    # A 16 MiB JSONL file can still contain well over 100,000 tiny events.
    # Keep the newest UI-relevant steps in a ring buffer so a bounded input
    # cannot expand into an unbounded response/DOM. ``None`` preserves the
    # complete-parser behavior used outside the dashboard.
    steps: deque[dict[str, Any]] = deque(maxlen=max_steps)
    # Codex emits `item.started` then `item.completed` for tool items, but a
    # log tailed mid-run can be missing either side; track ids so each action
    # still renders exactly one call step and at most one result step.  The
    # bounded dashboard path also bounds this bookkeeping set; otherwise a
    # stream of tiny ``item.started`` records could consume memory even while
    # the returned step deque stayed small.
    started_ids: set[str] = set()
    started_order: deque[str] | None = deque() if max_steps is not None else None
    seen_cap = max(32, (max_steps or 0) * 4) if max_steps is not None else None
    for record in _json_records(raw):
        record_type = record.get("type")
        if record_type == "thread.started":
            steps.append(
                {
                    "kind": "session",
                    "model": "codex",
                    "cwd": "",
                    "mcp_servers": [],
                    "tool_count": 0,
                    "thread_id": record.get("thread_id", ""),
                }
            )
            continue
        if record_type == "error":
            message = record.get("message")
            if message:
                steps.append(_tool_result_step("", str(message), [], True))
            continue
        if record_type == "turn.failed":
            error = record.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            steps.append({"kind": "result", "text": str(message or "codex turn failed"), "is_error": True})
            continue
        if record_type == "turn.completed":
            steps.append(_codex_result_step(record, steps))
            continue
        if record_type not in {"item.started", "item.completed"}:
            continue
        item = record.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        item_id = str(item.get("id") or "")
        if item_type == "agent_message":
            text = item.get("text")
            if record_type == "item.completed" and isinstance(text, str) and text.strip():
                steps.append({"kind": "text", "text": text})
            continue
        if item_type == "reasoning":
            text = item.get("text") or "\n".join(
                part for part in (item.get("summary") or []) if isinstance(part, str)
            )
            if record_type == "item.completed" and isinstance(text, str) and text.strip():
                steps.append({"kind": "thinking", "text": text})
            continue
        if item_type == "error":
            if record_type == "item.completed":
                steps.append(_tool_result_step(item_id, str(item.get("message") or ""), [], True))
            continue
        if item_type not in _CODEX_TOOL_ITEMS:
            continue
        if record_type == "item.started" or item_id not in started_ids:
            if item_id not in started_ids:
                started_ids.add(item_id)
                if started_order is not None:
                    started_order.append(item_id)
                    while len(started_order) > (seen_cap or 0):
                        started_ids.discard(started_order.popleft())
            steps.append(_codex_tool_use_step(item_id, item_type, item))
        if record_type == "item.completed":
            result = _codex_tool_result_step(item_id, item_type, item)
            if result is not None:
                steps.append(result)
    return list(steps)


def _codex_tool_use_step(item_id: str, item_type: str, item: dict[str, Any]) -> dict[str, Any]:
    if item_type == "command_execution":
        return {"kind": "tool_use", "id": item_id, "name": "shell", "input": {"command": item.get("command", "")}}
    if item_type == "file_change":
        return {"kind": "tool_use", "id": item_id, "name": "apply_patch", "input": {"changes": item.get("changes") or []}}
    if item_type == "mcp_tool_call":
        name = f"{item.get('server', '')}/{item.get('tool', '')}".strip("/")
        arguments = item.get("arguments")
        return {
            "kind": "tool_use",
            "id": item_id,
            "name": name or "mcp",
            "input": arguments if isinstance(arguments, dict) else {"arguments": arguments},
        }
    if item_type == "web_search":
        return {"kind": "tool_use", "id": item_id, "name": "web_search", "input": {"query": item.get("query", "")}}
    if item_type == "todo_list":
        return {"kind": "tool_use", "id": item_id, "name": "todo_list", "input": {"items": item.get("items") or []}}
    payload = {key: value for key, value in item.items() if key not in {"id", "type", "status"}}
    return {"kind": "tool_use", "id": item_id, "name": item_type, "input": payload}


def _codex_tool_result_step(item_id: str, item_type: str, item: dict[str, Any]) -> dict[str, Any] | None:
    failed = str(item.get("status") or "") == "failed"
    if item_type == "command_execution":
        exit_code = item.get("exit_code")
        text = str(item.get("aggregated_output") or "")
        if exit_code is not None:
            text = f"{text}\n[exit_code={exit_code}]".strip()
        return _tool_result_step(item_id, text, [], failed or _nonzero_exit_code(exit_code), status=item.get("status"))
    if item_type == "file_change":
        changes = item.get("changes") or []
        lines = [
            f"{change.get('kind', '')} {change.get('path', '')}".strip()
            for change in changes
            if isinstance(change, dict)
        ]
        return _tool_result_step(item_id, "\n".join(lines), [], failed, status=item.get("status"))
    if item_type == "mcp_tool_call":
        error = item.get("error")
        if isinstance(error, dict) and error.get("message"):
            return _tool_result_step(item_id, str(error["message"]), [], True, status="failed")
        text, images = _content_blocks_to_text(_codex_mcp_content(item))
        return _tool_result_step(item_id, text, images, failed, status=item.get("status"))
    if item_type in {"web_search", "todo_list"}:
        # These Codex items often carry no textual result, but item.completed
        # is still authoritative lifecycle evidence.  Emitting an empty paired
        # result prevents the UI from showing a permanently spinning tool row.
        return _tool_result_step(item_id, "", [], failed, status=item.get("status") or "completed")
    return _tool_result_step(item_id, "", [], failed, status=item.get("status"))


def _codex_mcp_content(item: dict[str, Any]) -> Any:
    result = item.get("result")
    if isinstance(result, dict):
        return result.get("content")
    return None


def _codex_result_step(record: dict[str, Any], steps: Any) -> dict[str, Any]:
    # Reuse the trailing assistant text so the dashboard's duplicate-text drop
    # keeps a single final answer, the same way it does for Claude.
    final_text = ""
    for step in reversed(steps):
        if step.get("kind") == "text":
            final_text = str(step.get("text") or "")
            break
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    return {
        "kind": "result",
        "text": final_text,
        "is_error": False,
        "num_turns": 1,
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


# ----------------------------------------------------------------------------
# Claude Code (`--output-format stream-json`)
# ----------------------------------------------------------------------------


def _claude_texts(raw: str) -> tuple[str, list[str]]:
    result_text = ""
    texts: list[str] = []
    for record in _json_records(raw):
        record_type = record.get("type")
        if record_type == "result" and isinstance(record.get("result"), str):
            result_text = record["result"]
            continue
        if record_type != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
    return result_text, texts


def _claude_tool_output(record: dict[str, Any]) -> list[str]:
    if record.get("type") != "user":
        return []
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return []
    parts: list[str] = []
    for block in message["content"]:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            text, _ = _content_blocks_to_text(block.get("content"))
            if text:
                parts.append(text)
    return parts


def _claude_trajectory(raw: str, *, max_steps: int | None = None) -> list[dict[str, Any]]:
    steps: deque[dict[str, Any]] = deque(maxlen=max_steps)
    for record in _json_records(raw):
        record_type = record.get("type")
        if record_type == "system":
            if record.get("subtype") == "init":
                servers = record.get("mcp_servers") or []
                steps.append(
                    {
                        "kind": "session",
                        "model": record.get("model", ""),
                        "cwd": record.get("cwd", ""),
                        "mcp_servers": [s.get("name") for s in servers if isinstance(s, dict)],
                        "tool_count": len(record.get("tools") or []),
                    }
                )
            # skip thinking_tokens and other noisy system deltas
            continue
        if record_type == "assistant":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            for block in message.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "thinking":
                    text = block.get("thinking")
                    if isinstance(text, str) and text.strip():
                        steps.append({"kind": "thinking", "text": text})
                elif block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        steps.append({"kind": "text", "text": text})
                elif block_type == "tool_use":
                    steps.append(
                        {
                            "kind": "tool_use",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input": block.get("input") or {},
                        }
                    )
            continue
        if record_type == "user":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text, images = _content_blocks_to_text(block.get("content"))
                steps.append(
                    _tool_result_step(
                        str(block.get("tool_use_id", "")), text, images, bool(block.get("is_error"))
                    )
                )
            continue
        if record_type == "result":
            result_text = record.get("result", "") if isinstance(record.get("result"), str) else ""
            # Claude's final `result` copies the last assistant text block, so it
            # would render twice. Drop the duplicate and keep the richer step.
            buffered = list(steps)
            for index in range(len(buffered) - 1, -1, -1):
                if buffered[index]["kind"] == "text":
                    if buffered[index].get("text", "").strip() == result_text.strip():
                        buffered.pop(index)
                    break
            steps = deque(buffered, maxlen=max_steps)
            steps.append(
                {
                    "kind": "result",
                    "text": result_text,
                    "is_error": bool(record.get("is_error")),
                    "duration_ms": record.get("duration_ms"),
                    "num_turns": record.get("num_turns"),
                    "cost_usd": record.get("total_cost_usd"),
                }
            )
    return list(steps)


# ----------------------------------------------------------------------------
# OpenCode (`opencode run --format json`)
# ----------------------------------------------------------------------------


def _opencode_texts(raw: str) -> list[str]:
    texts: list[str] = []
    for record in _json_records(raw):
        if record.get("type") != "text":
            continue
        part = record.get("part")
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts


def _opencode_tool_output(record: dict[str, Any]) -> list[str]:
    if record.get("type") != "tool_use":
        return []
    part = record.get("part")
    if not isinstance(part, dict) or part.get("type") != "tool":
        return []
    state = part.get("state")
    if not isinstance(state, dict) or str(state.get("status") or "") != "completed":
        return []
    output = state.get("output")
    if isinstance(output, str) and output.strip():
        return [output]
    return []


def _opencode_error_message(record: dict[str, Any]) -> str:
    payload = record.get("error")
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict):
            message = data.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        name = payload.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    message = record.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "opencode run failed"


def _opencode_trajectory(raw: str, *, max_steps: int | None = None) -> list[dict[str, Any]]:
    steps: deque[dict[str, Any]] = deque(maxlen=max_steps)
    seen_ids: set[str] = set()
    seen_order: deque[str] | None = deque() if max_steps is not None else None
    seen_cap = max(32, (max_steps or 0) * 4) if max_steps is not None else None
    for record in _json_records(raw):
        record_type = record.get("type")
        part = record.get("part") if isinstance(record.get("part"), dict) else {}
        part_type = part.get("type")
        if record_type == "text" and part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                steps.append({"kind": "text", "text": text})
            continue
        if record_type == "tool_use" and part_type == "tool":
            _opencode_tool_steps(record, part, steps, seen_ids, seen_order, seen_cap)
            continue
        if record_type == "step_finish" and str(part.get("reason") or "") == "stop":
            steps.append(_opencode_result_step(record, part, steps))
    return list(steps)


def _opencode_tool_steps(
    record: dict[str, Any],
    part: dict[str, Any],
    steps: deque[dict[str, Any]],
    seen_ids: set[str],
    seen_order: deque[str] | None,
    seen_cap: int | None,
) -> None:
    part_id = str(part.get("id") or record.get("id") or "")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    status = str(state.get("status") or "")
    if part_id not in seen_ids:
        seen_ids.add(part_id)
        if seen_order is not None:
            seen_order.append(part_id)
            while len(seen_order) > (seen_cap or 0):
                seen_ids.discard(seen_order.popleft())
        input_value = state.get("input")
        steps.append(
            {
                "kind": "tool_use",
                "id": part_id,
                "name": str(part.get("tool") or "tool"),
                "input": input_value if isinstance(input_value, dict) else {"input": input_value},
            }
        )
    if status in {"completed", "failed", "error"}:
        output = state.get("output")
        text = str(output) if output is not None else ""
        steps.append(_tool_result_step(part_id, text, [], status != "completed", status=status))


def _opencode_result_step(
    record: dict[str, Any],
    part: dict[str, Any],
    steps: deque[dict[str, Any]],
) -> dict[str, Any]:
    final_text = ""
    for step in reversed(steps):
        if step.get("kind") == "text":
            final_text = str(step.get("text") or "")
            break
    tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    result: dict[str, Any] = {
        "kind": "result",
        "text": final_text,
        "is_error": False,
        "num_turns": 1,
        "input_tokens": tokens.get("input"),
        "output_tokens": tokens.get("output"),
        "reasoning_tokens": tokens.get("reasoning"),
        "cached_input_tokens": cache.get("read"),
    }
    cost = part.get("cost")
    if isinstance(cost, (int, float)):
        result["cost_usd"] = cost
    return result


# ----------------------------------------------------------------------------
# Untyped chat transcripts
# ----------------------------------------------------------------------------


def _chat_message(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return the assistant message in an untyped chat record, else None."""
    if record.get("type") in _CODEX_EVENTS or record.get("type") in _CLAUDE_EVENTS:
        return None
    message = record.get("message") if isinstance(record.get("message"), dict) else record
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    return message


def _chat_assistant_texts(raw: str) -> list[str]:
    texts: list[str] = []
    for record in _json_records(raw):
        message = _chat_message(record)
        if message is None:
            continue
        text, _ = _content_blocks_to_text(message.get("content"))
        if text.strip():
            texts.append(text)
    return texts


# ----------------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------------


def _json_records(raw: str) -> Iterator[dict[str, Any]]:
    # ``splitlines`` allocates a second list containing every line. Streaming
    # from StringIO keeps peak memory proportional to one record plus the
    # caller's intentionally bounded step buffer.
    for line in io.StringIO(str(raw or "")):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _tool_result_step(
    tool_use_id: str,
    text: str,
    images: list[str],
    is_error: bool,
    *,
    status: Any = None,
) -> dict[str, Any]:
    result = {
        "kind": "tool_result",
        "tool_use_id": tool_use_id,
        "text": text,
        "images": images,  # data URLs with the base64 payload embedded
        "has_image": bool(images),
        "is_error": is_error,
    }
    if status is not None and str(status).strip():
        result["status"] = str(status)
    return result


def _nonzero_exit_code(value: Any) -> bool:
    """Interpret numeric exit codes without treating the string ``"0"`` as failure."""

    if isinstance(value, bool) or value is None:
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return False


def _content_blocks_to_text(content: Any) -> tuple[str, list[str]]:
    """Flatten a tool-result payload into text plus embedded image data URLs.

    Both Anthropic (``source.data`` / ``source.media_type``) and MCP
    (``data`` / ``mimeType``) image block shapes are accepted so screenshots
    render inline without any extra file writes.
    """
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return ("" if content is None else str(content)), []
    parts: list[str] = []
    images: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block_type == "image":
            data_url = _image_block_to_data_url(block)
            if data_url:
                images.append(data_url)
                parts.append("[image]")
    return "\n".join(parts), images


def _image_block_to_data_url(block: dict[str, Any]) -> str:
    source = block.get("source") if isinstance(block.get("source"), dict) else block
    data = source.get("data")
    if not isinstance(data, str) or not data:
        return ""
    media_type = source.get("media_type") or source.get("mimeType") or "image/png"
    return f"data:{media_type};base64,{data}"
