"""OSWorld-style VM-side LLM judge (binary 0/1 success rate).

This module mimics OSWorld's native `env.evaluate()` rule-based evaluator,
but uses an LLM agent with VM-side tools (bash + screenshot + cat) to
decide whether the task was completed. Output is strictly 0 or 1 —
comparable with OSWorld paper success-rate numbers.

The LLM judge is a host-side loop (NOT a second openclaw spawn — that's
heavy and races on the same VM). It talks to the agent rollout's chat
backend (LiteLLM 4200 by default) via OpenAI Responses API, exposing
three tools the judge can call any number of times:

  - bash(command)        — run an arbitrary shell command in the VM
  - read_file(path)      — read a file from the VM as text (UTF-8, 64KB cap)
  - screenshot()         — take a screenshot and return its filename

The judge stops when it emits a `final_answer(score, reason)` call.

Public API: `judge_in_vm(env, task, output_dir, model, base_url, api_key,
                         max_steps=15, timeout_s=900) -> dict`

Returned dict shape:
  { "score": 0|1, "reason": str, "tool_calls": int,
    "elapsed_s": float, "judge_log": "..." }
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

logger = logging.getLogger("vm_llm_judge")


# ---------------------------------------------------------------------------
# VM tool wrappers — thin shims over the existing _vm_exec / _vm_url helpers
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


# ---------------------------------------------------------------------------
# Judge prompt — describes the OSWorld evaluator semantics to the LLM
# ---------------------------------------------------------------------------
_JUDGE_SYSTEM_PROMPT = """You are an expert OSWorld task evaluator.

You judge whether a desktop-automation agent has successfully completed
a single OSWorld task. You have READ-ONLY tools to inspect the Ubuntu
22.04 VM where the agent just ran:

  - bash(command)       — run any shell command (e.g. gsettings get, cat,
                           ls, sqlite3, grep, xdotool, etc.)
  - read_file(path)     — read a file (e.g. config, downloaded doc, log)
  - screenshot()        — capture the current screen

You MUST verify the OUTCOME the task asked for, not just whether the
agent looked busy. Your verification should mimic what a unit-test would
check: inspect actual system state (settings, files, registry, document
contents), not just chat history or what the agent claimed to do.

After investigating, you MUST call `final_answer(score, reason)` where
`score` is exactly 0 or 1:
  - 1 = task succeeded by objective verification
  - 0 = task did not succeed (incomplete, wrong, or unverifiable)

For INFEASIBLE tasks (the user request cannot be done): score 1 ONLY if
the agent declared infeasibility (e.g. answered "I cannot do this" / "FAIL"
/ explicit refusal with reason). Otherwise score 0.

Rules:
  - Be strict. Partial completion = 0.
  - Do NOT score 1 just because the agent's chat said "Done".
  - Use bash/read_file to verify. Avoid relying solely on screenshots when
    a config/file check is possible.
  - Keep tool usage tight: typically 2-5 calls per task is enough.
  - Always end with exactly one `final_answer(...)` call.
