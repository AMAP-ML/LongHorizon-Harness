#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill_dir="$(cd "$script_dir/.." && pwd)"

die() {
  echo "error: $*" >&2
  exit 2
}

find_project_root_from() {
  local dir="$1"
  dir="$(cd "$dir" 2>/dev/null && pwd || true)"
  [ -n "$dir" ] || return 1
  while [ "$dir" != "/" ]; do
    if [ -d "$dir/WeaveBench" ] && [ -d "$dir/cua-harness/src/cua_harness" ]; then
      printf '%s\n' "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

project_root="$(find_project_root_from "$PWD" || find_project_root_from "$skill_dir" || true)"
[ -n "$project_root" ] || die "could not locate project root containing WeaveBench/ and cua-harness/"
weavebench_dir="$project_root/WeaveBench"
cua_dir="$project_root/cua-harness"

usage() {
  cat <<'EOF'
usage: reproduce.sh <command> [args]

Commands:
  install       Install local WeaveBench package and OpenClaw if needed.
  download      Download tasks, Claude Code runtime, judge template, and VM.
  vm120g        Create WeaveBench/cache/vm/Ubuntu_120G.qcow2.
  doctor        Run WeaveBench/scripts/check_env.sh.
  intake        Print the setup questions an agent should answer before setup.
  status        Print a non-destructive configuration status report.
  smoke         Run a one-task smoke test.
  full          Launch the full evaluation in tmux.
  stats [root]  Summarize scores under a qwen3.7-plus result root.
  plan          Print the minimal manual command sequence.

Environment:
  WEAVEBENCH_LITELLM_KEY
  WEAVEBENCH_LITELLM_BASE_URL
  WEAVEBENCH_IMAGE_PROXY=1|0
  WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL
  WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL
  WEAVEBENCH_NUM_ENVS
  WEAVEBENCH_DOMAINS
  WEAVEBENCH_TASK_FILTER
  FORCE=1              Overwrite an existing Ubuntu_120G.qcow2 in vm120g.
EOF
}

need_repo() {
  [ -d "$weavebench_dir" ] || die "missing $weavebench_dir"
  [ -d "$cua_dir/src/cua_harness" ] || die "missing $cua_dir/src/cua_harness"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 not found on PATH"
}

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

show_env_summary() {
  echo "Project root: $project_root"
  echo "WeaveBench:   $weavebench_dir"
  echo "CUA harness:  $cua_dir"
  echo "API base:     ${WEAVEBENCH_LITELLM_BASE_URL:-<unset>}"
  echo "API key:      $(redact_key "${WEAVEBENCH_LITELLM_KEY:-}")"
  echo "Image proxy:  ${WEAVEBENCH_IMAGE_PROXY:-1}"
}

intake() {
  cat <<'EOF'
Answer these before running setup on a new machine:

1. Host/runtime
   - Is this a Linux host with Docker and /dev/kvm?
   - Is it acceptable to download large WeaveBench assets and create a 120G VM copy?

2. Model API
   - What Anthropic-compatible endpoint should WEAVEBENCH_LITELLM_BASE_URL use?
   - Is WEAVEBENCH_LITELLM_KEY available in the shell or should it be placed in a local secret file?

3. Image input
   - Can the endpoint accept large base64 screenshots?
   - If not, what public image proxy upload/show URL templates should be used?

4. Evaluation scope
   - Run only doctor, smoke, a subset, or the full 114-task evaluation?
   - If subset: which WEAVEBENCH_DOMAINS or WEAVEBENCH_TASK_FILTER?

5. Verification
   - Should the agent run smoke before full?
   - Should the agent summarize scores after completion?

Stop and ask the user before printing secrets, deleting outputs, or starting a long/high-concurrency run when another experiment may already be active.
EOF
}

status_line() {
  local label="$1"
  local state="$2"
  local detail="${3:-}"
  printf '%-28s %-32s %s\n' "$label" "$state" "$detail"
}

status_report() {
  need_repo
  local openclaw_bin="${AJ_OPENCLAW_BIN:-}"
  if [ -z "$openclaw_bin" ] && command -v openclaw >/dev/null 2>&1; then
    openclaw_bin="$(command -v openclaw)"
  fi

  echo "WeaveBench CUA-Harness setup status"
  echo
  status_line "Project root" "configured" "$project_root"
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    status_line "Docker" "configured and verified" "$(command -v docker)"
  elif command -v docker >/dev/null 2>&1; then
    status_line "Docker" "configured but not verified" "docker exists but daemon is not reachable"
  else
    status_line "Docker" "not configured" "docker not found"
  fi
  if [ -e /dev/kvm ]; then
    status_line "KVM" "configured" "/dev/kvm"
  else
    status_line "KVM" "not configured" "/dev/kvm missing"
  fi
  if command -v python3 >/dev/null 2>&1; then
    status_line "Python" "configured" "$(python3 --version 2>&1)"
  else
    status_line "Python" "not configured" "python3 not found"
  fi
  if command -v node >/dev/null 2>&1; then
    status_line "Node.js" "configured" "$(node --version 2>/dev/null)"
  else
    status_line "Node.js" "not configured" "node not found"
  fi
  if [ -n "$openclaw_bin" ] && [ -x "$openclaw_bin" ]; then
    status_line "OpenClaw" "configured" "$openclaw_bin"
  else
    status_line "OpenClaw" "not configured" "install with npm install -g openclaw"
  fi
  if [ -d "$weavebench_dir/cache/tasks" ]; then
    status_line "Tasks" "configured" "$(find "$weavebench_dir/cache/tasks" -type f -name '*.md' | wc -l | tr -d ' ') task files"
  else
    status_line "Tasks" "not configured" "run reproduce.sh download"
  fi
  if [ -f "$weavebench_dir/cache/runtime_assets/claudecode.tar.gz" ]; then
    status_line "Claude runtime" "configured" "cache/runtime_assets/claudecode.tar.gz"
  else
    status_line "Claude runtime" "not configured" "run reproduce.sh download"
  fi
  if [ -f "$weavebench_dir/cache/vm/Ubuntu_120G.qcow2" ]; then
    status_line "120G VM" "configured" "cache/vm/Ubuntu_120G.qcow2"
  elif [ -f "$weavebench_dir/cache/vm/Ubuntu.qcow2" ]; then
    status_line "120G VM" "not configured" "run reproduce.sh vm120g"
  else
    status_line "120G VM" "not configured" "run reproduce.sh download then vm120g"
  fi
  if [ -d "$weavebench_dir/judge_agent_test/template_profile" ] && [ -d "$weavebench_dir/judge_agent_test/template_workspace" ]; then
    status_line "Judge templates" "configured" "judge_agent_test/template_*"
  else
    status_line "Judge templates" "not configured" "run reproduce.sh download"
  fi
  if [ -n "${WEAVEBENCH_LITELLM_BASE_URL:-}" ] && [ -n "${WEAVEBENCH_LITELLM_KEY:-}" ]; then
    status_line "Model API" "configured but not smoke-tested" "${WEAVEBENCH_LITELLM_BASE_URL} key=$(redact_key "$WEAVEBENCH_LITELLM_KEY")"
  else
    status_line "Model API" "blocked awaiting user action" "set WEAVEBENCH_LITELLM_BASE_URL and WEAVEBENCH_LITELLM_KEY"
  fi
  case "$(printf '%s' "${WEAVEBENCH_IMAGE_PROXY:-1}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on)
      if [ -n "${WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL:-}" ] && [ -n "${WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL:-}" ]; then
        status_line "Image proxy" "configured" "enabled"
      else
        status_line "Image proxy" "blocked awaiting user action" "set upload/show URL templates or disable proxy"
      fi
      ;;
    0|false|no|off)
      status_line "Image proxy" "configured" "disabled"
      ;;
    *)
      status_line "Image proxy" "not configured" "invalid WEAVEBENCH_IMAGE_PROXY=${WEAVEBENCH_IMAGE_PROXY:-}"
      ;;
  esac
}

