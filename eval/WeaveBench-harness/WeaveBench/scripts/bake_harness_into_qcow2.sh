#!/usr/bin/env bash
# Bake an agent harness INTO an OSWorld qcow2 image, ONCE, so that every
# subsequent task boots a VM that already has the harness installed and the
# runtime bootstrap is a no-op.
#
# WHY THIS EXISTS
# ---------------
# The docker provider mounts the qcow2 read-only (/System.qcow2:ro) and boots
# Ubuntu via KVM. Files written inside the docker container are invisible to
# that VM, so a Dockerfile cannot install the harness. The ONLY way to make an
# install persist is to:
#   1. copy the qcow2 to a writable file,
#   2. boot an isolated container that mounts the copy READ-WRITE,
#   3. run the harness's own bootstrap inside the VM,
#   4. graceful-shutdown so QEMU flushes the writes back into the qcow2.
# After that, every run sees /home/user/.<harness>_bootstrap.done already
# present and skips the ~500 MB upload + multi-minute install per task.
#
# ZERO-DRIFT
# ----------
# For openclaw we do NOT re-implement the install steps here. We extract the
# exact BOOTSTRAP_SH string the runtime uses (weavebench/agents/_openclaw/
# _chat.py) and execute that inside the VM, after staging the same three
# assets the runtime stages (openclaw.tar.gz + computer_tool_plugin/ +
# openclaw_patches/). Bake-time and run-time installs are therefore identical,
# and the marker file the bake writes is the same one the runtime checks.
#
# USAGE
#   scripts/bake_harness_into_qcow2.sh [openclaw|codex|claudecode|hermes]
#
#   SRC_QCOW=/path/to/Ubuntu.qcow2 \
#   DST_QCOW=/path/to/Ubuntu.qcow2.baked \
#   scripts/bake_harness_into_qcow2.sh openclaw
#
# ENV OVERRIDES
#   SRC_QCOW   source qcow2            (default ./docker_vm_data/Ubuntu.qcow2
#                                       or $OSWORLD_LOCAL_QCOW2_PATH)
#   DST_QCOW   baked output qcow2      (default <SRC>.<harness>_baked_<date>)
#   CACHE_DIR  runtime asset root      (default ./cache/runtime_assets)
#   PROMOTE=1  on success, repoint SRC's Ubuntu.qcow2 symlink at the baked file
#              (keeps a timestamped backup of whatever it replaced)
#   KEEP_GOING=1 do not delete a half-baked DST_QCOW on failure (for debugging)
#
# openclaw, codex, claudecode and hermes are all implemented.

set -euo pipefail

HARNESS="${1:-openclaw}"

case "${HARNESS}" in
  openclaw|codex|claudecode|hermes) ;;
  *)
    echo "[fatal] unknown harness: ${HARNESS} (expected openclaw|codex|claudecode|hermes)"; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Local 4200 / loopback must bypass any host http_proxy (clash etc.) or the
# curl calls to the bake container's Flask port hang.
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY || true
export NO_PROXY="127.0.0.1,localhost"

CACHE_DIR="${CACHE_DIR:-${REPO_ROOT}/cache/runtime_assets}"
CLIENT_PASSWORD="${CLIENT_PASSWORD:-password}"

# ---- resolve source qcow2 --------------------------------------------------
# Resolution order: explicit $SRC_QCOW -> $OSWORLD_LOCAL_QCOW2_PATH ->
# cache/vm/Ubuntu.qcow2 (where scripts/setup.sh downloads it) ->
# docker_vm_data/Ubuntu.qcow2 (legacy location).
if [ -n "${SRC_QCOW:-}" ]; then
  :
elif [ -n "${OSWORLD_LOCAL_QCOW2_PATH:-}" ]; then
  SRC_QCOW="${OSWORLD_LOCAL_QCOW2_PATH}"
elif [ -f "${REPO_ROOT}/cache/vm/Ubuntu.qcow2" ]; then
  SRC_QCOW="${REPO_ROOT}/cache/vm/Ubuntu.qcow2"
else
  SRC_QCOW="${REPO_ROOT}/docker_vm_data/Ubuntu.qcow2"
fi
SRC_QCOW="$(readlink -f "${SRC_QCOW}" 2>/dev/null || echo "${SRC_QCOW}")"
[ -f "${SRC_QCOW}" ] || { echo "[fatal] source qcow2 not found: ${SRC_QCOW}"; exit 1; }

