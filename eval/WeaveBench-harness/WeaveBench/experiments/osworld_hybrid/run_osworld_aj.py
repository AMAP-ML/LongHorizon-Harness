"""Run upstream OSWorld tasks via WCB's openclaw harness + agent-as-judge.

Pipeline per (domain, task_uuid):
  1. Spin up DesktopEnv (OSWorld qcow2 → KVM Ubuntu) — already supports
     OSWorld JSON `config[]` setup via env.reset(task_config=ex).
  2. Bootstrap openclaw + plugins inside VM (agent.bootstrap + .configure).
  3. (NEW for OSWorld) env.reset(task_config=ex) — runs `config[]` (e.g.
     launch chrome, open libreoffice) BEFORE the agent starts.
  4. agent.run(env, instruction, output_dir, system_prompt_override=...).
  5. Capture final screenshot to output_dir/final_screenshot.png.
  6. Archive any /tmp_workspace deliverables (usually empty for OSWorld).
  7. stage_osworld_case → judge_workspace/_eval/<case_id>/.
  8. judge_one (cop-api 4141 / gpt-5.5) → score.json with 8-dim + hack veto.

Output layout (parallel to WCB rollout_agentjudge_gui):
  Eyeson_bench/rollout_agentjudge_gui_OSWORLD/<MODEL>/<TAG>/<DOMAIN>/<UUID>/
    ├── chat.jsonl
    ├── agent.log
    ├── final_screenshot.png
    ├── results.tar.gz (if any)
    └── score.json  (judge_method=agent_as_judge_osworld)

CLI is intentionally similar to run_bench_gen.py for op familiarity.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime
from multiprocessing import Manager, Process, Queue, current_process
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop_env.desktop_env import DesktopEnv  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_osworld_aj")


# ---------------------------------------------------------------------------
# Task discovery
# ---------------------------------------------------------------------------
ALL_OSWORLD_DOMAINS = [
    "chrome", "gimp", "libreoffice_calc", "libreoffice_impress",
    "libreoffice_writer", "multi_apps", "os", "thunderbird", "vlc", "vs_code",
]


def discover_osworld_tasks(osworld_root: Path, domains: List[str],
                            task_filter: str = "",
                            test_all_meta_path: str | None = None) -> List[dict]:
    """Walk evaluation_examples/examples/<domain>/*.json and build task records.

    If ``test_all_meta_path`` is provided (the OSWorld-official subset file
    that lists which task UUIDs belong to the canonical paper benchmark),
    only tasks present in that file are returned — matching what
    ``run_multienv_*.py`` from upstream OSWorld evaluates.

    Each record is a dict with at least:
      task_uuid, domain, task_json_path, instruction, evaluator_func, snapshot
    """
    examples_root = osworld_root / "evaluation_examples" / "examples"
    if not examples_root.is_dir():
        raise FileNotFoundError(f"OSWorld examples dir not found: {examples_root}")

    # Optional whitelist from OSWorld's canonical test_all.json
    whitelist: dict[str, set[str]] | None = None
    if test_all_meta_path:
        meta_path = Path(test_all_meta_path)
        if not meta_path.is_absolute():
            meta_path = osworld_root / test_all_meta_path
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                whitelist = {dom: set(uuids) for dom, uuids in meta.items()}
                logger.info("Using OSWorld task whitelist from %s (%d domains, %d tasks)",
                            meta_path,
                            len(whitelist),
                            sum(len(u) for u in whitelist.values()))
            except Exception as exc:
                logger.warning("Failed to load test_all_meta_path %s: %s — falling back to all *.json",
                               meta_path, exc)
        else:
            logger.warning("test_all_meta_path missing: %s — falling back to all *.json",
                           meta_path)

    out = []
    for domain in domains:
        d = examples_root / domain
        if not d.is_dir():
            logger.warning("OSWorld domain dir missing, skipping: %s", d)
            continue
        for jp in sorted(d.glob("*.json")):
            try:
                d_json = json.loads(jp.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Skip unparseable task %s: %s", jp, exc)
                continue
            uuid = d_json.get("id") or jp.stem
            if whitelist is not None:
                dom_set = whitelist.get(domain, set())
                if uuid not in dom_set:
                    continue
            if task_filter and task_filter not in uuid:
                continue
            ev = d_json.get("evaluator", {}) or {}
            out.append({
                "task_uuid": uuid,
                "domain": domain,
                "task_json_path": str(jp.resolve()),
                "instruction": d_json.get("instruction", ""),
                "evaluator_func": ev.get("func", "?"),
                "snapshot": d_json.get("snapshot", domain),
                "infeasible": ev.get("func") == "infeasible",
                "max_steps_hint": 50,
            })
    return out


# ---------------------------------------------------------------------------
# Per-task helpers
# ---------------------------------------------------------------------------
def render_env_disclosure(task_config: dict) -> str:
    """Parse OSWorld task['config'][] into a CLI-agent-readable summary of
    environment state, equivalent to what a GUI agent would see in its
    first-frame screenshot. Does NOT reveal evaluator internals or solutions.

    Discloses (= info visible from screenshot or address bar):
      - launched apps (incl. Chrome's --remote-debugging-port flag, which is
        an environment fact verifiable via `ps aux | grep chrome`)
      - Chrome tabs (URL visible in address bar)
      - pre-downloaded / pre-uploaded files (icons visible on Desktop / in
        file manager)
      - active/foreground window (title bar visible)

    Does NOT disclose:
      - evaluator.func / evaluator.result / evaluator.expected (grader spoiler)
      - evaluator.postconfig keystroke contents (would leak hardcoded filenames)
      - solution hints ("use CDP", "write to /etc/foo")

    Returns empty string if no config[] or nothing notable to disclose.
    """
    lines = []
    cfg = task_config.get("config", []) or []

    for step in cfg:
        t = step.get("type")
        p = step.get("parameters", {}) or {}
        if t == "launch":
            cmd = p.get("command", []) or []
            if cmd:
                lines.append(f"- App launched: `{' '.join(str(x) for x in cmd)}`")
        elif t == "chrome_open_tabs":
            for u in p.get("urls_to_open", []) or []:
                lines.append(f"- Chrome tab open: {u}")
        elif t == "download":
            for f in p.get("files", []) or []:
                if f.get("path"):
                    lines.append(f"- File present: {f['path']}")
        elif t == "upload_file":
            dest = p.get("dest") or p.get("path")
            if dest:
                lines.append(f"- File present: {dest}")
        elif t == "activate_window":
            wn = p.get("window_name")
            if wn:
                lines.append(f"- Foreground window: {wn}")
        elif t == "open":
            # OSWorld's `open` step opens a file in its default app
            path = p.get("path")
            if path:
                lines.append(f"- File present (opened in default app): {path}")

    if not lines:
        return ""

    return (
        "\n=== RUNTIME ENVIRONMENT (already set up by the test harness) ===\n"
        "Equivalent to what a human or GUI agent sees at first glance "
        "(visible windows, tabs, desktop files). Verifiable from CLI via "
        "`ps aux`, `ls ~/Desktop/`, `ss -tlnp`, `wmctrl -lx` — disclosed "
        "here to spare discovery steps. Evaluator internals and solution "
        "approach are NOT disclosed.\n\n"
        + "\n".join(lines) + "\n"
    )


def osworld_system_prompt(instruction: str, evaluator_func: str,
                          infeasible: bool, gui: bool = True,
                          task_config: dict | None = None,
                          env_disclosure: bool = False) -> str:
    """Fair-eval OSWorld prompt.

    Design principles (do NOT add app/tool/config hints — those are spoilers):
      • Identity: Ubuntu 22.04 user with sudo (environment fact, parity with
        community baselines which also state the OS).
      • Tool: bash only (CLI variant) or bash + computer-use (GUI baseline).
        Modality is the variable under study; nothing else.
      • Grading: final state inspected; talk doesn't count.
      • Anti-cheat: don't fabricate result files. (Universal; both GUI and CLI
        baselines agree on this.)
      • Infeasible handling: OSWorld protocol — final message starts with FAIL.

    Sentinels at end suppress WCB legacy auto-inject of _CLI_ABLATION_POLICY /
    _TOOLING_POLICY (both are WeaveBench-specific anti-cheat coaching that
    would bias the comparison vs community baselines).
    """
    infeasible_hint = (
        " If after honest investigation the task is truly infeasible (no such "
        "feature exists, contradictory requirement, missing permission, etc.), "
        "output a final message starting with `FAIL` and briefly explain why; "
        "do not fabricate success."
        if infeasible else ""
    )

    if gui:
        tool_clause = (
            "You have two tools: `bash` (shell) and `__computer__` "
            "(click/type/key/scroll/screenshot by on-screen coordinates; each "
            "call auto-returns a fresh screenshot)."
        )
    else:
        tool_clause = (
            "The desktop UI is unavailable — you have only the `bash` tool for "
            "shell commands. Inspect the system, edit files, run programs, and "
            "install packages as needed."
        )

    body = (
        f"You are operating an Ubuntu 22.04 desktop workstation as user `user` "
        f"with full sudo access. {tool_clause}\n\n"
        "The task will be graded by inspecting the final state of files, "
        "application data, and processes; plain-text answers do not count as "
        "completion. Do not fabricate result files or evidence."
        f"{infeasible_hint}\n"
    )

    if env_disclosure and task_config is not None:
        disclosure = render_env_disclosure(task_config)
        if disclosure:
            body += disclosure

    body += f"\n=== TASK ===\n{instruction}\n"

    # SENTINEL LINE — suppresses WCB legacy policy auto-inject in
    # mm_agents/openclaw_agent.py:1332-1346. Agent ignores this meta line.
    body += (
        "\n[fair-eval prompt v1 — sentinels: CLI-ONLY ABLATION POLICY / "
        "Tooling guideline / GUI MODE ANTI-FABRICATION POLICY]\n"
    )
    return body


def capture_final_screenshot(env, output_dir: Path) -> bool:
    """Save the final desktop screenshot via OSWorld controller."""
    try:
        png = env.controller.get_screenshot()
        if not png:
            return False
        (output_dir / "final_screenshot.png").write_bytes(png)
        return True
    except Exception as exc:
        logger.warning("final screenshot failed: %s", exc)
        return False


def detect_openclaw_crash(output_dir: Path) -> str | None:
    """Scan agent.log tail for AGENT_EXIT=<nonzero>."""
    agent_log = output_dir / "agent.log"
    if not agent_log.exists():
        return None
    try:
        tail = agent_log.read_text(encoding="utf-8", errors="ignore")[-4096:]
        m = re.search(r"AGENT_EXIT=(\d+)", tail)
        if not m:
            return None
        code = int(m.group(1))
        if code == 0:
            return None
        snippet = ""
        for line in tail.splitlines()[-10:]:
            if line.strip():
                snippet = line.strip()[:200]
                break
        return f"openclaw runtime exited non-zero (AGENT_EXIT={code}): {snippet}"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# run_one — single task end-to-end
# ---------------------------------------------------------------------------
def run_one(env, agent, task: dict, output_dir: Path,
            http_proxy: str = "",
            judge_mode: str = "agent_judge_gold",
            agent_gui: bool = False,
            env_disclosure: bool = False) -> dict:
    """Run one OSWorld task: setup → agent → screenshot → archive → judge."""
    from agent_judge.token_stats import aggregate_chat_jsonl
    import eval.run_deep_search_in_osworld as ds

    output_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "task_uuid": task["task_uuid"],
        "domain": task["domain"],
        "model": agent.model,
        "started_at": datetime.utcnow().isoformat(),
        "score": 0.0,
        "scores": {},
        "error": None,
    }

    try:
        # 0. Bootstrap openclaw inside VM (idempotent).
        agent.bootstrap(env)
        agent.configure(env)

        # 1. OSWorld native env.reset — runs `config[]` (launch chrome, etc).
        task_json: dict = {}
        try:
            task_json = json.loads(Path(task["task_json_path"]).read_text(
                encoding="utf-8"))
            env.reset(task_config=task_json)
            logger.info("[%s/%s] env.reset OK (config steps: %d)",
                        task["domain"], task["task_uuid"][:8],
                        len(task_json.get("config", [])))
        except Exception as exc:
            record["error"] = f"env.reset_failed: {type(exc).__name__}: {str(exc)[:200]}"
            logger.error("[%s/%s] env.reset crashed: %s",
                         task["domain"], task["task_uuid"][:8], exc)
            # Continue anyway — agent may still partially work.

        # 2a. Create /tmp_workspace in VM (OSWorld qcow2 doesn't pre-create it,
        #     unlike the WCB qcow2 — the agent's first tool call EACCES'es without
        #     this). Mirror WCB's upload_workspace mkdir step.
        try:
            ds._sudo_mkdir(env,
                           ["/tmp_workspace", "/tmp_workspace/results",
                            "/tmp_workspace/tmp"],
                           password=agent.client_password)
            logger.info("[%s/%s] /tmp_workspace ready",
                        task["domain"], task["task_uuid"][:8])
        except Exception as exc:
            logger.warning("[%s/%s] mkdir /tmp_workspace failed (non-fatal): %s",
                           task["domain"], task["task_uuid"][:8], exc)

        # 2b. Inject HTTP proxy if set (for chrome tasks fetching external URLs)
        if http_proxy:
            try:
                ds.write_task_env_script(env, "",
                                          http_proxy=http_proxy)
            except Exception as exc:
                logger.warning("write_task_env_script failed (non-fatal): %s", exc)

        # 3. Init screenshot for agent's context
        ds.take_init_screenshot(env, output_dir)

        # 4. Build system prompt and run agent
        sys_prompt = osworld_system_prompt(
            instruction=task["instruction"],
            evaluator_func=task["evaluator_func"],
            infeasible=task["infeasible"],
            gui=agent_gui,
            task_config=task_json or None,
            env_disclosure=env_disclosure,
        )
        meta = agent.run(env, task["instruction"], output_dir,
                          system_prompt_override=sys_prompt,
                          already_configured=True)
        record.update({"agent_done": meta.get("agent_done"),
                       "elapsed_seconds": meta.get("elapsed_seconds")})

        # 4a. Token usage
        try:
            tu = aggregate_chat_jsonl(output_dir / "chat.jsonl")
            record["agent_token_usage"] = tu
        except Exception:
            pass

        # 4b. Detect openclaw crash
        crash = detect_openclaw_crash(output_dir)
        if crash:
            record["error"] = crash
            logger.warning("[%s/%s] %s",
                           task["domain"], task["task_uuid"][:8], crash[:140])

        # 5. Capture final screenshot (judge needs this)
        capture_final_screenshot(env, output_dir)

        # 6. Archive deliverables (mostly empty for OSWorld but try)
        try:
            ds._archive_deliverables(env, output_dir,
                                      client_password=agent.client_password)
        except Exception as exc:
            logger.warning("[%s/%s] archive_deliverables failed (non-fatal): %s",
                           task["domain"], task["task_uuid"][:8], exc)

        # 7. SCORING — record BOTH the OSWorld native grader and the gold-aware
        #    agent-as-judge for every task:
        #      score_native = OSWorld's own env.evaluate() (binary 0/1,
        #                     leaderboard-parity, same grader as the vision
        #                     baseline — apples-to-apples)
        #      score_gold   = the gold-aware in-VM agent judge: sees the
        #                     evaluator spec, reads real VM state live with full
        #                     output, credits ANY implementation path the rigid
        #                     native rules miss.
        #    Headline `score` = score_gold.
        record["judge_mode"] = judge_mode

        def _run_native() -> tuple[float, str | None]:
            try:
                # OSWorld DesktopEnv.evaluate() may return float (multi-getter
                # avg), bool, or 0/1 int. Treat anything >=0.5 as pass.
                raw = env.evaluate()
                s = float(raw) if raw is not None else 0.0
                return s, None
            except Exception as e:
                return 0.0, f"{type(e).__name__}: {str(e)[:300]}"

        native_score, native_err = _run_native()
        record["score_native"] = native_score
        if native_err:
            record["native_error"] = native_err
        logger.info("[%s/%s] native_eval → %.3f%s",
                    task["domain"], task["task_uuid"][:8], native_score,
                    f" err={native_err[:80]}" if native_err else "")

        try:
            from agent_judge.agent_judge_gold import judge_in_vm_gold
            litellm_base = getattr(agent, "litellm_base_url",
                                   "http://172.17.0.1:4200/v1")
            litellm_key = getattr(agent, "litellm_api_key",
                                  "sk-litellm-azure-direct")
            judge_base_url = os.environ.get(
                "VM_JUDGE_BASE_URL",
                litellm_base.replace("172.17.0.1", "127.0.0.1"))
            judge_model = os.environ.get("VM_JUDGE_MODEL", agent.model)
            judge_max_steps = int(os.environ.get("VM_JUDGE_MAX_STEPS", "15"))
            judge_timeout = int(os.environ.get("VM_JUDGE_TIMEOUT", "900"))
            gjrec = judge_in_vm_gold(
                env, task, output_dir,
                task_json_path=task["task_json_path"],
                model=judge_model,
                base_url=judge_base_url,
                api_key=os.environ.get("VM_JUDGE_API_KEY", litellm_key),
                max_steps=judge_max_steps,
                timeout_s=judge_timeout,
            )
            gold_score = float(gjrec.get("score", 0))
            record["score_gold"] = gold_score
            record["scores_gold"] = {
                "judge_method": "agent_judge_gold",
                **{k: v for k, v in gjrec.items() if k != "score"},
            }
            record["score"] = gold_score
            record["judge_method"] = "agent_judge_gold"
            logger.info("[%s/%s] agent_judge_gold → %s "
                        "(native was %.2f, evidence: %d items, reason: %s)",
                        task["domain"], task["task_uuid"][:8],
                        int(gold_score), native_score,
                        len(gjrec.get("evidence", []) or []),
                        (gjrec.get("reason") or "")[:140])
        except Exception as e:
            record["error"] = (record.get("error") or "") + \
                f" | agent_judge_gold_failed: {type(e).__name__}: {str(e)[:200]}"
            record["score"] = 0.0
            record["judge_method"] = "agent_judge_gold_crashed"
            logger.warning("[%s/%s] agent_judge_gold crashed: %s",
                           task["domain"], task["task_uuid"][:8], e)

    except Exception as exc:
        logger.error("[%s/%s] run_one crashed: %s\n%s",
                     task["domain"], task["task_uuid"][:8], exc,
                     traceback.format_exc())
        record["error"] = (record.get("error") or "") + \
            f" | run_one_exc: {str(exc)[-300:]}"

    record["finished_at"] = datetime.utcnow().isoformat()
    (output_dir / "score.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# Worker / multi-process pool
# ---------------------------------------------------------------------------
def already_done(task: dict, model: str, result_root: Path) -> bool:
    sp = result_root / task["domain"] / task["task_uuid"] / "score.json"
    if not sp.is_file():
        return False
    try:
        rec = json.loads(sp.read_text(encoding="utf-8"))
        # Re-do if last run had an error (transient).
        if rec.get("error"):
            return False
        # Re-do if judge errored.
        if (rec.get("scores", {}) or {}).get("judge_error"):
            return False
        return True
    except Exception:
        return False


def _import_openclaw_agent():
    """Import OpenClawAgent lazily inside each worker so multiprocessing
    spawn doesn't try to pickle a huge module graph."""
    from mm_agents.openclaw_agent import OpenClawAgent
    return OpenClawAgent


def worker(env_idx: int, task_queue: Queue, args: argparse.Namespace,
           shared: list) -> None:
    name = current_process().name
    logger.info("[%s] starting (idx=%d)", name, env_idx)
    result_root = Path(args.result_dir)

    OpenClawAgent = _import_openclaw_agent()

    # Stagger to avoid docker port-allocation lock storm.
    time.sleep(env_idx * args.startup_stagger_s)

    while True:
        try:
            task = task_queue.get(timeout=5)
        except Exception:
            break

        if already_done(task, args.model, result_root):
            logger.info("[%s] [%s/%s] already done — skip",
                        name, task["domain"], task["task_uuid"][:8])
            continue

        out_dir = result_root / task["domain"] / task["task_uuid"]
        out_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[%s] [%s/%s] start (instruction: %s)",
                    name, task["domain"], task["task_uuid"][:8],
                    (task["instruction"] or "")[:80])

        env = None
        try:
            if args.provider_name == "docker_lite":
                # Pure-CLI ablation: plain container, no desktop. Agent can ONLY
                # use bash; DISPLAY=:0 physically fails. Native eval is off in
                # this mode (judge_mode=agent_judge_gold scores via LLM judge).
                from desktop_env.providers.docker_lite.lite_env import DockerLiteEnv
                lite_image = args.cli_image or os.environ.get(
                    "WCB_LITE_IMAGE", "wildclawbench-osworld-cli:v1")
                env = DockerLiteEnv(
                    image=lite_image,
                    client_password=args.client_password,
                    task_id=task["task_uuid"],
                )
            else:
                env = DesktopEnv(
                    provider_name=args.provider_name,
                    region=args.region,
                    path_to_vm=args.path_to_vm,
                    action_space="pyautogui",
                    screen_size=(args.screen_width, args.screen_height),
                    headless=args.headless,
                    os_type=args.os_type,
                    require_a11y_tree=False,
                    require_terminal=True,
                    client_password=args.client_password,
                    snapshot_name=task.get("snapshot", "init_state"),
                )
            agent = OpenClawAgent(
                model=args.model,
                litellm_base_url=args.litellm_base_url,
                litellm_api_key=args.litellm_api_key,
                client_password=args.client_password,
                timeout=int(os.environ.get("OSW_AGENT_TIMEOUT", "3600")),
                gui=args.agent_gui,
                max_steps=task.get("max_steps_hint") or args.max_steps,
            )
            rec = run_one(env, agent, task, out_dir,
                           http_proxy=args.http_proxy,
                           judge_mode=args.judge_mode,
                           agent_gui=args.agent_gui,
                           env_disclosure=args.env_disclosure)
            shared.append({
                "domain": task["domain"],
                "task_uuid": task["task_uuid"],
                "score": rec.get("score", 0.0),
                "error": rec.get("error"),
            })
            logger.info("[%s] [%s/%s] → score=%.3f%s",
                        name, task["domain"], task["task_uuid"][:8],
                        rec.get("score", 0.0),
                        f"  ERR={rec['error'][:80]}" if rec.get("error") else "")
        except Exception as exc:
            logger.error("[%s] %s/%s crashed: %s\n%s",
                         name, task["domain"], task["task_uuid"][:8],
                         exc, traceback.format_exc())
            shared.append({
                "domain": task["domain"],
                "task_uuid": task["task_uuid"],
                "score": 0.0,
                "error": f"worker_exc: {type(exc).__name__}: {str(exc)[:200]}",
            })
        finally:
            if env is not None:
                try:
                    env.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Run upstream OSWorld tasks via WCB openclaw + LLM-as-judge.")
    # OSWorld VM
    ap.add_argument("--provider_name", default="docker",
                    help="docker = KVM+qcow2 DesktopEnv (default); docker_lite = "
                         "plain-container DockerLiteEnv (pure-CLI, no desktop).")
    ap.add_argument("--cli_image", default=None,
                    help="For --provider_name docker_lite: docker image tag to run "
                         "(e.g. wildclawbench-osworld-cli:v1). Falls back to WCB_LITE_IMAGE.")
    ap.add_argument("--region", default=None)
    ap.add_argument("--path_to_vm", default=None,
                    help="Absolute path to OSWorld qcow2 (required for "
                         "--provider_name docker; ignored for docker_lite).")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--screen_width", type=int, default=1920)
    ap.add_argument("--screen_height", type=int, default=1080)
    ap.add_argument("--os_type", default="Ubuntu")
    ap.add_argument("--client_password", default="password")
    ap.add_argument("--num_envs", type=int, default=2)
    ap.add_argument("--startup_stagger_s", type=int, default=8)

    # Agent / model
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--litellm_base_url", required=True)
    ap.add_argument("--litellm_api_key", required=True)
    ap.add_argument("--max_steps", type=int, default=50)

    # OSWorld task selection
    ap.add_argument("--osworld_root",
                    default=str(Path(__file__).resolve().parents[2]),
                    help="Path to OSWorld root (contains evaluation_examples/).")
    ap.add_argument("--domains",
                    default=",".join(ALL_OSWORLD_DOMAINS),
                    help="Comma-separated OSWorld domains.")
    ap.add_argument("--task_filter", default="",
                    help="Substring filter on task uuid.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Limit total tasks (0 = no limit).")
    ap.add_argument("--result_dir", required=True)
    ap.add_argument("--http_proxy",
                    default=os.environ.get("WCB_VM_HTTP_PROXY", ""),
                    help="HTTP(S) proxy URL injected into the VM.")
    ap.add_argument("--test_all_meta_path", default="",
                    help="Path to OSWorld's canonical test_all.json subset "
                         "(restricts tasks to the paper-comparable 370 set). "
                         "Empty = use ALL *.json in evaluation_examples/examples/.")

    # CLI ablation knobs
    def _bool(s):
        if isinstance(s, bool):
            return s
        if str(s).lower() in ("true", "1", "yes", "y"):
            return True
        if str(s).lower() in ("false", "0", "no", "n"):
            return False
        raise argparse.ArgumentTypeError(f"bool arg expected, got {s!r}")
    ap.add_argument("--agent_gui", type=_bool, default=False,
                    help="True = preserve __computer__ GUI tool (historical "
                         "baseline). False = lock down GUI, CLI-only agent — "
                         "default for OSWorld CLI ablation.")
    ap.add_argument("--judge_mode",
                    choices=("agent_judge_gold",),
                    default="agent_judge_gold",
                    help="agent_judge_gold (default, the only mode) = gold-aware "
                         "in-VM agent-as-judge. Records BOTH score_native "
                         "(OSWorld env.evaluate(), leaderboard parity) and "
                         "score_gold (intent-oriented, evaluator-spec-aware, "
                         "credits any implementation path). Headline score = "
                         "score_gold.")
    ap.add_argument("--env_disclosure", type=_bool, default=False,
                    help="When True, parse task['config'][] and prepend a "
                         "RUNTIME ENVIRONMENT section to the agent's system "
                         "prompt — apps launched, Chrome tabs/CDP port, "
                         "pre-existing files on Desktop, foreground window. "
                         "This is the CLI equivalent of a GUI agent's "
                         "first-frame screenshot (info-symmetry). Does NOT "
                         "leak evaluator internals or solution hints. "
                         "Default False = strict baseline (no disclosure).")

    return ap.parse_args()


def main() -> None:
    args = parse_args()

    logger.info("Run mode: agent_gui=%s  judge_mode=%s  env_disclosure=%s",
                args.agent_gui, args.judge_mode, args.env_disclosure)

    osworld_root = Path(args.osworld_root).resolve()
    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    tasks = discover_osworld_tasks(
        osworld_root, domains,
        task_filter=args.task_filter,
        test_all_meta_path=args.test_all_meta_path or None,
    )
    if args.limit:
        tasks = tasks[:args.limit]
    logger.info("Discovered %d OSWorld tasks across %d domains (num_envs=%d)",
                len(tasks), len(domains), args.num_envs)
    if not tasks:
        logger.error("No tasks to run — exiting.")
        return

    mgr = Manager()
    queue: Queue = mgr.Queue()
    shared: list = mgr.list()
    for t in tasks:
        queue.put(t)

    procs = [Process(target=worker, args=(i, queue, args, shared),
                     name=f"osw-env-{i+1}") for i in range(args.num_envs)]
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
        logger.info("=== %d completed runs, mean score = %.3f ===",
                    len(summary), (sum(nums) / len(nums)) if nums else 0.0)
        # Per-domain breakdown
        per_dom = {}
        for s in summary:
            per_dom.setdefault(s["domain"], []).append(s.get("score", 0.0))
        for dom, scores in sorted(per_dom.items()):
            logger.info("  %-22s n=%3d mean=%.3f",
                        dom, len(scores), sum(scores) / len(scores))


if __name__ == "__main__":
    main()