install_deps() {
  need_repo
  need_cmd python3
  need_cmd npm
  python3 -m pip install -e "$weavebench_dir"
  if command -v openclaw >/dev/null 2>&1; then
    echo "openclaw already installed: $(command -v openclaw)"
    openclaw --version || true
  else
    npm install -g openclaw
  fi
}

download_assets() {
  need_repo
  need_cmd weavebench-download-dataset
  need_cmd weavebench-download-assets
  need_cmd weavebench-download-judge
  need_cmd weavebench-download-vm
  cd "$weavebench_dir"
  export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
  weavebench-download-dataset --dest ./cache --include "tasks/*"
  weavebench-download-assets --dest ./cache --harness claudecode
  weavebench-download-judge --judge-home ./judge_agent_test
  weavebench-download-vm --dest ./cache
}

prepare_vm120g() {
  need_repo
  need_cmd qemu-img
  local src="$weavebench_dir/cache/vm/Ubuntu.qcow2"
  local dst="$weavebench_dir/cache/vm/Ubuntu_120G.qcow2"
  [ -f "$src" ] || die "missing original VM: $src"
  if [ -f "$dst" ] && [ "${FORCE:-0}" != "1" ]; then
    echo "VM copy already exists: $dst"
    echo "Set FORCE=1 to recreate it."
  else
    cp "$src" "$dst"
    qemu-img resize "$dst" 120G
  fi
  qemu-img info "$src" | sed 's/^/[Ubuntu.qcow2] /'
  qemu-img info "$dst" | sed 's/^/[Ubuntu_120G.qcow2] /'
}