DATESTAMP="$(date +%Y%m%d_%H%M%S)"
DST_QCOW="${DST_QCOW:-${SRC_QCOW%.qcow2}.${HARNESS}_baked_${DATESTAMP}.qcow2}"

# ---- staging on fast local disk (optional) ---------------------------------
# QEMU reads+writes the working copy throughout the bake. On NAS/NFS that is
# slow and was the source of write-lock flakiness. Set STAGE_DIR to a local
# SSD path to do the whole bake there, then move the finished image to
# DST_QCOW. The working copy only lives in STAGE_DIR for the duration of the
# bake, so the SSD only needs ~28 GB free transiently.
#   STAGE_DIR=/tmp  (or any local ext4/xfs path)
STAGE_DIR="${STAGE_DIR:-}"
if [ -n "${STAGE_DIR}" ]; then
  mkdir -p "${STAGE_DIR}" || { echo "[fatal] cannot create STAGE_DIR ${STAGE_DIR}"; exit 1; }
  WORK_QCOW="${STAGE_DIR%/}/$(basename "${DST_QCOW}")"
  echo "[bake] staging on local disk: ${WORK_QCOW} (will move to ${DST_QCOW} when done)"
else
  WORK_QCOW="${DST_QCOW}"
fi

# ---- isolated container ports (avoid colliding with live sweep workers) ----
NAME="wb_bake_${HARNESS}"
VNC=18020; SRV=15020; CHR=19320; VLC=18170

need() { command -v "$1" >/dev/null 2>&1 || { echo "[fatal] missing dependency: $1"; exit 1; }; }
need docker; need curl; need python3
[ -e /dev/kvm ] || { echo "[fatal] /dev/kvm absent — KVM required to boot the VM"; exit 1; }

# ---- python helpers: upload a file / exec a command via the VM Flask API ----
# The VM's /setup/execute runs as `user` whose sudo needs the client password,
# so we shim `sudo` to feed it on stdin (mirrors the runtime's _vm_exec).
_vm() {
  python3 - "$@" <<'PY'
import sys, os, json, requests
srv = os.environ["BAKE_SRV"]
op  = sys.argv[1]
base = f"http://127.0.0.1:{srv}"
sess = requests.Session(); sess.trust_env = False  # ignore http_proxy
if op == "ready":
    try:
        r = sess.get(base + "/screenshot", timeout=10)
        sys.exit(0 if r.ok else 1)
    except Exception:
        sys.exit(1)
elif op == "upload":
    local, remote = sys.argv[2], sys.argv[3]
    with open(local, "rb") as fh:
        r = sess.post(base + "/setup/upload",
                      files={"file_data": (os.path.basename(local), fh,
                                           "application/octet-stream")},
                      data={"file_path": remote}, timeout=1800)
    r.raise_for_status(); print(f"[upload] {local} -> {remote} ok")
elif op == "exec" or op == "rawexec":
    cmd = sys.argv[2]; timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 600
    pw = os.environ.get("CLIENT_PASSWORD", "password")
    if op == "exec":
        # Convenience shim: a bare `sudo ...` in simple helper commands
        # (mkdir, cp) gets the client password fed on stdin. NOT used for
        # rawexec, because BOOTSTRAP_SH defines its own SUDO() that already
        # pipes the password — a second shim would double the -S/-p flags
        # and mangle every sudo call.
        wrapped = (f'sudo() {{ echo "{pw}" | command sudo -S -p "" "$@"; }};'
                   f' export -f sudo 2>/dev/null || true\n{cmd}')
    else:
        wrapped = cmd
    r = sess.post(base + "/setup/execute",
                  json={"command": ["bash", "-c", wrapped], "shell": False},
                  timeout=timeout)
    try: j = r.json()
    except Exception:
        print("HTTP body:", r.text[:800], file=sys.stderr); sys.exit(2)
    print(j.get("output", ""), end="")
    rc = j.get("returncode", 0) or 0
    if rc != 0:
        print("STDERR:", j.get("error", ""), file=sys.stderr)
    sys.exit(rc)
PY
}
export CLIENT_PASSWORD

