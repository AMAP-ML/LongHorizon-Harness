#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <result_dir> <log_path>" >&2
  echo "       defaults follow the top-level README in WeaveBench-harness" >&2
  echo "       coordinate mode is automatic unless WEAVEBENCH_COMPUTER_COORD_MODE is set" >&2
  exit 2
fi

result_dir="$1"
log_path="$2"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project_root="$(cd "$repo/.." && pwd)"
tasks_root="${WEAVEBENCH_TASKS_ROOT:-$repo/cache/tasks}"
if [ ! -d "$tasks_root" ]; then
  echo "tasks_root does not exist: $tasks_root" >&2
  exit 2
fi

if [ -z "${WEAVEBENCH_LITELLM_KEY:-}" ]; then
  echo "WEAVEBENCH_LITELLM_KEY is not set" >&2
  exit 2
fi

if [ -z "${WEAVEBENCH_LITELLM_BASE_URL:-}" ]; then
  echo "WEAVEBENCH_LITELLM_BASE_URL is not set" >&2
  echo "Set it to the Anthropic-compatible endpoint used by Claude Code." >&2
  exit 2
fi

if [ -z "${AJ_OPENCLAW_BIN:-}" ] && [ -x /home/linuxbrew/.linuxbrew/bin/openclaw ]; then
  export AJ_OPENCLAW_BIN="/home/linuxbrew/.linuxbrew/bin/openclaw"
fi
if [ -z "${AJ_OPENCLAW_BIN:-}" ] && command -v openclaw >/dev/null 2>&1; then
  export AJ_OPENCLAW_BIN="$(command -v openclaw)"
fi
if [ -z "${AJ_OPENCLAW_BIN:-}" ]; then
  echo "AJ_OPENCLAW_BIN is not set and openclaw was not found on PATH" >&2
  exit 2
fi

mkdir -p "$(dirname "$log_path")"

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export OSWORLD_LOCAL_QCOW2_PATH="${OSWORLD_LOCAL_QCOW2_PATH:-$repo/cache/vm/Ubuntu_120G.qcow2}"
export WEAVEBENCH_GROW_ROOTFS="${WEAVEBENCH_GROW_ROOTFS:-1}"
export WEAVEBENCH_CLAUDE_CODE_EFFORT="${WEAVEBENCH_CLAUDE_CODE_EFFORT:-high}"
export WEAVEBENCH_ANTHROPIC_OUTPUT_EFFORT="${WEAVEBENCH_ANTHROPIC_OUTPUT_EFFORT:-high}"
export WEAVEBENCH_IMAGE_PROXY="${WEAVEBENCH_IMAGE_PROXY:-1}"
export WEAVEBENCH_IMAGE_PROXY_DEBUG_REQUESTS="${WEAVEBENCH_IMAGE_PROXY_DEBUG_REQUESTS:-0}"
image_proxy_flag="$(printf '%s' "$WEAVEBENCH_IMAGE_PROXY" | tr '[:upper:]' '[:lower:]')"
case "$image_proxy_flag" in
  1|true|yes|on)
    if [ -z "${WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL:-}" ] || [ -z "${WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL:-}" ]; then
      echo "WEAVEBENCH_IMAGE_PROXY=1 requires a public image hosting backend." >&2
      echo "Set WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL and WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL," >&2
      echo "or set WEAVEBENCH_IMAGE_PROXY=0 if your model endpoint accepts large base64 images directly." >&2
      exit 2
    fi
    ;;
  0|false|no|off)
    ;;
  *)
    echo "invalid WEAVEBENCH_IMAGE_PROXY=$WEAVEBENCH_IMAGE_PROXY; use 1/0, true/false, yes/no, or on/off" >&2
    exit 2
    ;;
esac
export WEAVEBENCH_DOCKER_PORT_LOCK_TIMEOUT="${WEAVEBENCH_DOCKER_PORT_LOCK_TIMEOUT:-180}"
export WEAVEBENCH_JUDGE_MODEL="${WEAVEBENCH_JUDGE_MODEL:-claude-opus-4-7}"
export JUDGE_MODEL="${JUDGE_MODEL:-$WEAVEBENCH_JUDGE_MODEL}"
export AJ_THINKING="${AJ_THINKING:-medium}"
export AJ_TIMEOUT="${AJ_TIMEOUT:-1800}"
export AJ_RETRY="${AJ_RETRY:-5}"
export AJ_RETRY_BACKOFF_S="${AJ_RETRY_BACKOFF_S:-30}"
export PYTHONPATH="$repo:${PYTHONPATH:-}"

