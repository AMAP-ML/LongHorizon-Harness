#!/usr/bin/env python3
"""Offline RE-JUDGE of an OSWorld rollout WITHOUT re-running the agent.

Reads the archived evidence per task and re-scores with a chosen judge standard:
  - chat.jsonl            : the agent's FULL trajectory (commands + real tool
                            results, untruncated — the primary evidence)
  - judge/judge_trace_*.jsonl : the live judge's in-VM probes (now untruncated)
  - results.tar.gz        : archived deliverables (file-producing tasks)
  - the task's ground-truth evaluator spec (via build_evaluator_hint)

This lets you tweak the judge standard ANYTIME and re-score in minutes — no VM,
no agent re-run. Writes score_rejudge.json alongside (never touches score.json).

Judge standards (--judge_prompt):
  intent  : INTENT-ORIENTED (default) — did the agent achieve the user's
            substantive goal by ANY path with REAL evidence? (counts CLI/curl/API
            alt-paths; fails fabricated/blocked/wrong). Mirrors agent_judge_gold's
            intent prompt.
  strict  : STRICT-GOLD — must match the exact object the OSWorld grader checks.

Usage:
  python rejudge_offline.py --rollout_dir <dir> --judge_prompt intent --workers 8
  python rejudge_offline.py --rollout_dir <dir> --only_uuids 368d9ba4,47543840
"""
import argparse, glob, json, os, sys, tarfile, time
import requests

from agent_judge.agent_judge_fair import build_evaluator_hint

# ---- Judge prompts (offline, single-shot — judge reads archived evidence) -----
_INTENT_SYSTEM = """You are a SKEPTICAL, INTENT-ORIENTED evaluator re-scoring an OSWorld task from archived evidence.

You are given: the user instruction, the ground-truth evaluator spec (use it to
UNDERSTAND what the user substantively wants), the agent's FULL action trajectory
(commands + real tool results), and any live-judge VM probes.

Decide whether the agent achieved the USER'S SUBSTANTIVE INTENT by ANY path, with
REAL evidence:
  PASS = the user's real goal was genuinely delivered (GUI, CLI, curl, API, config —
         any path) AND the evidence is real (real data/state, correctly-edited target).
         An alternative path that the GUI-state grader doesn't check STILL COUNTS if
         it delivers the real outcome.
  FAIL = fabricated evidence (faked DOM, spoofed title, unverified "Done", lying file),
         OR the goal was not actually achieved (403/blocked/empty/wrong/wrong-entity),
         OR an infeasible task (evaluator.func=='infeasible') the agent did not refuse.

The dividing line is REAL vs FABRICATED evidence. Read the agent's actual tool
RESULTS, not its claims. Default to FAIL unless the real outcome is demonstrated.

Output exactly one final_answer(score, reason) via the tool. score = 0 or 1.
reason = 1-3 sentences: what the user wanted, what the agent really delivered, is the
evidence real.
"""

_STRICT_SYSTEM = """You are a SKEPTICAL evaluator re-scoring an OSWorld task from archived evidence,
held to the EXACT ground-truth grading object.

You are given the user instruction, the ground-truth evaluator spec (AUTHORITATIVE
about which exact file/value/state to check), the agent's full trajectory, and live
-judge VM probes. PASS only if the EXACT object the grader checks ends up matching
what's expected (any implementation path is fine, but the checked object/value must
match). Reject artifacts at other paths, fabricated DOM, unverified claims. For
infeasible tasks, PASS only on explicit refusal.

Output exactly one final_answer(score, reason). score = 0 or 1.
"""