wait_ready() {
  local port="$1" label="$2"
  echo "[bake] waiting for VM Flask :${port} (${label})..."
  for i in $(seq 1 90); do
    if BAKE_SRV="${port}" _vm ready; then
      echo "[bake] VM ready after ~$((i*10))s"; return 0
    fi
    sleep 10
  done
  echo "[fatal] VM never became ready on :${port}"; return 1
}

cleanup() {
  docker rm -f "${NAME}" "${NAME}_verify" >/dev/null 2>&1 || true
  # Drop a staged working copy on abnormal exit unless KEEP_GOING is set, so a
  # killed run doesn't leave 28 GB stranded on the SSD.
  if [ "${WORK_QCOW:-}" != "${DST_QCOW:-}" ] && [ "${KEEP_GOING:-0}" != "1" ]; then
    [ "${BAKE_OK:-0}" = "1" ] || rm -f "${WORK_QCOW}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "==================================================="
echo "Bake harness '${HARNESS}' INTO qcow2"
echo "  source: ${SRC_QCOW} ($(du -h "${SRC_QCOW}" | cut -f1))"
echo "  dest:   ${DST_QCOW}"
echo "  assets: ${CACHE_DIR}"
echo "==================================================="

# ---- per-harness asset staging + bootstrap source --------------------------
# Each harness sets:
#   ASSETS_OK            (validates required files exist locally)
#   stage_assets()       (uploads tarball(s)+aux files to /tmp in the VM)
#   BOOTSTRAP_SH         (the exact install script, extracted from the agent)
#   MARKER               (the done-file the runtime checks)
#   VERIFY_CMD           (commands proving the install survived a reboot)
case "${HARNESS}" in
  openclaw)
    TARBALL="${CACHE_DIR}/openclaw.tar.gz"
    # The plugin/patch SOURCE files are NOT in the HF tarball download — they
    # ship in the repo's weavebench/assets/. Prefer cache (if a user populated
    # it) but fall back to the in-repo dir so a fresh checkout bakes correctly.
    INREPO_ASSETS="${REPO_ROOT}/weavebench/assets"
    if [ -f "${CACHE_DIR}/computer_tool_plugin/openclaw.plugin.json" ]; then
      PLUGIN_DIR="${CACHE_DIR}/computer_tool_plugin"
    else
      PLUGIN_DIR="${INREPO_ASSETS}/computer_tool_plugin"
    fi
    if [ -f "${CACHE_DIR}/openclaw_patches/openai-responses-shared.patched.js" ]; then
      PATCH_DIR="${CACHE_DIR}/openclaw_patches"
    else
      PATCH_DIR="${INREPO_ASSETS}/openclaw_patches"
    fi
    MARKER="/home/user/.openclaw_bootstrap.done"
    BOOTLOG="/tmp/openclaw_bootstrap.log"

    [ -f "${TARBALL}" ] || { echo "[fatal] missing ${TARBALL} — run scripts/download_assets.sh openclaw"; exit 1; }
    [ -f "${PLUGIN_DIR}/openclaw.plugin.json" ] || { echo "[fatal] missing computer_tool_plugin/openclaw.plugin.json (looked in cache and ${INREPO_ASSETS})"; exit 1; }
    [ -f "${PATCH_DIR}/openai-responses-shared.patched.js" ] || { echo "[fatal] missing openclaw patches (looked in cache and ${INREPO_ASSETS})"; exit 1; }

    # Extract the EXACT BOOTSTRAP_SH the runtime uses — no copy/paste drift.
    BOOTSTRAP_SH="$(python3 - <<'PY'
from weavebench.agents._openclaw import _chat
import sys
sys.stdout.write(_chat.BOOTSTRAP_SH)
PY
)"
    [ -n "${BOOTSTRAP_SH}" ] || { echo "[fatal] could not extract BOOTSTRAP_SH from _chat.py"; exit 1; }

    stage_assets() {
      echo "[bake] uploading openclaw.tar.gz ($(du -h "${TARBALL}" | cut -f1))..."
      BAKE_SRV="${SRV}" _vm upload "${TARBALL}" /tmp/openclaw.tar.gz
      BAKE_SRV="${SRV}" _vm exec 'mkdir -p /tmp/computer_tool_plugin /tmp/openclaw_patches' 60
      for f in openclaw.plugin.json index.ts; do
        [ -f "${PLUGIN_DIR}/${f}" ] && BAKE_SRV="${SRV}" _vm upload "${PLUGIN_DIR}/${f}" "/tmp/computer_tool_plugin/${f}"
      done
      for f in openai-responses-shared.patched.js openai-responses.patched.js openai-completions.patched.js; do
        [ -f "${PATCH_DIR}/${f}" ] && BAKE_SRV="${SRV}" _vm upload "${PATCH_DIR}/${f}" "/tmp/openclaw_patches/${f}"
      done
    }

    VERIFY_CMD='set -e
