"""Build a staging dir for ONE OSWorld task rollout so OpenClaw judge agent can read it.

OSWorld task vs WeaveBench task — key differences:
  • Source             : evaluation_examples/examples/<domain>/<uuid>.json
                         (instruction + config + evaluator)
                         vs WeaveBench .md with embedded grader Python.
  • Ground truth       : OSWorld evaluator self-describes (rule / cloud_file / ...).
                         No external gt/ dir — we copy the evaluator JSON
                         fragment so the judge can see what 'correctness'
                         means for this task.
  • Deliverables       : OSWorld tasks usually do not produce files in
                         /tmp_workspace/results/ — agent operates on the
                         live desktop. We rely on final screenshot + chat.jsonl.

Layout produced under <judge_workspace>/_eval/<case_id>/:
  ├── task.json                ← canonical OSWorld JSON (instruction + evaluator)
  ├── task_summary.md          ← human-readable rendering for judge (auto-built)
  ├── results/                 ← results.tar.gz contents (if any) — usually empty for OSWorld
  ├── final_screenshot.png     ← last screenshot taken by agent (if exists)
  ├── chat.jsonl               ← full agent trajectory
  └── (optional) agent.log
"""
from __future__ import annotations
import json
import shutil
import tarfile
from pathlib import Path
from typing import Optional


def _render_task_summary(task_json: dict) -> str:
    """Build a human-readable markdown rendering of an OSWorld task JSON.

    The judge agent reads this first to understand what 'correctness'
    means. Includes:
      - instruction (the user-facing prompt)
      - related_apps (app context)
      - evaluator.func + result + expected (anti-hack hint)
    """
    lines = []
    lines.append(f"# OSWorld Task: `{task_json.get('id', '?')}`")
    lines.append("")
    lines.append(f"**Snapshot/Domain**: `{task_json.get('snapshot', '?')}`")
    related = task_json.get("related_apps", [])
    if related:
        lines.append(f"**Related apps**: {', '.join(related)}")
    src = task_json.get("source")
    if src:
        lines.append(f"**Source**: {src}")
    lines.append("")
    lines.append("## Instruction")
    lines.append("")
    lines.append("> " + (task_json.get("instruction", "(missing)") or "").replace("\n", "\n> "))
    lines.append("")

    ev = task_json.get("evaluator", {}) or {}
    func = ev.get("func")
    lines.append("## Evaluator hint (use this to judge correctness)")
    lines.append("")
    if func == "infeasible":
        lines.append("**`func = infeasible`** — the task is intentionally impossible.")
        lines.append("The agent succeeds **iff** it explicitly reports FAIL / refuses ")
        lines.append("/ states the task cannot be completed. Otherwise score 0.")
    else:
        if isinstance(func, list):
            lines.append(f"**Multi-metric evaluator**: funcs = `{func}` (combined `{ev.get('metric_conj', 'and')}`)")
        else:
            lines.append(f"**Evaluator func**: `{func}`")
        result = ev.get("result")
        if result is not None:
            lines.append("")
            lines.append("**Required result state** (what the VM must look like after agent finishes):")
            lines.append("```json")
            lines.append(json.dumps(result, indent=2, ensure_ascii=False)[:2000])
            lines.append("```")
        expected = ev.get("expected")
        if expected is not None:
            lines.append("")
            lines.append("**Expected/gold values**:")
            lines.append("```json")
            lines.append(json.dumps(expected, indent=2, ensure_ascii=False)[:2000])
            lines.append("```")

    cfg = task_json.get("config", [])
    if cfg:
        lines.append("")
        lines.append("## Pre-task setup (already applied before agent ran)")
        for step in cfg[:5]:
            t = step.get("type", "?")
            p = step.get("parameters", {}) or {}
            cmd = p.get("command") or p.get("url") or p.get("path") or ""
            if isinstance(cmd, list):
                cmd = " ".join(str(c) for c in cmd)
            lines.append(f"- `{t}` :: {str(cmd)[:200]}")

    lines.append("")
    lines.append("## How to score")
    lines.append("")
    lines.append("Use the chat.jsonl trajectory (assistant turns + tool calls + tool results)")
    lines.append("and the final_screenshot.png to determine if the agent satisfied the instruction")
    lines.append("AND the evaluator's result/expected criteria above.")
    lines.append("")
    lines.append("If you cannot directly verify the VM end-state, judge based on the agent's")
    lines.append("LAST actions: did its actions plausibly achieve what the evaluator checks?")
    lines.append("If the agent took a screenshot late in the trajectory showing the success state,")
    lines.append("that is strong positive evidence. If the agent gave up / said cannot / FAIL")
    lines.append("for a non-infeasible task — that's failure (0).")
    return "\n".join(lines)


