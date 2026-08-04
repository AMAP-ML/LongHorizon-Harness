from __future__ import annotations

import base64
import time
from urllib.parse import urlparse

from ..types import ExecResult


class SSHEnvironment:
    def __init__(self, host: str, port: int = 22, username: str | None = None, key_path: str | None = None) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.key_path = key_path
        self._conn = None

    @classmethod
    def from_url(cls, url: str) -> SSHEnvironment:
        parsed = urlparse(url)
        return cls(
            host=parsed.hostname or "",
            port=parsed.port or 22,
            username=parsed.username,
        )

    async def _ensure_conn(self):
        if self._conn is None:
            import asyncssh

            kwargs = {"known_hosts": None}
            if self.key_path:
                kwargs["client_keys"] = [self.key_path]
            self._conn = await asyncssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                **kwargs,
            )
        return self._conn

    async def exec(self, command: str, timeout: int = 30, tee_path: str | None = None) -> ExecResult:
        # tee_path (live trajectory mirroring) is only supported for the local
        # environment; ssh runs remotely, so it is accepted and ignored.
        start = time.monotonic()
        try:
            conn = await self._ensure_conn()
            result = await conn.run(command, timeout=timeout, check=False)
            return ExecResult(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.exit_status if result.exit_status is not None else 0,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:
            return ExecResult("", f"{type(exc).__name__}: {exc}", -1, int((time.monotonic() - start) * 1000))

    async def screenshot(self) -> bytes:
        result = await self.exec("gnome-screenshot -f /tmp/_lh_harness_ss.png 2>/dev/null || import -window root /tmp/_lh_harness_ss.png 2>/dev/null; base64 /tmp/_lh_harness_ss.png 2>/dev/null", timeout=15)
        if result.exit_code != 0 or not result.stdout.strip():
            return b""
        try:
            return base64.b64decode(result.stdout)
        except Exception:
            return b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        import asyncssh

        conn = await self._ensure_conn()
        await asyncssh.scp(local_path, (conn, remote_path))

    async def download(self, remote_path: str, local_path: str) -> None:
        import asyncssh

        conn = await self._ensure_conn()
        await asyncssh.scp((conn, remote_path), local_path)
