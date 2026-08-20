from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from ..types import ExecResult


@runtime_checkable
class Environment(Protocol):
    """Where a role episode runs.

    ``run`` is the primary entry point and takes an **argv list**, not a shell
    string: the working directory, environment overrides and the agent's stdin
    are passed as real subprocess arguments. That keeps command construction
    free of shell quoting, which is both a portability fix (POSIX shells and
    cmd.exe disagree on nearly everything) and one less injection surface for
    model-supplied paths and model ids.

    ``exec`` remains for callers that genuinely want a shell. It is an escape
    hatch, and the caller owns the syntax for the platform it runs on.
    """

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        tee_path: str | None = None,
    ) -> ExecResult: ...

    async def exec(
        self,
        command: str,
        timeout: int = 30,
        tee_path: str | None = None,
    ) -> ExecResult: ...

    async def makedirs(self, path: str) -> None: ...

    async def write_text(self, path: str, content: str) -> None: ...

    async def screenshot(self) -> bytes: ...

    async def upload(self, local_path: str, remote_path: str) -> None: ...

    async def download(self, remote_path: str, local_path: str) -> None: ...