"""


def _build_user_prompt(task: dict, agent_chat_excerpt: str) -> str:
    instr = task.get("instruction", "(no instruction)")
    domain = task.get("domain", "?")
    uuid_short = (task.get("task_uuid") or "?")[:8]
    ev_func = task.get("evaluator_func", "?")
    infeasible = task.get("infeasible", False)
    evaluator_hint = task.get("evaluator_hint", "")

    parts = [
        f"# Task: {domain}/{uuid_short}",
        f"\n## User instruction\n{instr}",
        f"\n## Task type\n  - domain: {domain}",
        f"  - evaluator.func (hint about what to check): {ev_func}",
        f"  - infeasible: {infeasible}",
    ]
    if evaluator_hint:
        parts.append(f"\n## Evaluator hint (what canonical OSWorld eval checks)\n{evaluator_hint}")
    if agent_chat_excerpt:
        parts.append(f"\n## Agent rollout summary (last few turns)\n{agent_chat_excerpt}")
    parts.append(
        "\n## Your job\n"
        "Use the tools to verify whether the task was completed in the VM. "
        "Then call final_answer(score=0 or 1, reason='...')."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
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
        "name": "final_answer",
        "description": "Submit your final binary verdict and short reason. "
                       "Score is exactly 0 (failed) or 1 (succeeded). After this call the judge exits.",
        "parameters": {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "enum": [0, 1],
                          "description": "Final task success score (0 or 1)."},
                "reason": {"type": "string",
                           "description": "Concise rationale (≤300 chars) citing the verified state."},
            },
            "required": ["score", "reason"],
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
                        if c.get("type") in ("tool_use", "function_call"):
                            n_tool += 1
                            name = c.get("name") or "?"
                            inp = c.get("input") or c.get("arguments") or {}
                            summary_lines.append(
                                f"[tool_use:{name}] {json.dumps(inp)[:280]}")
                        elif c.get("type") == "text" and c.get("text", "").strip():
                            n_assist_text += 1
                            summary_lines.append(f"[assistant] {c['text'][:280]}")
        elif role in ("tool", "user") and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
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
    for attempt in range(3):
        try:
            r = requests.post(f"{base_url.rstrip('/')}/responses",
                              headers=headers, json=body, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 502, 503, 504):
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
    raise last_exc or RuntimeError("judge LLM failed after 3 attempts")


def _dispatch_tool(env, name: str, args: dict, save_dir: Path,
                   shot_counter: list[int]) -> tuple[str, bytes | None]:
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
        if name == "final_answer":
            # caller handles this — should not reach here
            return ("(final_answer dispatched at caller)", None)
        return (f"<unknown tool: {name}>", None)
    except Exception as exc:
        return (f"<tool {name} crashed: {type(exc).__name__}: {exc}>", None)


def judge_in_vm(env, task: dict, output_dir: Path,
                model: str, base_url: str, api_key: str,
                max_steps: int = 15, timeout_s: int = 900) -> dict:
    """Run the in-VM LLM judge for one task.

    Returns dict: {score: 0|1, reason: str, tool_calls: int, elapsed_s,
                   stop_reason, judge_log_path}
    """
    judge_dir = output_dir / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    judge_log_path = judge_dir / "judge_trace.jsonl"
    fh = judge_log_path.open("w", encoding="utf-8")

    def log(record: dict) -> None:
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
        except Exception:
            pass

    # Build initial messages
    excerpt = _extract_chat_excerpt(output_dir)
    ev_hint = build_evaluator_hint(task["task_json_path"]) if task.get("task_json_path") else ""
    task_with_hint = dict(task)
    task_with_hint["evaluator_hint"] = ev_hint
    user_msg = _build_user_prompt(task_with_hint, excerpt)

    log({"event": "judge_start", "task_uuid": task.get("task_uuid"),
         "domain": task.get("domain"), "model": model,
         "max_steps": max_steps, "timeout_s": timeout_s})
    log({"event": "user_prompt", "content": user_msg})

    # Responses API: maintain `input` list across turns
    input_items: list[dict] = [
        {"role": "user", "content": [{"type": "input_text", "text": user_msg}]},
    ]

    score = 0
    reason = "judge did not reach a final answer"
    stop_reason = "max_steps"
    tool_calls = 0
    shot_counter = [0]
    t0 = time.time()
    finalized = False

    for step in range(1, max_steps + 1):
        if time.time() - t0 > timeout_s:
            stop_reason = "timeout"
            reason = f"judge timed out after {int(time.time()-t0)}s without final_answer"
            break

        body = {
            "model": model,
            "instructions": _JUDGE_SYSTEM_PROMPT,
            "input": input_items,
            "tools": _TOOLS,
            "tool_choice": "auto",
        }
        try:
            resp = _responses_post(base_url, api_key, body, timeout=300)
        except Exception as exc:
            stop_reason = "llm_error"
            reason = f"judge LLM call failed: {type(exc).__name__}: {str(exc)[:200]}"
            log({"event": "llm_error", "step": step, "error": str(exc)})
            break

        log({"event": "llm_response", "step": step,
             "output_summary": [
                 {"type": o.get("type"), "name": o.get("name"),
                  "call_id": o.get("call_id"), "id": o.get("id")}
                 for o in (resp.get("output") or [])
             ]})

        outputs = resp.get("output") or []
        any_tool_call = False
        for item in outputs:
            itype = item.get("type")
            if itype == "function_call":
                any_tool_call = True
                tool_calls += 1
                name = item.get("name", "")
                call_id = item.get("call_id", "")
                try:
                    raw_args = item.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {}

                # final_answer short-circuits
                if name == "final_answer":
                    try:
                        score = int(args.get("score", 0))
                        if score not in (0, 1):
                            score = 1 if score >= 1 else 0
                    except Exception:
                        score = 0
                    reason = str(args.get("reason", ""))[:600]
                    stop_reason = "final_answer"
                    log({"event": "final_answer", "score": score, "reason": reason})
                    # Echo it back to the input to keep state consistent.
                    input_items.append(item)
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"acknowledged": True}),
                    })
                    finalized = True
                    break

                # Other tools — execute, append output
                result_text, png = _dispatch_tool(env, name, args, judge_dir, shot_counter)
                log({"event": "tool_call", "step": step, "name": name,
                     "args": args, "result_preview": result_text[:200]})
                input_items.append(item)
                # If screenshot — attach image too via input_image
                if name == "screenshot" and png:
                    b64 = base64.b64encode(png).decode("ascii")
                    # function_call_output must be text only; image goes
                    # as a follow-up user message.
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result_text,
                    })
                    input_items.append({
                        "role": "user",
                        "content": [
                            {"type": "input_text",
                             "text": f"(screenshot {shot_counter[0]} attached)"},
                            {"type": "input_image",
                             "image_url": f"data:image/png;base64,{b64}"},
                        ],
                    })
                else:
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result_text,
                    })
            elif itype == "message":
                # Assistant text — keep in transcript but doesn't advance
                input_items.append(item)
                content_list = item.get("content") or []
                for c in content_list:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        log({"event": "assistant_text", "step": step,
                             "text": c.get("text", "")[:500]})
            elif itype == "reasoning":
                # Pass through to keep Responses state happy
                input_items.append(item)

        if finalized:
            break
        if not any_tool_call:
            # Assistant just talked without acting — nudge it.
            input_items.append({
                "role": "user",
                "content": [{"type": "input_text",
                             "text": "Please continue by either calling a tool to "
                                     "verify state, or call final_answer(score, reason) "
                                     "if you have enough evidence."}],
            })

    fh.close()
    elapsed = time.time() - t0

    log_rec = {
        "score": int(score),
        "reason": reason,
        "tool_calls": tool_calls,
        "elapsed_s": round(elapsed, 1),
        "stop_reason": stop_reason,
        "judge_log_path": str(judge_log_path),
        "screenshots_taken": shot_counter[0],
        "judge_model": model,
        "max_steps": max_steps,
        "steps_used": step if 'step' in dir() else 0,
    }
    return log_rec