def stage_osworld_case(
    case_dir: str,
    task_json_path: str,
    judge_workspace: str,
    case_id: Optional[str] = None,
) -> Path:
    """Materialize judge_workspace/_eval/<case_id>/ for an OSWorld task rollout.

    Args:
      case_dir          : output dir of the rollout (contains chat.jsonl, agent.log,
                          results.tar.gz, final_screenshot.png if any)
      task_json_path    : path to the OSWorld task JSON
                          (evaluation_examples/examples/<domain>/<uuid>.json)
      judge_workspace   : where _eval/<case_id>/ should land
      case_id           : case identifier (default: <domain>__<uuid_short>)

    Returns the staged dir path.
    """
    cd = Path(case_dir).resolve()
    if not cd.is_dir():
        raise FileNotFoundError(f"case_dir does not exist: {cd}")

    tjp = Path(task_json_path).resolve()
    if not tjp.is_file():
        raise FileNotFoundError(f"task_json_path does not exist: {tjp}")

    task_json = json.loads(tjp.read_text(encoding="utf-8"))

    if not case_id:
        domain = tjp.parent.name
        uuid_short = (task_json.get("id") or tjp.stem)[:8]
        case_id = f"{domain}__{uuid_short}"

    stage = Path(judge_workspace) / "_eval" / case_id
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "results").mkdir(exist_ok=True)

    # 1. Canonical task JSON
    shutil.copy(tjp, stage / "task.json")

    # 2. Human-readable rendering for judge
    (stage / "task_summary.md").write_text(
        _render_task_summary(task_json), encoding="utf-8")
    # 3. WeaveBench-compatible alias so reused judge tools that look for "task.md" still work.
    shutil.copy(stage / "task_summary.md", stage / "task.md")

    # 4. results.tar.gz (usually empty for OSWorld but extract anyway)
    tar = cd / "results.tar.gz"
    if tar.exists():
        try:
            with tarfile.open(tar) as t:
                for m in t.getmembers():
                    if not m.isfile():
                        continue
                    name = m.name
                    if name.startswith("results/"):
                        name = name[len("results/"):]
                    if not name:
                        continue
                    out = (stage / "results" / name).resolve()
                    out.parent.mkdir(parents=True, exist_ok=True)
                    f = t.extractfile(m)
                    if f:
                        out.write_bytes(f.read())
        except Exception as exc:
            (stage / "_stage_error.txt").write_text(
                f"results.tar.gz extract failed: {exc}\n")

    # 5. chat.jsonl
    chat = cd / "chat.jsonl"
    if chat.exists():
        shutil.copy(chat, stage / "chat.jsonl")

    # 6. agent.log
    agent_log = cd / "agent.log"
    if agent_log.exists():
        shutil.copy(agent_log, stage / "agent.log")

    # 7. Final screenshot(s) — OSWorld evaluator is rule-based but our LLM
    #    judge needs visual evidence; copy any *.png at top level of case dir.
    for png in cd.glob("*.png"):
        shutil.copy(png, stage / png.name)

    # 8. Manifest
    manifest = {
        "case_dir": str(cd),
        "task_json_path": str(tjp),
        "case_id": case_id,
        "domain": tjp.parent.name,
        "task_uuid": task_json.get("id"),
        "stage_dir": str(stage),
        "results_files": sorted(p.relative_to(stage / "results").as_posix()
                                for p in (stage / "results").rglob("*") if p.is_file()),
        "screenshots": sorted(p.name for p in stage.glob("*.png")),
        "chat_jsonl_bytes": (stage / "chat.jsonl").stat().st_size
                            if (stage / "chat.jsonl").exists() else 0,
    }
    (stage / "_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False))
    return stage


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(allow_abbrev=False, )
    ap.add_argument("--case_dir", required=True)
    ap.add_argument("--task_json_path", required=True)
    ap.add_argument("--judge_workspace", required=True)
    ap.add_argument("--case_id", default=None)
    args = ap.parse_args()
    out = stage_osworld_case(args.case_dir, args.task_json_path,
                              args.judge_workspace, case_id=args.case_id)
    print(f"Staged → {out}")
    print((out / "_manifest.json").read_text())
