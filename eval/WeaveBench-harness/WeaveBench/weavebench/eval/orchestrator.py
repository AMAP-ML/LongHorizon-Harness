"""WeaveBench tasks executed inside OSWorld VMs.

WeaveBench tasks executed inside OSWorld VMs.
  • tasks discovered under     <tasks_root>/<CAT>/*.md
  • workspace resolved under   <tasks_root>/workspace/<CAT>/<dir>
  • 8-category axis: WEB, DAV, OPS, DES, GAM, DOC, SPA, DSK (all populated)
  • Tolerates leading HTML comment block (`<!-- resources: ... -->`) before
    YAML frontmatter, and inline single-backtick `## Workspace Path` values.


"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import uuid
from datetime import datetime
from multiprocessing import Manager, Process, Queue, current_process
from pathlib import Path
from typing import List, Tuple

# Make OSWorld repo importable
# This script is now self-contained inside weavebench/.
# parents[0]=eval, parents[1]=weavebench (was previously OSWorld root)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from weavebench.desktop_env.desktop_env import DesktopEnv  # noqa: E402
from weavebench.agents._openclaw._responses import (  # noqa: E402
    OpenClawAgent,
    _vm_exec,
    _vm_launch,
    _vm_upload,
    _vm_upload_bytes,
    _vm_fetch,
    _wait_file,
)

# Default WeaveBench root = the weavebench/ folder this script lives under.
WEAVEBENCH_ROOT_DEFAULT = str(Path(__file__).resolve().parents[1])

ALL_CATEGORIES = [
    "WEB",   # Web 前端
    "DAV",   # 数据分析 & 可视化
    "OPS",   # 运维 / 调试
    "DES",   # 设计 & 图像处理
    "GAM",   # 游戏 / 交互
    "DOC",   # 文档 & 演示
    "SPA",   # 空间推理
    "DSK",   # 桌面 & 系统设置
]

# Subdir under tasks_root that holds the task layout:
#   <tasks_root>/<CAT>/<task_id>.md
#   <tasks_root>/workspace/<CAT>/<dir>/{exec,gt,tmp}
# Tasks live under <tasks_root>/<DOMAIN>/*.md in the public release.
# bench_subdir is no longer needed — the paper batches have been
# merged into the 8 canonical domains (DAV/DES/DOC/DSK/GAM/OPS/SPA/WEB).
_TASKS_SUBROOT = "."
_TASKS_SUBDIR = "."

TMP_WORKSPACE = "/tmp_workspace"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_ds_osw")


def _grow_rootfs_enabled() -> bool:
    raw = os.environ.get("WEAVEBENCH_GROW_ROOTFS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def grow_vm_rootfs_if_needed(env, client_password: str, task_id: str,
                             output_dir: Path | None = None) -> None:
    """Expand the Docker VM's root partition to the qcow2's unused tail space.

    The shared Ubuntu.qcow2 is mounted read-only by the Docker provider, so this
    only changes the throwaway overlay of the current VM boot. We guard on the
    provider and root-device layout before touching the partition table.
    """
    log_path = output_dir / "vm_rootfs_grow.log" if output_dir else None

    def _log(message: str) -> None:
        logger.info("[%s] %s", task_id, message)
        if log_path is not None:
            try:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{datetime.utcnow().isoformat()} {message}\n")
            except Exception:  # noqa: BLE001
                logger.debug("[%s] failed to append vm_rootfs_grow.log", task_id,
                             exc_info=True)

    if not _grow_rootfs_enabled():
        _log("rootfs grow disabled by WEAVEBENCH_GROW_ROOTFS")
        return

    provider_name = getattr(env, "provider_name", "")
    if provider_name != "docker":
        _log(f"skip rootfs grow: provider={provider_name!r}")
        return

    pw = shlex.quote(client_password)
    script = f"""
set -u
echo "=== before ==="
df -h /
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT /dev/sda || true

root_src="$(findmnt -n -o SOURCE / || true)"
root_real="$(readlink -f "$root_src" 2>/dev/null || echo "$root_src")"
if [ "$root_src" != "/dev/sda3" ] && [ "$root_real" != "/dev/sda3" ]; then
  echo "SKIP root source is $root_src ($root_real), expected /dev/sda3"
  exit 0
fi

if lsblk -nr -o NAME /dev/sda 2>/dev/null | grep -qx 'sda4'; then
  echo "SKIP /dev/sda4 exists; not growing /dev/sda3 across another partition"
  exit 0
fi

if ! command -v sfdisk >/dev/null 2>&1 || ! command -v resize2fs >/dev/null 2>&1; then
  echo "SKIP missing sfdisk or resize2fs"
  exit 0
fi

disk_sectors="$(cat /sys/block/sda/size 2>/dev/null || echo 0)"
part_sectors="$(cat /sys/class/block/sda3/size 2>/dev/null || echo 0)"
tail_b=$(((disk_sectors - part_sectors) * 512))
echo "disk_sectors=$disk_sectors part_sectors=$part_sectors disk_minus_part_bytes=$tail_b"
if [ "$tail_b" -lt $((2 * 1024 * 1024 * 1024)) ]; then
  echo "SKIP less than 2GiB apparent tail space"
  exit 0
fi

printf '%s\\n' {pw} | sudo -S -p '' bash -lc 'set -e;
  echo ", +" | sfdisk --no-reread -N 3 /dev/sda;
  partprobe /dev/sda >/dev/null 2>&1 || true;
  resize2fs /dev/sda3'

