from __future__ import annotations

import os
import posixpath
import shlex
import sys
import tempfile
from pathlib import Path

from .base import Environment
from ..types import DEFAULT_TMP_DIR

_IS_WINDOWS = sys.platform.startswith("win")


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

    if _IS_WINDOWS:
        # chmod is a Unix command and isn't available through Windows cmd.exe.
        # File permissions on Windows are governed by ACLs, not this mode bit,
        # so there is nothing equivalent worth doing here.
        return

    result = await env.exec(f"chmod {shlex.quote(mode)} {shlex.quote(remote_path)}", timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"failed chmod {remote_path}: {result.stderr or result.stdout}")


async def ensure_remote_dir(env: Environment, remote_path: str) -> None:
    if _IS_WINDOWS:
        # Windows cmd.exe's `mkdir` already creates intermediate directories
        # (the Unix `-p` behavior is the default), but unlike `mkdir -p` it
        # errors if the target already exists. Check first so repeated calls
        # for the same run/tmp/prompts directory stay idempotent.
        check = await env.exec(
            f'if exist "{remote_path}" (exit 0) else (exit 1)', timeout=30
        )
        if check.exit_code == 0:
            return
        result = await env.exec(f'mkdir "{remote_path}"', timeout=30)
        if result.exit_code != 0:
            raise RuntimeError(f"failed creating {remote_path}: {result.stderr or result.stdout}")
        return

    result = await env.exec(f"mkdir -p {shlex.quote(remote_path)}", timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"failed creating {remote_path}: {result.stderr or result.stdout}")
