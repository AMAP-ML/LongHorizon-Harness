from __future__ import annotations

import os
import posixpath
import shlex
import tempfile
from pathlib import Path

from .environment.base import Environment
from .types import DEFAULT_TMP_DIR


async def write_remote_text(env: Environment, remote_path: str, content: str, mode: str = "0644") -> None:
    parent = posixpath.dirname(remote_path.rstrip("/"))
    if parent:
        await ensure_remote_dir(env, parent)

    tmp_path: str | None = None
    try:
        tmp_dir = Path(DEFAULT_TMP_DIR)
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


async def ensure_remote_dir(env: Environment, remote_path: str) -> None:
    result = await env.exec(f"mkdir -p {shlex.quote(remote_path)}", timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"failed creating {remote_path}: {result.stderr or result.stdout}")
