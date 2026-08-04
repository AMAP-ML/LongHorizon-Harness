"""Shared tool base for the OSWorld agent-as-judge.

This module is the in-VM, tool-using foundation that ``agent_judge_gold``
builds on. It provides the read-only VM tools the judge drives (bash,
read_file, screenshot, read_agent_actions), the OpenAI Responses-API tool
schema (``_TOOLS``) and POST helper (``_responses_post``), the tool dispatcher
(``_dispatch_tool``), the agent-trajectory excerpt extractor
(``_extract_chat_excerpt``), and the ground-truth evaluator-spec reader
(``build_evaluator_hint``).

The judge itself lives in ``agent_judge_gold`` (the gold-aware judge used for
the paper). This file holds no judge logic of its own — only the shared
plumbing gold and ``rejudge_offline`` import.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent_judge")


# ---------------------------------------------------------------------------
# VM tool wrappers — thin shims over WCB's existing _vm_exec / _vm_url helpers
# ---------------------------------------------------------------------------
import requests  # noqa: E402

_VM_EXEC_TIMEOUT = 60


def _vm_url(env, path: str) -> str:
    """Build URL to OSWorld VM REST shim (port from controller)."""
    if hasattr(env, "vm_ip") and env.vm_ip:
        return f"http://{env.vm_ip}:{env.server_port}{path}"
    # Fall back to controller's http_server.
    base = getattr(env.controller, "http_server", None)
    if base:
        return f"{base.rstrip('/')}{path}"
    raise RuntimeError("Cannot resolve VM URL from env")


def _vm_exec(env, cmd: list[str], shell: bool = False,
             timeout: int = _VM_EXEC_TIMEOUT) -> dict:
    """POST to VM /setup/execute. Returns {output, returncode, error}."""
    try:
        r = requests.post(_vm_url(env, "/setup/execute"),
                          json={"command": cmd, "shell": shell},
                          timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"output": "", "error": f"{type(exc).__name__}: {exc}",
                "returncode": -1}


def tool_bash(env, command: str, timeout: int = 30) -> str:
    """Run a bash command in the VM (as `user`). Returns combined stdout+stderr."""
    res = _vm_exec(env, ["bash", "-lc", command], timeout=timeout)
    out = (res.get("output") or "") + ((res.get("error") or "") if res.get("returncode") not in (0, None) else "")
    rc = res.get("returncode", "?")
    # Cap output to keep tokens bounded.
    out = out[:8000]
    return f"[exit={rc}]\n{out}"


def tool_read_file(env, path: str, max_bytes: int = 65536) -> str:
    """Read a text file from the VM. Returns content (UTF-8, truncated)."""
    # Use sudo because some paths (e.g. /etc/, others' home) need root.
    cmd = f"sudo -n head -c {max_bytes} '{path}' 2>&1 || head -c {max_bytes} '{path}' 2>&1"
    res = _vm_exec(env, ["bash", "-lc", cmd], timeout=15)
    rc = res.get("returncode", "?")
    out = (res.get("output") or "")[:max_bytes]
    return f"[exit={rc}] file={path}\n{out}"


def tool_screenshot(env, save_dir: Path, idx: int) -> tuple[str, bytes | None]:
    """Capture VM screen. Returns (filename, png_bytes or None)."""
    try:
        png = env.controller.get_screenshot()
        if not png:
            return ("<empty>", None)
        fname = f"judge_screenshot_{idx:02d}.png"
        (save_dir / fname).write_bytes(png)
        return (fname, png)
    except Exception as exc:
        return (f"<screenshot_failed: {exc}>", None)


# LLM tool schema (OpenAI Responses API)
# ---------------------------------------------------------------------------
_TOOLS = [
    {
        "type": "function",
        "name": "bash",
        "description": "Run a bash command in the VM and return combined stdout+stderr (capped at 8KB). "
                       "Use this to inspect system state, configs, files, settings (gsettings, sqlite3, cat, ls, grep, etc.). "
                       "Read-only inspection only; do NOT mutate state.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to execute."},
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a file from the VM as UTF-8 text (first 64KB). "
                       "Faster than `bash cat` for plain reads. Use sudo automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path in VM."},
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "screenshot",
        "description": "Take a screenshot of the current VM desktop. Returns the image inline.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "read_agent_actions",
        "description": "Return a structured summary of the agent's tool-use history "
                       "during the rollout (bash commands run + result previews; "
                       "no base64 screenshots). Useful to confirm the agent actually "
                       "ran commands rather than only claiming completion in chat.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "final_answer",
        "description": "Submit the final binary verdict. Score is exactly 0 (failed) "
                       "or 1 (succeeded). `evidence` MUST list >=2 concrete VM "
                       "observations supporting the verdict (e.g. 'Preferences JSON "
                       'short_name=\"Microsoft Bing\" (read_file)\'). After this call, '
                       "the judge exits.",
        "parameters": {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "enum": [0, 1],
                          "description": "Final task success score (0 or 1)."},
                "reason": {"type": "string",
                           "description": "Concise rationale (1-3 sentences) explaining the verdict, citing the implementation path the agent took."},
                "evidence": {"type": "array",
                             "items": {"type": "string"},
                             "minItems": 2,
                             "description": "List of >=2 concrete VM observations supporting the verdict."},
            },
            "required": ["score", "reason", "evidence"],
        },
    },
]


def _extract_chat_excerpt(output_dir: Path, max_chars: int = 6000) -> str:
    """Pull a compact summary of the agent's rollout for the judge."""
    chat_path = output_dir / "chat.jsonl"
    if not chat_path.exists():
        return "(no chat.jsonl found — agent may have crashed early)"
    try:
        lines = chat_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as exc:
        return f"(failed to read chat.jsonl: {exc})"

    summary_lines = []
    n_assist_text = 0
    n_tool = 0
    for ln in lines:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        msg = rec.get("message", {}) if isinstance(rec.get("message"), dict) else rec
        role = msg.get("role") or rec.get("role") or rec.get("type")
        content = msg.get("content")
        if role == "assistant":
            if isinstance(content, str) and content.strip():
                n_assist_text += 1
                summary_lines.append(f"[assistant] {content[:280]}")
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        # WCB 2026-06-05: openclaw writes camelCase "toolCall"
                        # (args under "arguments"); Anthropic uses "tool_use"
                        # (input under "input"); OpenAI uses "function_call".
                        # The original code only matched the latter two, so
                        # read_agent_actions reported "0 tool calls" for every
                        # openclaw rollout and the judge decided every case
                        # believing the agent took no actions. Match all three.
                        if c.get("type") in ("tool_use", "function_call", "toolCall"):
                            n_tool += 1
                            name = c.get("name") or "?"
                            inp = c.get("input") or c.get("arguments") or {}
                            summary_lines.append(
                                f"[tool_use:{name}] {json.dumps(inp)[:280]}")
                        elif c.get("type") == "text" and c.get("text", "").strip():
                            n_assist_text += 1
                            summary_lines.append(f"[assistant] {c['text'][:280]}")
        elif role in ("tool", "user", "toolResult") and isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                # openclaw toolResult message: {"role":"toolResult","content":[{"type":"text","text":...}]}
                if c.get("type") == "text" and c.get("text", "").strip():
                    summary_lines.append(f"[tool_result] {c['text'][:160]}")
                elif c.get("type") == "tool_result":
                    res = c.get("content")
                    if isinstance(res, str) and res.strip():
                        summary_lines.append(f"[tool_result] {res[:160]}")
                    elif isinstance(res, list):
                        for r in res:
                            if isinstance(r, dict) and r.get("type") == "text":
                                summary_lines.append(f"[tool_result] {r.get('text','')[:160]}")

    head = summary_lines[:10]
    tail = summary_lines[-15:]
    excerpt = "\n".join(head + ["... (truncated) ..."] + tail) \
        if len(summary_lines) > 25 else "\n".join(summary_lines)
    excerpt = excerpt[:max_chars]
    excerpt += f"\n\n[stats: {n_assist_text} text turns, {n_tool} tool calls]"
    return excerpt