export CUA_HARNESS_SOURCE_DIR="${CUA_HARNESS_SOURCE_DIR:-$project_root/cua-harness}"
export CUA_HARNESS_MAX_ROUNDS="${CUA_HARNESS_MAX_ROUNDS:-25}"
export CUA_HARNESS_TASK_TIMEOUT_SECONDS="${CUA_HARNESS_TASK_TIMEOUT_SECONDS:-1800}"
export CUA_HARNESS_GUI_TASK_TIMEOUT_SECONDS="${CUA_HARNESS_GUI_TASK_TIMEOUT_SECONDS:-$CUA_HARNESS_TASK_TIMEOUT_SECONDS}"
export CUA_HARNESS_CLI_TASK_TIMEOUT_SECONDS="${CUA_HARNESS_CLI_TASK_TIMEOUT_SECONDS:-$CUA_HARNESS_TASK_TIMEOUT_SECONDS}"
export CUA_HARNESS_ORCHESTRATOR_TIMEOUT_SECONDS="${CUA_HARNESS_ORCHESTRATOR_TIMEOUT_SECONDS:-300}"
export CUA_HARNESS_VERIFIER_TIMEOUT_SECONDS="${CUA_HARNESS_VERIFIER_TIMEOUT_SECONDS:-300}"
export CUA_HARNESS_ROLE_HISTORY_CHARS="${CUA_HARNESS_ROLE_HISTORY_CHARS:-0}"
export CUA_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS="${CUA_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS:-0}"
export CUA_HARNESS_ROLE_MEMORY_CHARS="${CUA_HARNESS_ROLE_MEMORY_CHARS:-0}"
export CUA_HARNESS_VERIFIER_TASK_OUTPUT_CHARS="${CUA_HARNESS_VERIFIER_TASK_OUTPUT_CHARS:-0}"

if [ ! -f "$OSWORLD_LOCAL_QCOW2_PATH" ]; then
  echo "OSWORLD_LOCAL_QCOW2_PATH does not exist: $OSWORLD_LOCAL_QCOW2_PATH" >&2
  exit 2
fi
if [ ! -d "$CUA_HARNESS_SOURCE_DIR" ]; then
  echo "CUA_HARNESS_SOURCE_DIR does not exist: $CUA_HARNESS_SOURCE_DIR" >&2
  exit 2
fi

model="${WEAVEBENCH_MODEL:-qwen3.7-plus}"
export WEAVEBENCH_MODEL="$model"
export CUA_HARNESS_MODEL="${CUA_HARNESS_MODEL:-$model}"
export CUA_HARNESS_VERIFIER_MODEL="${CUA_HARNESS_VERIFIER_MODEL:-$CUA_HARNESS_MODEL}"
harness="${WEAVEBENCH_HARNESS_NAME:-cua_harness_claudecode}"
num_envs="${WEAVEBENCH_NUM_ENVS:-5}"
domains="${WEAVEBENCH_DOMAINS:-DAV,DES,DOC,DSK,GAM,OPS,SPA,WEB}"
limit="${WEAVEBENCH_LIMIT:-0}"
task_filter="${WEAVEBENCH_TASK_FILTER:-}"
max_steps="${WEAVEBENCH_MAX_STEPS:-300}"

run_name="$(basename "${result_dir%/}")"
export AJ_JUDGE_WORKSPACE="${AJ_JUDGE_WORKSPACE:-$repo/judge_agent_test/$run_name}"
mkdir -p "$AJ_JUDGE_WORKSPACE"

cmd=(
  python3 -m weavebench.eval.run_weavebench
  --harness "$harness"
  --model "$model"
  --tasks_root "$tasks_root"
  --domains "$domains"
  --num_envs "$num_envs"
  --mode gui
  --max_steps "$max_steps"
  --result_dir "$result_dir"
  --litellm_base_url "$WEAVEBENCH_LITELLM_BASE_URL"
  --litellm_api_key "$WEAVEBENCH_LITELLM_KEY"
)

if [ -n "$task_filter" ]; then
  cmd+=(--task_filter "$task_filter")
fi

if [ "$limit" != "0" ]; then
  cmd+=(--limit "$limit")
fi

redact_key() {
  local key="${1:-}"
  if [ -z "$key" ]; then
    printf '<unset>'
  elif [ "${#key}" -le 10 ]; then
    printf '<set>'
  else
    printf '%s...%s' "${key:0:4}" "${key: -4}"
  fi
}

if [ "${WEAVEBENCH_LOG_FULL_API_KEY:-0}" = "1" ]; then
  api_key_display="$WEAVEBENCH_LITELLM_KEY"
else
  api_key_display="$(redact_key "$WEAVEBENCH_LITELLM_KEY")"
fi

cmd_display=("${cmd[@]}")
for i in "${!cmd_display[@]}"; do
  if [ "${cmd_display[$i]}" = "--litellm_api_key" ] && [ "$((i + 1))" -lt "${#cmd_display[@]}" ]; then
    cmd_display[$((i + 1))]="$api_key_display"
  fi
