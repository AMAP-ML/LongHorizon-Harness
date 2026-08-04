"""Drop-in replacement of `orchestrator.run_one` that swaps
the in-VM grader for our host-side Agent-as-Judge.

Pipeline per task (host-side coordination):
  1. (unchanged) bootstrap openclaw inside VM, upload workspace/skills/env
  2. (unchanged) warmup, agent.run, init_screenshot
  3. (NEW) skip `run_grading` — pass empty automated_checks so it no-ops
  4. (unchanged) `_archive_deliverables` to pull results.tar.gz to host
  5. (NEW) call `eval.agent_judge.stage_case` to assemble judge input
  6. (NEW) call `eval.agent_judge.judge_runner.judge_one` to score
  7. write score.json (same path as grader version) with judge result

The original `run_grading` is NEVER called when this wrapper is active.

Configuration env vars (all optional, sensible defaults):
  AJ_BENCH_ROOT          : path to tasks (default: derived from tasks_root)
  AJ_JUDGE_WORKSPACE     : openclaw workspace dir (default: <repo>/judge_agent_test/judge_workspace)
  AJ_TIMEOUT             : per-case judge timeout sec (default: 1800)
  AJ_THINKING            : openclaw thinking level (default: medium)
  AJ_OPENCLAW_BIN        : path to openclaw bin (default: PATH openclaw, then
                           <repo>/judge_agent_test/node_modules/.bin/openclaw)
  AJ_OPENCLAW_PROFILE    : openclaw profile (default: judge)
"""
from __future__ import annotations
import json
import logging
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .stage_case import stage_case
from .judge_runner import judge_one
from .token_stats import aggregate_chat_jsonl

if TYPE_CHECKING:
    from weavebench.agents._openclaw._responses import OpenClawAgent

logger = logging.getLogger("agent_judge.run_one_aj")

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parents[2]


def _default_judge_root() -> Path:
    return _REPO_ROOT / "judge_agent_test"


def _resolve_bench_subdir(task: dict) -> str:
    """Return the task's bench_subdir, defaulting to "." for the public layout
    where all tasks live directly under <tasks_root>/<DOMAIN>/."""
    return task.get("bench_subdir") or "."


def _resolve_bench_root(tasks_root: Path) -> Path:
    """Return the directory that directly contains the 8 domain folders
    (DAV/DES/DOC/DSK/GAM/OPS/SPA/WEB). Users pass --tasks_root pointing at
    that directory already (e.g. ./cache/tasks), so we return it as-is.

    Override with $AJ_BENCH_ROOT if your judge needs a different layout.
    """
    env_p = os.environ.get("AJ_BENCH_ROOT")
    if env_p:
        return Path(env_p)
    return Path(tasks_root)


def _resolve_judge_workspace() -> Path:
    env_p = os.environ.get("AJ_JUDGE_WORKSPACE")
    if env_p:
        return Path(env_p)
    return _default_judge_root() / "judge_workspace"


def _parse_agent_log_markers(agent_log: Path) -> dict:
    """Extract the AGENT_EXIT / RETRIES_USED markers that every harness runner
    (openclaw/codex/claudecode/hermes) echoes into agent.log at the end of its
    in-VM retry loop. Returns {agent_exit, retries_used} with None when a marker
    is absent (e.g. agent.log missing, or the runner was wall-clock-killed before
    it could echo AGENT_EXIT)."""
    out = {"agent_exit": None, "retries_used": None, "max_steps_reached": None}
    if not agent_log.exists():
        return out
    try:
        import re
        # Only the tail matters; the markers are echoed last. The Python side of
        # codex/claude/hermes also appends "[<harness>] AGENT_EXIT=N" lines, so a
        # plain AGENT_EXIT=(\d+) search would match those too — that's fine, they
        # carry the same value as the runner's own marker.
        tail = agent_log.read_text(encoding="utf-8", errors="ignore")[-8192:]
        m_exit = re.search(r"AGENT_EXIT=(\d+)", tail)
        if m_exit:
            out["agent_exit"] = int(m_exit.group(1))
        m_retry = re.search(r"RETRIES_USED=(\d+)", tail)
        if m_retry:
            out["retries_used"] = int(m_retry.group(1))
        m_steps = re.search(r"MAX_STEPS_REACHED=(\d+)", tail)
        if m_steps:
            out["max_steps_reached"] = int(m_steps.group(1))
    except Exception:
        pass
    return out


