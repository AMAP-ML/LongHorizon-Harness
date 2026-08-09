from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types import ExecResult


@runtime_checkable
class Environment(Protocol):
    async def exec(
        self,
        command: str,
        timeout: int = 30,
        tee_path: str | None = None,
    ) -> ExecResult: ...

    async def screenshot(self) -> bytes: ...

    async def upload(self, local_path: str, remote_path: str) -> None: ...

    async def download(self, remote_path: str, local_path: str) -> None: ...
