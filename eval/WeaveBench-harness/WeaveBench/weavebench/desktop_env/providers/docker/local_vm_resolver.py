"""Per-host local override for slow NFS-backed assets.

Why: the canonical weavebench layout puts heavy assets on a slow NFS share:

  - ``vm_image/Ubuntu.qcow2``        ~28 GB, mounted by every docker VM
  - ``weavebench/assets/openclaw.tar.gz``    ~491 MB, uploaded to every VM at bootstrap

Cold-booting 8 KVM VMs in parallel reads ~28 GB each from NFS, which under
contention takes well over the 30-min ``_wait_for_vm_ready`` budget and triggers
``TimeoutError("VM failed to become ready within timeout period")``. The
openclaw tarball causes a smaller (~4 GB) but still painful NFS spike during
agent bootstrap.

Fix: each host keeps its own pre-warmed copies on local NVMe and a small JSON
map (``<repo>/weavebench/vm_image/local_qcow2_map.json``) records which
host should use which local path. This module is the lookup point.

Resolution order for any asset key (first accepted candidate wins):

  1. ``$OSWORLD_LOCAL_<ASSET>_PATH`` env var (e.g. ``OSWORLD_LOCAL_QCOW2_PATH``).
     Also a back-compat alias ``$OSWORLD_LOCAL_VM_PATH`` for ``qcow2``.
  2. JSON map keyed by current host IPv4. Map values may be:
       - a plain string (legacy form, treated as the ``qcow2`` path)
       - a dict like ``{"qcow2": "...", "openclaw_tar_gz": "..."}``
  3. ``default_path`` (fallback, original NFS behavior).

Acceptance policy (controlled per-asset by ``WEAVEBENCH_<ASSET>_STRICT_SIZE``):

  - default ``0`` (TRUST mode) — accept any candidate that exists, is readable,
    and passes a sanity-size floor (``qcow2`` must be ≥ 5 GB). Size differences
    versus the NFS canonical are LOG-INFO-and-use, not fatal. This is the right
    default when NFS canonical and local cache are both valid bootable images
    that just happen to be different versions (e.g. local pre-warmed v3 vs NFS
    still v2). Without this, every host whose NFS is slightly out of date would
    be silently downgraded to the slow NFS read.
  - ``1`` (STRICT mode) — original behaviour: any size mismatch warns and
    falls back to NFS. Use when you suspect the local cache is half-copied
    or corrupted.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path
from typing import Iterable, Optional, Tuple

logger = logging.getLogger("desktopenv.providers.docker.local_vm_resolver")

_DEFAULT_MAP_REL_PATHS = (
    "vm_image/local_qcow2_map.json",
    "weavebench/vm_image/local_qcow2_map.json",
)

# Canonical asset keys + their env-var override names. Add new heavy assets
# here; consumers call ``resolve_asset_path("<key>", default)``.
_ASSET_ENV_VARS = {
    "qcow2": ("OSWORLD_LOCAL_QCOW2_PATH", "OSWORLD_LOCAL_VM_PATH"),
    "openclaw_tar_gz": ("OSWORLD_LOCAL_OPENCLAW_TAR_GZ",),
}


def _list_local_ipv4() -> list[str]:
    """Return all non-loopback IPv4 addresses bound on this host."""
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:
        import subprocess
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            check=False, capture_output=True, text=True, timeout=2,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    ip = parts[i + 1].split("/")[0]
                    if ip and not ip.startswith("127.") and ip not in ips:
                        ips.append(ip)
                    break
    except Exception:
        pass
    return ips


def _load_map(map_search_paths: Iterable[Path]) -> Tuple[Optional[dict], Optional[Path]]:
    for p in map_search_paths:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data, p
            except Exception as exc:
                logger.warning("local_vm_resolver: cannot parse %s: %s", p, exc)
    return None, None


def _file_size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _accept(candidate: str, default_path: str, asset_key: str) -> Optional[str]:
    """Return ``candidate`` iff it exists, is readable, and (in strict mode)
    its size matches the canonical (``default_path``). Otherwise warn and
    return None.

    Strict-vs-trust mode is selected per-asset via env var
    ``WEAVEBENCH_<ASSET_KEY>_STRICT_SIZE`` (e.g. ``WEAVEBENCH_QCOW2_STRICT_SIZE``):

    * default ``0`` (trust mode) — accept candidate as long as it exists +
      readable + passes a minimal sanity-size floor. If sizes differ from
      the NFS canonical we log INFO (not WARN) and use the local cache
      anyway. This is the right default when the NFS canonical and the
      pre-warmed local cache may legitimately be different versions of the
      same asset (e.g. NFS still points at v2 while ``prime_local_vm_cache.sh``
      already pulled v3 — both are valid bootable qcow2 images, the local
      copy just happens to be newer).
    * ``1`` (strict mode) — original behavior: any size mismatch warns and
      falls back to NFS. Use this when you suspect the local cache may be
      half-copied / corrupted.

    Sanity floor (trust mode only):

    * ``qcow2``         — must be ≥ 5 GB (smaller almost certainly means
                          half-downloaded or wrong file).
    * other assets      — must be > 0 bytes.
    """
    if not candidate:
        return None
    if not os.path.isfile(candidate):
        logger.warning("local_vm_resolver[%s]: candidate %r not a file; falling back",
                       asset_key, candidate)
        return None
    if not os.access(candidate, os.R_OK):
        logger.warning("local_vm_resolver[%s]: candidate %r not readable; falling back",
                       asset_key, candidate)
        return None
    cand_size = _file_size(candidate)
    if cand_size is None:
        logger.warning("local_vm_resolver[%s]: cannot stat candidate %r; falling back",
                       asset_key, candidate)
        return None

    nfs_size = _file_size(default_path)  # may be None if NFS canonical missing

    strict_env = f"WEAVEBENCH_{asset_key.upper()}_STRICT_SIZE"
    strict = os.environ.get(strict_env, "0").lower() in ("1", "true", "yes", "on")

    if strict:
        if nfs_size is None:
            logger.warning("local_vm_resolver[%s]: strict mode + NFS canonical missing (%s); "
                           "falling back",
                           asset_key, default_path)
            return None
        if cand_size != nfs_size:
            logger.warning(
                "local_vm_resolver[%s]: STRICT size mismatch local=%d nfs=%d for %r vs %r — "
                "cache stale, re-prime with scripts/prime_local_vm_cache.sh; "
                "falling back to NFS (set %s=0 to trust the local cache anyway)",
                asset_key, cand_size, nfs_size, candidate, default_path, strict_env,
            )
            return None
        return candidate

    # ---- trust mode (default) ----
    sanity_floor = 5 * 1024 * 1024 * 1024 if asset_key == "qcow2" else 1
    if cand_size < sanity_floor:
        logger.warning(
            "local_vm_resolver[%s]: candidate %r too small (%d bytes < %d sanity floor); "
            "falling back",
            asset_key, candidate, cand_size, sanity_floor,
        )
        return None
    if nfs_size is not None and cand_size != nfs_size:
        logger.info(
            "local_vm_resolver[%s]: size differs (local=%d vs nfs=%d) for %r vs %r — "
            "trusting local cache (set %s=1 for old strict behavior)",
            asset_key, cand_size, nfs_size, candidate, default_path, strict_env,
        )
    return candidate


def _extract_local_for_ip(mapping_value, asset_key: str) -> Optional[str]:
    """Pull the per-asset local path from a map entry.

    Map values may be:
      - a string (legacy form): treated as the ``qcow2`` path
      - a dict: looked up by ``asset_key``
    """
    if isinstance(mapping_value, str):
        return mapping_value if asset_key == "qcow2" else None
    if isinstance(mapping_value, dict):
        v = mapping_value.get(asset_key)
        return v if isinstance(v, str) else None
    return None


def resolve_asset_path(asset_key: str, default_path: str) -> str:
    """Generic resolver. ``asset_key`` is one of the keys in ``_ASSET_ENV_VARS``.

    Returns the local cached path if available and validated, else
    ``default_path`` unchanged.
    """
    # 1. env-var override(s)
    for env_name in _ASSET_ENV_VARS.get(asset_key, ()):
        v = os.environ.get(env_name, "").strip()
        if v:
            accepted = _accept(v, default_path, asset_key)
            if accepted:
                logger.info("local_vm_resolver[%s]: using $%s=%r",
                            asset_key, env_name, accepted)
                return accepted

    # 2. JSON map by IP
    cwd = Path.cwd()
    search = [cwd / rel for rel in _DEFAULT_MAP_REL_PATHS]
    try:
        default_anchor = Path(default_path).resolve()
        for parent in default_anchor.parents:
            cand = parent / "vm_image" / "local_qcow2_map.json"
            if cand not in search:
                search.append(cand)
            if (parent / ".git").exists() or parent.name == "weavebench":
                break
    except Exception:
        pass

    mapping, map_path = _load_map(search)
    if mapping:
        ips = _list_local_ipv4()
        for ip in ips:
            entry = mapping.get(ip)
            local = _extract_local_for_ip(entry, asset_key)
            if local:
                accepted = _accept(local, default_path, asset_key)
                if accepted:
                    logger.info(
                        "local_vm_resolver[%s]: using local cache %r "
                        "(matched IP %s in %s)",
                        asset_key, accepted, ip, map_path,
                    )
                    return accepted
        logger.info(
            "local_vm_resolver[%s]: no IP match in %s for host IPs=%s; using NFS",
            asset_key, map_path, ips,
        )

    # 3. fallback
    return default_path


# Back-compat thin wrapper used by manager.py.
def resolve_qcow2_path(default_path: str) -> str:
    return resolve_asset_path("qcow2", default_path)