doctor() {
  need_repo
  export CUA_HARNESS_SOURCE_DIR="${CUA_HARNESS_SOURCE_DIR:-$cua_dir}"
  cd "$weavebench_dir"
  ./scripts/check_env.sh
}

smoke() {
  need_repo
  export CUA_HARNESS_SOURCE_DIR="${CUA_HARNESS_SOURCE_DIR:-$cua_dir}"
  export AJ_OPENCLAW_BIN="${AJ_OPENCLAW_BIN:-$(command -v openclaw 2>/dev/null || true)}"
  export AJ_TEMPLATE_PROFILE="${AJ_TEMPLATE_PROFILE:-$weavebench_dir/judge_agent_test/template_profile}"
  export AJ_TEMPLATE_WORKSPACE="${AJ_TEMPLATE_WORKSPACE:-$weavebench_dir/judge_agent_test/template_workspace}"
  cd "$weavebench_dir"
  local run_name="${RUN_NAME:-smoke_qwen_cua_$(date +%Y%m%d_%H%M%S)}"
  show_env_summary
  ./scripts/smoke_qwen37plus_cua_harness_eval.sh \
    "$PWD/results/$run_name" \
    "$PWD/logs/$run_name.log"
}

full() {
  need_repo
  need_cmd tmux
  export CUA_HARNESS_SOURCE_DIR="${CUA_HARNESS_SOURCE_DIR:-$cua_dir}"
  export AJ_OPENCLAW_BIN="${AJ_OPENCLAW_BIN:-$(command -v openclaw 2>/dev/null || true)}"
  export AJ_TEMPLATE_PROFILE="${AJ_TEMPLATE_PROFILE:-$weavebench_dir/judge_agent_test/template_profile}"
  export AJ_TEMPLATE_WORKSPACE="${AJ_TEMPLATE_WORKSPACE:-$weavebench_dir/judge_agent_test/template_workspace}"
  cd "$weavebench_dir"
  local run_name="${RUN_NAME:-cua_harness_qwen37plus_114tasks_norm1000_rounds25_numenvs${WEAVEBENCH_NUM_ENVS:-5}_vm120g_$(date +%Y%m%d_%H%M%S)}"
  show_env_summary
  tmux new -s "$run_name" \
    "cd \"$PWD\" && ./scripts/run_qwen37plus_cua_harness_eval.sh \"$PWD/results/$run_name\" \"$PWD/logs/${run_name}.log\""
}