echo "=== after ==="
df -h /
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT /dev/sda || true
"""

    try:
        result = _vm_exec(env, ["bash", "-lc", script], shell=False, timeout=120)
        output = (result or {}).get("output", "") or ""
        error = (result or {}).get("error", "") or ""
        rc = (result or {}).get("returncode", 0)
        if log_path is not None:
            log_path.write_text(
                f"returncode={rc}\n\nSTDOUT:\n{output}\n\nSTDERR:\n{error}\n",
                encoding="utf-8",
            )
        if rc == 0:
            after = ""
            for line in output.splitlines():
                if line.startswith("/dev/"):
                    after = line.strip()
            suffix = f" -> {after}" if after else ""
            _log(f"rootfs grow check complete{suffix}")
        else:
            _log(f"rootfs grow command returned rc={rc}; continuing")
    except Exception as exc:  # noqa: BLE001
        _log(f"rootfs grow failed ({exc}); continuing")


# WeaveBench import — add its root to sys.path on demand
def _import_tasks_root(tasks_root: Path):
    sp = str(tasks_root)
    if sp not in sys.path:
        sys.path.insert(0, sp)
    from weavebench.utils.task_parser import parse_task_md  # noqa: E402
    return parse_task_md


def _normalise_task_md(raw: str) -> str:
    """Strip leading `<!-- ... -->` comment blocks and convert single-backtick
    `## Workspace Path` blocks into triple-backtick code fences so that the
    upstream parse_task_md can consume them.
    """
    text = raw.lstrip("\ufeff")
    while True:
        text = text.lstrip()
        m = re.match(r"<!--.*?-->\s*", text, re.DOTALL)
        if not m:
            break
        text = text[m.end():]

    # Convert "## Workspace Path\n\n`some/path`\n" -> fenced code block
    def _fix_ws(m: re.Match) -> str:
        path = m.group(1).strip()
        return f"## Workspace Path\n\n```\n{path}\n```\n"

    text = re.sub(
        r"## Workspace Path\s*\n+`([^`\n]+)`\s*\n",
        _fix_ws,
        text,
    )
    return text


def discover_tasks(tasks_root: Path, categories: List[str]) -> List[dict]:
    """Parse every task .md under tasks_root/<CAT>/*.md.

    Like the tasks variant, but rooted in the task subtree and
    tolerant of the resources HTML comment/inline-backtick workspace path
    conventions used by task files.
    """
    parse_task_md = _import_tasks_root(tasks_root)
    tasks: List[dict] = []
    ds_root = tasks_root / _TASKS_SUBROOT / _TASKS_SUBDIR
    if not ds_root.is_dir():
        logger.error("task root missing: %s", ds_root)
        return tasks
    with tempfile.TemporaryDirectory(prefix="weavebench_md_") as _tmp_root_str:
        tmp_root = Path(_tmp_root_str)
        for cat in categories:
            cat_dir = ds_root / cat
            if not cat_dir.is_dir():
                logger.warning("Category dir missing: %s", cat_dir)
                continue
            tmp_cat = tmp_root / cat
            tmp_cat.mkdir(parents=True, exist_ok=True)
            for md in sorted(cat_dir.glob("*.md")):
                try:
                    norm = _normalise_task_md(md.read_text(encoding="utf-8"))
                    tmp_md = tmp_cat / md.name
                    tmp_md.write_text(norm, encoding="utf-8")
                    task = parse_task_md(tmp_md)
                except Exception as exc:
                    logger.warning("parse_task_md failed for %s: %s", md, exc)
                    continue
                # Preserve original .md file path for reference
                task["file_path"] = str(md.resolve())
                task["category"] = cat
                tid = task["task_id"]
                prefix = f"{cat}_"
                dir_name = tid[len(prefix):] if tid.startswith(prefix) else tid
                new_ws = (ds_root / "workspace" / cat / dir_name).resolve()
                if new_ws.is_dir():
                    task["workspace_path"] = str(new_ws)
                else:
                    logger.warning(
                        "task workspace missing for %s/%s (expected %s) - "
                        "keeping md value %s", cat, tid, new_ws,
                        task["workspace_path"],
                    )
                tasks.append(task)
    return tasks


def already_done(task: dict, mode: str, model: str, result_root: Path) -> bool:
    """A task is 'done' only if score.json exists AND has no terminal error
    AND has a numeric score field. Failed runs are eligible for retry.

    Layout-tolerant: tries both:
      - <result_root>/<mode>/<model>/<cat>/<task>/score.json    (flat layout)
      - <result_root>/<subdir>/<mode>/<model>/<cat>/<task>/score.json
        (legacy multi-batch layout — extra batch dir
         level inserted between result_root and mode)
    """
    cat = task["category"]
    tid = task["task_id"]

    candidates = [result_root / mode / model / cat / tid / "score.json"]
    # Also probe nested subdir layouts.
    for sub in result_root.iterdir() if result_root.exists() else []:
        if not sub.is_dir():
            continue
        candidates.append(sub / mode / model / cat / tid / "score.json")

    score_path = next((p for p in candidates if p.exists()), None)
    if score_path is None:
        return False
    try:
        rec = json.loads(score_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if rec.get("error"):
        return False
    if not isinstance(rec.get("score"), (int, float)):
        return False
    scores = rec.get("scores")
    if isinstance(scores, dict) and "error" in scores:
        return False
    return True


# ---------------------------------------------------------------------------
# VM-side setup helpers (use OSWorld REST, not docker exec)
# ---------------------------------------------------------------------------
def _sudo_mkdir(env, paths: List[str], owner: str = "user", password: str = "password") -> None:
    """Create directories with sudo (since VM REST runs as `user`), then chown."""
    quoted = " ".join(paths)
    cmd = (
        f"echo '{password}' | sudo -S -p '' bash -c "
        f"'mkdir -p {quoted} && chown -R {owner}:{owner} {quoted} && chmod -R u+w {quoted}'"
    )
    out = _vm_exec(env, ["bash", "-c", cmd], timeout=30)
    if out.get("returncode") not in (0, None):
        raise RuntimeError(f"sudo mkdir failed for {paths}: {out}")


def _sudo_rmrf(env, path: str, password: str = "password") -> None:
    cmd = f"echo '{password}' | sudo -S -p '' rm -rf {path}"
    _vm_exec(env, ["bash", "-c", cmd], timeout=60)


def upload_workspace(env, workspace_path: str, remote_root: str = TMP_WORKSPACE,
                     client_password: str = "password") -> None:
    """Mirror WildClaw's workspace layout into the VM (REST upload, no docker exec).

    WildClaw native (docker_utils.start_container + setup_workspace):
      • `workspace_path/exec/.` → `/tmp_workspace/.`     (the agent's working dir)
      • `workspace_path/tmp/.`  → `/tmp_workspace/tmp/.` (auxiliary fixtures)

    NOTE: `gt/` is intentionally NOT staged here — WildClaw native copies it
    only inside `grade_the_task`, AFTER the agent finishes, so the agent
    cannot read ground-truth answers. We mirror that via `stage_gt_for_grading`.
    """
    wp = Path(workspace_path)
    if not wp.is_dir():
        logger.warning("Workspace path missing on host: %s", wp)
        return
    # Need sudo: VM REST runs as `user`, /tmp/* is owned by root.
    _sudo_rmrf(env, remote_root, password=client_password)
    _sudo_mkdir(env, [remote_root, f"{remote_root}/tmp"], password=client_password)

    LARGE_FILE_BYTES = 256 * 1024 * 1024  # >256MB → upload raw, skip tar

    def _push(src_dir: Path, dest_dir: str) -> None:
        if not src_dir.is_dir():
            return
        large_files: list[Path] = []
        small_children: list[Path] = []
        for child in sorted(src_dir.iterdir()):
            if child.is_file() and child.stat().st_size > LARGE_FILE_BYTES:
                large_files.append(child)
            else:
                small_children.append(child)

        _vm_exec(env, ["bash", "-c", f"mkdir -p {dest_dir} && chmod -R u+w {dest_dir} || true"],
                 timeout=60)

        if small_children:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
                tar_local = Path(tf.name)
            try:
                with tarfile.open(tar_local, "w:gz") as tar:
                    for child in small_children:
                        tar.add(child, arcname=child.name)
                remote_tar = f"/tmp/weavebench_{src_dir.name}_{uuid.uuid4().hex[:6]}.tar.gz"
                _vm_upload_bytes(env, tar_local, remote_tar)
                out = _vm_exec(env, ["bash", "-c",
                                     f"cd {dest_dir} && tar xzf {remote_tar} "
                                     f"&& rm -f {remote_tar} && echo UNPACKED"],
                               timeout=300)
                if "UNPACKED" not in (out.get("output") or ""):
                    raise RuntimeError(f"Workspace unpack failed for {src_dir}: {out}")
            finally:
                tar_local.unlink(missing_ok=True)

        for big in large_files:
            remote_path = f"{dest_dir}/{big.name}"
            size = big.stat().st_size
            logger.info("Uploading large file %s (%.1f MB) chunked -> %s",
                        big.name, size / 1024 / 1024, remote_path)
            chunk_size = 128 * 1024 * 1024  # 128 MB chunks (server can handle)
            chunk_dir_remote = f"/tmp/weavebench_chunks_{uuid.uuid4().hex[:8]}"
            _vm_exec(env, ["bash", "-c", f"mkdir -p {chunk_dir_remote}"], timeout=30)
            chunk_idx = 0
            with open(big, "rb") as fh:
                while True:
                    buf = fh.read(chunk_size)
                    if not buf:
                        break
                    with tempfile.NamedTemporaryFile(suffix=".chunk", delete=False) as ctf:
                        ctf.write(buf)
                        chunk_local = Path(ctf.name)
                    try:
                        chunk_remote = f"{chunk_dir_remote}/part_{chunk_idx:04d}"
                        _vm_upload_bytes(env, chunk_local, chunk_remote, timeout=600)
                        logger.info("  chunk %d (%.1f MB) uploaded", chunk_idx, len(buf) / 1024 / 1024)
                    finally:
                        chunk_local.unlink(missing_ok=True)
                    chunk_idx += 1
            out = _vm_exec(env, ["bash", "-c",
                                 f"cat {chunk_dir_remote}/part_* > {remote_path} "
                                 f"&& rm -rf {chunk_dir_remote} "
                                 f"&& stat -c %s {remote_path}"],
                           timeout=300)
            actual = (out.get("output") or "").strip().split()[-1] if out else ""
            if str(actual) != str(size):
                raise RuntimeError(f"Reassembled size mismatch for {big.name}: got {actual} expected {size}")
            logger.info("  reassembled %s = %s bytes OK", remote_path, actual)
        if large_files:
            _vm_exec(env, ["bash", "-c", f"chmod -R u+w {dest_dir} || true"], timeout=60)

    _push(wp / "exec", remote_root)
    _push(wp / "tmp",  f"{remote_root}/tmp")


def stage_gt_for_grading(env, workspace_path: str,
                         remote_root: str = TMP_WORKSPACE,
                         client_password: str = "password") -> None:
    """Copy `workspace_path/gt/` → /tmp_workspace/gt INSIDE the VM, only after
    the agent has finished. Mirrors WildClaw run_batch.grade_the_task:67-77."""
    gt = Path(workspace_path) / "gt"
    if not gt.is_dir():
        return
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
        tar_local = Path(tf.name)
    try:
        with tarfile.open(tar_local, "w:gz") as tar:
            for child in sorted(gt.iterdir()):
                tar.add(child, arcname=child.name)
        remote_tar = f"/tmp/weavebench_gt_{uuid.uuid4().hex[:6]}.tar.gz"
        _vm_upload_bytes(env, tar_local, remote_tar)
        _sudo_mkdir(env, [f"{remote_root}/gt"], password=client_password)
        cmd = (
            f"echo '{client_password}' | sudo -S -p '' bash -c "
            f"'cd {remote_root}/gt && tar xzf {remote_tar} && rm -f {remote_tar} "
            f"&& chown -R user:user {remote_root}/gt'"
        )
        _vm_exec(env, ["bash", "-c", cmd], timeout=120)
    finally:
        tar_local.unlink(missing_ok=True)


def upload_skills(env, skills: str, skills_path: str,
                  remote_skills_dir: str = "/home/user/.openclaw/skills") -> None:
    """Tar each requested skill directory and unpack into the user's openclaw
    skills dir inside the VM. Note: WildClaw native uses /root/skills (their
    container runs as root); inside OSWorld VM openclaw runs as `user`, so
    skills go under /home/user/.openclaw/skills (matches openclaw's own
    bootstrap convention; see openclaw_agent.py line 134/317)."""
    if not skills.strip():
        return
    requested = [s.strip() for s in skills.splitlines() if s.strip()]
    if not requested:
        return
    sp = Path(skills_path)
    found = [name for name in requested if (sp / name).is_dir()]
    if not found:
        logger.warning("No requested skills found under %s: %s", sp, requested)
        return
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
        tar_local = Path(tf.name)
    try:
        with tarfile.open(tar_local, "w:gz") as tar:
            for name in found:
                tar.add(sp / name, arcname=name)
        _vm_exec(env, ["bash", "-c", f"mkdir -p {remote_skills_dir}"])
        _vm_upload_bytes(env, tar_local, "/tmp/weavebench_skills.tar.gz")
        _vm_exec(env, ["bash", "-c",
                       f"cd {remote_skills_dir} && tar xzf /tmp/weavebench_skills.tar.gz"],
                 timeout=180)
    finally:
        tar_local.unlink(missing_ok=True)


def write_task_env_script(env, env_block: str, http_proxy: str = "") -> None:
    """Convert ## Env block (one VAR per line, name only) into a sourced script.

    Values come from the host process environment; matches WildClaw's
    docker_utils.start_container which forwards `os.environ.get(key, "")`.

    If `http_proxy` is set, also exports http_proxy/https_proxy/HTTP_PROXY/
    HTTPS_PROXY/no_proxy so warmup commands and the agent can reach blocked
    sites (e.g. Wikipedia / Google) through the host's proxy. Local addresses
    (localhost, 127.0.0.0/8, the OSWorld VM itself) are kept direct via
    no_proxy so the openclaw gateway / VM REST loopback isn't accidentally
    proxied.

    Critical (2026-05-11): the no_proxy list MUST contain literal docker
    bridge gateway IPs (172.17.0.1, 172.18.0.1, …), NOT just the CIDR
    `172.16.0.0/12`. httpx/openai-python honour no_proxy by hostname /
    suffix match only — they do not parse CIDRs. Without literal IPs, the
    agent's OpenAI SDK call to e.g. `http://172.17.0.1:4022/v1` is sent
    via the squid/clash on `${http_proxy}`, which (correctly) refuses to
    forward to docker-internal IPs and the request times out. Symptom is
    a chat.jsonl full of `errorMessage: "Connection error."` and
    `usage: 0/0` (observed in the 2026-05-11 nano-CLI run where every
    one of 43 tasks died with 4× retry timeout, 0 toolCalls).
    """
    lines = ["#!/bin/bash", "# task env, auto-generated"]
    if http_proxy:
        # Literal docker bridge IPs come first (httpx no_proxy = suffix match,
        # not CIDR). Add IPv4 loopback + the 172.16/12 + 10/8 + 192.168/16
        # blocks afterwards as documentation, even though SDKs ignore them.
        no_proxy = ",".join([
            "localhost", "127.0.0.1", "::1",
            "172.17.0.1", "172.18.0.1", "172.19.0.1", "172.20.0.1",
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "20.20.20.0/24",
        ])
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            lines.append(f"export {k}='{http_proxy}'")
        lines.append(f"export no_proxy='{no_proxy}'")
        lines.append(f"export NO_PROXY='{no_proxy}'")
    for raw in (env_block or "").splitlines():
        key = raw.strip()
        if not key or key.startswith("#"):
            continue
        # KEY=VALUE form: passed through verbatim.
        if "=" in key:
            kpart = key.split("=", 1)[0].strip()
            # Sanity: only emit `export` if the LHS is a valid shell identifier.
            # Otherwise the line is a yaml/markdown listing (e.g. "apt: pkg1
            # pkg2"), not an env directive — skip silently.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", kpart):
                continue
            lines.append(f"export {key}")
            continue
        # Bare token: only export if it is a valid shell identifier (so
        # yaml-style "apt:" / "pip:" / freeform descriptions are skipped).
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        val = os.environ.get(key, "")
        safe = val.replace("'", "'\\''")
        lines.append(f"export {key}='{safe}'")
    payload = "\n".join(lines) + "\n"
    _vm_upload(env, payload, "/tmp/openclaw_task_env.sh")
    _vm_exec(env, ["bash", "-c", "chmod +x /tmp/openclaw_task_env.sh"])

    # Tell apt to bypass the proxy. Tunneling apt through host clash often
    # returns transient 502s for archive.ubuntu.com (clash race forwarding
    # plain http to upstream); ubuntu mirrors are reachable directly from
    # CN, so a direct route is faster and reliable. pip / curl / git keep
    # using the proxy so they can reach pypi.org / github.com.
    #
    # Symptom this fixes: warmup `set -e` aborts at apt-get update on a
    # 502 → pip install never runs → dbt-core / pillow missing → DAV/DOC
    # tasks fail at agent step. (Observed on DAV_task_11 2026-05-11 with
    # WEAVEBENCH_VM_HTTP_PROXY=http://172.17.0.1:7890.)
    if http_proxy:
        apt_conf = (
            'Acquire::http::Proxy "false";\n'
            'Acquire::https::Proxy "false";\n'
        )
        _vm_upload(env, apt_conf, "/tmp/_apt_no_proxy.conf")
        _vm_exec(env, ["bash", "-c",
                       "echo 'password' | sudo -S -p '' "
                       "install -m 644 /tmp/_apt_no_proxy.conf "
                       "/etc/apt/apt.conf.d/99-no-proxy 2>/dev/null || true"])

    _fix_google_chrome_apt_key(env, http_proxy=http_proxy)


def _fix_google_chrome_apt_key(env, http_proxy: str = "",
                               client_password: str = "password") -> None:
    """Repair the stale Google Chrome apt signing key inside the VM.

    The published Ubuntu.qcow2 ships
    ``/etc/apt/sources.list.d/google-chrome.list`` pointing at
    ``https://dl.google.com/linux/chrome/deb stable``, but Google has since
    rotated its signing key. Every ``apt-get update`` then fails with
    ``NO_PUBKEY FD533C07C264648F`` / ``repository ... is not signed``, and any
    warmup running under ``set -e`` aborts there (reported for
    ``DAV_task_10_polars_streamlit_xfilter``). Chrome is already pre-installed
    in the image, so this online source is only needed for in-VM upgrades.

    Strategy (idempotent, never fatal):

    1. Fetch Google's current signing key into a dedicated keyring and rewrite
       the ``.list`` with ``signed-by=`` (the modern, non-deprecated form).
       Needs network — routed via ``http_proxy`` when set, since
       ``dl.google.com`` is geo-blocked from some regions otherwise.
    2. If the key refresh fails (offline / proxy down), disable the source so
       ``apt-get update`` stops erroring on it.

    A sentinel file makes re-entry on the same VM boot a no-op. All failures
    are swallowed so this never blocks task startup.
    """
    proxy_export = (
        f"export https_proxy='{http_proxy}' http_proxy='{http_proxy}'; "
        if http_proxy else ""
    )
    script = (
        "set -u\n"
        "SENT=/tmp/_weavebench_chrome_key_fixed\n"
        "[ -f \"$SENT\" ] && exit 0\n"
        "LIST=/etc/apt/sources.list.d/google-chrome.list\n"
        "[ -f \"$LIST\" ] || { touch \"$SENT\"; exit 0; }\n"
        "KEYRING=/usr/share/keyrings/google-chrome.gpg\n"
        f"{proxy_export}"
        "if curl -fsSL --max-time 30 "
        "https://dl.google.com/linux/linux_signing_key.pub "
        "| gpg --dearmor -o \"$KEYRING\" 2>/dev/null && [ -s \"$KEYRING\" ]; then\n"
        "  echo \"deb [arch=amd64 signed-by=$KEYRING] "
        "https://dl.google.com/linux/chrome/deb/ stable main\" > \"$LIST\"\n"
        "  echo weavebench-chrome-key-refreshed\n"
        "else\n"
        "  mv -f \"$LIST\" \"${LIST}.disabled\" 2>/dev/null || true\n"
        "  echo weavebench-chrome-source-disabled\n"
        "fi\n"
        "touch \"$SENT\"\n"
    )
    # VM I/O is best-effort: a transport error here must never crash task
    # startup (the in-VM step is already guarded by `|| true`).
    try:
        _vm_upload(env, script, "/tmp/_weavebench_fix_chrome_key.sh")
        _vm_exec(env, ["bash", "-c",
                       "echo '%s' | sudo -S -p '' bash "
                       "/tmp/_weavebench_fix_chrome_key.sh 2>/dev/null || true"
                       % client_password],
                 timeout=60)
    except Exception as exc:  # noqa: BLE001 — never block startup on this fix
        logger.warning("google-chrome apt-key fix skipped (%s)", exc)


def _inject_warmup_cache(env, client_password: str = "password") -> None:
    """Idempotently upload + unpack host-side apt/pip/npm cache into VM.

    Reads $WEAVEBENCH_WARMUP_CACHE_TAR (typically weavebench/assets/weavebench_warmup_cache_v1.tar.gz,
    ~1.2 GB) and pushes it to /tmp/weavebench_warmup_cache.tar.gz, then:
      - extracts apt .deb files to /var/cache/apt/archives/ (apt finds local
        on `apt install X`)
      - configures pip default index to file:///tmp/weavebench_warmup_cache/pip
        (pip install X uses local wheels first)
      - npm cache add for each tgz under /tmp/weavebench_warmup_cache/npm

    Writes /tmp/_weavebench_warmup_cache_injected sentinel so re-entry no-ops.
    No-op when env var unset (existing behaviour preserved).
    """
    tar_local = os.environ.get("WEAVEBENCH_WARMUP_CACHE_TAR", "").strip()
    if not tar_local:
        return
    if not os.path.isfile(tar_local):
        logger.warning("WEAVEBENCH_WARMUP_CACHE_TAR=%s not a file — skipping inject", tar_local)
        return

    # Idempotent: skip if already done for this VM boot
    chk = _vm_exec(env, ["bash", "-c", "test -f /tmp/_weavebench_warmup_cache_injected"])
    if chk.get("returncode") == 0:
        return

    logger.info("Injecting warmup cache (%.0f MB) into VM...",
                os.path.getsize(tar_local) / 1024 / 1024)
    _vm_upload_bytes(env, Path(tar_local), "/tmp/weavebench_warmup_cache.tar.gz",
                     timeout=600)

    # Upload the inject script as a standalone file (avoids brittle nested
    # bash quoting). Script runs as `user`, escalates via sudo where needed.
    # CRITICAL: minimal in-VM work — only file copies. We deliberately do NOT
    # run `apt-get update` or build a local apt repo, because those eat
    # several hundred MB of RAM and OOM the VM Flask server on 8G RAM VMs.
    # apt will find our .deb files in /var/cache/apt/archives/ on next
    # `apt install X` (matched by version against the existing stale apt
    # index that was baked into the qcow2).
    inject_sh = r"""#!/bin/bash
# wcb warmup cache inject helper. Run inside the VM as `user`.
# Requires CLIENT_PASSWORD env var (set by caller via sudo -S).
set -u
echo "[weavebench-inject] starting at $(date)"
mkdir -p /tmp/weavebench_warmup_cache
tar xzf /tmp/weavebench_warmup_cache.tar.gz -C /tmp/weavebench_warmup_cache/ 2>&1 | tail -3
# Free the big tarball immediately — we've extracted it.
rm -f /tmp/weavebench_warmup_cache.tar.gz
N_DEB=$(ls /tmp/weavebench_warmup_cache/apt/*.deb 2>/dev/null | wc -l)
N_PIP=$(ls /tmp/weavebench_warmup_cache/pip/ 2>/dev/null | wc -l)
N_NPM=$(ls /tmp/weavebench_warmup_cache/npm/*.tgz 2>/dev/null | wc -l)
echo "[weavebench-inject] unpacked: apt=$N_DEB pip=$N_PIP npm=$N_NPM"

# 1. Stage .deb files into apt archives — `apt install X` looks here BEFORE
#    fetching from network. We MOVE (not copy) to avoid keeping duplicate
#    copies in /tmp (which is tmpfs and tight on RAM-constrained VMs).
echo "$CLIENT_PASSWORD" | sudo -S -p '' bash -c '
  set -u
  mv /tmp/weavebench_warmup_cache/apt/*.deb /var/cache/apt/archives/ 2>/dev/null || true
  rmdir /tmp/weavebench_warmup_cache/apt 2>/dev/null || true
'

# 2. Configure pip to look at the local wheel dir first, but still allow
#    PyPI fallback for anything not cached.
echo "$CLIENT_PASSWORD" | sudo -S -p '' bash -c '
  set -u
  mkdir -p /root/.pip /home/user/.pip
  cat > /root/.pip/pip.conf <<PIP_EOF
[global]
no-index = false
find-links = file:///tmp/weavebench_warmup_cache/pip
index-url = https://pypi.org/simple
timeout = 30
PIP_EOF
  cp /root/.pip/pip.conf /home/user/.pip/pip.conf
  chown user:user /home/user/.pip/pip.conf
'

# 3. Pre-seed npm offline cache so `npm install -g X` finds the tgz locally.
echo "$CLIENT_PASSWORD" | sudo -S -p '' bash -c '
  for tgz in /tmp/weavebench_warmup_cache/npm/*.tgz; do
    [ -f "$tgz" ] && npm cache add "$tgz" 2>&1 | tail -1 || true
  done
' || true

# Final cleanup: keep only pip wheels in /tmp/weavebench_warmup_cache (~600 MB).
# npm cache was already absorbed by `npm cache add`.
rm -rf /tmp/weavebench_warmup_cache/npm 2>/dev/null || true

DF_AFTER=$(df -h /tmp | awk 'NR==2{print $4 " free"}')
echo "[weavebench-inject] /tmp ${DF_AFTER}"
touch /tmp/_weavebench_warmup_cache_injected
echo "[weavebench-inject] DONE at $(date)"
"""
    _vm_upload(env, inject_sh, "/tmp/_wcb_inject.sh")
    _vm_exec(env, ["bash", "-c", "chmod +x /tmp/_wcb_inject.sh"])
    out = _vm_exec(env,
                   ["bash", "-c",
                    f"CLIENT_PASSWORD='{client_password}' bash /tmp/_wcb_inject.sh 2>&1"],
                   timeout=300)
    logger.info("warmup_cache inject output: %s",
                (out.get("output") or "")[-800:].strip())


def run_warmup(env, warmup: str, env_script: str = "/tmp/openclaw_task_env.sh",
               per_cmd_timeout: int = 1800,
               client_password: str = "password") -> None:
    """Run warmup commands inside VM, one per /setup/launch (no 120s ceiling).

    WildClaw native runs warmup as root inside docker. The OSWorld VM REST
    runs as user `user`, so we wrap each warmup command in `sudo -S bash -c
    '...'` to match WildClaw semantics (so `apt install ...`, writes under
    /opt, etc. all work). Stdout/stderr go to /tmp/_warmup.log; rc to
    /tmp/_warmup.rc; completion sentinel /tmp/_warmup.done. A nonzero rc
    raises (matches WildClaw native fail-fast).
    """
    # Inject host-side apt/pip/npm cache BEFORE running warmup so `apt install`
    # / `pip install` / `npm install` find local files instead of hitting
    # archive.ubuntu.com / pypi.org / registry.npm. No-op when
    # WEAVEBENCH_WARMUP_CACHE_TAR env var is unset.
    try:
        _inject_warmup_cache(env, client_password=client_password)
    except Exception as e:
        logger.warning("warmup cache inject failed (non-fatal): %s", e)

    # Env override so retry launchers can extend warmup ceiling for heavy
    # apt-install tasks (colmap/meshlab/blender) on contended networks.
    try:
        _env_to = int(os.environ.get("WEAVEBENCH_WARMUP_TIMEOUT", "") or 0)
        if _env_to > 0:
            per_cmd_timeout = _env_to
    except ValueError:
        pass
    if not warmup or not warmup.strip():
        return
    # Run the entire Warmup block as a single bash script so multi-line
    # constructs (`if/then/fi`, `for/done`, heredocs, `\` continuations)
    # work as a normal bash file would. We still substitute legacy
    # WildClaw conda paths with system python/pip.
    script = warmup
    for prefix in ("~/miniconda3/envs/eval/bin/pip",
                   "/root/miniconda3/envs/eval/bin/pip"):
        script = script.replace(prefix, "pip3")
    for prefix in ("~/miniconda3/envs/eval/bin/python",
                   "/root/miniconda3/envs/eval/bin/python"):
        script = script.replace(prefix, "python3")

    logger.info("Running warmup (%d bytes) in VM as one bash script",
                len(script))

    _vm_exec(env, ["bash", "-c",
                   "rm -f /tmp/_warmup.done /tmp/_warmup.rc /tmp/_warmup.log /tmp/_warmup.sh"])
    # Upload the warmup body as a real bash script. `set -e` makes the
    # script abort on first failing command (matching WildClaw fail-fast).
    # Prepend an apt-lock wait: fresh-booted VMs run `packagekitd` which
    # holds /var/lib/apt/lists/lock for the first ~30-90s, racing any
    # `apt install` in the warmup. Wait up to 120s for lock release.
    apt_lock_wait = (
        "# wait for apt lock release (packagekitd auto-update on fresh boot)\n"
        "for _i in $(seq 1 60); do\n"
        "  if ! fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; then\n"
        "    break\n"
        "  fi\n"
        "  if [ \"$_i\" = \"1\" ]; then echo 'WeaveBench: waiting for apt lock (packagekitd)...' >&2; fi\n"
        "  sleep 2\n"
        "done\n"
    )
    full_script = "#!/bin/bash\nset -e\n" + apt_lock_wait + script + "\n"
    _vm_upload(env, full_script, "/tmp/_warmup.sh")
    _vm_exec(env, ["bash", "-c", "chmod +x /tmp/_warmup.sh"])

    # Wrap: source the per-task env, cd to workspace, run the script as root.
    wrapped = (
        f"( "
        f"if [ -f {env_script} ]; then set -a; . {env_script}; set +a; fi; "
        f"cd {TMP_WORKSPACE}; "
        f"echo '{client_password}' | sudo -S -p '' "
        f"  env DEBIAN_FRONTEND=noninteractive PATH=\"$PATH\" HOME=/root "
        f"  http_proxy=\"${{http_proxy:-}}\" https_proxy=\"${{https_proxy:-}}\" "
        f"  HTTP_PROXY=\"${{HTTP_PROXY:-}}\" HTTPS_PROXY=\"${{HTTPS_PROXY:-}}\" "
        f"  no_proxy=\"${{no_proxy:-}}\" NO_PROXY=\"${{NO_PROXY:-}}\" "
        f"  bash /tmp/_warmup.sh; "
        f"echo $? >/tmp/_warmup.rc "
        f") >/tmp/_warmup.log 2>&1; touch /tmp/_warmup.done"
    )
    _vm_launch(env, ["bash", "-c", wrapped])
    ok = _wait_file(env, "/tmp/_warmup.done", timeout=per_cmd_timeout)
    if not ok:
        raise RuntimeError("Warmup did not complete within timeout")
    rc_out = _vm_exec(env, ["bash", "-c", "cat /tmp/_warmup.rc"])
    rc_str = (rc_out.get("output") if isinstance(rc_out, dict) else rc_out) or "0"
    try:
        rc = int(rc_str.strip())
    except ValueError:
        rc = -1
    if rc != 0:
        log_out = _vm_exec(env, ["bash", "-c",
                                 "tail -n 80 /tmp/_warmup.log 2>/dev/null"])
        log_text = (log_out.get("output") if isinstance(log_out, dict) else log_out) or ""
        snippet = (script[:200] + "...") if len(script) > 200 else script
        raise RuntimeError(
            f"Warmup script failed (rc={rc}). First 200 chars of script:\n"
            f"{snippet}\n--- last 80 log lines ---\n{log_text}"
        )
    logger.info("warmup ok | %d-byte script", len(script))
    # Done. Skip the legacy per-line loop below.
    return
    if not cmds:
        return
    logger.info("Running warmup (%d commands) in VM", len(cmds))
    for idx, cmd in enumerate(cmds):
        _vm_exec(env, ["bash", "-c",
                       "rm -f /tmp/_warmup.done /tmp/_warmup.rc /tmp/_warmup.log"])
        # WildClaw native warmups assume root + a conda env at
        # /root/miniconda3/envs/eval. Our VM has neither — but the agent
        # uses the system python3. Substitute the conda pip path with
        # system pip3 so packages land where the agent will import them.
        cmd_eff = cmd
        for prefix in ("~/miniconda3/envs/eval/bin/pip",
                       "/root/miniconda3/envs/eval/bin/pip"):
            cmd_eff = cmd_eff.replace(prefix, "pip3")
        for prefix in ("~/miniconda3/envs/eval/bin/python",
                       "/root/miniconda3/envs/eval/bin/python"):
            cmd_eff = cmd_eff.replace(prefix, "python3")
        if cmd_eff != cmd:
            logger.info("warmup rewrote conda path: %s -> %s",
                        cmd[:60], cmd_eff[:60])
        # double-quote the user command for embedding inside `sudo bash -c "..."`
        # which is itself inside a single-quoted outer block.
        safe_dq = cmd_eff.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
        # Source env, cd workspace, run via sudo so WildClaw's root-assumed
        # warmups (apt install, /opt writes, etc.) work in the user-mode VM.
        # DEBIAN_FRONTEND=noninteractive prevents apt prompts from hanging.
        wrapped = (
            f"( "
            f"if [ -f {env_script} ]; then set -a; . {env_script}; set +a; fi; "
            f"cd {TMP_WORKSPACE}; "
            f"echo '{client_password}' | sudo -S -p '' "
            f"  env DEBIAN_FRONTEND=noninteractive PATH=\"$PATH\" HOME=/root "
            f"  bash -c \"cd {TMP_WORKSPACE} && {safe_dq}\"; "
            f"echo $? >/tmp/_warmup.rc "
            f") >/tmp/_warmup.log 2>&1; touch /tmp/_warmup.done"
        )
        _vm_launch(env, ["bash", "-c", wrapped])
        ok = _wait_file(env, "/tmp/_warmup.done", timeout=per_cmd_timeout)
        if not ok:
            raise RuntimeError(f"Warmup timed out (>{per_cmd_timeout}s): {cmd!r}")
        rc_out = _vm_exec(env, ["bash", "-c", "cat /tmp/_warmup.rc"])
        rc_str = (rc_out.get("output") or "").strip() or "?"
        try:
            rc = int(rc_str)
        except ValueError:
            rc = -1
        if rc != 0:
            log_out = _vm_exec(env, ["bash", "-c",
                                     "tail -n 50 /tmp/_warmup.log 2>/dev/null || true"])
            raise RuntimeError(
                f"Warmup command failed (rc={rc}): {cmd!r}\n"
                f"--- last 50 log lines ---\n{(log_out.get('output') or '').strip()}"
            )
        logger.info("warmup ok | %s", cmd[:120])


def take_init_screenshot(env, output_dir: Path,
                         remote: str = "/tmp/init_screenshot.png") -> None:
    """Capture an initial desktop screenshot via the OSWorld controller (no host
    deps), then upload it back into the VM at `remote` so the agent's prompt
    pointer (/tmp/init_screenshot.png) actually exists."""
    try:
        png = env.controller.get_screenshot()
        if not png:
            return
        (output_dir / "init_screenshot.png").write_bytes(png)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(png)
            tmp_local = Path(tf.name)
        try:
            _vm_upload_bytes(env, tmp_local, remote)
        finally:
            tmp_local.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("init screenshot failed: %s", exc)


# ---------------------------------------------------------------------------
# Grading inside the OSWorld VM (async launch — no 120s _vm_exec ceiling)
# ---------------------------------------------------------------------------
GRADER_RUNNER_TEMPLATE = r'''#!/usr/bin/env python3
"""Auto-generated WildClaw grader runner. Writes JSON to /tmp/_grade.json,
then touches /tmp/_grade.done so the host poller can pick it up."""
import json, sys, traceback, os, subprocess
sys.path.insert(0, "/opt/eval")
os.environ.setdefault("PYTHONPATH", "/opt/eval")

# --- bootstrap grader deps (idempotent) ---
_GRADER_DEPS = ["pytesseract", "Pillow", "numpy", "imagehash", "cairosvg",
                "pikepdf", "pdf2image", "pdfannots", "pypdf", "psutil"]
def _ensure_deps():
    missing = []
    # Some VMs ship a stub PIL (only some plugins, no Image module). Validate
    # by importing the actual top-level submodule the graders need.
    name_to_module = {{"Pillow":"PIL.Image", "pdf2image":"pdf2image"}}
    for pkg in _GRADER_DEPS:
        mod = name_to_module.get(pkg, pkg.lower().replace("-","_"))
        try: __import__(mod)
        except ImportError: missing.append(pkg)
    if missing:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                            "--disable-pip-version-check", "--no-warn-script-location",
                            "--break-system-packages", "--upgrade", "--force-reinstall",
                            *missing], check=False, timeout=300)
        except Exception:
            pass
        # Also try installing without --break-system-packages for older pip
        try:
            still_missing = []
            for pkg in missing:
                mod = name_to_module.get(pkg, pkg.lower().replace("-","_"))
                try: __import__(mod)
                except ImportError: still_missing.append(pkg)
            if still_missing:
                subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                                "--disable-pip-version-check", "--user",
                                "--upgrade", "--force-reinstall",
                                *still_missing], check=False, timeout=300)
        except Exception:
            pass
_ensure_deps()

WORKSPACE = {workspace!r}

# --- task-supplied automated_checks (defines `grade(**kwargs)`) ---
{automated_checks}

if __name__ == "__main__":
    try:
        result = grade(transcript=[], workspace_path=WORKSPACE)
    except Exception as exc:
        result = {{"error": f"{{type(exc).__name__}}: {{exc}}",
                   "traceback": traceback.format_exc()}}
    try:
        with open("/tmp/_grade.json", "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    finally:
        with open("/tmp/_grade.done", "w") as fh:
            fh.write("done\n")
'''


def stage_eval_bundle(env, tasks_root: Path, client_password: str = "password") -> None:
    """Tar tasks_root/eval and upload to /opt/eval inside VM (mirrors WildClaw)."""
    eval_dir = tasks_root / "eval"
    if not eval_dir.is_dir():
        return
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tf:
        tar_local = Path(tf.name)
    try:
        with tarfile.open(tar_local, "w:gz") as tar:
            for child in sorted(eval_dir.iterdir()):
                tar.add(child, arcname=child.name)
        _sudo_rmrf(env, "/opt/eval", password=client_password)
        _sudo_mkdir(env, ["/opt/eval"], password=client_password)
        _vm_upload_bytes(env, tar_local, "/tmp/weavebench_eval.tar.gz")
        _vm_exec(env, ["bash", "-c",
                       "cd /opt/eval && tar xzf /tmp/weavebench_eval.tar.gz"], timeout=120)
    finally:
        tar_local.unlink(missing_ok=True)


def run_grading(env, automated_checks: str, output_dir: Path,
                timeout: int = 1800, client_password: str = "password") -> dict:
    """Stage runner, launch async, poll done file, fetch JSON."""
    if not automated_checks or not automated_checks.strip():
        return {}
    runner_src = GRADER_RUNNER_TEMPLATE.format(
        workspace=TMP_WORKSPACE,
        automated_checks=automated_checks,
    )
    _vm_exec(env, ["bash", "-c",
                   "rm -f /tmp/_grade.json /tmp/_grade.done /tmp/_grade.log"])
    _vm_upload(env, runner_src, "/tmp/_grade_runner.py")
    # Some WildClaw graders (notably 06_Safety_*) hardcode
    # `/root/.openclaw/agents/main/sessions/chat.jsonl` because native runs
    # openclaw as root. In our VM openclaw runs as `user`, so we expose the
    # user's openclaw home at /root via a sudo-managed symlink, and then run
    # the grader itself as root so it can read /root/* and chat.jsonl.
    # Pull LiteLLM endpoint from host env so the in-VM judge can reach it.
    # WEAVEBENCH_LITELLM_BASE_URL / WEAVEBENCH_LITELLM_KEY are the same vars the agent uses;
    # JUDGE_MODEL can be overridden via WEAVEBENCH_JUDGE_MODEL.
    # Fall back to OPENROUTER_BASE_URL/KEY (the host eval shell exports these).
    judge_base_url = (os.environ.get("WEAVEBENCH_LITELLM_BASE_URL")
                      or os.environ.get("OPENROUTER_BASE_URL")
                      or "https://openrouter.ai/api/v1")
    judge_api_key = (os.environ.get("WEAVEBENCH_LITELLM_KEY")
                     or os.environ.get("OPENROUTER_API_KEY")
                     or "")
    judge_model = (os.environ.get("WEAVEBENCH_JUDGE_MODEL")
                   or os.environ.get("JUDGE_MODEL")
                   or "openai/gpt-5.5")
    bash = (
        "echo '{pw}' | sudo -S -p '' bash -c '"
        "if [ ! -e /root/.openclaw ]; then "
        "  ln -sfn /home/user/.openclaw /root/.openclaw; "
        "fi; "
        "if [ -f /tmp/openclaw_task_env.sh ]; then "
        "  set -a; . /tmp/openclaw_task_env.sh; set +a; "
        "fi; "
        "if [ -f /etc/profile.d/weavebench_openrouter.sh ]; then "
        "  set -a; . /etc/profile.d/weavebench_openrouter.sh; set +a; "
        "fi; "
        "OPENROUTER_BASE_URL={base}; "
        "OPENROUTER_API_KEY={key}; "
        "JUDGE_MODEL={model}; "
        "export OPENROUTER_BASE_URL OPENROUTER_API_KEY JUDGE_MODEL; "
        "PYTHONPATH=/opt/eval python3 /tmp/_grade_runner.py "
        ">/tmp/_grade.log 2>&1; "
        "chown user:user /tmp/_grade.json /tmp/_grade.log 2>/dev/null || true; "
        "touch /tmp/_grade.done; "
        "chown user:user /tmp/_grade.done 2>/dev/null || true"
        "'"
    ).format(pw=client_password, base=judge_base_url,
             key=judge_api_key, model=judge_model)
    _vm_launch(env, ["bash", "-c", bash])
    ok = _wait_file(env, "/tmp/_grade.done", timeout=timeout)
    # Always pull the log for debugging
    try:
        _vm_fetch(env, "/tmp/_grade.log", output_dir / "grade.log")
    except Exception:
        pass
    if not ok:
        return {"error": f"grading timeout after {timeout}s"}
    local_json = output_dir / "_grade.json"
    if not _vm_fetch(env, "/tmp/_grade.json", local_json):
        return {"error": "grader did not produce /tmp/_grade.json"}
    try:
        scores = json.loads(local_json.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"json parse failed: {exc}"}
    return scores


def _archive_deliverables(env, output_dir: Path,
                          client_password: str = "password",
                          max_size_mb: int = 1024) -> bool:
    """Pull /tmp_workspace/ deliverables from the VM as a tar.gz into output_dir.

    Saves agent deliverables (PNG/JSON/CSV/text/...) so they survive VM
    teardown and can be re-graded offline by eval/regrade_offline.py.

    Auto-promotion: many task prompts tell the agent to write under
    ``/tmp_workspace/`` (without ``/results``). Earlier this archiver only
    looked in ``/tmp_workspace/results/`` and silently lost everything the
    agent wrote at the workspace root. We now:

      1. Ensure ``/tmp_workspace/results/`` exists.
      2. **Promote** every regular file at the workspace root (or in any
         non-special subdir) into ``/tmp_workspace/results/`` via hardlinks
         (cp -al). We skip the staging dirs that are bench infra:
         ``gt/`` (ground truth), ``input/``, ``staging/``, ``_eval/``,
         ``tmp/``, plus dotfiles, so we never leak gt material into the
         judge bundle.
      3. Tar up the (now populated) ``results/`` as before.

    Layout written:
        output_dir/results.tar.gz       — gzipped tar of /tmp_workspace/results/
        output_dir/results_manifest.txt — `tar -tvzf` listing for quick inspection
        output_dir/_archive_status.txt  — short status string (kept for debug)

    Returns True on success, False if no files at all could be archived.

    Size cap: tarballs larger than max_size_mb are skipped with a warning,
    to keep the rollout dir from being polluted by runaway agents that
    dumped multi-GB junk into results/.
    """
    # Names we never promote from the workspace root into results/
    # (they are bench-controlled inputs / infra, not agent deliverables).
    RESERVED = "gt input staging _eval tmp logs .git .ipynb_checkpoints .openclaw .cache __pycache__ node_modules venv .venv"
    # Top-level files we never promote (auth tokens, openclaw profile docs, env).
    RESERVED_FILES = ".env .bashrc .profile AGENTS.md SOUL.md IDENTITY.md HEARTBEAT.md BOOTSTRAP.md USER.md TOOLS.md CLAUDE.md README.md"
    # When promoting a subdirectory, refuse if it has > MAX_PROMOTE_FILES files
    # (likely node_modules / virtualenv / build artifacts).
    MAX_PROMOTE_FILES = 500
    # Hard cap on final tar entry count.
    MAX_TOTAL_ENTRIES = 2000
    archive_cmd = (
        f"echo '{client_password}' | sudo -S -p '' bash -c '"
        f"set -e; "
        f"mkdir -p {TMP_WORKSPACE}/results; "
        # Promote root-level regular files into results/ via hardlink (cheap,
        # original location is also kept). Skip RESERVED_FILES.
        f"shopt -s nullglob dotglob; "
        f"for f in {TMP_WORKSPACE}/*; do "
        f"  [ -f \"$f\" ] || continue; "
        f"  base=$(basename \"$f\"); "
        f"  case \" {RESERVED_FILES} \" in *\" $base \"*) continue ;; esac; "
        f"  [ -f \"{TMP_WORKSPACE}/results/$base\" ] || cp -al \"$f\" \"{TMP_WORKSPACE}/results/$base\" 2>/dev/null || cp \"$f\" \"{TMP_WORKSPACE}/results/$base\" 2>/dev/null || true; "
        f"done; "
        # Promote subdirectories (except RESERVED) into results/<dirname>/, but
        # skip if the subdir has more than MAX_PROMOTE_FILES files (= likely a
        # node_modules / venv / build dir, not a real deliverable).
        f"for d in {TMP_WORKSPACE}/*/; do "
        f"  [ -d \"$d\" ] || continue; "
        f"  base=$(basename \"$d\"); "
        f"  case \" {RESERVED} results \" in *\" $base \"*) continue ;; esac; "
        f"  fc=$(find \"$d\" -type f 2>/dev/null | head -n {MAX_PROMOTE_FILES + 1} | wc -l); "
        f"  if [ \"$fc\" -gt {MAX_PROMOTE_FILES} ]; then "
        f"    echo \"SKIP_LARGE_DIR $base ($fc files)\" >> /tmp/_archive_skipped.txt; "
        f"    continue; "
        f"  fi; "
        f"  if [ ! -e \"{TMP_WORKSPACE}/results/$base\" ]; then "
        f"    cp -al \"$d\" \"{TMP_WORKSPACE}/results/$base\" 2>/dev/null || cp -r \"$d\" \"{TMP_WORKSPACE}/results/$base\" 2>/dev/null || true; "
        f"  fi; "
        f"done; "
        # Strip any node_modules / __pycache__ / .git that snuck in via cp -r.
        f"find {TMP_WORKSPACE}/results -type d \\( -name node_modules -o -name __pycache__ -o -name .git -o -name .venv -o -name venv \\) -prune -exec rm -rf {{}} + 2>/dev/null || true; "
        # Now check the populated results/ — empty means agent produced nothing.
        f"files=({TMP_WORKSPACE}/results/*); "
        f"if [ ${{#files[@]}} -eq 0 ]; then "
        f"  echo EMPTY_RESULTS_DIR > /tmp/_archive_status; exit 0; "
        f"fi; "
        # Hard cap on entries — if still > MAX_TOTAL_ENTRIES, refuse to tar.
        f"total_entries=$(find {TMP_WORKSPACE}/results -type f 2>/dev/null | head -n {MAX_TOTAL_ENTRIES + 1} | wc -l); "
        f"if [ \"$total_entries\" -gt {MAX_TOTAL_ENTRIES} ]; then "
        f"  echo \"TOO_MANY_FILES $total_entries\" > /tmp/_archive_status; exit 0; "
        f"fi; "
        f"rm -f /tmp/_results.tar.gz; "
        f"tar czf /tmp/_results.tar.gz -C {TMP_WORKSPACE} results/ 2>/tmp/_archive_err; "
        f"size_bytes=$(stat -c%s /tmp/_results.tar.gz 2>/dev/null || echo 0); "
        f"echo OK $size_bytes > /tmp/_archive_status; "
        f"chown user:user /tmp/_results.tar.gz /tmp/_archive_status 2>/dev/null || true"
        f"'"
    )
    out = _vm_exec(env, ["bash", "-c", archive_cmd], timeout=120)
    if (out.get("returncode") or 0) != 0:
        logger.warning("archive command failed: %s", out)
        return False
    status_local = output_dir / "_archive_status.txt"
    if not _vm_fetch(env, "/tmp/_archive_status", status_local):
        return False
    status = status_local.read_text(encoding="utf-8", errors="ignore").strip()
    status_local.unlink(missing_ok=True)
    if status.startswith("EMPTY_RESULTS_DIR"):
        logger.info("no deliverables to archive (%s) — even root-promotion found nothing", status)
        return False
    if status.startswith("TOO_MANY_FILES"):
        logger.warning("archive aborted (%s) — workspace had way more files than expected; "
                       "judge would choke on enumerating them", status[:80])
        return False
    if not status.startswith("OK"):
        logger.warning("unexpected archive status: %s", status[:200])
        return False
    parts = status.split()
    size_bytes = int(parts[1]) if len(parts) > 1 else 0
    if size_bytes > max_size_mb * 1024 * 1024:
        logger.warning("results.tar.gz is %.1f MB > %d MB cap — skipping pull",
                       size_bytes / 1024 / 1024, max_size_mb)
        return False
    if not _vm_fetch(env, "/tmp/_results.tar.gz", output_dir / "results.tar.gz"):
        return False
    # Best-effort manifest for quick inspection without untarring.
    try:
        with tarfile.open(output_dir / "results.tar.gz", "r:gz") as tf:
            lines = []
            for m in tf.getmembers():
                kind = "d" if m.isdir() else "-"
                lines.append(f"{kind} {m.size:>12d}  {m.name}")
            (output_dir / "results_manifest.txt").write_text(
                "\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    logger.info("archived %d bytes deliverable → %s",
                size_bytes, output_dir / "results.tar.gz")
    return True