echo "--- marker ---"; test -f /home/user/.openclaw_bootstrap.done && echo MARKER_OK || { echo MARKER_MISSING; exit 1; }
echo "--- openclaw ---"; which openclaw; openclaw --version | head -1
echo "--- node ---"; node --version
echo "--- plugin ---"; test -f /home/user/.openclaw/extensions/computer-tool/openclaw.plugin.json && echo PLUGIN_OK
echo "--- sudo ---"; sudo -n true && echo PASSWORDLESS_SUDO_OK'
    ;;

  codex|claudecode)
    # codex/claudecode bootstrap is a short synchronous install (no apt/service
    # restarts), so BOOTSTRAP_SH stays empty (sentinel) and Step 3 runs
    # INSTALL_BODY directly via _vm exec instead of the detached-poll path.
    TARBALL="${CACHE_DIR}/${HARNESS}.tar.gz"
    MCP_SRC="${REPO_ROOT}/weavebench/assets/weavebench_computer_mcp/server.py"
    MARKER="/home/user/.weavebench_${HARNESS}_bootstrap.done"
    BOOTLOG="/tmp/${HARNESS}_bootstrap.log"
    BOOTSTRAP_SH=""
    [ -f "${TARBALL}" ] || { echo "[fatal] missing ${TARBALL} — copy from sibling cache or run scripts/download_assets.sh ${HARNESS}"; exit 1; }
    [ -f "${MCP_SRC}" ] || { echo "[fatal] missing MCP server ${MCP_SRC}"; exit 1; }

    if [ "${HARNESS}" = "codex" ]; then
      JS_PATH="/usr/lib/node_modules/@openai/codex/bin/codex.js"; BIN="/usr/local/bin/codex"; MCP_ADD=""
    else
      JS_PATH="/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"; BIN="/usr/local/bin/claude"
      MCP_ADD="${BIN} mcp remove weavebench_computer 2>/dev/null; ${BIN} mcp add -s user weavebench_computer -- /usr/bin/python3 /usr/local/lib/weavebench_computer_mcp/server.py"
    fi

    stage_assets() {
      echo "[bake] uploading ${HARNESS}.tar.gz ($(du -h "${TARBALL}" | cut -f1))..."
      BAKE_SRV="${SRV}" _vm upload "${TARBALL}" "/tmp/${HARNESS}.tar.gz"
      BAKE_SRV="${SRV}" _vm exec 'mkdir -p /tmp/weavebench_computer_mcp' 60
      BAKE_SRV="${SRV}" _vm upload "${MCP_SRC}" /tmp/weavebench_computer_mcp/server.py
    }

    INSTALL_BODY="set -e
sudo tar xzf /tmp/${HARNESS}.tar.gz -C /
sudo ln -sf ${JS_PATH} ${BIN}
test -x ${JS_PATH}
sudo mkdir -p /usr/local/lib/weavebench_computer_mcp
sudo cp -f /tmp/weavebench_computer_mcp/server.py /usr/local/lib/weavebench_computer_mcp/server.py
sudo chmod +x /usr/local/lib/weavebench_computer_mcp/server.py
${BIN} --version 2>&1
mkdir -p \$(dirname ${MARKER}) && date -u +%FT%TZ > ${MARKER}"

    VERIFY_CMD="set -e
