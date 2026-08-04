#!/usr/bin/env python3
"""Aggregate OSWorld-V2 codex-hybrid (GPT-5.5) per-task scores into
per_task_scores.json + summary.json.

Source of truth: each task's score.json written by the inject runner under
  <run_dir>/pyautogui/screenshot/<model>/tasks/<task_id>/score.json
plus the action-mix CSV (CLI vs GUI tool calls) and codex total-token counts
parsed from each task's agent.log.

Run from this folder:
  RUN_DIR=/path/to/FULL_108_gpt55_codex_xhigh_20260628_125852 \
  python aggregate_results.py

If RUN_DIR is unset it falls back to the path used for the published numbers.
The committed JSON in results/codex_hybrid_gpt55/ was produced by this script;
re-running it against the same run reproduces those files exactly.
"""
import csv
import json
import os
import re
import statistics as st
from pathlib import Path

RUN_DIR = Path(
    os.environ.get(
        "RUN_DIR",
        # Point this at your inject-runner output directory, e.g.
        #   results/osworld_v2_inject/OSW2_codex_hybrid_<timestamp>
        "results/osworld_v2_inject/FULL_108_gpt55_codex_xhigh_20260628_125852",
    )
)
MODEL = os.environ.get("MODEL", "gpt-5.5")
OUT_DIR = Path(__file__).parent / "results" / "codex_hybrid_gpt55"

# Infra-failure tasks excluded from the de-infra cohort (root cause unrelated to
# model capability): VM disk fault, network resets, framework multiphase limit.
INFRA = {"063", "064", "082", "069"}
INFRA_REASON = {
    "063": "VM ext4 journal error -> root remounted read-only",
    "064": "network: Connection broken / IncompleteRead during execution",
    "082": "network: Connection reset by peer during task setup",
    "069": "framework: multiphase_unsupported_in_inject_runner",
}

TOK_RE = re.compile(r"^([0-9,]+)$")


def parse_tokens(agent_log: Path):
    """Sum codex 'tokens used' totals across turns (total tok = input+reasoning+output)."""
    if not agent_log.exists():
        return None
    toks = []
    lines = agent_log.read_text(errors="replace").splitlines()
    for i, l in enumerate(lines):
        if l.strip() == "tokens used" and i + 1 < len(lines):
            m = TOK_RE.match(lines[i + 1].strip())
            if m:
                toks.append(int(m.group(1).replace(",", "")))
    return sum(toks) if toks else None


def load_action_mix():
    csv_path = OUT_DIR / "action_mix.csv"
    if not csv_path.exists():
        return {}
    mix = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            mix[row["task_id"]] = {
                "cli": int(row["cli"]),
                "gui": int(row["gui"]),
                "total": int(row["total"]),
            }
    return mix


def main():
    tasks_dir = RUN_DIR / "pyautogui" / "screenshot" / MODEL / "tasks"
    mix = load_action_mix()
    rows = []
    for td in sorted(tasks_dir.glob("*")):
        sj = td / "score.json"
        if not sj.exists():
            continue
        s = json.loads(sj.read_text())
        tid = s["task_id"]
        rows.append(
            {
                "task_id": tid,
                "score": s.get("score") or 0.0,
                "agent_done": s.get("agent_done"),
                "error": s.get("error"),
                "elapsed_seconds": s.get("elapsed_seconds"),
                "total_tokens": parse_tokens(td / "agent.log"),
                "tool_calls": mix.get(tid, {}).get("total"),
                "gui_calls": mix.get(tid, {}).get("gui"),
                "cli_calls": mix.get(tid, {}).get("cli"),
            }
        )

    def cohort(keep):
        n = len(keep)
        binary = sum(1 for r in keep if r["score"] >= 0.999)
        avg = sum(r["score"] for r in keep) / n
        nonzero = sum(1 for r in keep if 0 < r["score"] < 0.999)
        return {
            "n": n,
            # Paper Table 3 metrics: Binary = fraction at score 1.0; Partial =
            # mean partial-credit score over all tasks (NOT the fraction of
            # tasks with a partial score).
            "binary_pct": round(binary / n * 100, 2),
            "partial_pct": round(avg * 100, 2),
            "binary_count": binary,
            "nonzero_partial_count": nonzero,
        }

    def mean(key, src=rows):
        vs = [r[key] for r in src if r.get(key) is not None]
        return round(sum(vs) / len(vs), 1) if vs else None

    def median(key, src=rows):
        vs = [r[key] for r in src if r.get(key) is not None]
        return round(st.median(vs), 1) if vs else None

    all108 = cohort(rows)
    deinfra = cohort([r for r in rows if r["task_id"] not in INFRA])

    summary = {
        "run": RUN_DIR.name,
        "model": MODEL,
        "harness": "codex CLI injected into VM + GUI channel (hybrid)",
        "reasoning_effort": "xhigh",
        "scoring": "native OSWorld env.evaluate() (paper-faithful)",
        "cohorts": {"all_108": all108, "de_infra_104": deinfra},
        "efficiency": {
            "tool_calls_per_task": {"mean": mean("tool_calls"), "median": median("tool_calls")},
            "gui_calls_per_task": {"mean": mean("gui_calls"), "median": median("gui_calls")},
            "cli_calls_per_task": {"mean": mean("cli_calls"), "median": median("cli_calls")},
            "total_tokens_per_task_incl_input": {
                "mean": mean("total_tokens"),
                "median": median("total_tokens"),
                "note": "codex CLI logs only a combined token total (input+reasoning+output); "
                "output-only tokens and cost were not recorded -> reported as '-' in tables",
            },
        },
        "excluded_infra_tasks": INFRA_REASON,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "per_task_scores.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print("wrote", OUT_DIR / "per_task_scores.json", "(", len(rows), "tasks )")
    print("wrote", OUT_DIR / "summary.json")
    print("all_108:", all108)
    print("de_infra_104:", deinfra)


if __name__ == "__main__":
    main()
