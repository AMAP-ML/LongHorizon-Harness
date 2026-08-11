"""PLAN.md §D7: write_remote_text's staging-file cleanup must tolerate any
OSError, not just FileNotFoundError -- some bind mounts and container/
network filesystems raise PermissionError on unlink() even for a file the
current process just created, which used to escape the `finally` block and
fail the whole episode after the prompt had already uploaded successfully.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import asyncio  # noqa: E402

from kusudaemon.environment.remote_files import write_remote_text  # noqa: E402
from kusudaemon.types import ExecResult  # noqa: E402


class _FakeEnvironment:
    """Records exec/upload calls; no real subprocess or filesystem remote."""

    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.executed: list[str] = []

    async def exec(self, command, timeout=300, tee_path=None):
        self.executed.append(command)
        return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=1)

    async def upload(self, local_path, remote_path):
        self.uploaded.append((local_path, remote_path))

    async def screenshot(self) -> bytes:
        return b""

    async def download(self, remote_path, local_path) -> None:
        pass


class WriteRemoteTextCleanupTest(unittest.TestCase):
    def test_permission_error_on_unlink_does_not_fail_the_write(self) -> None:
        env = _FakeEnvironment()
        with tempfile.TemporaryDirectory() as staging:
            env.staging_dir = staging  # type: ignore[attr-defined]

            from unittest import mock

            with mock.patch(
                "kusudaemon.environment.remote_files.Path.unlink",
                side_effect=PermissionError("cannot unlink on this mount"),
            ):
                asyncio.run(write_remote_text(env, "/remote/prompt.md", "hello"))

        self.assertEqual(len(env.uploaded), 1)
        self.assertTrue(any("chmod" in cmd for cmd in env.executed))

    def test_file_not_found_on_unlink_still_tolerated(self) -> None:
        # The original guard's exact case (someone else already removed the
        # staged file) must keep working under the broadened except clause.
        env = _FakeEnvironment()
        with tempfile.TemporaryDirectory() as staging:
            env.staging_dir = staging  # type: ignore[attr-defined]
            asyncio.run(write_remote_text(env, "/remote/prompt.md", "hello"))

        self.assertEqual(len(env.uploaded), 1)


if __name__ == "__main__":
    unittest.main()