echo '--- marker ---'; test -f ${MARKER} && echo MARKER_OK || { echo MARKER_MISSING; exit 1; }
echo '--- bin ---'; which ${BIN##*/}; ${BIN} --version | head -1
echo '--- node ---'; node --version
echo '--- mcp ---'; test -f /usr/local/lib/weavebench_computer_mcp/server.py && echo MCP_OK || { echo MCP_MISSING; exit 1; }"
    ;;

  hermes)
    # hermes is a self-contained Python venv tarball (rooted at /opt/hermes),
    # NOT a node CLI. Bootstrap also installs `mcp` offline from bundled wheels
    # (hermes 0.14.0 doesn't ship it) and applies the vision-bridge source
    # patch. All synchronous, so BOOTSTRAP_SH stays empty (sentinel).
    TARBALL="${CACHE_DIR}/hermes.tar.gz"
    WHEELS="${CACHE_DIR}/hermes_mcp_wheels.tar.gz"
    MCP_SRC="${REPO_ROOT}/weavebench/assets/weavebench_computer_mcp/server.py"
    VISION_PATCH="${REPO_ROOT}/weavebench/assets/hermes_patches/vision_bridge.py"
    MARKER="/home/user/.weavebench_hermes_bootstrap.done"
    BOOTLOG="/tmp/hermes_bootstrap.log"
    BOOTSTRAP_SH=""; MCP_ADD=""
    BIN="/usr/local/bin/hermes"; ENTRY="/opt/hermes/.venv/bin/hermes"; VENV_PY="/opt/hermes/.venv/bin/python3"
    [ -f "${TARBALL}" ] || { echo "[fatal] missing ${TARBALL} — copy from sibling cache or run scripts/download_assets.sh hermes"; exit 1; }
    [ -f "${WHEELS}" ] || { echo "[fatal] missing ${WHEELS} — copy from sibling cache or run scripts/download_assets.sh hermes"; exit 1; }
    [ -f "${MCP_SRC}" ] || { echo "[fatal] missing MCP server ${MCP_SRC}"; exit 1; }
    [ -f "${VISION_PATCH}" ] || { echo "[fatal] missing vision patch ${VISION_PATCH}"; exit 1; }

    stage_assets() {
      echo "[bake] uploading hermes.tar.gz ($(du -h "${TARBALL}" | cut -f1))..."
      BAKE_SRV="${SRV}" _vm upload "${TARBALL}" /tmp/hermes.tar.gz
      BAKE_SRV="${SRV}" _vm upload "${WHEELS}" /tmp/hermes_mcp_wheels.tar.gz
      BAKE_SRV="${SRV}" _vm exec 'mkdir -p /tmp/weavebench_computer_mcp' 60
      BAKE_SRV="${SRV}" _vm upload "${MCP_SRC}" /tmp/weavebench_computer_mcp/server.py
      BAKE_SRV="${SRV}" _vm upload "${VISION_PATCH}" /tmp/weavebench_hermes_vision_patch.py
    }

    INSTALL_BODY="set -e
sudo mkdir -p /opt
sudo tar xzf /tmp/hermes.tar.gz -C /
test -x ${ENTRY}
sudo ln -sf ${ENTRY} ${BIN}
rm -rf /tmp/hermes_mcp_wheels && mkdir -p /tmp/hermes_mcp_wheels
tar xzf /tmp/hermes_mcp_wheels.tar.gz -C /tmp/hermes_mcp_wheels
sudo ${VENV_PY} -m pip install -q --no-input --no-index --find-links /tmp/hermes_mcp_wheels /tmp/hermes_mcp_wheels/mcp-*.whl 2>&1 | tail -5
${VENV_PY} -c 'from mcp import StdioServerParameters, ClientSession; print(\"mcp OK\")'
sudo ${VENV_PY} /tmp/weavebench_hermes_vision_patch.py 2>&1
sudo mkdir -p /usr/local/lib/weavebench_computer_mcp
sudo cp -f /tmp/weavebench_computer_mcp/server.py /usr/local/lib/weavebench_computer_mcp/server.py
sudo chmod +x /usr/local/lib/weavebench_computer_mcp/server.py
${BIN} version 2>&1 | head -3 || ${BIN} --version 2>&1 | head -3
mkdir -p \$(dirname ${MARKER}) && date -u +%FT%TZ > ${MARKER}"

    VERIFY_CMD="set -e
echo '--- marker ---'; test -f ${MARKER} && echo MARKER_OK || { echo MARKER_MISSING; exit 1; }
echo '--- bin ---'; which hermes; ${BIN} version 2>&1 | head -1 || ${BIN} --version 2>&1 | head -1
echo '--- mcp import ---'; ${VENV_PY} -c 'from mcp import StdioServerParameters; print(\"MCP_IMPORT_OK\")'
echo '--- vision patch ---'; test -f /opt/hermes/.weavebench_vision_bridge_patched && echo VISION_PATCH_OK || { echo VISION_PATCH_MISSING; exit 1; }
echo '--- mcp server ---'; test -f /usr/local/lib/weavebench_computer_mcp/server.py && echo MCP_OK || { echo MCP_MISSING; exit 1; }"
    ;;
