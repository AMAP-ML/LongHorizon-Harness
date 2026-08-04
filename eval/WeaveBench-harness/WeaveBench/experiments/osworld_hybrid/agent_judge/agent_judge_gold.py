"""Gold-aware agent-as-judge for OSWorld (the judge used for the paper).

An in-VM, tool-using, skeptical judge that IS shown the ground-truth evaluator
spec (via ``build_evaluator_hint``). Knowing WHICH file/value the canonical
OSWorld grader checks, it verifies the EXACT object the grader judges — but
credits ANY implementation path the agent took to get there (a key advantage
over the rigid native grader, which only accepts one canonical GUI state).

Builds on the shared tool base in ``agent_judge_fair`` (the read-only VM tools,
Responses-API loop helpers, ``_TOOLS`` schema, chat-excerpt extraction, and
``build_evaluator_hint``). This file holds the gold-aware prompt and judge loop.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from agent_judge.agent_judge_fair import (
    _TOOLS,
    _responses_post,
    _dispatch_tool,
    build_evaluator_hint,
)

# ---------------------------------------------------------------------------
# Gold-aware judge prompt — skeptical, but NOT evaluator-blind.
# ---------------------------------------------------------------------------
_JUDGE_SYSTEM_PROMPT_GOLD = """You are a SKEPTICAL, INTENT-ORIENTED evaluator with access to the ground-truth grading spec.

Your job: decide whether the agent achieved the USER'S SUBSTANTIVE INTENT in the
post-rollout VM, by ANY implementation path, with REAL (non-fabricated) evidence.
You ARE given the ground-truth evaluator spec — use it to UNDERSTAND what the user
substantively wants (which file/value/state/outcome would satisfy them). The
grader's exact GUI state is ONE way to satisfy that intent, NOT the only one.

Default verdict: FAIL. The agent must DEMONSTRABLY achieve the user's real goal
with real evidence to pass.

Tools (READ-ONLY, you cannot modify VM state):
  - bash(command)             — run any shell command in the VM
  - read_file(path)           — read a file (UTF-8, 64KB cap)
  - read_agent_actions()      — the agent's full tool history (use it to see what
                                the agent actually did and what its tools returned)