def stage_gt_into_workspace(env, workspace_path: str) -> None:
    """If host workspace has gt/, ensure it's also in /tmp_workspace/gt
    (it should already be inside the tar — this is a safety net)."""
    gt_host = Path(workspace_path) / "gt"
    if gt_host.is_dir():
        _vm_exec(env, ["bash", "-c",
                       f"test -d {TMP_WORKSPACE}/gt && echo HAS || echo MISS"])


# ---------------------------------------------------------------------------
# WildClaw native system prompt (verbatim from eval/run_batch.py:185-192)
# ---------------------------------------------------------------------------

# VM environment constants — the WeaveBench OSWorld VM image is fixed at
# 1920x1080 Ubuntu 22.04 with `password` as the sudo credential
# (verified per weavebench/CLAUDE.md). Adjust here in lockstep with
# any image change.
_VM_PLATFORM = "Ubuntu"
_VM_SCREEN_W = 1920
_VM_SCREEN_H = 1080
_VM_CLIENT_PASSWORD = "password"
_VM_HOME_DIR = "/home/user"


def _gui_coordinate_tips(model: str | None) -> str:
    """Coordinate guidance for the shared GUI system prompt.

    Non-Qwen models keep the historical raw-pixel guidance. Qwen gets the same
    strong coordinate wording that the GUI tool description exposes, so
    norm1000 experiments are not contradicted by a global pixel-only prompt.
    """
    try:
        from weavebench.agents.qwen_compat import (
            is_qwen_model,
            resolve_computer_coord_mode,
        )
    except Exception:
        is_qwen = False
        coord_mode = "pixel"
    else:
        is_qwen = is_qwen_model(model or "")
        coord_mode = resolve_computer_coord_mode(model or "")

    if is_qwen and coord_mode == "norm1000":
        return (
            "- Qwen coordinate mode: norm1000. Use normalized coordinates only "
            "on a 0-1000 scale across the full desktop screenshot.\n"
            "- Do NOT use raw pixel coordinates and do NOT use the original "
            "screen width or height as the coordinate range. x=0 is the left "
            "edge, x=1000 is the right edge, y=0 is the top edge, y=1000 is "
            "the bottom edge.\n"
        )

    if is_qwen and coord_mode == "pixel":
        return (
            f"- Qwen coordinate mode: pixel. Use raw screen pixel coordinates "
            f"only for this {_VM_SCREEN_W}x{_VM_SCREEN_H} desktop screenshot.\n"
            "- Do NOT use normalized coordinates, do NOT use a 0-1000 scale, "
            "and do NOT rescale the screenshot.\n"
            f"- x ranges from 0 to {_VM_SCREEN_W - 1}; y ranges from 0 to "
            f"{_VM_SCREEN_H - 1}.\n"
        )

    return (
        f"- Coordinates are absolute screen pixels in the "
        f"{_VM_SCREEN_W}x{_VM_SCREEN_H} system. Do NOT normalize.\n"
        f"- IMPORTANT: Output every (x, y) in the "
        f"{_VM_SCREEN_W}x{_VM_SCREEN_H} space. The image you see "
        f"may visually appear smaller in your perception (the API "
        f"may display a downscaled view), but the ACTUAL screen "
        f"is {_VM_SCREEN_W}x{_VM_SCREEN_H}. Do NOT rescale your "
        f"coordinates to a smaller image space; the harness will "
        f"execute your raw (x, y) directly on the "
        f"{_VM_SCREEN_W}x{_VM_SCREEN_H} display.\n"
    )