# ---------------------------------------------------------------------------
# Evaluator hint — render OSWorld evaluator block as natural-language hint
# ---------------------------------------------------------------------------
def build_evaluator_hint(task_json_path: str | Path) -> str:
    """Read the OSWorld JSON and produce a NL hint of what the canonical
    evaluator would check (helps the LLM judge focus its inspection)."""
    try:
        d = json.loads(Path(task_json_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return f"(could not parse task JSON: {exc})"
    ev = d.get("evaluator", {}) or {}
    func = ev.get("func", "?")
    result = ev.get("result", {})
    expected = ev.get("expected", {})

    parts = [f"evaluator.func: {func!r}"]
    if isinstance(result, dict):
        rtype = result.get("type", "?")
        parts.append(f"result.getter: get_{rtype}")
        for k, v in result.items():
            if k != "type":
                parts.append(f"  result.{k} = {json.dumps(v)[:200]}")
    elif isinstance(result, list):
        parts.append(f"result: list of {len(result)} getters (multi-metric eval)")
    if isinstance(expected, dict) and expected:
        etype = expected.get("type", "?")
        parts.append(f"expected.getter: get_{etype}")
        for k, v in expected.items():
            if k != "type":
                parts.append(f"  expected.{k} = {json.dumps(v)[:200]}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Judge loop — OpenAI Responses API tool-calling loop
# ---------------------------------------------------------------------------
def _responses_post(base_url: str, api_key: str, body: dict,
                    timeout: int = 600) -> dict:
    """POST to /v1/responses with retries. Returns parsed JSON or raises."""
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}"}
    last_exc = None
    for attempt in range(5):
        try:
            r = requests.post(f"{base_url.rstrip('/')}/responses",
                              headers=headers, json=body, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            # 500 included: the copilot-api /responses endpoint (port 4141)
            # intermittently returns a raw 500 "fetch failed" that succeeds on
            # retry. Treat it as transient alongside the usual 429/5xx.
            if r.status_code in (429, 500, 502, 503, 504):
                sleep_s = 5 * (2 ** attempt)
                logger.warning("judge LLM transient %d on attempt %d; sleep %ds",
                               r.status_code, attempt + 1, sleep_s)
                time.sleep(sleep_s)
                last_exc = RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                continue
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            sleep_s = 5 * (2 ** attempt)
            logger.warning("judge LLM network err %s attempt %d; sleep %ds",
                           exc, attempt + 1, sleep_s)
            time.sleep(sleep_s)
    raise last_exc or RuntimeError("judge LLM failed after 5 attempts")


def _dispatch_tool(env, name: str, args: dict, save_dir: Path,
                   shot_counter: list[int],
                   output_dir: Path | None = None) -> tuple[str, bytes | None]:
    """Execute one tool call. Returns (text_result, optional_png_bytes)."""
    try:
        if name == "bash":
            return (tool_bash(env, args.get("command", "")), None)
        if name == "read_file":
            return (tool_read_file(env, args.get("path", "")), None)
        if name == "screenshot":
            shot_counter[0] += 1
            fname, png = tool_screenshot(env, save_dir, shot_counter[0])
            return (f"saved {fname}", png)
        if name == "read_agent_actions":
            if output_dir is None:
                return ("<read_agent_actions unavailable: no output_dir>", None)
            return (_extract_chat_excerpt(output_dir, max_chars=6000), None)
        if name == "final_answer":
            # caller handles this — should not reach here
            return ("(final_answer dispatched at caller)", None)
        return (f"<unknown tool: {name}>", None)
    except Exception as exc:
        return (f"<tool {name} crashed: {type(exc).__name__}: {exc}>", None)