stats() {
  need_repo
  local root="${1:-}"
  if [ -z "$root" ]; then
    root="$(find "$weavebench_dir/results" -type d -path '*/gui/qwen3.7-plus' 2>/dev/null | sort | tail -1 || true)"
  fi
  [ -n "$root" ] || die "no result root provided and none found under WeaveBench/results"
  python3 - "$root" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).expanduser().resolve()
scores = []
by_domain = {}
for p in root.rglob("score.json"):
    try:
        score = float(json.loads(p.read_text()).get("score", 0.0))
    except Exception:
        continue
    rel = p.relative_to(root)
    domain = rel.parts[0] if len(rel.parts) > 1 else "UNKNOWN"
    scores.append(score)
    by_domain.setdefault(domain, []).append(score)

def row(name, vals):
    n = len(vals)
    avg = sum(vals) / n if n else 0.0
    pr = sum(v >= 0.8 for v in vals)
    zero = sum(v == 0 for v in vals)
    return f"{name}\t{n}\t{avg:.4f}\t{pr}/{n} = {(pr/n*100 if n else 0):.2f}%\t{zero}/{n} = {(zero/n*100 if n else 0):.2f}%"

print(f"Result root\t{root}")
print("Scope\tTasks\tAverage\tPR >= 0.8\t0 score")
print(row("ALL", scores))
for domain in sorted(by_domain):
    print(row(domain, by_domain[domain]))
PY
}

plan() {
  cat <<'EOF'
# From the WeaveBench-harness project root:
python3 -m pip install -e ./WeaveBench
npm install -g openclaw

cd WeaveBench
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
weavebench-download-dataset --dest ./cache --include "tasks/*"
weavebench-download-assets --dest ./cache --harness claudecode
weavebench-download-judge --judge-home ./judge_agent_test
weavebench-download-vm --dest ./cache
cp ./cache/vm/Ubuntu.qcow2 ./cache/vm/Ubuntu_120G.qcow2
qemu-img resize ./cache/vm/Ubuntu_120G.qcow2 120G

export WEAVEBENCH_LITELLM_KEY="YOUR_API_KEY"
export WEAVEBENCH_LITELLM_BASE_URL="https://YOUR_ANTHROPIC_COMPATIBLE_ENDPOINT/v1"
export WEAVEBENCH_IMAGE_PROXY=1
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL="https://YOUR_IMAGE_UPLOAD_ENDPOINT/{id}"
export WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL="https://YOUR_PUBLIC_IMAGE_URL/{id}.png"
export WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE="raw"
./scripts/check_env.sh

smoke_name="smoke_qwen_cua_$(date +%Y%m%d_%H%M%S)"
./scripts/smoke_qwen37plus_cua_harness_eval.sh "$PWD/results/$smoke_name" "$PWD/logs/$smoke_name.log"

run_name="cua_harness_qwen37plus_114tasks_norm1000_rounds25_numenvs5_vm120g_$(date +%Y%m%d_%H%M%S)"
tmux new -s "$run_name" "cd \"$PWD\" && ./scripts/run_qwen37plus_cua_harness_eval.sh \"$PWD/results/$run_name\" \"$PWD/logs/${run_name}.log\""
EOF
}

cmd="${1:-}"
case "$cmd" in
  install) shift; install_deps "$@" ;;
  download) shift; download_assets "$@" ;;
  vm120g) shift; prepare_vm120g "$@" ;;
  doctor) shift; doctor "$@" ;;
  intake) shift; intake "$@" ;;
  status) shift; status_report "$@" ;;
  smoke) shift; smoke "$@" ;;
  full) shift; full "$@" ;;
  stats) shift; stats "$@" ;;
  plan) shift; plan "$@" ;;
  -h|--help|help|"") usage ;;
  *) usage; die "unknown command: $cmd" ;;
esac
