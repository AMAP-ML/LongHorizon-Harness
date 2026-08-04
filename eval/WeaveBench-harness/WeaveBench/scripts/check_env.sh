#!/usr/bin/env bash
set -u

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project_root="$(cd "$repo/.." && pwd)"

failures=0
warnings=0

ok() {
  printf '[OK]   %s\n' "$*"
}

warn() {
  warnings=$((warnings + 1))
  printf '[WARN] %s\n' "$*"
}

fail() {
  failures=$((failures + 1))
  printf '[FAIL] %s\n' "$*"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

check_cmd() {
  if have_cmd "$1"; then
    ok "$1: $(command -v "$1")"
  else
    fail "$1 not found on PATH"
  fi
}

version_ge() {
  # Usage: version_ge "3.10.1" "3.10"
  awk -v a="$1" -v b="$2" '
    BEGIN {
      split(a, av, "."); split(b, bv, ".");
      for (i = 1; i <= 3; i++) {
        ai = av[i] + 0; bi = bv[i] + 0;
        if (ai > bi) exit 0;
        if (ai < bi) exit 1;
      }
      exit 0;
    }'
}

print_section() {
  printf '\n== %s ==\n' "$1"
}

print_section "Host tools"
for cmd in docker python3 node npm qemu-img tmux; do
  check_cmd "$cmd"
done

if have_cmd python3; then
  py_ver="$(python3 - <<'PY' 2>/dev/null
import sys
print(".".join(map(str, sys.version_info[:3])))
PY
)"
  if version_ge "$py_ver" "3.10"; then
    ok "python version >= 3.10: $py_ver"
  else
    fail "python version must be >= 3.10, got $py_ver"
  fi

  if python3 - <<'PY' >/dev/null 2>&1
import weavebench
PY
  then
    ok "python package import works: weavebench"
  else
    warn "python cannot import weavebench; run: python3 -m pip install -e ./WeaveBench"
  fi
fi

if have_cmd node; then
  node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
  if [ "${node_major:-0}" -ge 22 ] 2>/dev/null; then
    ok "node major version >= 22: $(node --version)"
  else
    fail "node major version must be >= 22, got $(node --version 2>/dev/null || echo unknown)"
  fi
fi

print_section "Docker and KVM"
if have_cmd docker; then
  if docker info >/dev/null 2>&1; then
    ok "docker daemon is reachable"
  else
    fail "docker daemon is not reachable by current user"
  fi
fi

if [ -e /dev/kvm ]; then
  ok "/dev/kvm exists"
  if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    ok "current user can read/write /dev/kvm"
  else
    warn "current user may not have read/write permission on /dev/kvm"
  fi
else
  fail "/dev/kvm does not exist"
fi

print_section "OpenClaw judge"
openclaw_bin="${AJ_OPENCLAW_BIN:-}"
if [ -z "$openclaw_bin" ] && have_cmd openclaw; then
  openclaw_bin="$(command -v openclaw)"
fi
if [ -n "$openclaw_bin" ] && [ -x "$openclaw_bin" ]; then
  ok "openclaw: $openclaw_bin"
  "$openclaw_bin" --version 2>/dev/null | sed 's/^/[INFO] openclaw version: /' || true
else
  fail "openclaw not found; set AJ_OPENCLAW_BIN or install with npm install -g openclaw"
fi

print_section "Repository paths"
cua_dir="${CUA_HARNESS_SOURCE_DIR:-$project_root/cua-harness}"
if [ -d "$cua_dir/src/cua_harness" ]; then
  ok "CUA harness source: $cua_dir"
else
  fail "CUA harness source not found: $cua_dir"
fi

tasks_root="${WEAVEBENCH_TASKS_ROOT:-$repo/cache/tasks}"
if [ -d "$tasks_root" ]; then
  task_count="$(find "$tasks_root" -type f -name '*.md' | wc -l | tr -d ' ')"
  ok "tasks root exists: $tasks_root ($task_count task files)"
else
  fail "tasks root missing: $tasks_root"
fi

runtime_asset="$repo/cache/runtime_assets/claudecode.tar.gz"
if [ -f "$runtime_asset" ]; then
  ok "Claude Code runtime asset exists: $runtime_asset"
else
  fail "Claude Code runtime asset missing: $runtime_asset"
fi

vm_path="${OSWORLD_LOCAL_QCOW2_PATH:-$repo/cache/vm/Ubuntu_120G.qcow2}"
if [ -f "$vm_path" ]; then
  ok "VM image exists: $vm_path"
  if have_cmd qemu-img; then
    qemu-img info "$vm_path" 2>/dev/null | sed -n 's/^virtual size:/[INFO] VM virtual size:/p'
  fi
else
  fail "VM image missing: $vm_path"
fi

if [ -f "$repo/cache/vm/Ubuntu.qcow2" ]; then
  ok "original VM image exists: $repo/cache/vm/Ubuntu.qcow2"
else
  warn "original VM image missing: $repo/cache/vm/Ubuntu.qcow2"
fi

judge_home="${AJ_JUDGE_HOME:-$repo/judge_agent_test}"
if [ -d "${AJ_TEMPLATE_PROFILE:-$judge_home/template_profile}" ]; then
  ok "judge template_profile exists"
else
  fail "judge template_profile missing; run weavebench-download-judge --judge-home ./judge_agent_test"
fi

if [ -d "${AJ_TEMPLATE_WORKSPACE:-$judge_home/template_workspace}" ]; then
  ok "judge template_workspace exists"
else
  fail "judge template_workspace missing; run weavebench-download-judge --judge-home ./judge_agent_test"
fi

print_section "Model API"
if [ -n "${WEAVEBENCH_LITELLM_KEY:-}" ]; then
  key="$WEAVEBENCH_LITELLM_KEY"
  if [ "${#key}" -le 10 ]; then
    shown="<set>"
  else
    shown="${key:0:4}...${key: -4}"
  fi
  ok "WEAVEBENCH_LITELLM_KEY is set: $shown"
else
  fail "WEAVEBENCH_LITELLM_KEY is not set"
fi

if [ -n "${WEAVEBENCH_LITELLM_BASE_URL:-}" ]; then
  ok "WEAVEBENCH_LITELLM_BASE_URL=$WEAVEBENCH_LITELLM_BASE_URL"
else
  fail "WEAVEBENCH_LITELLM_BASE_URL is not set"
fi

print_section "Image proxy"
image_proxy="${WEAVEBENCH_IMAGE_PROXY:-1}"
case "$(printf '%s' "$image_proxy" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    ok "WEAVEBENCH_IMAGE_PROXY is enabled"
    if [ -n "${WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL:-}" ]; then
      ok "image proxy upload URL template is set"
    else
      fail "WEAVEBENCH_IMAGE_PROXY_UPLOAD_URL_TPL is required when image proxy is enabled"
    fi
    if [ -n "${WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL:-}" ]; then
      ok "image proxy public show URL template is set"
    else
      fail "WEAVEBENCH_IMAGE_PROXY_SHOW_URL_TPL is required when image proxy is enabled"
    fi
    ;;
  0|false|no|off)
    ok "WEAVEBENCH_IMAGE_PROXY is disabled"
    ;;
  *)
    fail "invalid WEAVEBENCH_IMAGE_PROXY=$image_proxy"
    ;;
esac

print_section "Summary"
if [ "$failures" -eq 0 ]; then
  ok "environment check passed with $warnings warning(s)"
  exit 0
fi

fail "$failures failure(s), $warnings warning(s)"
exit 1
