"""Run OSWorld-V2 (108 tasks) with an *injected* cuaclaw/openclaw agent.

Everything except the agent is OSWorld-V2 NATIVE (paper-faithful):
  - task loading  : task_class/task_*.py via load_task_config(eval_version=v2)
  - setup         : env.reset(task_config=task)  → task.setup() inside the VM
  - final-state   : env.evaluate()               → task.evaluate(env), native
  - persistence   : lib_run_single._persist_evaluation_result → result.txt/json

The ONLY non-native part is the agent: instead of the upstream predict/step
loop, we INJECT the whole cuaclaw CLI into the VM (WeaveBench "hybrid" style)
and let it drive the desktop autonomously (GUI __computer__ + bash CLI), talking
to gpt-5.5 via the LiteLLM proxy on the host. This is the OSWorld-V2 port of
WeaveBench/experiments/osworld_hybrid.

Per-task pipeline:
  1. env.reset(task_config=task)         # native VM revert + task.setup()
  2. agent.bootstrap(env)/configure(env) # inject 491MB openclaw + plugin + LLM cfg
  3. /tmp_workspace mkdir (agent scratch)
  4. agent.run(env, instruction)         # cuaclaw runs the whole task in-VM
  5. env.evaluate()                      # NATIVE final-state check (0..1)
  6. _persist_evaluation_result          # result.txt / result.json

Output layout (parallel to upstream run_multienv):
  <result_dir>/pyautogui/screenshot/<model>/tasks/<task_id>/
      result.txt          # native score (leaderboard parity)
      result.json         # native dict (if evaluator returns dict)
      score.json          # this runner's per-task record
      chat.jsonl agent.log final_screenshot.png ...
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from multiprocessing import Manager, Process
from pathlib import Path
from typing import List

# OSWorld-V2 repo root (experiments/osworld_v2_inject/ -> repo root is parents[2])
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from desktop_env.desktop_env import DesktopEnv  # noqa: E402
from task_loader import load_task_config  # noqa: E402
import lib_run_single  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("osw2_inject")


# ---------------------------------------------------------------------------
# Task discovery: read evaluation_examples/test_v2.json -> ["001", ...]
# ---------------------------------------------------------------------------
def discover_v2_tasks(osworld_root: Path, meta_path: str,
                      task_filter: str = "") -> List[str]:
    mp = Path(meta_path)
    if not mp.is_absolute():
        mp = osworld_root / meta_path
    meta = json.loads(mp.read_text(encoding="utf-8"))
    ids = meta.get("tasks", []) if isinstance(meta, dict) else list(meta)
    if task_filter:
        ids = [t for t in ids if task_filter in t]
    return list(ids)


def _task_instruction(task) -> str:
    # BaseTask is a dict subclass; instruction is both attr and key.
    try:
        return task.get("instruction") or getattr(task, "instruction", "") or ""
    except Exception:
        return getattr(task, "instruction", "") or ""


def _is_multiphase(task) -> bool:
    fn = getattr(task, "get_phases", None)
    return callable(fn)


# ---------------------------------------------------------------------------
# System prompt — hybrid (GUI __computer__ + bash CLI). No spoilers.
# ---------------------------------------------------------------------------
def build_system_prompt(instruction: str, client_password: str, gui: bool = True) -> str:
    if gui:
        head = (
        f"You are operating an Ubuntu 22.04 desktop workstation as user `user` "
        f"with full sudo access (password: {client_password}). You have two "
        f"equally-available tools and may mix them freely: (1) the `bash` tool "
        f"for shell commands, file edits, gsettings/dconf, package installs, "
        f"git, etc.; (2) `__computer__` (click/type/key/scroll/drag/screenshot/"
        f"wait by on-screen pixel coordinates; each call auto-returns a fresh "
        f"full-screen screenshot to ground your next step). Neither tool is "
        f"preferred — pick what is most direct for each sub-step. The display is "
        f"`:0`; an initial screenshot is at /tmp/init_screenshot.png. For tasks "
        f"that involve a web page or web app, the relevant page is ALREADY OPEN "
        f"in a tab of the on-screen Chrome (it may be a remote site, a local "
        f"http://localhost service, or a file:// page) — and for stateful sites "
        f"that tab already carries the right session. That Chrome also exposes a "
        f"DevTools endpoint on localhost:1337 (and :9222). Do your web work in "
        f"THAT same Chrome instance and its already-open tab — clicking via "
        f"`__computer__`, driving it over CDP, or scripting it are all fine; the "
        f"only requirement is that the change lands in that one Chrome tab/"
        f"session. Do NOT open a separate or headless browser, do NOT start a "
        f"fresh session, and do NOT hit the site with bare requests/curl that "
        f"bypass that tab's session — the verifier inspects the state of that "
        f"one already-open Chrome tab, so anything done elsewhere is invisible "
        f"to it. If you genuinely need a page that is not open yet, navigate to "
        f"it inside that same existing Chrome tab rather than spawning a new "
        f"browser.\n\n")
    else:
        head = (
        f"You are operating an Ubuntu 22.04 desktop workstation as user `user` "
        f"with full sudo access (password: {client_password}) over a COMMAND-LINE "
        f"ONLY interface. You have exactly one tool: `bash` (shell commands, file "
        f"edits, gsettings/dconf, package installs, git, headless office "
        f"conversions, etc.). There is NO GUI, NO mouse/keyboard control, and NO "
        f"screenshots — do everything via the shell. There is a desktop X server "
        f"on `:0` if a task strictly needs it, but you cannot click; drive any "
        f"app through its CLI/scripting interface. For web tasks, there is an "
        f"already-open Chrome with a DevTools endpoint on localhost:1337 (and "
        f":9222) carrying the right session — drive it over CDP from bash "
        f"(curl/websocket to the DevTools API), not a separate or headless "
        f"browser, so the change lands in that one tab the verifier inspects.\n\n")
    return (
        head +
        "The task is graded by an automated verifier that inspects the FINAL "
        "STATE of files, application data, and processes — plain-text answers, "
        "explanations, or 'here's how you would do it' DO NOT count. Before "
        "declaring done you must have actually changed the system state the "
        "verifier will inspect.\n\n"
        "=== EXECUTION POLICY (read carefully) ===\n"
        "1. ACT, DON'T EXPLAIN. You are an operating agent, not a tutor. "
        "Accomplish the task by actually invoking tools; a tutorial/table/"
        "explanation is an automatic 0.\n"
        "2. FILE OUTPUTS — EXACT PATHS AND NAMES. When a path or filename is "
        "specified, write to EXACTLY that path with EXACTLY that name (case, "
        "spaces, extension all matter). Do not rename, do not save a 'similar' "
        "name, do not save a *_filled / *_copy variant — the verifier fetches "
        "the literal path and 404s otherwise. Overwrite the original file in "
        "place when asked to edit it. If no path is given, save to "
        "/home/user/Desktop/ with a sensible name.\n"
        "3. FORMATTING — APPLY GLOBALLY AND PRECISELY. The verifier inspects "
        "format at the finest granularity (run-level font in docx, per-shape "
        "position/EMU in pptx, exact RGB, cell-level number format in xlsx). "
        "(a) apply the change to ALL matching elements unless scope is limited; "
        "(b) use exact numeric values (do not round); (c) always SAVE and "
        "cleanly close the document — unsaved buffers don't count.\n"
        "4. WEB TASKS — USE THE EXACT SITE NAMED. Navigate to exactly the domain "
        "named in the task; the verifier checks the resulting state/URL. Never "
        "substitute a site you think is equivalent. Complete the flow end-to-end "
        "(don't stop on the homepage), and do it in the one already-open Chrome "
        "(GUI: click it; CLI: drive it over CDP) so the state lands where the "
        "verifier reads it.\n"
        "5. NO FABRICATION. Do not fake completion: do not copy a ground-truth/"
        "reference file to pose as your output, do not hand-write a stub project "
        "file (e.g. a fake .mlt/.rpp/.json) that you didn't genuinely produce, "
        "and do not paint placeholder images with PIL/fpdf/matplotlib to satisfy "
        "a file-existence/screenshot check. If a deliverable is genuinely "
        "uncapturable, skip it and briefly say why — accept the points loss "
        "rather than faking it. The verifier deep-inspects content and will cap "
        "fabricated work at 0.\n"
        "6. INFEASIBLE — REFUSE EXPLICITLY. If after honest investigation the "
        "task is truly impossible in the named application, output a final "
        "message starting with `FAIL` and one sentence why; do not fake success "
        "on an unrelated setting.\n"
        "=== END EXECUTION POLICY ===\n\n"
        f"=== TASK ===\n{instruction}\n"
    )


def grow_vm_rootfs(env, client_password: str, task_id: str) -> None:
    """Expand the guest root partition to fill the (resized) virtual disk.

    The OSWorld-V2 qcow2 ships a 29.5G sda3 inside a 70G disk (after our
    `qemu-img resize +20G`), leaving the rootfs ~1.2G free — too small for the
    490MB openclaw tarball + its Node22/openclaw unpack. The qcow2 is bind-
    mounted read-only, so every fresh VM starts from the un-grown partition and
    guest writes land in QEMU's throwaway overlay. Hence we grow sda3 ONCE per
    VM boot, right after reset and before injection. Idempotent: skipped if the
    rootfs already has comfortable headroom.
    """
    import requests
    url = f"http://{env.vm_ip}:{env.server_port}/setup/execute"

    def _exec(cmd: str, timeout: int = 90) -> str:
        r = requests.post(url, json={"command": ["bash", "-c", cmd],
                                     "shell": False}, timeout=timeout)
        r.raise_for_status()
        return (r.json() or {}).get("output", "") or ""

    try:
        avail = _exec("df -BG --output=avail / | tail -1 | tr -dc '0-9'")
        avail_g = int(avail.strip() or "0")
        if avail_g >= 8:
            logger.info("[%s] rootfs has %dG free — skip growpart", task_id, avail_g)
            return
    except Exception as exc:
        logger.warning("[%s] disk check failed (continue to growpart): %s",
                       task_id, exc)

    grow = (
        f"echo '{client_password}' | sudo -S -p '' bash -c '"
        "echo \", +\" | sfdisk --no-reread -N 3 /dev/sda >/dev/null 2>&1; "
        "partprobe /dev/sda >/dev/null 2>&1 || true; "
        "resize2fs /dev/sda3 >/dev/null 2>&1'"
    )
    try:
        _exec(grow, timeout=120)
        after = _exec("df -h / | tail -1")
        logger.info("[%s] growpart done -> %s", task_id, after.strip())
    except Exception as exc:
        logger.warning("[%s] growpart failed (injection may run out of disk): %s",
                       task_id, exc)


def quiesce_pkg_daemons(env, client_password: str, task_id: str) -> None:
    """Stop background package daemons BEFORE task.setup() runs.

    OSWorld-V2's image runs packagekitd / unattended-upgrades, which hold the
    apt/dpkg lock right after boot. Several tasks' own setup scripts pip/apt-
    install dependencies (e.g. task_022 installs fpdf2); those collide with the
    daemon and fail ("Could not get lock"), leaving the task env incomplete.
    The VM is already up after DesktopEnv.__init__, so we can do this before
    env.reset() kicks off task.setup().
    """
    import requests
    try:
        requests.post(
            f"http://{env.vm_ip}:{env.server_port}/setup/execute",
            json={"command": ["bash", "-c",
                f"echo '{client_password}' | sudo -S -p '' bash -c '"
                "systemctl stop packagekit.service unattended-upgrades.service "
                "apt-daily.service apt-daily-upgrade.service 2>/dev/null; "
                "pkill -9 -f packagekitd 2>/dev/null; "
                "pkill -9 -f unattended-upgrade 2>/dev/null; "
                "dpkg --configure -a 2>/dev/null; true'"], "shell": False},
            timeout=60)
        logger.info("[%s] package daemons quiesced (pre-setup)", task_id)
    except Exception as exc:
        logger.warning("[%s] quiesce_pkg_daemons failed (non-fatal): %s",
                       task_id, exc)


# ---------------------------------------------------------------------------
# run_one — single task, native env + injected agent
# ---------------------------------------------------------------------------
def run_one(env, agent, task_id: str, task, out_dir: Path,
            args) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    instruction = _task_instruction(task)
    record = {
        "task_id": task_id,
        "model": agent.model,
        "started_at": datetime.utcnow().isoformat(),
        "score": 0.0,
        "error": None,
        "multiphase": _is_multiphase(task),
    }

    if _is_multiphase(task):
        # Native multi-phase tasks interleave per-phase setup/evaluate with the
        # agent loop — our single injected run can't honor phase gating. Flag it
        # so we can handle/triage separately rather than silently mis-scoring.
        record["error"] = "multiphase_unsupported_in_inject_runner"
        logger.warning("[%s] multi-phase task — inject runner runs phase-1 setup "
                       "only; native evaluate() scores phase 1.", task_id)

    try:
        # 0. Quiesce package daemons BEFORE reset so task.setup()'s own pip/apt
        #    installs (e.g. task_022 fpdf2) don't collide with the apt lock.
        quiesce_pkg_daemons(env, agent.client_password, task_id)

        # 1. NATIVE reset: revert VM + run task.setup() inside the VM.
        env.reset(task_config=task)
        logger.info("[%s] env.reset OK", task_id)
        time.sleep(args.post_reset_sleep)

        # 1b. Grow rootfs to fit the injected agent (qcow2 ships a 29G part in a
        #     70G disk; ro mount means we re-grow every boot).
        grow_vm_rootfs(env, agent.client_password, task_id)

        # 2. Inject cuaclaw into the VM. agent.run() calls bootstrap()+configure()
        #    internally (both idempotent), so we don't pre-call them here.

        # 3. Agent scratch dir (the computer-tool plugin writes screenshots here).
        try:
            import requests
            requests.post(
                f"http://{env.vm_ip}:{env.server_port}/setup/execute",
                json={"command": [
                    "bash", "-c",
                    f"echo '{agent.client_password}' | sudo -S -p '' bash -c "
                    f"'mkdir -p /tmp_workspace/results /tmp_workspace/tmp "
                    f"/tmp_workspace/_screenshots && chown -R user:user /tmp_workspace'"
                ], "shell": False}, timeout=60)
        except Exception as exc:
            logger.warning("[%s] /tmp_workspace mkdir failed (non-fatal): %s",
                           task_id, exc)

        # 4. Run the injected agent (self-contained multi-step loop in the VM).
        sys_prompt = build_system_prompt(instruction, agent.client_password,
                                         gui=bool(getattr(args, "agent_gui", True)))
        meta = agent.run(env, instruction, out_dir,
                         system_prompt_override=sys_prompt)
        record["agent_done"] = meta.get("agent_done")
        record["elapsed_seconds"] = meta.get("elapsed_seconds")

        # 5. NATIVE final-state evaluation (this is the paper-faithful score).
        try:
            raw = env.evaluate()
        except Exception as e:
            raw = 0.0
            record["error"] = (record.get("error") or "") + \
                f" | evaluate_failed: {type(e).__name__}: {str(e)[:200]}"
            logger.error("[%s] env.evaluate crashed: %s", task_id, e)

        # 6. NATIVE persistence -> result.txt / result.json. Returns float score.
        score = lib_run_single._persist_evaluation_result(raw, str(out_dir))
        record["score"] = score
        record["score_native"] = score
        logger.info("[%s] native score = %.3f", task_id, score)

    except Exception as exc:
        logger.error("[%s] run_one crashed: %s\n%s", task_id, exc,
                     traceback.format_exc())
        record["error"] = (record.get("error") or "") + \
            f" | run_one_exc: {str(exc)[-300:]}"

    record["finished_at"] = datetime.utcnow().isoformat()
    (out_dir / "score.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# Resume: a task is done if result.txt exists and last run had no hard error.
# ---------------------------------------------------------------------------
def already_done(out_dir: Path) -> bool:
    if not (out_dir / "result.txt").is_file():
        return False
    sp = out_dir / "score.json"
    if sp.is_file():
        try:
            rec = json.loads(sp.read_text(encoding="utf-8"))
            err = rec.get("error") or ""
            # Re-run on transient infra errors; keep real (graded) results.
            for transient in ("run_one_exc", "evaluate_failed", "Bootstrap",
                              "openclaw", "Timeout", "Connection"):
                if transient in err:
                    return False
        except Exception:
            return False
    return True


# ---------------------------------------------------------------------------
# Worker: one DesktopEnv per process, pull task ids from the shared queue.
# ---------------------------------------------------------------------------
def worker(env_idx: int, task_ids: list, args, shared: list) -> None:
    harness = getattr(args, "agent_harness", "openclaw")
    if harness == "codex":
        from mm_agents.codex_agent import CodexAgent as _AgentClass
    elif harness == "claudecode":
        from mm_agents.claudecode_agent import ClaudeCodeAgent as _AgentClass
    else:
        from mm_agents.openclaw_agent import OpenClawAgent as _AgentClass

    name = f"osw2-env-{env_idx+1}"
    result_root = Path(args.result_dir) / "pyautogui" / "screenshot" / args.model / "tasks"
    time.sleep(env_idx * args.startup_stagger_s)

    for task_id in task_ids:
        out_dir = result_root / task_id
        if already_done(out_dir):
            logger.info("[%s] [%s] already done — skip", name, task_id)
            continue
        out_dir.mkdir(parents=True, exist_ok=True)

        env = None
        try:
            task = load_task_config(
                None, task_id=task_id, base_dir="evaluation_examples",
                domain="tasks", eval_version="v2",
            )
            logger.info("[%s] [%s] loaded: %s", name, task_id,
                        (_task_instruction(task) or "")[:70])

            env = DesktopEnv(
                provider_name=args.provider_name,
                path_to_vm=args.path_to_vm,
                action_space="pyautogui",
                screen_size=(args.screen_width, args.screen_height),
                headless=args.headless,
                os_type=args.os_type,
                require_a11y_tree=False,
                require_terminal=False,
                client_password=args.client_password,
                snapshot_name=args.snapshot_name,
            )
            agent = _AgentClass(
                model=args.model,
                litellm_base_url=args.litellm_base_url,
                litellm_api_key=args.litellm_api_key,
                client_password=args.client_password,
                timeout=int(os.environ.get("OSW_AGENT_TIMEOUT", str(args.agent_timeout))),
                gui=args.agent_gui,
                max_steps=args.max_steps,
            )
            rec = run_one(env, agent, task_id, task, out_dir, args)
            shared.append({"task_id": task_id, "score": rec.get("score", 0.0),
                           "error": rec.get("error")})
            logger.info("[%s] [%s] -> score=%.3f%s", name, task_id,
                        rec.get("score", 0.0),
                        f"  ERR={str(rec['error'])[:80]}" if rec.get("error") else "")
        except Exception as exc:
            logger.error("[%s] [%s] worker crash: %s\n%s", name, task_id, exc,
                         traceback.format_exc())
            shared.append({"task_id": task_id, "score": 0.0,
                           "error": f"worker_exc: {type(exc).__name__}: {str(exc)[:200]}"})
        finally:
            if env is not None:
                try:
                    env.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
def _bool(s):
    if isinstance(s, bool):
        return s
    if str(s).lower() in ("true", "1", "yes", "y"):
        return True
    if str(s).lower() in ("false", "0", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"bool expected, got {s!r}")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    # VM / provider
    ap.add_argument("--provider_name", default="docker")
    ap.add_argument("--path_to_vm", default=None,
                    help="Absolute path to OSWorld-V2 qcow2. For docker, "
                         "None lets the manager auto-resolve/download.")
    ap.add_argument("--headless", type=_bool, default=True)
    ap.add_argument("--screen_width", type=int, default=1920)
    ap.add_argument("--screen_height", type=int, default=1080)
    ap.add_argument("--os_type", default="Ubuntu")
    ap.add_argument("--client_password", default="osworld-public-evaluation")
    ap.add_argument("--snapshot_name", default="init_state")
    ap.add_argument("--num_envs", type=int, default=6)
    ap.add_argument("--startup_stagger_s", type=int, default=8)
    ap.add_argument("--post_reset_sleep", type=int, default=60)

    # Agent / model
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--litellm_base_url", required=True,
                    help="LiteLLM base URL reachable FROM INSIDE the VM "
                         "(e.g. http://172.17.0.1:4200/v1).")
    ap.add_argument("--litellm_api_key", required=True)
    ap.add_argument("--max_steps", type=int, default=50)
    ap.add_argument("--agent_timeout", type=int, default=3600)
    ap.add_argument("--agent_gui", type=_bool, default=True,
                    help="True (default) = hybrid GUI+CLI agent (computer-tool "
                         "plugin enabled). False = CLI-only ablation.")

    # Task selection
    ap.add_argument("--osworld_root", default=str(REPO_ROOT))
    ap.add_argument("--test_all_meta_path",
                    default="evaluation_examples/test_v2.json")
    ap.add_argument("--task_filter", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--result_dir", required=True)
    ap.add_argument("--agent_harness", default="openclaw",
                    choices=["openclaw", "codex", "claudecode"],
                    help="In-VM agent harness: openclaw (default) or codex.")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    osworld_root = Path(args.osworld_root).resolve()
    task_ids = discover_v2_tasks(osworld_root, args.test_all_meta_path,
                                 args.task_filter)
    if args.limit:
        task_ids = task_ids[:args.limit]
    logger.info("Discovered %d OSWorld-V2 tasks (num_envs=%d, model=%s, gui=%s, harness=%s)",
                len(task_ids), args.num_envs, args.model, args.agent_gui,
                getattr(args, "agent_harness", "openclaw"))
    if not task_ids:
        logger.error("No tasks — exiting.")
        return

    # Round-robin tasks across workers (each worker keeps its VM warm).
    n = max(1, args.num_envs)
    buckets: list[list[str]] = [[] for _ in range(n)]
    for i, tid in enumerate(task_ids):
        buckets[i % n].append(tid)

    mgr = Manager()
    shared: list = mgr.list()
    procs = [Process(target=worker, args=(i, buckets[i], args, shared),
                     name=f"osw2-env-{i+1}") for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    out = Path(args.result_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = list(shared)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    (out / f"summary_{ts}.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if summary:
        nums = [s["score"] for s in summary
                if isinstance(s.get("score"), (int, float))]
        mean = (sum(nums) / len(nums)) if nums else 0.0
        n_pass = sum(1 for x in nums if x >= 0.5)
        logger.info("=== %d runs, mean=%.4f, pass(>=0.5)=%d/%d (%.1f%%) ===",
                    len(summary), mean, n_pass, len(nums),
                    100.0 * n_pass / max(1, len(nums)))


if __name__ == "__main__":
    main()