def _gui_screen_resolution_line(model: str | None) -> str:
    try:
        from weavebench.agents.qwen_compat import is_qwen_model
    except Exception:
        is_qwen = False
    else:
        is_qwen = is_qwen_model(model or "")

    if is_qwen:
        return f"- The screen resolution is {_VM_SCREEN_W}x{_VM_SCREEN_H} pixels.\n"
    return (
        f"- The screen resolution is {_VM_SCREEN_W}x{_VM_SCREEN_H} "
        "pixels. All click coordinates MUST be within this range "
        "and refer to absolute screen pixels.\n"
    )


def weavebench_system_prompt(timeout_seconds: int, gui: bool, model: str | None = None) -> str:
    """WildClaw native system prompt (verbatim from eval/run_batch.py:185-192),
    plus a tool-availability hint that is symmetric across modes — so that
    the only real difference between cli and gui modes is the presence of
    the GUI computer-use tool, not the model's awareness of OpenClaw's
    built-in helpers (pdf/web_fetch/process/...).

    `model` is currently unused (kept for API stability with older callers
    that pass it explicitly). The GUI section is identical for every model
    since the GUI surface was unified to a single `__computer__` tool.
    """
    sp = (
        f"You are a software engineering assistant working in a Linux sandbox. "
        f"There is no wall-clock time limit — work at the pace you need, but "
        f"the number of agent steps is bounded, so do not waste turns on "
        f"redundant exploration. "
        f"Run commands in the foreground without background services and "
        f"produce a complete, working solution in a single pass.\n"
        "Built-in helper tools available in BOTH modes: `exec` (shell), "
        "`read`/`write`/`edit` (files), `process` (long-running jobs), "
        "`pdf` (structured PDF extraction including tables), `web_fetch` "
        "(HTTP), `memory_search` (recall prior context). Pick whichever is "
        "most direct for the sub-step.\n"
        "Working directory: `/tmp_workspace/` is the per-task scratch root "
        "mounted inside the VM. Task-specific input files (if any) are "
        "staged there before the task starts; auxiliary read-only fixtures "
        "may be at `/tmp_workspace/tmp/`. Unless the task prompt specifies "
        "another absolute path, write any output artifacts under "
        "`/tmp_workspace/` so the grader can find them. Do NOT touch "
        "`/tmp_workspace/gt/` — it is reserved for post-task grading and "
        "is not present during your run. The OpenClaw framework's own "
        "`## Workspace` section in the underlying system prompt has been "
        "configured to point at the SAME `/tmp_workspace/` path (via "
        "`agents.defaults.workspace`), so the two sources agree. Anything "
        "you write under `/home/user/.openclaw/...` is OpenClaw's internal "
        "agent state — invisible to the grader and effectively lost.\n"
        "Output discipline: BEFORE you declare the task done, you MUST "
        "verify on disk that every output artifact required by the task "
        "prompt actually exists at the requested path (use `exec` like "
        "`ls -la <path>` or `read` a few of the created files). If any "
        "required file is missing, malformed, or empty, generate it now — "
        "do not return a 'done' message until all required outputs are "
        "present. When the task involves a batch of files, sample-read at "
        "least 2-3 of them to confirm content is correct, and re-run the "
        "generation if the content looks wrong.\n"
    )
    if gui:
        # Single GUI surface for every model — `__computer__` (auto-
        # screenshots after each batch). Operator tips below apply
        # uniformly; the {tool reference parts are anchored to __computer__.
        current_date = datetime.utcnow().strftime("%Y-%m-%d")
        screen_resolution_line = _gui_screen_resolution_line(model)
        coord_tips = _gui_coordinate_tips(model)
        sp += (
            f"GUI mode is enabled. Operator tips:\n"
            f"- You are operating an {_VM_PLATFORM} desktop with internet "
            f"access. The display is `:0`.\n"
            f"{screen_resolution_line}"
            f'- The sudo password is "{_VM_CLIENT_PASSWORD}" when sudo '
            f"is needed.\n"
            f"- The current date is {current_date} (UTC).\n"
            f'- The home directory is "{_VM_HOME_DIR}".\n'
            f"- An initial desktop screenshot was saved to "
            f"/tmp/init_screenshot.png after warmup; you can read it "
            f"with the `image` tool, or call `__computer__` with "
            f"`actions:[]` (empty batch) to grab a fresh capture.\n"
            f"- Stick to the website or application already opened for "
            f"the task when possible.\n"
            f"- Prefer Chrome over Firefox/Chromium unless the task "
            f"says otherwise.\n"
            f"- You can act without asking for confirmation.\n"
            f"- If content may be off-screen, scroll or zoom out before "
            f"deciding it is unavailable.\n"
            f"- GUI is exposed via ONE function tool: `__computer__`. It "
            f"runs one or more GUI actions and ALWAYS returns a fresh "
            f"full-screen screenshot of the resulting desktop. Call with "
            f"either single-action shape `{{action:{{type:<name>, "
            f"...kwargs}}}}` OR batched shape "
            f"`{{actions:[{{action:<name>, ...kwargs}}, ...]}}`. After "
            f"every `__computer__` call you receive a screenshot; no "
            f"separate observation tool is needed. To re-observe without "
            f"acting (e.g. after a non-GUI tool may have changed the "
            f"desktop), call `__computer__` with `actions:[]` (empty "
            f"batch) — the screenshot is still returned.\n"
            f"- You MUST drive the GUI by calling `__computer__` with "
            f"one or more actions. Each action is a dict with an "
            f"`action` field plus its kwargs. PREFER batching multiple "
            f"imminent actions in a single `__computer__` call (e.g. "
            f"click then type, click then keypress, scroll then click) "
            f"to minimize round-trips. Do NOT describe actions in plain "
            f"text.\n"
            f"- CORE LOOP — `__computer__` always auto-screenshots after "
            f"each batch, so the loop is simply: "
            f"`__computer__([...])` → analyse the returned screenshot "
            f"→ `__computer__([...])` → ...\n"
            f"- DO NOT stop after a single observation. If you call "
            f"`__computer__` with `actions:[]` (or any other observation-"
            f"only invocation) and receive a screenshot, you MUST "
            f"continue with concrete click/type/keypress/scroll/drag "
            f"actions on the next turn — observing the screen does NOT "
            f"satisfy the task. Plan-then-act: take the screenshot, "
            f"think about what to do, then call `__computer__` again "
            f"with real actions. Never end the turn after only seeing "
            f"the desktop unless the task's deliverable is genuinely "
            f"already complete on disk (verify with `exec ls -la "
            f"/tmp_workspace/`).\n"
            f"- Many tasks involve BOTH GUI actions AND file deliverables "
            f"(.png screenshots, .txt logs, .py code, etc.). Don't "
            f"forget the file deliverables: drive the GUI to reach the "
            f"required state, capture evidence screenshots, AND write "
            f"the supporting artifacts via `exec`/`write`. Verify the "
            f"deliverable filenames listed in the task prompt all exist "
            f"under `/tmp_workspace/` before declaring done.\n"
            f"{coord_tips}"
            f"- For mouse buttons, use integers: 1=left, 2=wheel/"
            f"middle, 3=right, 4=back, 5=forward.\n"
        )
    # in CLI ablation runs (no desktop), append the explicit
    # anti-cheating policy. Imported lazily to avoid a hard dependency on the
    # mm_agents package at module load time.
    if not gui:
        try:
            from weavebench.agents._openclaw._messages import _CLI_ABLATION_POLICY
        except Exception:
            try:
                from weavebench.agents._openclaw._responses import _CLI_ABLATION_POLICY
            except Exception:
                _CLI_ABLATION_POLICY = ""
        sp = sp + _CLI_ABLATION_POLICY
    # in GUI runs, append an explicit anti-fabrication
    # policy banning PIL/ImageDraw/matplotlib.savefig fake screenshots.
    # Earlier GUI-mode prompt had no such guard; agents were observed
    # cheating with PIL-painted DevTools/editor mock-ups ~30% of the time
    # even with a real desktop available.
    else:
        try:
            from weavebench.agents._openclaw._messages import _GUI_ANTI_HACK_POLICY
        except Exception:
            try:
                from weavebench.agents._openclaw._responses import _GUI_ANTI_HACK_POLICY
            except Exception:
                _GUI_ANTI_HACK_POLICY = ""
        sp = sp + _GUI_ANTI_HACK_POLICY
    return sp


