from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import signal
import stat
import sys
import time
from pathlib import Path

from ..types import DEFAULT_TMP_DIR, ExecResult
from ..supervisor.control_bus import _ensure_dir_fd_nofollow, _open_private_regular_at
from ..trajectory_artifacts import StreamingTrajectoryArtifactWriter
from ..utils.process_group import (
    kill_process_group,
    signal_process_group,
    track_process_group,
    untrack_process_group,
)


# Agent CLIs can emit very large tool results or base64 screenshots.  Keep the
# useful tail (which contains the final assistant/result records) while
# bounding both the in-memory ExecResult and the live dashboard trajectory.
_MAX_STDOUT_CAPTURE_BYTES = 32 * 1024 * 1024
_MAX_STDERR_CAPTURE_BYTES = 4 * 1024 * 1024
_MAX_LIVE_TRAJECTORY_BYTES = 16 * 1024 * 1024
_TEE_COMPACTION_SLACK_BYTES = 1024 * 1024


def _append_bounded_tail(buffer: bytearray, chunk: bytes, limit: int) -> None:
    if not chunk or limit <= 0:
        return
    if len(chunk) >= limit:
        buffer[:] = chunk[-limit:]
        return
    overflow = len(buffer) + len(chunk) - limit
    if overflow > 0:
        del buffer[:overflow]
    buffer.extend(chunk)


def _open_trajectory_file(path: Path):
    """Open a live trajectory below an anchored, no-follow parent directory.

    The worker/agent can write the run tree while the dashboard reads it. A
    normal ``path.parent.mkdir(); open(path, 'wb')`` sequence would follow a
    swapped ``round_*`` or ``logs`` symlink and redirect screenshots/tool
    traces outside the run. Keep the parent descriptor anchored through the
    open, then retain only the file descriptor used by the tee.
    """

    parent_fd = _ensure_dir_fd_nofollow(path.parent)
    fd: int | None = None
    try:
        fd = _open_private_regular_at(
            parent_fd,
            path.name,
            os.O_WRONLY,
            mode=0o600,
        )
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("trajectory file is not a private regular file")
        # Truncate only after the regular-file/unique-inode check. Passing
        # O_TRUNC into the open would damage an external hard-link alias
        # before we had a chance to reject it.
        os.ftruncate(fd, 0)
        handle = os.fdopen(fd, "wb", buffering=0)
        fd = None
        return handle
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