done
command_display="$(printf '%q ' "${cmd_display[@]}")"

mkdir -p "$result_dir"
provenance_path="$result_dir/run_provenance.json"
export WB_PROVENANCE_PATH="$provenance_path"
export WB_PROJECT_ROOT="$project_root"
export WB_REPO="$repo"
export WB_RUN_NAME="$run_name"
export WB_RESULT_DIR="$result_dir"
export WB_LOG_PATH="$log_path"
export WB_TASKS_ROOT="$tasks_root"
export WB_HARNESS="$harness"
export WB_MODEL="$model"
export WB_DOMAINS="$domains"
export WB_TASK_FILTER="$task_filter"
export WB_LIMIT="$limit"
export WB_NUM_ENVS="$num_envs"
export WB_MAX_STEPS="$max_steps"
export WB_COMMAND_DISPLAY="$command_display"
export WB_API_KEY_REDACTED="$api_key_display"

python3 - <<'PY'
import hashlib
import json
import os
import pathlib
import platform
import subprocess
from datetime import datetime, timezone


def run(cmd, timeout=10):
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return (p.stdout or "").strip().splitlines()[:5]
    except Exception as exc:
        return [f"<unavailable: {exc}>"]


def sha256_file(path):
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_info(path, hash_file=False):
    p = pathlib.Path(path)
    if not p.exists():
        return {"path": str(p), "exists": False}
    out = {"path": str(p), "exists": True, "size_bytes": p.stat().st_size}
    if hash_file:
        out["sha256"] = sha256_file(p)
    return out


repo = pathlib.Path(os.environ["WB_REPO"])
runtime_tarball = repo / "cache" / "runtime_assets" / "claudecode.tar.gz"
original_vm = repo / "cache" / "vm" / "Ubuntu.qcow2"
active_vm = pathlib.Path(os.environ.get("OSWORLD_LOCAL_QCOW2_PATH", ""))

data = {
    "schema_version": 1,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "project": {
        "root": os.environ.get("WB_PROJECT_ROOT"),
        "weavebench_dir": os.environ.get("WB_REPO"),
        "cua_harness_source_dir": os.environ.get("CUA_HARNESS_SOURCE_DIR"),
    },
    "run": {
        "run_name": os.environ.get("WB_RUN_NAME"),
        "result_dir": os.environ.get("WB_RESULT_DIR"),
        "log_path": os.environ.get("WB_LOG_PATH"),
        "command": os.environ.get("WB_COMMAND_DISPLAY"),
    },
    "evaluation": {
        "harness": os.environ.get("WB_HARNESS"),
        "execution_backend": "Claude Code",
        "model": os.environ.get("WB_MODEL"),
        "mode": "gui",
        "tasks_root": os.environ.get("WB_TASKS_ROOT"),
        "domains": os.environ.get("WB_DOMAINS"),
        "task_filter": os.environ.get("WB_TASK_FILTER") or None,
        "limit": os.environ.get("WB_LIMIT"),
        "num_envs": os.environ.get("WB_NUM_ENVS"),
        "max_steps": os.environ.get("WB_MAX_STEPS"),
        "coordinate_mode": os.environ.get("WEAVEBENCH_COMPUTER_COORD_MODE", "auto"),
    },
    "cua_harness": {
        "max_rounds": os.environ.get("CUA_HARNESS_MAX_ROUNDS"),
        "task_timeout_seconds": os.environ.get("CUA_HARNESS_TASK_TIMEOUT_SECONDS"),
        "gui_task_timeout_seconds": os.environ.get("CUA_HARNESS_GUI_TASK_TIMEOUT_SECONDS"),
        "cli_task_timeout_seconds": os.environ.get("CUA_HARNESS_CLI_TASK_TIMEOUT_SECONDS"),
        "orchestrator_timeout_seconds": os.environ.get("CUA_HARNESS_ORCHESTRATOR_TIMEOUT_SECONDS"),
        "verifier_timeout_seconds": os.environ.get("CUA_HARNESS_VERIFIER_TIMEOUT_SECONDS"),
        "role_history_chars": os.environ.get("CUA_HARNESS_ROLE_HISTORY_CHARS"),
        "role_verified_context_chars": os.environ.get("CUA_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS"),
        "role_memory_chars": os.environ.get("CUA_HARNESS_ROLE_MEMORY_CHARS"),
        "verifier_task_output_chars": os.environ.get("CUA_HARNESS_VERIFIER_TASK_OUTPUT_CHARS"),
    },
    "effort": {
        "claude_code_effort": os.environ.get("WEAVEBENCH_CLAUDE_CODE_EFFORT"),
        "anthropic_output_effort": os.environ.get("WEAVEBENCH_ANTHROPIC_OUTPUT_EFFORT"),
        "judge_thinking": os.environ.get("AJ_THINKING"),
    },
    "judge": {
        "model": os.environ.get("WEAVEBENCH_JUDGE_MODEL"),
        "workspace": os.environ.get("AJ_JUDGE_WORKSPACE"),
        "openclaw_bin": os.environ.get("AJ_OPENCLAW_BIN"),
        "timeout_seconds": os.environ.get("AJ_TIMEOUT"),
        "retry": os.environ.get("AJ_RETRY"),
        "retry_backoff_seconds": os.environ.get("AJ_RETRY_BACKOFF_S"),
    },
    "api": {
        "base_url": os.environ.get("WEAVEBENCH_LITELLM_BASE_URL"),
        "api_key_present": bool(os.environ.get("WEAVEBENCH_LITELLM_KEY")),
        "api_key_redacted": os.environ.get("WB_API_KEY_REDACTED"),
    },
    "image_proxy": {
        "enabled": os.environ.get("WEAVEBENCH_IMAGE_PROXY"),
        "upload_mode": os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE"),
        "upload_url_template_set": bool(os.environ.get("WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL")),
        "show_url_template_set": bool(os.environ.get("WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL")),
        "debug_requests": os.environ.get("WEAVEBENCH_IMAGE_PROXY_DEBUG_REQUESTS"),
    },
    "vm": {
        "active": file_info(active_vm),
        "original": file_info(original_vm),
        "grow_rootfs": os.environ.get("WEAVEBENCH_GROW_ROOTFS"),
        "active_qemu_img_info": run(f"qemu-img info {str(active_vm)!r}", timeout=10),
    },
    "assets": {
        "claude_code_tarball": file_info(runtime_tarball, hash_file=True),
        "task_file_count": len(list((repo / "cache" / "tasks").rglob("*.md")))
            if (repo / "cache" / "tasks").is_dir()
            else 0,
    },
    "versions": {
        "python": platform.python_version(),
        "node": run("node --version", timeout=5),
        "npm": run("npm --version", timeout=5),
        "docker": run("docker --version", timeout=5),
        "qemu_img": run("qemu-img --version", timeout=5),
        "tmux": run("tmux -V", timeout=5),
        "openclaw": run(f"{os.environ.get('AJ_OPENCLAW_BIN', 'openclaw')} --version", timeout=5),
    },
}

