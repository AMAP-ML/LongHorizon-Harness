from __future__ import annotations

import asyncio
import base64
import signal
import time

from harness_types import ExecResult


class DockerEnvironment:
    def __init__(self, container: str) -> None:
        self.container = container

    @classmethod
    def from_url(cls, url: str) -> DockerEnvironment:
        return cls(url.removeprefix("docker://"))

    async def exec(self, command: str, timeout: int = 30) -> ExecResult:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                self.container,
                "timeout",
                "-k",
                "2s",
                str(max(1, timeout)),
                "bash",
                "-lc",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
            return ExecResult(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode if proc.returncode is not None else -1,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except asyncio.TimeoutError:
            if "proc" in locals() and proc.returncode is None:
                proc.send_signal(signal.SIGKILL)
                await proc.wait()
            return ExecResult("", f"Command timed out after {timeout}s", -1, int((time.monotonic() - start) * 1000))

    async def screenshot(self) -> bytes:
        result = await self.exec("gnome-screenshot -f /tmp/_cua_ss.png 2>/dev/null || import -window root /tmp/_cua_ss.png 2>/dev/null; base64 /tmp/_cua_ss.png 2>/dev/null", timeout=15)
        if result.exit_code != 0 or not result.stdout.strip():
            return b""
        try:
            return base64.b64decode(result.stdout)
        except Exception:
            return b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        proc = await asyncio.create_subprocess_exec("docker", "cp", local_path, f"{self.container}:{remote_path}")
        await proc.communicate()
        if proc.returncode:
            raise RuntimeError(f"docker cp upload failed: {local_path} -> {remote_path}")

    async def download(self, remote_path: str, local_path: str) -> None:
        proc = await asyncio.create_subprocess_exec("docker", "cp", f"{self.container}:{remote_path}", local_path)
        await proc.communicate()
        if proc.returncode:
            raise RuntimeError(f"docker cp download failed: {remote_path} -> {local_path}")
