from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..types import DEFAULT_TMP_DIR, ExecResult
from ..supervisor.control_bus import _ensure_dir_fd_nofollow, _open_private_regular_at
from ..trajectory_artifacts import StreamingTrajectoryArtifactWriter
from ..utils import paths as long_paths
from ..utils.process_group import (
    force_kill_process_group,
    kill_process_group,
    new_process_group_kwargs,
    terminate_process_group,
    track_process_group,
    untrack_process_group,
)

IS_WINDOWS = sys.platform == "win32"


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

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        tee_path: str | None = None,
    ) -> ExecResult:
        """Run an argv list directly, with no shell in between.

        The agent's working directory, environment and prompt all travel as real
        subprocess arguments, so nothing here has to be quoted for a shell.
        """
        return await self._spawn(
            list(argv),
            timeout=timeout,
            cwd=cwd,
            env=env,
            stdin=stdin,
            tee_path=tee_path,
            shell=False,
        )

    async def exec(
        self,
        command: str,
        timeout: int = 30,
        tee_path: str | None = None,
    ) -> ExecResult:
        """Run a shell command string. Escape hatch: the caller owns the syntax.

        Nothing in the harness's own agent path uses this -- it exists for
        callers that need a pipeline or a builtin. The shell is chosen
        explicitly rather than inherited from ``COMSPEC``/``/bin/sh`` guesswork.
        """
        argv = ["cmd.exe", "/c", command] if IS_WINDOWS else ["/bin/sh", "-c", command]
        return await self._spawn(
            argv, timeout=timeout, cwd=None, env=None, stdin=None, tee_path=tee_path, shell=True
        )

    async def makedirs(self, path: str) -> None:
        long_paths.makedirs(Path(path).expanduser())

    async def write_text(self, path: str, content: str) -> None:
        long_paths.write_text(Path(path).expanduser(), content)

    async def _spawn(
        self,
        argv: list[str],
        *,
        timeout: int,
        cwd: str | None,
        env: Mapping[str, str] | None,
        stdin: str | None,
        tee_path: str | None,
        shell: bool,
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
            child_env.update(env or {})
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                # Overrides are layered onto the real environment (minus the
                # scrubbed token); a partial mapping would wipe PATH and the
                # agent's own credentials.
                env=child_env,
                # Own group, so one sweep reaps the agent CLI and everything it
                # spawned. It also detaches the child from our terminal, so
                # Ctrl+C never reaches it, so every exit path below must kill it.
                **new_process_group_kwargs(),
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
                    stdin,
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
        """Ask the agent's whole group to stop, escalating if it lingers."""
        if proc is None or proc.returncode is not None:
            return
        terminate_process_group(proc.pid)
        try:
            # Shielded because this often runs while a CancelledError propagates,
            # and an unshielded await would be cancelled before the child exits.
            await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=5))
        except asyncio.TimeoutError:
            force_kill_process_group(proc.pid)
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
        stdin: str | None,
        tee_path: str | None,
        stdout_chunks: bytearray,
        stderr_chunks: bytearray,
    ) -> None:
        """Feed stdin, then drain both streams, optionally teeing stdout live.

        stdout is written incrementally (one line at a time) so an external
        reader (the dashboard) sees the agent's stream-json trajectory grow
        live. The caller owns the chunk lists, so partial output survives even
        when the wait is interrupted by timeout or cancellation.
        """
        path = Path(tee_path) if tee_path else None
        if path is not None:
            long_paths.makedirs(path.parent)

        async def _write_stdin() -> None:
            # The prompt goes straight down the pipe, which is what removes the
            # `< prompt.md` redirect (and therefore the shell) from the agent
            # command. Closing stdin is what tells the CLI the prompt is done.
            if proc.stdin is None:
                return
            try:
                if stdin:
                    proc.stdin.write(stdin.encode("utf-8"))
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                    proc.stdin.close()

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

        await asyncio.gather(_write_stdin(), _read_stdout(), _read_stderr(), proc.wait())

    async def screenshot(self) -> bytes:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        path = self._tmp_dir / "_lh_harness_screenshot.png"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return b""
        for argv in _screenshot_commands(path):
            result = await self.run(argv, timeout=15)
            if result.exit_code == 0 and path.exists() and path.stat().st_size:
                break
        return path.read_bytes() if path.exists() else b""

    async def upload(self, local_path: str, remote_path: str) -> None:
        long_paths.makedirs(Path(remote_path).parent)
        shutil.copy2(long_paths.os_path(local_path), long_paths.os_path(remote_path))

    async def download(self, remote_path: str, local_path: str) -> None:
        long_paths.makedirs(Path(local_path).parent)
        shutil.copy2(long_paths.os_path(remote_path), long_paths.os_path(local_path))


def _screenshot_commands(path: Path) -> list[list[str]]:
    """Whole-screen capture, tried in order until one produces a file."""
    if IS_WINDOWS:
        # The scratch directory is caller-controlled and an apostrophe is a
        # legal Windows filename character, so it has to be escaped for the
        # single-quoted PowerShell literal below -- otherwise the path would
        # close the string and the rest would run as script.
        literal = str(path).replace("'", "''")
        # .NET is always present on Windows, so this needs no extra install.
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
            "$g=[System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
            f"$bmp.Save('{literal}',[System.Drawing.Imaging.ImageFormat]::Png)"
        )
        return [["powershell", "-NoProfile", "-NonInteractive", "-Command", script]]
    if sys.platform == "darwin":
        return [["screencapture", "-x", str(path)]]
    return [
        ["gnome-screenshot", "-f", str(path)],
        ["import", "-window", "root", str(path)],
        ["scrot", str(path)],
    ]