class LocalEnvironment:
    def __init__(self, tmp_dir: str | None = None) -> None:
        # Library usage falls back to user-scoped scratch storage.
        self._tmp_dir = Path(tmp_dir).expanduser() if tmp_dir else Path(DEFAULT_TMP_DIR)

    @property
    def staging_dir(self) -> Path:
        """Where callers may stage files before uploading them into this env."""
        return self._tmp_dir

    async def exec(
        self,
        command: str,
        timeout: int = 30,
        tee_path: str | None = None,
    ) -> ExecResult:
        start = time.monotonic()
        proc = None
        io_task: asyncio.Task[None] | None = None
        stdout_chunks = bytearray()
        stderr_chunks = bytearray()
        try:
            # The Web bearer token protects the supervisor control plane.  An
            # embedded ``run --dashboard`` executes agent CLIs directly from
            # this process, so relying only on the standalone supervisor's
            # worker sanitisation would expose that credential to the agent.
            child_env = os.environ.copy()
            child_env.pop("LH_HARNESS_WEB_TOKEN", None)
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own session, so one killpg reaps the agent CLI and everything
                # it spawned. It also detaches the child from our terminal, so
                # Ctrl+C never reaches it, so every exit path below must kill it.
                start_new_session=True,
                env=child_env,
                # Claude Code emits one JSON object per line in stream-json mode.
                # A single line (e.g. a tool_result carrying a base64 screenshot)
                # can far exceed asyncio's default 64KB StreamReader limit and
                # truncate the trajectory. Raise the limit so full lines survive.
                limit=64 * 1024 * 1024,
            )
            track_process_group(proc.pid)
            # Always drain incrementally. Besides powering the live dashboard,
            # this leaves the bytes already received available if a timeout or
            # cancellation happens before the child exits normally.
            io_task = asyncio.create_task(
                self._communicate_streaming(
                    proc,
                    tee_path,
                    stdout_chunks,
                    stderr_chunks,
                )
            )
            await asyncio.wait_for(asyncio.shield(io_task), timeout=timeout)
            return ExecResult(
                stdout=bytes(stdout_chunks).decode("utf-8", errors="replace"),
                stderr=bytes(stderr_chunks).decode("utf-8", errors="replace"),
                exit_code=proc.returncode if proc.returncode is not None else -1,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except asyncio.TimeoutError:
            await self._terminate(proc)
            await self._finish_io(io_task)
            captured_stderr = bytes(stderr_chunks).decode("utf-8", errors="replace")
            timeout_message = f"Command timed out after {timeout}s"
            return ExecResult(
                stdout=bytes(stdout_chunks).decode("utf-8", errors="replace"),
                stderr=(captured_stderr + "\n" + timeout_message).lstrip("\n"),
                exit_code=-1,
                duration_ms=int((time.monotonic() - start) * 1000),
                termination_reason="timeout",
            )
        except BaseException:
            # Covers Ctrl+C and task cancellation: kill the agent before the
            # exception unwinds, or it keeps running with no parent.
            await self._terminate(proc)
            await self._finish_io(io_task)
            raise
        finally:
            if proc is not None:
                untrack_process_group(proc.pid)

    @staticmethod
    async def _terminate(proc) -> None:
        """SIGTERM the agent's whole group, escalating to SIGKILL if it lingers."""
        if proc is None or proc.returncode is not None:
            return
        signal_process_group(proc.pid, signal.SIGTERM)
        try:
            # Shielded because this often runs while a CancelledError propagates,
            # and an unshielded await would be cancelled before the child exits.
            await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=5))
        except asyncio.TimeoutError:
            signal_process_group(proc.pid, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=5))
        except asyncio.CancelledError:
            # Cancelled again mid-wait: fall back to the blocking sweep so the
            # agent cannot outlive us.
            kill_process_group(proc.pid)

    @staticmethod
    async def _finish_io(io_task: asyncio.Task[None] | None) -> None:
        if io_task is None or io_task.done():
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(io_task), timeout=5)

    @staticmethod
    async def _communicate_streaming(
        proc,
        tee_path: str | None,
        stdout_chunks: bytearray,
        stderr_chunks: bytearray,
    ) -> None:
        """Drain both streams while optionally teeing stdout to a live file.

        stdout is written incrementally (one line at a time) so an external
        reader (the dashboard) sees the agent's stream-json trajectory grow
        live. The caller owns the chunk lists, so partial output survives even
        when the wait is interrupted by timeout or cancellation.
        """
        path = Path(tee_path) if tee_path else None

        async def _read_stdout() -> None:
            # Open in binary truncating mode once at episode start. Every later
            # save is guarded against replacing this partial stream with empty
            # output.
            fh = _open_trajectory_file(path) if path is not None else None
            artifact_writer = (
                StreamingTrajectoryArtifactWriter.from_live_path(path)
                if path is not None
                else None
            )
            tee_tail = bytearray()
            tee_size = 0
            compact_after = _MAX_LIVE_TRAJECTORY_BYTES
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    _append_bounded_tail(stdout_chunks, line, _MAX_STDOUT_CAPTURE_BYTES)
                    if fh is not None:
                        try:
                            _append_bounded_tail(tee_tail, line, _MAX_LIVE_TRAJECTORY_BYTES)
                            fh.write(line)
                            tee_size += len(line)
                            if tee_size > compact_after:
                                # Keep the most recent complete-ish JSONL tail.
                                # The first retained line may be partial; all
                                # trajectory parsers deliberately skip malformed
                                # records and continue with later complete lines.
                                fh.seek(0)
                                fh.write(tee_tail)
                                fh.truncate()
                                tee_size = len(tee_tail)
                                compact_after = _MAX_LIVE_TRAJECTORY_BYTES + _TEE_COMPACTION_SLACK_BYTES
                        except OSError:
                            pass
                    if artifact_writer is not None:
                        try:
                            artifact_writer.consume_line(line)
                        except (OSError, ValueError):
                            # Raw provider output is still the authoritative
                            # diagnostic stream. A screenshot persistence error
                            # must not deadlock or abort the agent process.
                            artifact_writer = None
            finally:
                if fh is not None:
                    fh.close()

        async def _read_stderr() -> None:
            while True:
                chunk = await proc.stderr.read(64 * 1024)
                if not chunk:
                    return
                _append_bounded_tail(stderr_chunks, chunk, _MAX_STDERR_CAPTURE_BYTES)

        await asyncio.gather(_read_stdout(), _read_stderr(), proc.wait())

    async def screenshot(self) -> bytes:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        path = self._tmp_dir / "_lh_harness_screenshot.png"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return b""
        quoted_path = shlex.quote(str(path))
        command = (
            f"screencapture -x {quoted_path} 2>/dev/null"
            if sys.platform == "darwin"
            else (
                f"gnome-screenshot -f {quoted_path} 2>/dev/null || "
                f"import -window root {quoted_path} 2>/dev/null"
            )
        )
        result = await self.exec(command, timeout=10)
        if result is not None and result.exit_code != 0:
            return b""
        return path.read_bytes() if path.exists() else b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, remote_path)

    async def download(self, remote_path: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(remote_path, local_path)
