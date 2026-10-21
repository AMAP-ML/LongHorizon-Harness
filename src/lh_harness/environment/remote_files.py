from __future__ import annotations

import os
import posixpath
import shlex
import tempfile
from pathlib import Path

from .base import Environment
from ..types import DEFAULT_TMP_DIR


def _remote_parent(path: str) -> str:
    """Parent of a remote path, handling both POSIX and Windows separators."""

    normalized = str(path).rstrip("/\\")
    cut = max(normalized.rfind("/"), normalized.rfind("\\"))
    return normalized[:cut] if cut > 0 else ""


async def write_remote_text(env: Environment, remote_path: str, content: str, mode: str = "0644") -> None:
    parent = _remote_parent(remote_path)
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

    native_chmod = getattr(env, "chmod", None)
    if callable(native_chmod):
        await native_chmod(str(remote_path), mode)
        return
    result = await env.exec(f"chmod {shlex.quote(mode)} {shlex.quote(str(remote_path))}", timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"failed chmod {remote_path}: {result.stderr or result.stdout}")


async def ensure_remote_dir(env: Environment, remote_path: str) -> None:
    native_ensure = getattr(env, "ensure_dir", None)
    if callable(native_ensure):
        await native_ensure(str(remote_path))
        return
    result = await env.exec(f"mkdir -p {shlex.quote(str(remote_path))}", timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"failed creating {remote_path}: {result.stderr or result.stdout}")