esac

# ---- Step 1: writable copy of the qcow2 (into STAGE_DIR if set) -------------
if [ ! -f "${WORK_QCOW}" ]; then
  echo "[bake] copying qcow2 to working location (several minutes)..."
  cp "${SRC_QCOW}" "${WORK_QCOW}"
fi
chmod u+rw "${WORK_QCOW}"
echo "[bake] writable copy ready: $(du -h "${WORK_QCOW}" | cut -f1)"

# ---- Step 2: boot isolated container mounting the copy as the MAIN disk -----
# CRITICAL: mount as /boot.qcow2, NOT /System.qcow2.
#   - /System.qcow2  → the image treats it as a READ-ONLY backing file and
#     boots a throwaway /boot.qcow2 overlay on top (install.sh:191). Writes
#     never reach the file we mounted, so a bake would silently vanish.
#   - /boot.qcow2    → the image's findFile() picks it up directly as the main
#     disk (-hda /boot.qcow2), so QEMU reads AND writes our file. Changes
#     persist into the qcow2. This is what makes "bake once" actually stick.
# At runtime the provider mounts this same file as /System.qcow2:ro, where it
# becomes the read-only backing — and the backing now already contains openclaw.
docker rm -f "${NAME}" >/dev/null 2>&1 || true
echo "[bake] booting bake container (qcow2 as main disk, RW)..."
docker run -d --rm --name "${NAME}" \
  --cap-add NET_ADMIN --device /dev/kvm \
  -e DISK_SIZE=32G -e RAM_SIZE=8G -e CPU_CORES=8 \
  -p ${VNC}:8006 -p ${SRV}:5000 -p ${CHR}:9222 -p ${VLC}:8080 \
  -v "${WORK_QCOW}:/boot.qcow2:rw" \
  happysixd/osworld-docker >/dev/null

wait_ready "${SRV}" "bake" || { docker logs --tail 50 "${NAME}" || true; exit 1; }

# ---- Step 3: stage assets + run the harness's own bootstrap ----------------
stage_assets
if [ -n "${BOOTSTRAP_SH}" ]; then
echo "[bake] running ${HARNESS} bootstrap inside the VM (zero-drift)..."
# Stage BOOTSTRAP_SH as a file in the VM and run it DETACHED (setsid nohup),
# then poll for the marker. Running it inline over a single /setup/execute call
# is fragile: the bootstrap restarts services (apt, packagekit) which can drop
# the VM's Flask connection mid-call, and a 6 KB script in one synchronous HTTP
# request is exactly the kind of long call that gets cut. Detached + poll makes
# each HTTP call short and immune to mid-run network blips.
BOOT_LAUNCH="$(printf 'cat > /tmp/wb_bootstrap.sh <<'\''WBEOF'\''\nexport CLIENT_PASSWORD=%q\n%s\nWBEOF\nchmod +x /tmp/wb_bootstrap.sh\nsetsid nohup bash /tmp/wb_bootstrap.sh >/tmp/wb_bootstrap.run.log 2>&1 < /dev/null &\necho LAUNCHED' "${CLIENT_PASSWORD}" "${BOOTSTRAP_SH}")"
BAKE_SRV="${SRV}" _vm rawexec "${BOOT_LAUNCH}" 120 \
  || { echo "[fatal] could not launch bootstrap"; exit 1; }

echo "[bake] bootstrap launched; polling for marker ${MARKER} (up to ~30 min)..."
BOOT_OK=0
for i in $(seq 1 180); do
  sleep 10
  OUT="$(BAKE_SRV="${SRV}" _vm rawexec "test -f ${MARKER} && echo DONE || echo WAIT; tail -1 ${BOOTLOG} 2>/dev/null" 30 2>/dev/null || echo CONNERR)"
  case "${OUT}" in
    *DONE*) echo "[bake] marker present after ~$((i*10))s"; BOOT_OK=1; break ;;
    *CONNERR*) echo "  [t=$((i*10))s] VM busy (apt/service restart) — retrying" ;;
    *) echo "  [t=$((i*10))s] ${OUT}" ;;
  esac
