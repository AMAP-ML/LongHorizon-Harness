from __future__ import annotations

import asyncio
import os
import signal
import shutil
import tempfile
import time
from pathlib import Path

from ..types import ExecResult


class LocalEnvironment:
    def __init__(self, tmp_dir: str | None = None) -> None:
        # Per-run scratch dir for local artifacts (screenshots, etc.). Falls back
        # to the system temp dir when not provided so behavior stays unchanged.
        self._tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir())

    async def exec(self, command: str, timeout: int = 30, tee_path: str | None = None) -> ExecResult:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid,
                # Claude Code emits one JSON object per line in stream-json mode.
                # A single line (e.g. a tool_result carrying a base64 screenshot)
                # can far exceed asyncio's default 64KB StreamReader limit and
                # truncate the trajectory. Raise the limit so full lines survive.
                limit=64 * 1024 * 1024,
            )
            if tee_path:
                # Stream stdout to a local file line-by-line so the dashboard can
                # render the trajectory in real time while the agent is running.
                stdout, stderr = await asyncio.wait_for(
                    self._communicate_tee(proc, tee_path), timeout=timeout
                )
            else:
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

    @staticmethod
    async def _communicate_tee(proc, tee_path: str) -> tuple[bytes, bytes]:
        """Drain stdout/stderr concurrently, mirroring stdout to ``tee_path``.

        stdout is written incrementally (one line at a time) so an external
        reader — the dashboard — sees the agent's stream-json trajectory grow
        live. The full stdout/stderr bytes are still returned unchanged.
        """
        path = Path(tee_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        async def _read_stdout() -> bytes:
            chunks: list[bytes] = []
            # Open in binary append-less write mode; flush after every line.
            with open(path, "wb", buffering=0) as fh:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    chunks.append(line)
                    try:
                        fh.write(line)
                    except OSError:
                        pass
            return b"".join(chunks)

        async def _read_stderr() -> bytes:
            return await proc.stderr.read()

        stdout, stderr = await asyncio.gather(_read_stdout(), _read_stderr())
        await proc.wait()
        return stdout, stderr

    async def screenshot(self) -> bytes:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        path = self._tmp_dir / "_lh_harness_screenshot.png"
        await self.exec(f"gnome-screenshot -f {path} 2>/dev/null || import -window root {path} 2>/dev/null", timeout=10)
        return path.read_bytes() if path.exists() else b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, remote_path)

    async def download(self, remote_path: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(remote_path, local_path)