_TOOLS = [{
    "type": "function", "name": "final_answer",
    "description": "Emit the final verdict.",
    "parameters": {"type": "object", "properties": {
        "score": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"}},
        "required": ["score", "reason"], "additionalProperties": False},
}]


def extract_agent_trajectory(chat_path, max_actions=60, cmd_cap=600, res_cap=1200):
    """Full agent trajectory from chat.jsonl: each command + its REAL result."""
    if not os.path.exists(chat_path):
        return "(no chat.jsonl)", ""
    lines, finals, n = [], [], 0
    for ln in open(chat_path, errors="ignore"):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") != "message":
            continue
        m = o.get("message", {})
        role = m.get("role")
        for c in (m.get("content") or []):
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "toolCall":
                n += 1
                a = c.get("arguments") or {}
                cmd = a.get("command", "") if isinstance(a, dict) else json.dumps(a)
                if len(lines) < max_actions * 2:
                    lines.append(f"$ {str(cmd)[:cmd_cap]}")
            elif ct == "text" and role == "toolResult":
                if len(lines) < max_actions * 2:
                    lines.append(f"  -> {str(c.get('text',''))[:res_cap]}")
            elif ct == "text" and role == "assistant":
                t = (c.get("text") or "").strip()
                if t:
                    finals.append(t)
    traj = "\n".join(lines)
    return traj, " | ".join(finals[-3:])[:800]


def extract_judge_probes(trace_path):
    """The live judge's in-VM probes (now untruncated) from judge_trace_*.jsonl."""
    if not trace_path or not os.path.exists(trace_path):
        return ""
    out = []
    for ln in open(trace_path, errors="ignore"):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("event") == "tool_call":
            a = o.get("args", {})
            cmd = a.get("command") or a.get("path") or json.dumps(a)
            out.append(f"$ {str(cmd)[:600]}\n  -> {str(o.get('result_preview',''))[:1200]}")
    return "\n".join(out)


def list_tar(tar_path):
    if not os.path.exists(tar_path):
        return ""
    try:
        with tarfile.open(tar_path) as t:
            return "deliverable files: " + ", ".join(m.name for m in t.getmembers() if m.isfile())[:1500]
    except Exception:
        return ""


def call_judge(base_url, api_key, model, system, user_text, timeout=180):
    body = {"model": model, "instructions": system,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": user_text}]}],
            "tools": _TOOLS, "tool_choice": "required", "max_output_tokens": 3000}
    last = None
    for attempt in range(5):
        try:
            r = requests.post(f"{base_url.rstrip('/')}/responses", json=body,
                              headers={"Authorization": f"Bearer {api_key}",
                                       "Content-Type": "application/json"}, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1)); last = f"HTTP {r.status_code}"; continue
            r.raise_for_status()
            for item in r.json().get("output", []):
                if item.get("type") == "function_call" and item.get("name") == "final_answer":
                    a = json.loads(item.get("arguments", "{}"))
                    return int(a.get("score", 0)), a.get("reason", "")
            last = "NO_FINAL_ANSWER"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"; time.sleep(3 * (attempt + 1))
    return None, f"RETRY_EXHAUSTED: {last}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rollout_dir", required=True)
    ap.add_argument("--base_url", default="http://127.0.0.1:4141/v1")
    ap.add_argument("--api_key", default="sk-dummy")
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--judge_prompt", choices=["intent", "strict"], default="intent")
    ap.add_argument("--only_uuids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    system = _INTENT_SYSTEM if args.judge_prompt == "intent" else _STRICT_SYSTEM
    out_name = f"score_rejudge_{args.judge_prompt}.json"
    only = set(args.only_uuids.split(",")) if args.only_uuids else None

    todo = []
    for sf in sorted(glob.glob(os.path.join(args.rollout_dir, "*/*/score.json"))):
        tdir = os.path.dirname(sf)
        uuid = os.path.basename(tdir)
        if only and not any(u in uuid for u in only):
            continue
        out = os.path.join(tdir, out_name)
        if os.path.exists(out) and not args.force:
            continue
        todo.append((sf, tdir, uuid, out))
    if args.limit:
        todo = todo[:args.limit]
    print(f"re-judging {len(todo)} tasks [{args.judge_prompt}] via {args.base_url}", file=sys.stderr)

    def work(item):
        sf, tdir, uuid, out = item
        d = json.load(open(sf))
        domain = d.get("domain")
        tj = glob.glob(os.path.join(ROOT, "evaluation_examples/examples/*", f"{uuid}.json"))
        gold = build_evaluator_hint(tj[0]) if tj else "(task def not found)"
        instr = d.get("instruction") or (json.load(open(tj[0])).get("instruction") if tj else "")
        traj, final = extract_agent_trajectory(os.path.join(tdir, "chat.jsonl"))
        # judge trace: gold first, fall back to fair
        tr = None
        for cand in ("judge_trace_gold.jsonl", "judge_trace_fair.jsonl"):
            p = os.path.join(tdir, "judge", cand)
            if os.path.exists(p):
                tr = p; break
        probes = extract_judge_probes(tr)
        deliv = list_tar(os.path.join(tdir, "results.tar.gz"))
        user_text = (
            f"# Task ({domain}/{uuid[:8]})\n## User instruction\n{instr}\n\n"
            f"## GROUND-TRUTH evaluator spec (understand what the user wants)\n{gold}\n\n"
            f"## Agent FULL trajectory (commands + real tool results)\n{traj}\n\n"
            f"## Agent final message\n{final}\n\n"
            f"## Live-judge VM probes (independent inspection)\n{probes or '(none)'}\n\n"
            f"## Archived deliverables\n{deliv or '(none)'}\n\n"
            f"Decide. Call final_answer(score, reason)."
        )
        try:
            score, reason = call_judge(args.base_url, args.api_key, args.model, system, user_text)
        except Exception as exc:
            score, reason = None, f"REJUDGE_ERROR: {type(exc).__name__}: {exc}"
        rec = {"task_uuid": uuid, "domain": domain, "judge_prompt": args.judge_prompt,
               "score_rejudge": score, "rejudge_reason": reason,
               "score_native": d.get("score_native"), "score_gold": d.get("score_gold"),
               "judge_model": args.model}
        json.dump(rec, open(out, "w"), ensure_ascii=False, indent=1)
        return 1

    done = 0
    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for f in as_completed([ex.submit(work, it) for it in todo]):
                done += f.result()
                if done % 20 == 0:
                    print(f"  {done}/{len(todo)}", file=sys.stderr)
    else:
        for it in todo:
            done += work(it)
            if done % 20 == 0:
                print(f"  {done}/{len(todo)}", file=sys.stderr)
    print(f"done {done} -> {out_name}", file=sys.stderr)


if __name__ == "__main__":
    main()
