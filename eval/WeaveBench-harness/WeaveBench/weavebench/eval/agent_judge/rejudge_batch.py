#!/usr/bin/env python3
"""Batch re-judge tasks that have chat.jsonl + results.tar.gz but
score.json is missing or has judge_error.

Usage:
  python eval/agent_judge/rejudge_batch.py \
    --root ./results/<run> \
    --bench_root tasks \
    --concurrency 4

Reads judge_workspace_<harness> from the env (set by sourcing
_common.sh). Each task gets re-staged + re-judged via judge_one.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from weavebench.eval.agent_judge.stage_case import stage_case  # noqa: E402
from weavebench.eval.agent_judge.judge_runner import judge_one  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rejudge_batch")


def find_candidates(rollout_root: Path) -> list[dict]:
    """Walk rollout dir and yield tasks that need (re)judging."""
    cands = []
    # Layout: <rollout_root>/[<bench_subdir>/]gui/<model>/<CAT>/<TASK>/.
    # `**` tolerates an optional bench_subdir level (older runs) and also
    # matches when result_dir is the harness dir directly (gui right under root).
    for task_dir in rollout_root.glob("**/gui/*/*/*"):
        if not task_dir.is_dir():
            continue
        chat = task_dir / "chat.jsonl"
        results = task_dir / "results.tar.gz"
        if not (chat.exists() and results.exists()):
            continue
        score = task_dir / "score.json"
        need = True
        if score.exists():
            try:
                sd = json.load(open(score))
                if (sd.get("scores", {}).get("judge_method") == "agent_as_judge"
                        and not sd.get("scores", {}).get("judge_error")
                        and sd.get("scores", {}).get("final_score") is not None):
                    need = False
            except Exception:
                pass
        if not need:
            continue
        # Extract bench_subdir / category / task_id from path:
        # .../AJ_GUI_X/<subdir>/gui/<model>/<CAT>/<TASK>/
        parts = task_dir.parts
        try:
            mode_idx = parts.index("gui")
            bench_subdir = parts[mode_idx - 1]   # <subdir>
            category = parts[mode_idx + 2]
            task_id = parts[mode_idx + 3]
        except (ValueError, IndexError):
            continue
        cands.append({
            "task_dir": str(task_dir.rstrip("/") if isinstance(task_dir, str) else task_dir),
            "case_dir": str(task_dir).rstrip("/"),
            "bench_subdir": bench_subdir,
            "category": category,
            "task_id": task_id,
            "case_id": f"{category}_{task_id}",
        })
    return cands


def rejudge_one(c: dict, bench_root: Path, judge_ws: Path) -> dict:
    case_id = c["case_id"]
    try:
        stage = stage_case(
            case_dir=c["case_dir"],
            bench_root=str(bench_root),
            bench_subdir=c["bench_subdir"],
            judge_workspace=str(judge_ws),
            case_id=case_id,
            category=c["category"],
            task_id=c["task_id"],
        )
    except Exception as exc:
        return {"case_id": case_id, "ok": False, "error": f"stage_case: {exc}"}

    if stage is None:
        return {"case_id": case_id, "ok": False, "error": "stage_case returned None"}

    timeout = int(os.environ.get("AJ_TIMEOUT", "1800"))
    thinking = os.environ.get("AJ_THINKING", "medium")
    max_attempts = int(os.environ.get("AJ_RETRY", "10"))
    base_backoff_s = int(os.environ.get("AJ_RETRY_BACKOFF_S", "45"))

    aj = None
    for attempt in range(1, max_attempts + 1):
        aj = judge_one(stage, case_id,
                       openclaw_bin=os.environ.get("AJ_OPENCLAW_BIN"),
                       openclaw_profile=os.environ.get("AJ_OPENCLAW_PROFILE", "judge"),
                       thinking=thinking, timeout=timeout)
        if aj.get("ok"):
            break
        time.sleep(base_backoff_s)

    if not aj or not aj.get("ok"):
        return {"case_id": case_id, "ok": False, "error": aj.get("error", "?"), "aj": aj}

    score_dst = Path(c["case_dir"]) / "score.json"
    src = Path(stage) / "score.json"
    if src.is_file():
        # Wrap in the same shape run_one_aj writes (scores key + final_score)
        try:
            scores = json.load(open(src))
        except Exception as exc:
            return {"case_id": case_id, "ok": False, "error": f"score.json parse: {exc}"}

        # Merge with whatever was at task_dir/score.json before
        prior = {}
        if score_dst.exists():
            try:
                prior = json.load(open(score_dst))
            except Exception:
                pass
        prior_scores = prior.get("scores", {}) if isinstance(prior.get("scores"), dict) else {}
        merged_scores = {**prior_scores, **scores,
                          "judge_method": "agent_as_judge",
                          "judge_model": os.environ.get("JUDGE_MODEL", "openai/gpt-5.5"),
                          "judge_elapsed_s": aj.get("elapsed_s"),
                          "rejudged_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        prior["scores"] = merged_scores
        # Compute final score = scores.final_score (preferred) or mean of dimension scores
        final = scores.get("final_score")
        if final is None:
            dims = scores.get("dimensions") or {}
            num = [v.get("score", 0) for v in dims.values() if isinstance(v, dict)]
            final = sum(num) / len(num) if num else 0.0
        prior["score"] = float(final or 0.0)
        prior["error"] = None
        # Don't keep stale judge_error
        prior["scores"].pop("judge_error", None)
        json.dump(prior, open(score_dst, "w"), indent=2, ensure_ascii=False)
        return {"case_id": case_id, "ok": True, "score": prior["score"]}
    else:
        return {"case_id": case_id, "ok": False, "error": "no score.json in stage_dir"}


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False, )
    ap.add_argument("--root", required=True, help="rollout root dir (one harness)")
    ap.add_argument("--bench_root", required=True, help="Root of the task tree")
    ap.add_argument("--judge_ws", default=None,
                    help="judge workspace (default from $AJ_JUDGE_WORKSPACE)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    args = ap.parse_args()

    rollout_root = Path(args.root).resolve()
    bench_root = Path(args.bench_root).resolve()
    judge_ws = Path(args.judge_ws or os.environ["AJ_JUDGE_WORKSPACE"]).resolve()

    cands = find_candidates(rollout_root)
    logger.info("found %d candidates needing re-judge in %s",
                len(cands), rollout_root.name)
    if args.limit > 0:
        cands = cands[: args.limit]

    if not cands:
        return

    done = 0
    ok = 0
    failed = 0
    scores = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(rejudge_one, c, bench_root, judge_ws): c for c in cands}
        for fut in concurrent.futures.as_completed(futs):
            try:
                r = fut.result()
            except Exception as exc:
                c = futs[fut]
                r = {"case_id": c.get("case_id", "?"), "ok": False, "error": str(exc)}
            done += 1
            if r.get("ok"):
                ok += 1
                scores.append(r["score"])
                logger.info("[%d/%d] OK %s score=%.3f",
                            done, len(cands), r["case_id"], r["score"])
            else:
                failed += 1
                logger.warning("[%d/%d] FAIL %s: %s",
                               done, len(cands), r["case_id"],
                               (r.get("error", "?") or "?")[:200])

    logger.info("done: %d ok / %d failed / %d total",
                ok, failed, done)
    if scores:
        logger.info("mean=%.3f  ≥0.5=%d  ≥0.8=%d",
                    sum(scores) / len(scores),
                    sum(1 for s in scores if s >= 0.5),
                    sum(1 for s in scores if s >= 0.8))


if __name__ == "__main__":
    main()
