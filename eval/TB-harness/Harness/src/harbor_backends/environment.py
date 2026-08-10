from __future__ import annotations

import posixpath
import shlex
import time
from typing import Any

from harbor.environments.base import BaseEnvironment

from harness_types import ExecResult


class HarborEnvironmentAdapter:
    """Small async bridge from Harbor's environment API to CUA-Harness."""

    def __init__(
        self,
        environment: BaseEnvironment,
        *,
        workspace_path: str,
    ) -> None:
        self.environment = environment
        self.workspace_path = workspace_path.rstrip("/") or "/app"

    async def exec(self, command: str, timeout: int = 30) -> ExecResult:
        started = time.monotonic()
        result = await self.environment.exec(
            command=command,
            cwd=self.workspace_path,
            timeout_sec=timeout,
        )
        return ExecResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=int(result.return_code),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def screenshot(self) -> bytes:
        return b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        parent = posixpath.dirname(remote_path.rstrip("/"))
        if parent:
            await self.environment.exec(
                command=f"mkdir -p {shlex.quote(parent)}",
                timeout_sec=30,
            )
        await self.environment.upload_file(local_path, remote_path)

    async def download(self, remote_path: str, local_path: str) -> None:
        await self.environment.download_file(remote_path, local_path)


async def workspace_snapshot(
    environment: BaseEnvironment,
    workspace_path: str,
) -> dict[str, str] | None:
    workspace = workspace_path.rstrip("/") or "/app"
    quoted_workspace = shlex.quote(workspace)
    ignored_harness = shlex.quote(f"{workspace}/.cua_harness")
    ignored_gt = shlex.quote(f"{workspace}/gt")
    command = (
        "set -o pipefail; "
        f"if [ ! -d {quoted_workspace} ]; then exit 0; fi; "
        f"find {quoted_workspace} -xdev "
        f"\\( -path {ignored_harness} -o -path '{ignored_harness}/*' "
        f"-o -path {ignored_gt} -o -path '{ignored_gt}/*' \\) -prune "
        "-o -type f -printf 'f\\t%p\\t%s\\t%T@\\n' "
        "-o -type l -printf 'l\\t%p\\t%l\\n' "
        "-o -type d -printf 'd\\t%p\\n'"
    )
    result = await environment.exec(
        command=command,
        cwd=workspace,
        timeout_sec=120,
    )
    if result.return_code != 0:
        return None
    snapshot: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        kind, path = parts[0], parts[1]
        if path == workspace:
            continue
        snapshot[path] = kind + "\t" + (parts[2] if len(parts) > 2 else "")
    return snapshot


def workspace_snapshot_diff(
    before: dict[str, str],
    after: dict[str, str],
) -> dict[str, Any]:
    before_paths = set(before)
    after_paths = set(after)
    added = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    common = before_paths & after_paths
    changed: list[str] = []
    type_changed: list[str] = []
    for path in sorted(common):
        if before[path] == after[path]:
            continue
        if before[path].split("\t", 1)[0] != after[path].split("\t", 1)[0]:
            type_changed.append(path)
        else:
            changed.append(path)
    mutations = {
        "added": added[:100],
        "deleted": deleted[:100],
        "changed": changed[:100],
        "type_changed": type_changed[:100],
    }
    counts = {
        "added": len(added),
        "deleted": len(deleted),
        "changed": len(changed),
        "type_changed": len(type_changed),
    }
    return {
        "detected": any(counts.values()),
        "counts": counts,
        "mutations": mutations,
    }
