"""Lightweight (no-desktop) CLI ablation runner — OpenAI Chat Completions variant.

Identical to the lite CLI runner except the agent class is
``OpenClawChatAgent`` (OpenAI ``/v1/chat/completions`` transport) instead of
``OpenClawAgent`` (OpenAI Responses).

Use this for chat-only models (smaller chat-only models
that don't expose ``/v1/responses``). Agent transport = chat.completions;
grader/judge endpoint is independently configured via the env vars
``OPENROUTER_BASE_URL`` / ``OPENROUTER_API_KEY`` / ``JUDGE_MODEL`` (see
``eval/_judge_helper.py``) and is not touched by this runner.

Original lite runner docstring follows.

------

Lightweight (no-desktop) CLI ablation runner for the tasks
``task subdir`` task pools.

Same task discovery / agent stack / grader as the GUI runners, but
``DesktopEnv`` is replaced with ``DockerLiteEnv`` (one short-lived
``weavebench-ubuntu`` container per task, with a stdlib REST shim
that mimics the OSWorld VM API). CLI-only — there is no desktop.

Result layout matches the existing GUI runs but lives under ``cli/``:

    <RESULT_ROOT>/<bench_subdir>/cli/<model>/<CAT>/<task>/score.json

Usage::

    python -m weavebench.eval._runners.chat_cli \\
        --bench_subdirs . \\
        --categories WEB,DAV,OPS,DOC,DES,GAM,SPA,DSK \\
        --num_envs 8 --model openai/gpt-5.5 \\
        --result_dir tasks/rollout/gpt-5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from multiprocessing import Manager, Process
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import weavebench.eval.orchestrator as ds  # noqa: E402

# WeaveBench public release: the trajectory-aware agent-as-judge is the
# only supported scoring path. Monkey-patch run_one at import time so the
# runner unconditionally skips the in-task grader and uses run_one_aj.
# See docs/AGENT_JUDGE.md for the host-side openclaw + template requirements.
from weavebench.eval.agent_judge.run_one_aj import run_one_aj  # noqa: E402
ds._original_run_one = ds.run_one
ds.run_one = run_one_aj

logger = logging.getLogger("lite_chat_runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# ---------------------------------------------------------------------------
# Monkey-patch ds.run_warmup so warmup failures are SOFT in lite-cli mode.
#
# The tasks warmup scripts assume the OSWorld VM image with desktop
# tools baked in (xrandr, gnome-control-center, blender, …). On the lite
# weavebench-ubuntu image those tools are absent, and `set -e` aborts
# the warmup on the first missing binary.
#
# For the no-desktop ablation that's the whole point: we want the agent to
# attempt the task with ONLY the CLI/shell channel and let the grader
# decide. Aborting on warmup hides every task behind a single error and
# defeats the experiment.
#
# Behaviour:
#   • Try the original ds.run_warmup.
#   • On RuntimeError("Warmup script failed (rc=...)") log a warning and
#     return cleanly so run_one proceeds to the agent step.
#   • Any other exception (e.g. timeout) still propagates.
# ---------------------------------------------------------------------------
_ORIG_RUN_WARMUP = ds.run_warmup


def _soft_run_warmup(env, warmup, *args, **kwargs):
    try:
        return _ORIG_RUN_WARMUP(env, warmup, *args, **kwargs)
    except RuntimeError as exc:
        msg = str(exc)
        if "Warmup script failed" in msg:
            logger.warning(
                "warmup failed (lite-cli soft fail) — continuing to agent. "
                "tail: %s", msg.splitlines()[-1][:200] if msg else "")
            return None
        raise


ds.run_warmup = _soft_run_warmup



def _real_time_worker(env_idx, task_queue, args, shared, progress):
    from multiprocessing import current_process
    name = current_process().name
    base_result_root = Path(args.result_dir)
    tasks_root = Path(args.tasks_root)
    ds._import_tasks_root(tasks_root)

    # Re-apply the soft-warmup monkey-patch in the worker process — under the
    # `spawn` start method the parent's monkey-patch wouldn't be inherited,
    # and we want the same behaviour either way.
    if getattr(ds.run_warmup, "__name__", "") != "_soft_run_warmup":
        ds.run_warmup = _soft_run_warmup

    from desktop_env.providers.docker_lite.lite_env import DockerLiteEnv
    from weavebench.agents._openclaw._chat import OpenClawChatAgent

    # Stagger startup to avoid simultaneous docker race / image-cache thrash.
    stagger_s = float(getattr(args, "startup_stagger_s", 2.0)) * env_idx
    if stagger_s > 0:
        time.sleep(stagger_s)

    while True:
        try:
            task, mode = task_queue.get(timeout=5)
        except Exception:
            break
        if mode != "cli":
            # Lite runner is CLI-only by design; skip anything else.
            continue
        sub = task.get("bench_subdir")
        result_root = (base_result_root / sub) if sub else base_result_root
        if ds.already_done(task, mode, args.model, result_root):
            progress.append({"event": "skip", "name": name,
                             "task": task["task_id"], "mode": mode})
            continue
        out_dir = result_root / mode / args.model / task["category"] / task["task_id"]
        progress.append({"event": "start", "name": name,
                         "task": task["task_id"], "mode": mode,
                         "ts": time.time()})
        env = None
        try:
            env = DockerLiteEnv(
                image=args.lite_image,
                client_password=args.client_password,
                task_id=task["task_id"],
                docker_network=args.lite_docker_network or None,
            )
            agent = OpenClawChatAgent(
                model=args.model,
                litellm_base_url=args.litellm_base_url,
                litellm_api_key=args.litellm_api_key,
                client_password=args.client_password,
                timeout=3600,  # 1h hard cap per task to prevent multi-hour worker hangs
                gui=False,
                max_steps=task.get("max_steps") or args.max_steps,
            )
            rec = ds.run_one(env, agent, task, mode, out_dir, tasks_root,
                             http_proxy=args.http_proxy)
            shared.append({
                "bench_subdir": sub,
                "category": task["category"],
                "task_id": task["task_id"],
                "mode": mode,
                "score": rec.get("score", 0.0),
                "scores": rec.get("scores"),
                "error": rec.get("error"),
                "elapsed_seconds": rec.get("elapsed_seconds"),
            })
            progress.append({"event": "done", "name": name,
                             "task": task["task_id"], "mode": mode,
                             "score": rec.get("score", 0.0),
                             "error": rec.get("error"),
                             "ts": time.time()})
        except Exception as exc:
            import traceback
            progress.append({"event": "crash", "name": name,
                             "task": task["task_id"], "mode": mode,
                             "error": str(exc)[-300:],
                             "ts": time.time()})
            logger.error("[%s] %s/%s [%s] crashed: %s\n%s",
                         name, task["category"], task["task_id"], mode,
                         exc, traceback.format_exc())
        finally:
            if env is not None:
                try: env.close()
                except Exception: pass


def _monitor(progress, total, stop_after):
    seen_idx = 0
    finished = 0
    t0 = time.time()
    while finished < total and (time.time() - t0) < stop_after:
        time.sleep(2)
        cur_len = len(progress)
        for i in range(seen_idx, cur_len):
            ev = progress[i]
            kind = ev["event"]
            tag = f"[{ev['name']}][{ev['mode']}] {ev['task']}"
            if kind == "start":
                logger.info("▶ START  %s", tag)
            elif kind == "skip":
                logger.info("⊘ SKIP   %s (already done)", tag)
                finished += 1
            elif kind == "done":
                err = ev.get("error")
                if err:
                    logger.warning("✗ DONE   %s score=%.3f err=%s",
                                   tag, ev.get("score", 0.0), str(err)[:120])
                else:
                    logger.info("✓ DONE   %s score=%.3f",
                                tag, ev.get("score", 0.0))
                finished += 1
            elif kind == "crash":
                logger.error("☠ CRASH  %s err=%s", tag, ev.get("error"))
                finished += 1
        seen_idx = cur_len
    logger.info("monitor exit: finished=%d/%d, elapsed=%.0fs",
                finished, total, time.time() - t0)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(allow_abbrev=False, 
        description="Run WeaveBench tasks in CLI-only lightweight Docker (chat.completions transport).")
    ap.add_argument("--client_password", default="password")
    ap.add_argument("--num_envs", type=int, default=4)
    ap.add_argument("--lite_image",
                    default=os.environ.get("WEAVEBENCH_LITE_IMAGE",
                                           "weavebench-ubuntu:v1.2"))
    ap.add_argument("--lite_docker_network",
                    default=os.environ.get("WEAVEBENCH_LITE_DOCKER_NETWORK", ""))

    ap.add_argument("--model", default="openai/gpt-5.5")
    ap.add_argument("--litellm_base_url", default=os.environ.get(
        "WEAVEBENCH_LITELLM_BASE_URL",
        os.environ.get("OPENROUTER_BASE_URL",
                       "https://openrouter.ai/api/v1")))
    ap.add_argument("--litellm_api_key", default=os.environ.get(
        "WEAVEBENCH_LITELLM_KEY",
        os.environ.get("OPENROUTER_API_KEY", "")))
    ap.add_argument("--max_steps", type=int, default=200)
    ap.add_argument("--mode", choices=["cli", "gui", "both"], default="cli",
                    help="Lite/CLI runner only supports --mode cli; accepted for forward-compat with weavebench-run dispatch.")

    ap.add_argument("--tasks_root", default=str(ROOT))
    ap.add_argument("--bench_subdir", default="bench_gen",
                    help="Single-bench mode (ignored if --bench_subdirs is set).")
    ap.add_argument("--bench_subdirs", default=None,
                    help="CSV of bench subdirs to merge into one global pool.")
    ap.add_argument("--categories",
                    default="WEB,DAV,OPS,DOC,DES,GAM,SPA,DSK")
    ap.add_argument("--task_filter", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--result_dir", default="./results_bench_gen_lite_chat")
    ap.add_argument("--http_proxy",
                    default=os.environ.get("WEAVEBENCH_VM_HTTP_PROXY", ""))
    ap.add_argument("--monitor_max_seconds", type=int, default=86400)
    ap.add_argument("--startup_stagger_s", type=float, default=2.0,
                    help="Per-worker startup stagger to avoid docker race.")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    tasks_root = Path(args.tasks_root).resolve()

    if args.bench_subdirs:
        subdirs = [s.strip() for s in args.bench_subdirs.split(",") if s.strip()]
        tasks = []
        for sub in subdirs:
            ds._TASKS_SUBDIR = sub
            sub_tasks = ds.discover_tasks(tasks_root, cats)
            for t in sub_tasks:
                t["bench_subdir"] = sub
            tasks.extend(sub_tasks)
            logger.info("  [%s] discovered %d tasks", sub, len(sub_tasks))
    else:
        ds._TASKS_SUBDIR = args.bench_subdir
        tasks = ds.discover_tasks(tasks_root, cats)

    if args.task_filter:
        filters = [f.strip() for f in args.task_filter.split(",") if f.strip()]
        tasks = [t for t in tasks if any(f in t["task_id"] for f in filters)]
    if args.limit:
        tasks = tasks[:args.limit]

    if not tasks:
        logger.error("No tasks discovered.")
        return

    modes = ["cli"]  # lite runner is CLI-only
    total_runs = len(tasks) * len(modes)

    logger.info("=" * 70)
    logger.info("LITE bench_gen runner (chat) — %d tasks (cli-only) = %d runs",
                len(tasks), total_runs)
    logger.info("Image:        %s", args.lite_image)
    logger.info("LiteLLM:      %s  model=%s",
                args.litellm_base_url, args.model)
    logger.info("Result root:  %s", Path(args.result_dir).resolve())
    if args.bench_subdirs:
        logger.info("Bench subs:   %s", args.bench_subdirs)
    logger.info("Categories:   %s", ", ".join(cats))
    logger.info("=" * 70)

    mgr = Manager()
    queue = mgr.Queue()
    shared = mgr.list()
    progress = mgr.list()
    for t in tasks:
        for m in modes:
            queue.put((t, m))

    procs = [Process(target=_real_time_worker,
                     args=(i, queue, args, shared, progress),
                     name=f"env-{i+1}")
             for i in range(args.num_envs)]
    for p in procs:
        p.start()

    try:
        _monitor(progress, total_runs, stop_after=args.monitor_max_seconds)
    finally:
        for p in procs:
            p.join(timeout=10)

    out = Path(args.result_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / f"summary_lite_chat_{datetime.now():%Y%m%d_%H%M%S}.json"
    summary_path.write_text(
        json.dumps(list(shared), indent=2, ensure_ascii=False),
        encoding="utf-8")
    logger.info("Summary written: %s", summary_path)


if __name__ == "__main__":
    main()