done

if [ "${BOOT_OK}" != "1" ]; then
  echo "[fatal] bootstrap did not complete — dumping log tail:"
  BAKE_SRV="${SRV}" _vm rawexec "tail -60 ${BOOTLOG} 2>/dev/null" 60 || true
  exit 1
fi
else
  # Short synchronous install (codex/claudecode): run INSTALL_BODY via sudo
  # shim, then the user-scope MCP add if any. No detached poll needed.
  echo "[bake] running ${HARNESS} install inside the VM (synchronous)..."
  BAKE_SRV="${SRV}" _vm exec "${INSTALL_BODY}" 300 \
    || { echo "[fatal] ${HARNESS} install failed"; exit 1; }
  if [ -n "${MCP_ADD:-}" ]; then
    echo "[bake] registering MCP with ${HARNESS} (user scope)..."
    BAKE_SRV="${SRV}" _vm rawexec "${MCP_ADD}" 120 || echo "[bake] WARN: MCP add returned non-zero (continuing)"
  fi
  BAKE_SRV="${SRV}" _vm exec "test -f ${MARKER} && echo MARKER_OK" 30 \
    || { echo "[fatal] marker ${MARKER} not written"; exit 1; }
fi

# ---- Step 3b: bake the repo's in-VM Flask server into the image ------------
# The VM's control server (/home/user/server/main.py) is part of the qcow2,
# NOT uploaded at runtime — so a fix to weavebench/desktop_env/server/main.py
# never reaches an already-built image unless we write it in here. The key fix
# is threaded=True (the stock image runs single-threaded, so a large
# /setup/upload blocks every other request and the task appears to hang).
# We upload the repo copy (multipart, no shell escaping) and mv it into place;
# on next boot the server starts threaded. The repo file is the same 26-route
# server with only the app.run line changed, so a whole-file replace is safe.
REPO_SERVER="${REPO_ROOT}/weavebench/desktop_env/server/main.py"
if [ -f "${REPO_SERVER}" ] && grep -q "threaded=True" "${REPO_SERVER}"; then
  echo "[bake] baking threaded Flask server into the image (anti-hang)..."
  BAKE_SRV="${SRV}" _vm upload "${REPO_SERVER}" /tmp/wb_server_main.py
  BAKE_SRV="${SRV}" _vm exec 'test -f /home/user/server/main.py && { sudo cp /home/user/server/main.py /home/user/server/main.py.prebake 2>/dev/null || true; sudo cp /tmp/wb_server_main.py /home/user/server/main.py && sudo chown user:user /home/user/server/main.py && echo SERVER_PATCHED; grep -n "app.run" /home/user/server/main.py; } || echo "WARN: /home/user/server/main.py not found, skipped"' 60 \
    || echo "[bake] WARN: Flask server bake failed (continuing; client-side retry still mitigates)"
fi

# ---- Step 4: shut down so QEMU flushes the qcow2 ---------------------------
# The container's PID 1 is `exec qemu-system-x86_64` (via tini). `docker stop`
# sends SIGTERM → tini → qemu; qemu's block layer flushes and closes the qcow2
# as it exits. We give it a long grace window. NOTE: do NOT trigger a guest-side
# `poweroff` first — that races the flush (the Flask channel drops instantly,
# the host thinks the guest is "down", and docker then SIGKILLs qemu mid-flush,
# losing the writes). A plain `sudo sync` to push guest page-cache to the guest
# disk, then a clean `docker stop`, is what reliably persists.
echo "[bake] sync guest page-cache, then docker stop (QEMU flush on exit)..."
BAKE_SRV="${SRV}" _vm exec 'sudo sync; sudo sync; sudo sync' 30 || true
sleep 3
echo "[bake] docker stop -t 300 (waiting for QEMU to flush + exit)..."
docker stop -t 300 "${NAME}" >/dev/null 2>&1 || true
# Confirm the container is actually gone before reading the qcow2 back.
for i in $(seq 1 30); do
  docker ps --format '{{.Names}}' | grep -qx "${NAME}" || { echo "[bake] container stopped after ~$((i*2))s"; break; }
  sleep 2
done
docker rm -f "${NAME}" >/dev/null 2>&1 || true
sync; sleep 3  # flush host's own writeback of the qcow2 to NAS