path = pathlib.Path(os.environ["WB_PROVENANCE_PATH"])
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY

{
  echo "SESSION=${TMUX_PANE:-unknown}"
  echo "RESULT_DIR=$result_dir"
  echo "PROVENANCE=$provenance_path"
  echo "COORD_MODE=${WEAVEBENCH_COMPUTER_COORD_MODE:-auto}"
  echo "HARNESS=$harness model=$model"
  echo "TASKS_ROOT=$tasks_root"
  echo "DOMAINS=$domains task_filter=${task_filter:-<none>} limit=$limit"
  echo "VM=$OSWORLD_LOCAL_QCOW2_PATH grow_rootfs=$WEAVEBENCH_GROW_ROOTFS"
  echo "num_envs=$num_envs max_steps=$max_steps"
  echo "CUA max_rounds=$CUA_HARNESS_MAX_ROUNDS role_turns=unlimited"
  echo "CUA timeouts task=$CUA_HARNESS_TASK_TIMEOUT_SECONDS orchestrator=$CUA_HARNESS_ORCHESTRATOR_TIMEOUT_SECONDS verifier=$CUA_HARNESS_VERIFIER_TIMEOUT_SECONDS"
  echo "CUA context role_history=$CUA_HARNESS_ROLE_HISTORY_CHARS verified_context=$CUA_HARNESS_ROLE_VERIFIED_CONTEXT_CHARS memory=$CUA_HARNESS_ROLE_MEMORY_CHARS verifier_task_output=$CUA_HARNESS_VERIFIER_TASK_OUTPUT_CHARS"
  echo "JUDGE model=$WEAVEBENCH_JUDGE_MODEL workspace=$AJ_JUDGE_WORKSPACE openclaw=$AJ_OPENCLAW_BIN thinking=$AJ_THINKING timeout=$AJ_TIMEOUT retry=$AJ_RETRY backoff=$AJ_RETRY_BACKOFF_S"
  echo "IMAGE_PROXY=$WEAVEBENCH_IMAGE_PROXY base_url=$WEAVEBENCH_LITELLM_BASE_URL api_key=$api_key_display"
  echo "START_TS=$(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "COMMAND=$command_display"
} | tee "$log_path"

set -o pipefail
"${cmd[@]}" 2>&1 | tee -a "$log_path"

rc=${PIPESTATUS[0]}
echo "EXIT_STATUS=$rc" | tee -a "$log_path"
exit "$rc"