# ---------------------------------------------------------------------------
# Per-task execution
# ---------------------------------------------------------------------------
def _compute_score(scores: dict) -> float:
    """WildClaw parity: prefer scores['overall_score'] when present, else
    average over numeric leaf fields."""
    if not isinstance(scores, dict) or "error" in scores:
        return 0.0
    if "overall_score" in scores and isinstance(scores["overall_score"], (int, float)):
        return float(scores["overall_score"])
    nums = [v for v in scores.values() if isinstance(v, (int, float))]
    return float(sum(nums) / len(nums)) if nums else 0.0


def run_one(env, agent: OpenClawAgent, task: dict, mode: str,
            output_dir: Path, tasks_root: Path, http_proxy: str = "") -> dict:
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
        grow_vm_rootfs_if_needed(env, agent.client_password, task["task_id"],
                                 output_dir=output_dir)

        # 0. Bootstrap node + openclaw + plugins BEFORE warmup, because many
        #    WildClaw warmups need npm/pip/etc. (e.g. `npm install -g agent-browser`).
        agent.bootstrap(env)
        agent.configure(env)

        # 1. workspace + skills + env + warmup
        upload_workspace(env, task["workspace_path"],
                         client_password=agent.client_password)
        upload_skills(env, task.get("skills", ""), task["skills_path"])
        write_task_env_script(env, task.get("env", ""), http_proxy=http_proxy)
        run_warmup(env, task.get("warmup", ""), client_password=agent.client_password)
        if mode == "gui":
            take_init_screenshot(env, output_dir)

        # 2. eval bundle (so osworld_grade_helper etc. import)
        stage_eval_bundle(env, tasks_root, client_password=agent.client_password)

        # 3. agent (skip its internal bootstrap/configure since we just did them)
        sys_prompt = weavebench_system_prompt(86400, gui=(mode == "gui"), model=agent.model)
        meta = agent.run(env, task["prompt"], output_dir,
                         system_prompt_override=sys_prompt,
                         already_configured=True)
        record.update({"agent_done": meta.get("agent_done"),
                       "elapsed_seconds": meta.get("elapsed_seconds")})

        # 3a. Detect openclaw runtime crash (file lock timeout, model-fallback
        #     failure, etc.) by scanning agent.log for the runner's exit-code
        #     marker. If openclaw exited non-zero we treat it as a transient
        #     error so the run is retried on the next pass (already_done()
        #     refuses to skip records that have a top-level `error`).
        agent_log = output_dir / "agent.log"
        if agent_log.exists():
            try:
                tail = agent_log.read_text(encoding="utf-8", errors="ignore")[-4096:]
                m = re.search(r"AGENT_EXIT=(\d+)", tail)
                if m and int(m.group(1)) != 0:
                    code = int(m.group(1))
                    snippet = ""
                    for line in tail.splitlines()[-10:]:
                        if line.strip():
                            snippet = line.strip()[:200]
                            break
                    record["error"] = (f"openclaw runtime exited non-zero "
                                       f"(AGENT_EXIT={code}): {snippet}")
                    logger.warning("[%s/%s/%s] openclaw crash detected (%s) — "
                                   "will be retried on next pass",
                                   task["category"], task["task_id"], mode,
                                   record["error"][:140])
            except Exception:
                pass

        # 4. Stage gt/ ONLY now (post-agent), then grade. This prevents the
        #    agent from peeking at ground truth — same ordering as WildClaw
        #    native run_batch.grade_the_task.
        stage_gt_for_grading(env, task["workspace_path"],
                             client_password=agent.client_password)
        grade_timeout = 1800  # 30min cap on grader (was 24h)
        scores = run_grading(env, task.get("automated_checks", ""),
                             output_dir, timeout=grade_timeout,
                             client_password=agent.client_password)
        record["scores"] = scores
        record["score"] = _compute_score(scores)

        # 5. Archive deliverables for offline re-grading.
        #    The VM is about to be torn down; the agent's outputs in
        #    /tmp_workspace/results/ would be lost forever. Pull them as a
        #    tarball into output_dir so a later eval/regrade_offline.py run
        #    can re-execute the task .md's grade() without booting a VM.
        try:
            _archive_deliverables(env, output_dir,
                                  client_password=agent.client_password)
        except Exception as exc:
            logger.warning("[%s/%s/%s] deliverable archival failed (non-fatal): %s",
                           task["category"], task["task_id"], mode, exc)

    except Exception as exc:
        logger.error("[%s/%s/%s] crashed: %s\n%s",
                     task["category"], task["task_id"], mode,
                     exc, traceback.format_exc())
        record["error"] = str(exc)[-500:]

    record["finished_at"] = datetime.utcnow().isoformat()
    (output_dir / "score.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# Worker / multiprocessing pool
# ---------------------------------------------------------------------------
def worker(env_idx: int, task_queue: Queue, args: argparse.Namespace,
           shared: list) -> None:
    name = current_process().name
    logger.info("[%s] starting (idx=%d)", name, env_idx)
    result_root = Path(args.result_dir)
    tasks_root = Path(args.tasks_root)

    # Re-import WeaveBench inside the child process
    _import_tasks_root(tasks_root)

    while True:
        try:
            task, mode = task_queue.get(timeout=5)
        except Exception:
            break
        if mode not in ("cli", "gui"):
            continue
        if already_done(task, mode, args.model, result_root):
            logger.info("[%s] [%s] %s/%s already done — skipping",
                        name, mode, task["category"], task["task_id"])
            continue

        out_dir = result_root / mode / args.model / task["category"] / task["task_id"]
        logger.info("[%s] [%s] %s/%s (fresh VM)",
                    name, mode, task["category"], task["task_id"])

        env = None
        try:
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
            )
            agent = OpenClawAgent(
                model=args.model,
                litellm_base_url=args.litellm_base_url,
                litellm_api_key=args.litellm_api_key,
                client_password=args.client_password,
                timeout=3600,  # 1h hard cap per task to prevent multi-hour worker hangs
                gui=(mode == "gui"),
                max_steps=task.get("max_steps") or args.max_steps,
            )
            rec = run_one(env, agent, task, mode, out_dir, tasks_root,
                          http_proxy=args.http_proxy)
            shared.append({
                "category": task["category"],
                "task_id": task["task_id"],
                "mode": mode,
                "score": rec.get("score", 0.0),
                "error": rec.get("error"),
            })
            logger.info("[%s] [%s] %s/%s → score=%.3f",
                        name, mode, task["category"], task["task_id"],
                        rec.get("score", 0.0))
        except Exception as exc:
            logger.error("[%s] %s/%s [%s] crashed: %s\n%s",
                         name, task["category"], task["task_id"], mode,
                         exc, traceback.format_exc())
        finally:
            if env is not None:
                try:
                    env.close()
                except Exception:
                    pass
    logger.info("[%s] done.", name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(allow_abbrev=False, 
        description="Run WeaveBench tasks (01-06) inside OSWorld VMs.")
    # OSWorld VM
    ap.add_argument("--provider_name", default="docker")
    ap.add_argument("--region", default=None)
    ap.add_argument("--path_to_vm", default=None)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--screen_width", type=int, default=1920)
    ap.add_argument("--screen_height", type=int, default=1080)
    ap.add_argument("--os_type", default="Ubuntu")
    ap.add_argument("--client_password", default="password")
    ap.add_argument("--num_envs", type=int, default=2)

    # Agent / model
    ap.add_argument("--model", required=True,
                    help="Model id forwarded to the LLM gateway, e.g. 'openai/gpt-5.5'.")
    ap.add_argument("--litellm_base_url", default=os.environ.get("OPENROUTER_BASE_URL", os.environ.get("WEAVEBENCH_LITELLM_BASE_URL", "https://openrouter.ai/api/v1")))
    ap.add_argument("--litellm_api_key", default=os.environ.get("OPENROUTER_API_KEY", os.environ.get("WEAVEBENCH_LITELLM_KEY", "")))
    ap.add_argument("--max_steps", type=int, default=100,
                    help="Default per-task tool-call cap when task.md "
                         "doesn't specify max_steps.")

    # WeaveBench task selection
    ap.add_argument("--tasks_root", default=WEAVEBENCH_ROOT_DEFAULT,
                    help="Path to the WeaveBench repository root.")
    ap.add_argument("--categories", default=",".join(ALL_CATEGORIES),
                    help="Comma-separated category dirs under tasks/.")
    ap.add_argument("--task_filter", default=None,
                    help="Substring filter on task_id.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Limit total tasks (0 = no limit).")
    ap.add_argument("--mode", choices=["cli", "gui", "both"], default="both")
    ap.add_argument("--result_dir", default="./results")
    ap.add_argument("--http_proxy", default=os.environ.get("WEAVEBENCH_VM_HTTP_PROXY", ""),
                    help="HTTP(S) proxy URL injected into the VM via "
                         "/tmp/openclaw_task_env.sh (e.g. http://proxy.example.com:8080). "
                         "Needed for tasks that fetch from blocked sites "
                         "(Wikipedia, Google, etc.). Empty = no proxy.")

    return ap.parse_args()


def main() -> None:
    args = parse_args()

    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    tasks_root = Path(args.tasks_root).resolve()
    tasks = discover_tasks(tasks_root, cats)
    if args.task_filter:
        tasks = [t for t in tasks if args.task_filter in t["task_id"]]
    if args.limit:
        tasks = tasks[:args.limit]
    logger.info("Discovered %d tasks across %d categories (mode=%s, num_envs=%d)",
                len(tasks), len(cats), args.mode, args.num_envs)
    if not tasks:
        logger.error("No tasks to run — exiting.")
        return

    modes = ["cli", "gui"] if args.mode == "both" else [args.mode]

    mgr = Manager()
    queue: Queue = mgr.Queue()
    shared: list = mgr.list()
    for t in tasks:
        for m in modes:
            queue.put((t, m))

    procs = [Process(target=worker, args=(i, queue, args, shared),
                     name=f"env-{i+1}") for i in range(args.num_envs)]
    for p in procs:
        p.start()
        time.sleep(2)
    for p in procs:
        p.join()

    out = Path(args.result_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = list(shared)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    (out / f"summary_{ts}.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if summary:
        nums = [s["score"] for s in summary if isinstance(s.get("score"), (int, float))]
        logger.info("=== %d completed runs, mean score = %.3f ===",
                    len(summary), (sum(nums) / len(nums)) if nums else 0.0)
        # Per-mode breakdown
        for m in modes:
            mvals = [s["score"] for s in summary if s["mode"] == m
                     and isinstance(s.get("score"), (int, float))]
            if mvals:
                logger.info("  %s: n=%d mean=%.3f", m, len(mvals),
                            sum(mvals) / len(mvals))


if __name__ == "__main__":
    main()