# ---- Step 5: reboot EXACTLY like the runtime does, and verify --------------
# Mount as /System.qcow2:ro — identical to what the docker provider does at
# runtime (provider.py). The baked file becomes the read-only backing under a
# fresh throwaway overlay. If openclaw shows up here, it will show up for every
# real task too. This is the test that matters.
echo "[bake] re-booting as the runtime would (/System.qcow2:ro) to verify..."
docker run -d --rm --name "${NAME}_verify" \
  --cap-add NET_ADMIN --device /dev/kvm \
  -e DISK_SIZE=32G -e RAM_SIZE=8G -e CPU_CORES=8 \
  -p $((VNC+1)):8006 -p $((SRV+1)):5000 -p $((CHR+1)):9222 -p $((VLC+1)):8080 \
  -v "${WORK_QCOW}:/System.qcow2:ro" \
  happysixd/osworld-docker >/dev/null

wait_ready "$((SRV+1))" "verify" || { docker logs --tail 50 "${NAME}_verify" || true; exit 1; }
echo "[bake] verify checks:"
if BAKE_SRV="$((SRV+1))" _vm exec "${VERIFY_CMD}" 120; then
  VERIFIED=1
else
  VERIFIED=0
fi
docker stop -t 60 "${NAME}_verify" >/dev/null 2>&1 || true
docker rm -f "${NAME}_verify" >/dev/null 2>&1 || true

if [ "${VERIFIED}" != "1" ]; then
  echo "[fatal] verification FAILED — baked qcow2 did not retain the install."
  [ "${KEEP_GOING:-0}" = "1" ] || rm -f "${WORK_QCOW}"
  exit 1
fi

# ---- Step 5b: move the finished image from STAGE_DIR to its final home ------
if [ "${WORK_QCOW}" != "${DST_QCOW}" ]; then
  echo "[bake] moving baked image to final location (${DST_QCOW})..."
  # mv across filesystems (SSD -> NAS) is a copy+unlink; do it explicitly so a
  # partial transfer never masquerades as a complete image.
  if cp "${WORK_QCOW}" "${DST_QCOW}.partial" && sync; then
    mv "${DST_QCOW}.partial" "${DST_QCOW}"
    rm -f "${WORK_QCOW}"
    echo "[bake] moved to ${DST_QCOW}; staged copy removed."
  else
    echo "[fatal] failed to move staged image to ${DST_QCOW}; it remains at ${WORK_QCOW}"
    rm -f "${DST_QCOW}.partial"
    exit 1
  fi
fi

echo ""
echo "==================================================="
echo "BAKE DONE ✓  (${HARNESS})"
echo "  baked qcow2: ${DST_QCOW} ($(du -h "${DST_QCOW}" | cut -f1))"
echo "==================================================="
BAKE_OK=1

# ---- Step 6: optional promote ----------------------------------------------
if [ "${PROMOTE:-0}" = "1" ]; then
  # Repoint the canonical image the runtime boots from. Prefer cache/vm
  # (scripts/setup.sh's download target) when it exists, else docker_vm_data.
  if [ -e "${REPO_ROOT}/cache/vm/Ubuntu.qcow2" ] || [ -d "${REPO_ROOT}/cache/vm" ]; then
    LINK="${REPO_ROOT}/cache/vm/Ubuntu.qcow2"
  else
    LINK="${REPO_ROOT}/docker_vm_data/Ubuntu.qcow2"
  fi
  mkdir -p "$(dirname "${LINK}")"
  if [ -L "${LINK}" ] || [ -f "${LINK}" ]; then
    BACKUP="${LINK}.prebake_${DATESTAMP}"
    mv "${LINK}" "${BACKUP}"
    echo "[promote] backed up previous image -> ${BACKUP}"
  fi
  ln -sf "${DST_QCOW}" "${LINK}"
  echo "[promote] ${LINK} -> ${DST_QCOW}"
  echo "[promote] runs will now boot the baked image; bootstrap becomes a no-op."
else
  echo "To use it, point the runtime at the baked image, e.g.:"
  echo "  export OSWORLD_LOCAL_QCOW2_PATH=${DST_QCOW}"
  echo "  # or re-run with PROMOTE=1 to repoint the default Ubuntu.qcow2 automatically."
fi
