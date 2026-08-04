from __future__ import annotations

import asyncio
import os
import signal
import shutil
import time
from pathlib import Path

from ..types import ExecResult


class LocalEnvironment:
    async def exec(self, command: str, timeout: int = 30) -> ExecResult:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return ExecResult(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode if proc.returncode is not None else -1,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except asyncio.TimeoutError:
            if "proc" in locals() and proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            return ExecResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    async def screenshot(self) -> bytes:
        path = Path("/tmp/_cua_harness_screenshot.png")
        await self.exec(f"gnome-screenshot -f {path} 2>/dev/null || import -window root {path} 2>/dev/null", timeout=10)
        return path.read_bytes() if path.exists() else b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, remote_path)

    async def download(self, remote_path: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(remote_path, local_path)
