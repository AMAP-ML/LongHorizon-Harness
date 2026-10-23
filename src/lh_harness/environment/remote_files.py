from __future__ import annotations

import os
import posixpath
import shlex
import tempfile
from pathlib import Path

from .base import Environment
from ..types import DEFAULT_TMP_DIR


async def write_remote_text(env: Environment, remote_path: str, content: str, mode: str = "0644") -> None:
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

    # Check if this is a local environment on Windows
    is_local_windows = (
        hasattr(env, '__class__')
        and env.__class__.__name__ == 'LocalEnvironment'
        and os.name == 'nt'
    )

    if is_local_windows:
        # For local Windows, use Python's os.chmod directly
        try:
            # Convert Unix mode string (e.g., "0644") to integer
            mode_int = int(mode, 8) if isinstance(mode, str) else mode
            os.chmod(remote_path, mode_int)
        except Exception as e:
            # Permission changes are best-effort on Windows
            pass
    else:
        # For remote or Unix systems, use chmod command
        result = await env.exec(f"chmod {shlex.quote(mode)} {shlex.quote(remote_path)}", timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(f"failed chmod {remote_path}: {result.stderr or result.stdout}")


async def ensure_remote_dir(env: Environment, remote_path: str) -> None:
    # Check if this is a local environment on Windows
    is_local_windows = (
        hasattr(env, '__class__')
        and env.__class__.__name__ == 'LocalEnvironment'
        and os.name == 'nt'
    )

    if is_local_windows:
        # For local Windows, use Python's Path.mkdir directly
        try:
            Path(remote_path).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"failed creating {remote_path}: {e}")
    else:
        # For remote or Unix systems, use mkdir -p
        result = await env.exec(f"mkdir -p {shlex.quote(remote_path)}", timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(f"failed creating {remote_path}: {result.stderr or result.stdout}")
