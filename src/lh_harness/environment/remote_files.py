"""Write harness bookkeeping into whatever environment a run uses.

An environment that can touch its own filesystem directly (the local one) does
so through ``makedirs``/``write_text``. Only a genuinely remote environment
falls back to shelling out, and that fallback is POSIX because the remote side
is a Linux VM or container.
"""

from __future__ import annotations

import os
import posixpath
import shlex
from pathlib import Path

from .base import Environment


async def write_remote_text(env: Environment, remote_path: str, content: str, mode: str = "0644") -> None:
    native = getattr(env, "write_text", None)
    if callable(native):
        await native(remote_path, content)
        return
    await _shell_write_text(env, remote_path, content, mode)


async def ensure_remote_dir(env: Environment, remote_path: str) -> None:
    native = getattr(env, "makedirs", None)
    if callable(native):
        await native(remote_path)
        return
    result = await env.exec(f"mkdir -p {shlex.quote(remote_path)}", timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"failed creating {remote_path}: {result.stderr or result.stdout}")


async def _shell_write_text(
    env: Environment, remote_path: str, content: str, mode: str
) -> None:
    """Stage locally, upload, then fix permissions -- the remote-environment path."""
    import tempfile

    from ..types import DEFAULT_TMP_DIR

    parent = posixpath.dirname(remote_path.rstrip("/"))
    if parent:
        await ensure_remote_dir(env, parent)

    tmp_path: str | None = None
    try:
        # Prompts can carry task secrets, so stage them in the run's own tmp dir
        # rather than a directory shared by every run on the machine.
        staging = getattr(env, "staging_dir", None)
        tmp_dir = Path(staging) if staging else Path(DEFAULT_TMP_DIR)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="lh_harness_remote_", dir=tmp_dir, text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        await env.upload(tmp_path, remote_path)
    finally:
        if tmp_path is not None:
            try:
                Path(tmp_path).unlink()
            except FileNotFoundError:
                pass

    result = await env.exec(f"chmod {shlex.quote(mode)} {shlex.quote(remote_path)}", timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"failed chmod {remote_path}: {result.stderr or result.stdout}")