# Below this many generated output tokens we treat a "completed" run as having
# produced nothing — the retry-exhausted empty-run case the process signals
# (agent_done=True, AGENT_EXIT=0) cannot otherwise distinguish. Not exactly 0
# because a bare refusal ("I'm sorry, ...") still emits a few dozen tokens.
EMPTY_RUN_OUTPUT_TOKENS = 8


def _classify_agent_status(agent_done, markers: dict, token_usage: dict) -> dict:
    """Unified post-run health verdict for ALL four harnesses.

    Returns a dict {status, reason, agent_exit, retries_used} where status is:
      - "timeout"    : runner never wrote its done-file (agent_done is False);
                       agent.log usually lacks an AGENT_EXIT marker.
      - "crashed"    : runner finished but the last agent invocation exited
                       non-zero (AGENT_EXIT != 0).
      - "empty_run"  : runner finished cleanly (agent_done True, AGENT_EXIT 0)
                       but the model produced ~no output — the retry-exhausted
                       upstream-failure / silent-fail case that the process
                       signals alone cannot catch.
      - "ok"         : finished cleanly and produced real output.

    This is the single point of truth that makes the four harnesses consistent:
    they all funnel chat.jsonl through aggregate_chat_jsonl and all echo the same
    AGENT_EXIT/RETRIES_USED markers, so one classifier covers every harness.
    """
    agent_exit = markers.get("agent_exit")
    retries_used = markers.get("retries_used")
    max_steps_reached = markers.get("max_steps_reached")
    verdict = {"status": "ok", "reason": None,
               "agent_exit": agent_exit, "retries_used": retries_used,
               "max_steps_reached": max_steps_reached}

    if agent_done is False:
        verdict["status"] = "timeout"
        verdict["reason"] = "runner did not write done-file (wall-clock timeout)"
        return verdict

    if max_steps_reached:
        verdict["status"] = "step_capped"
        verdict["reason"] = "agent reached the configured max_steps/max_turns limit"
        return verdict

    if agent_exit is not None and agent_exit != 0:
        verdict["status"] = "crashed"
        verdict["reason"] = f"agent runner exited non-zero (AGENT_EXIT={agent_exit})"
        return verdict

    # agent_done True (or unknown) and no non-zero exit: did the model actually
    # produce anything? Use the token aggregation we already computed.
    tu_ok = bool(token_usage.get("ok"))
    n_calls = token_usage.get("n_calls") or 0
    output = token_usage.get("output") or 0
    if tu_ok and (n_calls == 0 or output <= EMPTY_RUN_OUTPUT_TOKENS):
        verdict["status"] = "empty_run"
        rsuffix = f", retries_used={retries_used}" if retries_used else ""
        verdict["reason"] = (f"clean exit but no model output "
                             f"(n_calls={n_calls}, output_tokens={output}{rsuffix})")
        return verdict

    return verdict


