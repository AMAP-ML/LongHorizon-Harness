"""Invoke OpenClaw judge agent on a staged case + parse the resulting score.json.

Per-case ISOLATION:
  Each call materializes a FRESH OpenClaw profile + workspace by copying from
  template dirs, then sets OPENCLAW_STATE_DIR + OPENCLAW_CONFIG_PATH to point
  at the isolated copy. This guarantees:
    - No conversation history from prior tasks bleeds in
    - No memory/* files persist across tasks
    - workspace/_eval/ contains ONLY the current case (no neighboring cases)
    - Subprocess state is fully fresh

After judging, isolated dirs are deleted (unless AJ_KEEP_ISOLATED=1).

Usage::

    from weavebench.eval.agent_judge.judge_runner import judge_one
    result = judge_one(
        stage_dir="./judge_agent_test/judge_workspace/_eval/DAV17",
        case_id="DAV17",
        timeout=1800,
    )

CLI::

    python eval/agent_judge/judge_runner.py \\
      --stage_dir ./judge_agent_test/judge_workspace/_eval/DAV17 \\
      --case_id DAV17

Environment variables:
  AJ_OPENCLAW_BIN          path to openclaw CLI
  AJ_TEMPLATE_PROFILE      path to clean profile dir to copy from
                           (default: <repo>/judge_agent_test/template_profile)
  AJ_TEMPLATE_WORKSPACE    path to clean workspace dir to copy from
                           (default: <repo>/judge_agent_test/template_workspace)
  AJ_ISOLATED_ROOT         where to place per-case isolated dirs
                           (default: /tmp/aj_isolated)
  AJ_KEEP_ISOLATED         "1" → keep isolated dirs after run (debug)
  AJ_PROMPT_TEMPLATE       path to prompt template
  AJ_THINKING              openclaw thinking level (default: medium)
  AJ_TIMEOUT               per-case timeout sec (default: 1800)
  AJ_OPENCLAW_NODE         node 22+ binary path
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional


_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parents[2]
_DEFAULT_PROMPT = _THIS_DIR / "prompt_template.txt"
_DEFAULT_NODE = Path.home() / ".nvm" / "versions" / "node" / "v22.22.2" / "bin" / "node"
_DEFAULT_ISO_ROOT = Path("/tmp/aj_isolated")


def _default_judge_root() -> Path:
    return _REPO_ROOT / "judge_agent_test"


_DEFAULT_JUDGE_ROOT = _default_judge_root()
_DEFAULT_BIN = _DEFAULT_JUDGE_ROOT / "node_modules" / ".bin" / "openclaw"
_DEFAULT_TPL_PROFILE = _DEFAULT_JUDGE_ROOT / "template_profile"
_DEFAULT_TPL_WORKSPACE = _DEFAULT_JUDGE_ROOT / "template_workspace"


def _build_prompt(case_id: str, prompt_template: Path) -> str:
    txt = prompt_template.read_text()
    return txt.replace("{CASE_ID}", case_id)


def _resolve_node_path() -> Optional[str]:
    """Find a node 22+ binary."""
    env_node = os.environ.get("AJ_OPENCLAW_NODE")
    if env_node and Path(env_node).is_file():
        return env_node
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        v22 = sorted([p for p in nvm_dir.iterdir() if p.name.startswith("v22.")],
                      reverse=True)
        if v22:
            cand = v22[0] / "bin" / "node"
            if cand.is_file():
                return str(cand)
    sysnode = shutil.which("node")
    if sysnode:
        try:
            out = subprocess.run([sysnode, "--version"], capture_output=True,
                                  text=True, timeout=5)
            major_raw = out.stdout.strip().lstrip("v").split(".")[0]
            if major_raw.isdigit() and int(major_raw) >= 22:
                return sysnode
        except (subprocess.SubprocessError, ValueError):
            # Node either timed out or printed something unparseable —
            # treat as "no usable system node" and fall through.
            pass
    return None


def _build_env(extra: Optional[dict] = None) -> dict:
    """Build subprocess env: strip http_proxy + add NO_PROXY for the local gateway."""
    env = os.environ.copy()
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)
    env["NO_PROXY"] = env.get("NO_PROXY", "127.0.0.1,localhost")
    node_path = _resolve_node_path()
    if node_path:
        env["PATH"] = str(Path(node_path).parent) + os.pathsep + env.get("PATH", "")
    if extra:
        env.update(extra)
    return env


def _provision_isolated(case_id: str, stage_dir: Path,
                         template_profile: Path, template_workspace: Path,
                         iso_root: Path) -> tuple[Path, Path, Path]:
    """Copy template profile + workspace into a fresh per-case dir.

    Returns (isolated_state_dir, isolated_config_path, isolated_workspace).
    The staged case files (under stage_dir) are linked into isolated_workspace
    at _eval/<case_id>/ so the agent's relative path "./_eval/<case_id>/..."
    still resolves.
    """
    iso_root.mkdir(parents=True, exist_ok=True)
    iso_state = iso_root / f"state_{case_id}"
    iso_ws = iso_root / f"ws_{case_id}"
    if iso_state.exists():
        shutil.rmtree(iso_state)
    if iso_ws.exists():
        shutil.rmtree(iso_ws)

    if not template_profile.is_dir():
        raise FileNotFoundError(f"template profile missing: {template_profile}")
    if not template_workspace.is_dir():
        raise FileNotFoundError(f"template workspace missing: {template_workspace}")

    shutil.copytree(template_profile, iso_state)
    shutil.copytree(template_workspace, iso_ws)

    # Patch openclaw.json to point at isolated workspace.
    cfg_path = iso_state / "openclaw.json"
    if not cfg_path.is_file():
        raise FileNotFoundError(f"template profile missing openclaw.json: {cfg_path}")
    cfg = json.loads(cfg_path.read_text())
    cfg.setdefault("agents", {}).setdefault("defaults", {})["workspace"] = str(iso_ws)
    cfg_path.write_text(json.dumps(cfg, indent=2))

    # Hard-copy the staged case into the isolated workspace.
    # (We previously used a symlink, but openclaw's image tool calls realpath()
    # and rejects paths whose resolved location is outside the workspace dir.)
    eval_link_parent = iso_ws / "_eval"
    eval_link_parent.mkdir(exist_ok=True)
    case_target = eval_link_parent / case_id
    if case_target.exists() or case_target.is_symlink():
        if case_target.is_symlink():
            case_target.unlink()
        else:
            shutil.rmtree(case_target)
    shutil.copytree(stage_dir, case_target)

    return iso_state, cfg_path, iso_ws


def _cleanup_isolated(iso_state: Path, iso_ws: Path) -> None:
    """Remove per-case isolated dirs unless AJ_KEEP_ISOLATED=1 is set."""
    if os.environ.get("AJ_KEEP_ISOLATED") == "1":
        return
    for p in (iso_state, iso_ws):
        try:
            if p.is_dir():
                shutil.rmtree(p)
        except Exception:
            pass


def _sweep_stale_isolated(iso_root: Path, max_age_seconds: int = 24 * 3600) -> int:
    """Remove orphaned state_*/ws_* dirs older than max_age_seconds.

    Worker crashes / SIGKILLs leave isolated dirs behind. Without a sweep,
    a full 114-task × 8-env sweep can drop ~900 dirs under /tmp/aj_isolated/
    between runs. Called once at judge startup; bounded so a fresh dir from
    a concurrent worker isn't wiped mid-flight.
    """
    if not iso_root.is_dir() or os.environ.get("AJ_KEEP_ISOLATED") == "1":
        return 0
    cutoff = time.time() - max_age_seconds
    swept = 0
    for child in iso_root.iterdir():
        if not (child.name.startswith("state_") or child.name.startswith("ws_")):
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                swept += 1
        except OSError:
            continue
    return swept


def judge_one(stage_dir: str | Path,
              case_id: str,
              openclaw_bin: Optional[str] = None,
              openclaw_profile: str = "judge",  # kept for backward-compat, ignored when isolated
              prompt_template: Optional[str | Path] = None,
              thinking: str = "medium",
              timeout: int = 1800,
              isolated: bool = True) -> dict:
    """Run OpenClaw judge agent on stage_dir and return parsed score.json.

    With isolated=True (default), each call gets a fresh OpenClaw profile +
    workspace materialized from templates. With isolated=False, falls back
    to the shared `--profile <openclaw_profile>` mode (legacy behavior).
    """
    stage = Path(stage_dir).resolve()
    if not stage.is_dir():
        return {"ok": False, "error": f"stage_dir not found: {stage}"}

    score_path = stage / "score.json"
    if score_path.exists():
        score_path.unlink()

    # Resolution order for the openclaw binary:
    #   1. explicit `openclaw_bin=` parameter
    #   2. $AJ_OPENCLAW_BIN env var
    #   3. `openclaw` on PATH (matches `npm install -g openclaw` install path)
    #   4. repo-local default under <repo>/judge_agent_test/node_modules/.bin/openclaw
    bin_path = (openclaw_bin
                or os.environ.get("AJ_OPENCLAW_BIN")
                or shutil.which("openclaw")
                or str(_DEFAULT_BIN))
    if not Path(bin_path).is_file():
        return {"ok": False,
                "error": (f"openclaw bin not found: {bin_path}. "
                          f"Run `npm install -g openclaw` and ensure it is on "
                          f"PATH, or set $AJ_OPENCLAW_BIN explicitly.")}

    prompt_path = Path(prompt_template) if prompt_template else \
                  Path(os.environ.get("AJ_PROMPT_TEMPLATE", str(_DEFAULT_PROMPT)))
    if not prompt_path.is_file():
        return {"ok": False, "error": f"prompt template not found: {prompt_path}"}

    thinking_level = os.environ.get("AJ_THINKING", thinking)
    timeout_s = int(os.environ.get("AJ_TIMEOUT", str(timeout)))
    message = _build_prompt(case_id, prompt_path)

    extra_env = {}
    iso_state = iso_ws = None
    if isolated:
        tpl_profile = Path(os.environ.get("AJ_TEMPLATE_PROFILE", str(_DEFAULT_TPL_PROFILE)))
        tpl_workspace = Path(os.environ.get("AJ_TEMPLATE_WORKSPACE", str(_DEFAULT_TPL_WORKSPACE)))
        iso_root = Path(os.environ.get("AJ_ISOLATED_ROOT", str(_DEFAULT_ISO_ROOT)))
        # Sweep stale state_*/ws_* dirs from crashed previous runs.
        # Process-level cache so a 114-task sweep only walks /tmp once.
        if not getattr(judge_one, "_swept", False):
            try:
                _sweep_stale_isolated(iso_root)
            except Exception:
                pass
            judge_one._swept = True  # type: ignore[attr-defined]
        try:
            iso_state, iso_cfg, iso_ws = _provision_isolated(
                case_id, stage, tpl_profile, tpl_workspace, iso_root)
        except Exception as exc:
            return {"ok": False, "error": f"provision_isolated failed: {exc}"}
        extra_env["OPENCLAW_STATE_DIR"] = str(iso_state)
        extra_env["OPENCLAW_CONFIG_PATH"] = str(iso_cfg)
        cmd = [
            bin_path,
            "agent", "--local",
            "--session-id", case_id,
            "--thinking", thinking_level,
            "--message", message,
        ]
    else:
        profile = os.environ.get("AJ_OPENCLAW_PROFILE", openclaw_profile)
        cmd = [
            bin_path,
            "--profile", profile,
            "agent", "--local",
            "--session-id", case_id,
            "--thinking", thinking_level,
            "--message", message,
        ]

    env = _build_env(extra_env)

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                               timeout=timeout_s)
        elapsed = time.time() - t0
    except subprocess.TimeoutExpired:
        if isolated and iso_state and iso_ws:
            _cleanup_isolated(iso_state, iso_ws)
        return {"ok": False, "error": f"openclaw timed out after {timeout_s}s",
                "elapsed_s": time.time() - t0}

    # When isolated, score.json gets written to iso_ws/_eval/<case_id>/score.json
    # (which is NOT a symlink anymore — we hard-copy). Pull it back to the
    # original stage_dir so callers can read score.json from where they staged.
    if isolated and iso_ws is not None:
        iso_score = iso_ws / "_eval" / case_id / "score.json"
        if iso_score.is_file():
            try:
                shutil.copy(iso_score, score_path)
            except Exception:
                pass

    stderr_tail = "\n".join(proc.stderr.splitlines()[-30:])

    # When isolated, score.json gets written to iso_ws/_eval/<case_id>/score.json
    # (already copied back above to score_path).
    if not score_path.is_file():
        result = {"ok": False,
                  "error": f"openclaw exited rc={proc.returncode} but no score.json written",
                  "elapsed_s": elapsed,
                  "openclaw_stderr_tail": stderr_tail,
                  "openclaw_stdout_tail": "\n".join(proc.stdout.splitlines()[-30:])}
        if isolated and iso_state and iso_ws:
            _cleanup_isolated(iso_state, iso_ws)
        return result

    try:
        score = json.loads(score_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # LLMs sometimes emit invalid JSON escapes like \' inside strings.
        # Apply a minimal repair (unescape these chars) and retry.
        try:
            import re as _re
            raw = score_path.read_text(encoding="utf-8")
            repaired = _re.sub(r"\\([\\'\\$\\`])", r"\1", raw)
            score = json.loads(repaired)
        except Exception as exc:
            result = {"ok": False, "error": f"score.json parse failed (even after repair): {exc}",
                      "elapsed_s": elapsed,
                      "openclaw_stderr_tail": stderr_tail}
            if isolated and iso_state and iso_ws:
                _cleanup_isolated(iso_state, iso_ws)
            return result
    except Exception as exc:
        result = {"ok": False, "error": f"score.json parse failed: {exc}",
                  "elapsed_s": elapsed,
                  "openclaw_stderr_tail": stderr_tail}
        if isolated and iso_state and iso_ws:
            _cleanup_isolated(iso_state, iso_ws)
        return result

    if isolated and iso_state and iso_ws:
        _cleanup_isolated(iso_state, iso_ws)

    return {
        "ok": True,
        "score_json": score,
        "final_score": score.get("final_score"),
        "is_hack": score.get("is_hack"),
        "hack_confidence": score.get("hack_confidence"),
        "hack_patterns": score.get("hack_patterns", []),
        "n_artifact_checks": len(score.get("artifact_checks", [])),
        "elapsed_s": round(elapsed, 1),
        "openclaw_stderr_tail": stderr_tail,
        "isolated": bool(isolated),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(allow_abbrev=False, )
    ap.add_argument("--stage_dir", required=True)
    ap.add_argument("--case_id", required=True)
    ap.add_argument("--openclaw_bin", default=None)
    ap.add_argument("--profile", default="judge",
                    help="(legacy --isolated=false only) openclaw shared profile name")
    ap.add_argument("--thinking", default="medium")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--no-isolated", action="store_true",
                    help="Use shared profile (legacy). Default = fresh isolated per-case.")
    args = ap.parse_args()
    result = judge_one(args.stage_dir, args.case_id,
                       openclaw_bin=args.openclaw_bin,
                       openclaw_profile=args.profile,
                       thinking=args.thinking,
                       timeout=args.timeout,
                       isolated=not args.no_isolated)
    print(json.dumps({k: v for k, v in result.items() if k != "score_json"},
                     indent=2, ensure_ascii=False))
    if result.get("ok"):
        print("---first 3 artifact_checks---")
        for a in (result["score_json"].get("artifact_checks") or [])[:3]:
            print(json.dumps(a, indent=2, ensure_ascii=False))
