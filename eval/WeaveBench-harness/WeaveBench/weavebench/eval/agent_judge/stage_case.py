"""Build a staging dir for ONE rollout case so OpenClaw judge agent can read it.

Layout produced under <judge_workspace>/_eval/<case_id>/:
  ├── task.md                  # task spec from tasks/<batch>/<CAT>/<task>.md
  ├── results/                 # extracted results.tar.gz contents (or empty if absent)
  ├── gt/                      # copied from tasks/<batch>/workspace/<CAT>/<task>/gt
  ├── chat.jsonl               # full agent trajectory
  └── (optional) agent.log     # runtime log

`case_id` defaults to "<CAT>_<task_id>" but caller can override.

Usage:
  from weavebench.eval.agent_judge.stage_case import stage_case
  stage_dir = stage_case(
      case_dir="/.../rollout/.../DAV/DAV_task_17_jaeger_trace_root_cause_compare",
      bench_root="/.../tasks",
      bench_subdir="batch3",
      judge_workspace="~/judge_agent_test/judge_workspace",
      case_id="DAV_task_17_jaeger_trace_root_cause_compare",
  )
"""
from __future__ import annotations
import json
import shutil
import tarfile
from pathlib import Path
from typing import Optional


def task_md_path(bench_root: Path, bench_subdir: str, category: str, task_id: str) -> Path:
    """Resolve task .md path under <bench_root>/<bench_subdir>/<CAT>/<task>.md.
    """
    primary = Path(bench_root) / bench_subdir / category / f"{task_id}.md"
    return primary


def gt_dir_path(bench_root: Path, bench_subdir: str, category: str, task_id: str) -> Path:
    """Resolve gt/ dir.

    Workspace layout: <bench_root>/<bench_subdir>/workspace/<CAT>/<task_short>/gt
    where task_short is task_id with the leading "<CAT>_" stripped (e.g.
    DAV_task_17_jaeger... → task_17_jaeger...).
    """
    short_task = task_id
    if task_id.startswith(f"{category}_"):
        short_task = task_id[len(category) + 1:]
    primary = Path(bench_root) / bench_subdir / "workspace" / category / short_task / "gt"
    return primary


def stage_case(
    case_dir: str,
    bench_root: str,
    bench_subdir: str,
    judge_workspace: str,
    case_id: Optional[str] = None,
    category: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Path:
    """Materialize judge_workspace/_eval/<case_id>/ from a rollout dir.

    Returns the staged dir path.
    """
    cd = Path(case_dir).resolve()
    if not cd.is_dir():
        raise FileNotFoundError(f"case_dir does not exist: {cd}")

    if not category:
        category = cd.parent.name
    if not task_id:
        task_id = cd.name
    if not case_id:
        case_id = task_id

    stage = Path(judge_workspace) / "_eval" / case_id
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "results").mkdir(exist_ok=True)

    md = task_md_path(Path(bench_root), bench_subdir, category, task_id)
    if md.exists():
        shutil.copy(md, stage / "task.md")
    else:
        (stage / "task.md").write_text(f"(task md not found at {md})\n")

    tar = cd / "results.tar.gz"
    if tar.exists():
        try:
            with tarfile.open(tar) as t:
                # Members are named like "results/<file>"; strip prefix.
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
            (stage / "_stage_error.txt").write_text(f"results.tar.gz extract failed: {exc}\n")

    chat = cd / "chat.jsonl"
    if chat.exists():
        shutil.copy(chat, stage / "chat.jsonl")

    agent_log = cd / "agent.log"
    if agent_log.exists():
        shutil.copy(agent_log, stage / "agent.log")

    gt_src = gt_dir_path(Path(bench_root), bench_subdir, category, task_id)
    if gt_src.is_dir():
        shutil.copytree(gt_src, stage / "gt")

    manifest = {
        "case_dir": str(cd),
        "bench_subdir": bench_subdir,
        "category": category,
        "task_id": task_id,
        "case_id": case_id,
        "stage_dir": str(stage),
        "task_md_present": (stage / "task.md").exists(),
        "results_files": sorted(p.relative_to(stage / "results").as_posix()
                                for p in (stage / "results").rglob("*") if p.is_file()),
        "gt_files": sorted(p.relative_to(stage / "gt").as_posix()
                           for p in (stage / "gt").rglob("*") if p.is_file()) if (stage / "gt").exists() else [],
        "chat_jsonl_bytes": (stage / "chat.jsonl").stat().st_size if (stage / "chat.jsonl").exists() else 0,
    }
    (stage / "_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return stage


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(allow_abbrev=False, )
    ap.add_argument("--case_dir", required=True)
    ap.add_argument("--bench_root", required=True,
                    help="Root of the WeaveBench task tree (e.g. ./cache/tasks).")
    ap.add_argument("--bench_subdir", required=True, help="subdir under tasks/ (e.g. '.', 'batch1')")
    ap.add_argument("--judge_workspace", required=True)
    ap.add_argument("--case_id", default=None)
    args = ap.parse_args()
    out = stage_case(args.case_dir, args.bench_root, args.bench_subdir,
                     args.judge_workspace, case_id=args.case_id)
    print(f"Staged → {out}")
    print((out / "_manifest.json").read_text())