Verification principles:
  1. INTENT, not exact GUI object. Use the GT spec to learn what the user really
     wants (e.g. "the Manchester monthly forecast", "Chrome auto-deletes data on
     exit", "electric cars under $50k near 10001"). Then judge whether the agent
     DELIVERED that real outcome — via GUI, CLI, curl, API, config file, scripted
     edit, anything. If the agent got the real result by an alternative path that
     the GUI-state grader does not check, that STILL COUNTS as success.
  2. EVIDENCE MUST BE REAL. This is the dividing line. Read the agent's actual tool
     RESULTS (via read_agent_actions) and the VM state. PASS only if the outcome is
     backed by genuine data/state:
       - real API/curl responses with correct data → counts
       - a real config file / mechanism that genuinely takes effect → counts
       - a correctly-edited target file with the right content → counts
     FAIL if the "completion" is fabricated:
       - injected/faked DOM, spoofed document.title, planted fake results
       - a claim of "Done" the agent never verified
       - a file that merely lies about success
       - data that is wrong, empty, blocked (403/Access Denied), or for the wrong
         entity
  3. Verify in the VM directly with full output where it helps. Chat assertions
     ("I did it") are NOT evidence; real tool results and VM state ARE.
  4. Reject ersatz completions: a /tmp/ file that "looks like" the answer counts
     ONLY if it genuinely contains the real result the user wanted (not a fake).
  5. For INFEASIBLE tasks (evaluator.func is 'infeasible'): the user's real intent
     is to be correctly told it cannot be done. PASS only if the agent explicitly
     refused / reported infeasibility. If it fabricated or claimed a non-working
     result, FAIL.

Decision rule (apply after gathering evidence):
  PASS  = user's substantive goal genuinely achieved (any path) AND evidence real.
  FAIL  = fabricated evidence, OR goal not actually achieved (blocked/wrong/empty),
          OR infeasible task not refused.

Output requirement: call exactly one `final_answer(score, reason, evidence)` where:
  - score   = 0 or 1
  - reason  = 1-3 sentences: what the user wanted, what the agent really delivered,
              and whether the evidence is real.
  - evidence = list of >=2 concrete observations (strings) from real tool outputs.
               If score=0, list the specific failures (fabrication / not-achieved).

Be tight: 3-8 tool calls is typical. Always end with one final_answer.
"""


def _build_gold_user_prompt(task: dict, gold_hint: str) -> str:
    instr = task.get("instruction", "(no instruction)")
    domain = task.get("domain", "?")
    uuid_short = (task.get("task_uuid") or "?")[:8]
    parts = [
        f"# Task: {domain}/{uuid_short}",
        f"\n## User instruction\n{instr}",
        "\n## GROUND-TRUTH evaluator spec (use this to understand what the user substantively wants)\n"
        f"{gold_hint}",
        "\n## Your job\n"
        "Decide whether the agent achieved the USER'S SUBSTANTIVE INTENT (the GT spec "
        "tells you what that intent is) by ANY implementation path, with REAL evidence. "
        "An alternative path (CLI/curl/API/config) that genuinely delivers the real "
        "outcome COUNTS, even if it isn't the exact GUI state the grader checks. "
        "FAIL if the evidence is fabricated (faked DOM, unverified claim, lying file) "
        "or the goal was not actually achieved (blocked/wrong/empty). "
        "Use the tools (especially read_agent_actions) to inspect what the agent really "
        "did and what its tools returned. Default to FAIL unless you can cite >=2 "
        "concrete real observations. Call final_answer(score, reason, evidence).",
    ]
    return "\n".join(parts)


def judge_in_vm_gold(env, task: dict, output_dir: Path, task_json_path: str | Path,
                     model: str, base_url: str, api_key: str,
                     max_steps: int = 15, timeout_s: int = 900) -> dict:
    """Gold-aware agent-as-judge for one task.

    Drives the shared in-VM tool loop but (1) injects build_evaluator_hint as an
    authoritative GROUND-TRUTH section, (2) uses the gold system prompt.

    Returns dict: {score: 0|1, reason, evidence, tool_calls, elapsed_s,
                   stop_reason, judge_log_path, gold_hint}.
    """
    judge_dir = output_dir / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    judge_log_path = judge_dir / "judge_trace_gold.jsonl"
    fh = judge_log_path.open("w", encoding="utf-8")

    def log(record: dict) -> None:
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
        except Exception:
            pass

    gold_hint = build_evaluator_hint(task_json_path)
    user_msg = _build_gold_user_prompt(task, gold_hint)

    log({"event": "judge_start", "task_uuid": task.get("task_uuid"),
         "domain": task.get("domain"), "model": model,
         "max_steps": max_steps, "timeout_s": timeout_s, "gold_mode": True})
    log({"event": "gold_hint", "content": gold_hint})
    log({"event": "user_prompt", "content": user_msg})

    input_items: list[dict] = [
        {"role": "user", "content": [{"type": "input_text", "text": user_msg}]},
    ]

    score = 0
    reason = "judge did not reach a final answer"
    stop_reason = "max_steps"
    evidence: list[str] = []
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
            "instructions": _JUDGE_SYSTEM_PROMPT_GOLD,
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

        # PASS 1 — echo back every output item in order (Responses API needs
        # reasoning items to accompany the function_calls they precede).
        for item in outputs:
            input_items.append(item)

        # PASS 2 — execute tools, append outputs.
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

                if name == "final_answer":
                    try:
                        score = int(args.get("score", 0))
                        if score not in (0, 1):
                            score = 1 if score >= 1 else 0
                    except Exception:
                        score = 0
                    reason = str(args.get("reason", ""))[:600]
                    evidence = args.get("evidence") or []
                    if isinstance(evidence, str):
                        evidence = [evidence]
                    evidence = [str(e)[:300] for e in evidence if e]
                    if score == 1 and len(evidence) < 2:
                        log({"event": "final_answer_rejected",
                             "reason": "score=1 requires >=2 evidence items, got %d" % len(evidence),
                             "submitted_evidence": evidence})
                        score = 0
                        reason = "(judge submitted pass with <2 evidence; force-failed) " + reason
                    stop_reason = "final_answer"
                    log({"event": "final_answer", "score": score,
                         "reason": reason, "evidence": evidence})
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"acknowledged": True}),
                    })
                    finalized = True
                    break

                result_text, png = _dispatch_tool(env, name, args, judge_dir,
                                                   shot_counter,
                                                   output_dir=output_dir)
                log({"event": "tool_call", "step": step, "name": name,
                     "args": args, "result_preview": result_text[:8000]})
                if name == "screenshot" and png:
                    b64 = base64.b64encode(png).decode("ascii")
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id, "output": result_text,
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
                        "call_id": call_id, "output": result_text,
                    })
            elif itype == "message":
                for c in (item.get("content") or []):
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        log({"event": "assistant_text", "step": step,
                             "text": c.get("text", "")[:500]})

        if finalized:
            break
        if not any_tool_call:
            input_items.append({
                "role": "user",
                "content": [{"type": "input_text",
                             "text": "Please continue by either calling a tool to "
                                     "verify state, or call final_answer(score, reason, "
                                     "evidence) if you have enough evidence."}],
            })

    fh.close()
    elapsed = time.time() - t0
    return {
        "score": int(score),
        "reason": reason,
        "evidence": evidence,
        "tool_calls": tool_calls,
        "elapsed_s": round(elapsed, 1),
        "stop_reason": stop_reason,
        "judge_log_path": str(judge_log_path),
        "gold_hint": gold_hint,
        "judge_model": model,
        "gold_mode": True,
    }