def run_one_aj(env, agent: "OpenClawAgent", task: dict, mode: str,
                output_dir: Path, tasks_root: Path, http_proxy: str = "") -> dict:
    """Same signature as ds.run_one but uses Agent-as-Judge instead of grader."""
    # Lazy import to avoid circular when this module is imported during patching.
    import weavebench.eval.orchestrator as ds

    output_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task["task_id"],
        "category": task["category"],
        "mode": mode,
        "model": agent.model,
        "started_at": datetime.utcnow().isoformat(),
        "score": 0.0,
        "scores": {},
        "error": None,
    }

    try:
        ds.grow_vm_rootfs_if_needed(env, agent.client_password, task["task_id"],
                                    output_dir=output_dir)

        # 0-3 (unchanged from ds.run_one): bootstrap → workspace → warmup → agent
        agent.bootstrap(env)
        agent.configure(env)
        ds.upload_workspace(env, task["workspace_path"],
                            client_password=agent.client_password)
        ds.upload_skills(env, task.get("skills", ""), task["skills_path"])
        ds.write_task_env_script(env, task.get("env", ""), http_proxy=http_proxy)
        ds.run_warmup(env, task.get("warmup", ""),
                      client_password=agent.client_password)
        if mode == "gui":
            ds.take_init_screenshot(env, output_dir)
        ds.stage_eval_bundle(env, tasks_root, client_password=agent.client_password)

        sys_prompt = ds.weavebench_system_prompt(86400, gui=(mode == "gui"),
                                          model=agent.model)
        meta = agent.run(env, task["prompt"], output_dir,
                          system_prompt_override=sys_prompt,
                          already_configured=True)
        record.update({"agent_done": meta.get("agent_done"),
                       "elapsed_seconds": meta.get("elapsed_seconds")})

        try:
            tu = aggregate_chat_jsonl(output_dir / "chat.jsonl")
            record["agent_token_usage"] = tu
            if tu.get("ok") and tu.get("n_calls"):
                logger.info("[%s/%s/%s] agent tokens: calls=%d in=%d out=%d "
                            "cache_r=%d cache_w=%d reasoning=%d total=%d",
                            task["category"], task["task_id"], mode,
                            tu["n_calls"], tu["input"], tu["output"],
                            tu["cache_read"], tu["cache_write"],
                            tu["reasoning"], tu["total_tokens"])
        except Exception as exc:
            record["agent_token_usage"] = {"ok": False,
                                           "error": f"{type(exc).__name__}: {exc}"}
            logger.warning("[%s/%s/%s] token aggregation failed (non-fatal): %s",
                           task["category"], task["task_id"], mode, exc)

        # 3a. Unified harness-health verdict (openclaw/codex/claudecode/hermes).
        #     All four echo AGENT_EXIT/RETRIES_USED into agent.log and funnel
        #     chat.jsonl through aggregate_chat_jsonl, so one classifier makes
        #     them consistent. Catches the retry-exhausted "empty_run" case that
        #     agent_done=True + AGENT_EXIT=0 alone cannot distinguish.
        agent_log = output_dir / "agent.log"
        markers = _parse_agent_log_markers(agent_log)
        verdict = _classify_agent_status(
            record.get("agent_done"), markers,
            record.get("agent_token_usage") or {})
        record["agent_status"] = verdict["status"]
        record["retries_used"] = verdict["retries_used"]
        record["agent_exit"] = verdict["agent_exit"]
        record["max_steps_reached"] = verdict.get("max_steps_reached")
        if verdict["status"] != "ok":
            note = f"agent_{verdict['status']}: {verdict['reason']}"
            record["error"] = (record.get("error") or "")
            record["error"] = (record["error"] + " | " + note) if record["error"] else note
            logger.warning("[%s/%s/%s] agent health=%s (%s)",
                           task["category"], task["task_id"], mode,
                           verdict["status"], verdict["reason"])

        # 4. (NEW: skip grader). Still archive deliverables — this is what the
        #    judge needs to read on the host.
        try:
            ds._archive_deliverables(env, output_dir,
                                     client_password=agent.client_password)
        except Exception as exc:
            logger.warning("[%s/%s/%s] deliverable archival failed (non-fatal): %s",
                           task["category"], task["task_id"], mode, exc)

        # 5-7. Agent-as-Judge step (host-side, after VM teardown is OK).
        bench_subdir = _resolve_bench_subdir(task)
        bench_root = _resolve_bench_root(tasks_root)
        judge_ws = _resolve_judge_workspace()

        case_id = f"{task['category']}_{task['task_id']}"
        try:
            stage = stage_case(
                case_dir=str(output_dir),
                bench_root=str(bench_root),
                bench_subdir=bench_subdir,
                judge_workspace=str(judge_ws),
                case_id=case_id,
                category=task["category"],
                task_id=task["task_id"],
            )
        except Exception as exc:
            record["error"] = (record.get("error") or "") + \
                f" | stage_case_failed: {type(exc).__name__}: {str(exc)[:200]}"
            logger.error("[%s/%s/%s] stage_case crashed: %s",
                         task["category"], task["task_id"], mode, exc)
            stage = None

        if stage is not None:
            timeout = int(os.environ.get("AJ_TIMEOUT", "1800"))
            thinking = os.environ.get("AJ_THINKING", "medium")
            max_attempts = int(os.environ.get("AJ_RETRY", "5"))
            # Backoff between attempts (seconds). Default jittered 30-60s so two
            # retries don't hammer Azure with the exact same payload back-to-back
            # when the previous failure was a transient backend issue.
            base_backoff_s = int(os.environ.get("AJ_RETRY_BACKOFF_S", "30"))

            aj = None
            for attempt in range(1, max_attempts + 1):
                aj = judge_one(stage, case_id,
                               openclaw_bin=os.environ.get("AJ_OPENCLAW_BIN"),
                               openclaw_profile=os.environ.get("AJ_OPENCLAW_PROFILE", "judge"),
                               thinking=thinking, timeout=timeout)
                if aj.get("ok"):
                    break
                # Retry on transient-looking failures: short refusals
                # (Azure content filter false positive), rc=0/rc=1 with no JSON,
                # or Azure 5xx-ish "Unknown error" / "no error details".
                err = aj.get("error", "")
                stdout_tail = aj.get("openclaw_stdout_tail", "")
                stderr_tail = aj.get("openclaw_stderr_tail", "")
                stderr_low = stderr_tail.lower()
                short_refusal = ("cannot assist" in stdout_tail.lower()
                                 or "i'm sorry" in stdout_tail.lower())
                no_json = "no score.json written" in err
                azure_5xx = ("unknown error" in stderr_low
                             or "no error details in response" in stderr_low
                             or "internal_server_error" in stderr_low
                             or "service_unavailable" in stderr_low
                             or "litellm.internalservererror" in stderr_low
                             or "503" in stderr_low and "error" in stderr_low)
                rate_limited = ("429" in stderr_low or "rate" in stderr_low and "limit" in stderr_low)
                retryable = short_refusal or no_json or azure_5xx or rate_limited
                if attempt < max_attempts and retryable:
                    reason = ("refusal" if short_refusal
                              else "azure_5xx" if azure_5xx
                              else "rate_limit" if rate_limited
                              else err[:80])
                    # Jittered backoff to avoid hammering Azure when downstream
                    # is overloaded. Base 30s + 0-30s jitter, exponential per attempt.
                    import random
                    backoff = base_backoff_s * (2 ** (attempt - 1)) + random.randint(0, 30)
                    backoff = min(backoff, 300)  # cap at 5 min
                    logger.warning("[%s/%s/%s] judge attempt %d/%d failed (%s); "
                                   "sleeping %ds then retrying",
                                   task["category"], task["task_id"], mode,
                                   attempt, max_attempts, reason, backoff)
                    time.sleep(backoff)
                    continue
                break

            if aj.get("ok"):
                record["score"] = aj.get("final_score") or 0.0
                record["scores"] = {
                    "judge_method": "agent_as_judge",
                    "judge_model": os.environ.get("JUDGE_MODEL", "openclaw_config_default"),
                    "judge_elapsed_s": aj.get("elapsed_s"),
                    **(aj.get("score_json") or {}),
                }
            else:
                err = aj.get("error", "?")
                record["error"] = (record.get("error") or "") + \
                    f" | judge_failed: {err[:300]}"
                record["scores"] = {
                    "judge_method": "agent_as_judge",
                    "judge_error": err,
                    "openclaw_stderr_tail": aj.get("openclaw_stderr_tail", ""),
                    "openclaw_stdout_tail": aj.get("openclaw_stdout_tail", ""),
                }
                logger.error("[%s/%s/%s] judge failed: %s",
                             task["category"], task["task_id"], mode, err)

    except Exception as exc:
        logger.error("[%s/%s/%s] crashed: %s\n%s",
                     task["category"], task["task_id"], mode,
                     exc, traceback.format_exc())
        record["error"] = (record.get("error") or "") + \
            f" | run_one_aj exc: {str(exc)[-300:]}"

    record["finished_at"] = datetime.utcnow().isoformat()
    (output_dir / "score.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record
