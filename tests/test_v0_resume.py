"""v0 resumability proof (PLAN.md §10, §13).

No network, no real `claude`/`codex` binary, no API key: every "agent CLI" in
this file is tests/fixtures/fake_stream_agent.py, a deterministic stdlib
script. Two of the four cases actually SIGKILL a live OS process to prove
resume survives a real crash, not just an in-process exception.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from lh_harness.environment.local import LocalEnvironment  # noqa: E402
from lh_harness.types import EpisodeBudget  # noqa: E402
from lh_harness.v0.events import EventLog  # noqa: E402
from lh_harness.v0.run_dir import create_run_dir  # noqa: E402
from lh_harness.v0.runner import run_node  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"
RUN_NODE_SUBPROCESS = _REPO_ROOT / "tests" / "fixtures" / "run_node_subprocess.py"


# ---------------------------------------------------------------------------
# Process helpers for the kill -9 tests
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout: float, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


def _wait_for_event(events_file: Path, node_id: str, event_type: str, timeout: float = 10.0):
    log = EventLog(events_file)
    box: dict = {}

    def _check():
        found = log.last_event(node_id, event_type)
        if found is not None:
            box["event"] = found
            return True
        return False

    _wait_until(_check, timeout=timeout)
    return box.get("event")


def _read_pid(pidfile: Path, timeout: float = 5.0) -> int:
    def _has_content():
        return pidfile.exists() and pidfile.read_text().strip() != ""

    if not _wait_until(_has_content, timeout=timeout):
        raise TimeoutError(f"pidfile never populated: {pidfile}")
    return int(pidfile.read_text().strip())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_pgid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _launch_runner_subprocess(
    *, run_dir: Path, node_id: str, pidfile: Path, prompt_dir: Path,
    startup_delay: float, work_delay: float, timeout: int = 60,
) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(RUN_NODE_SUBPROCESS),
        "--run-dir", str(run_dir),
        "--node-id", node_id,
        "--script-path", str(FAKE_CLI),
        "--pidfile", str(pidfile),
        "--prompt-dir", str(prompt_dir),
        "--startup-delay", str(startup_delay),
        "--work-delay", str(work_delay),
        "--timeout", str(timeout),
    ]
    # start_new_session=True makes this subprocess its own process-group
    # leader (pgid == pid), so os.killpg(proc.pid, ...) reliably reaps it —
    # the same reason LocalEnvironment.exec does this for the agent CLI.
    return subprocess.Popen(
        cmd, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def _crash_and_verify_dead(proc: subprocess.Popen, fake_pid: int) -> None:
    """SIGKILL both the runner process and the actual fake-CLI process.

    The fake CLI runs under LocalEnvironment, which launches it with its own
    `start_new_session=True` — it becomes a session leader in a *different*
    process group from the runner script the instant it's spawned. Killing
    only the runner's group would leave it running, orphaned, exactly the
    leak lh_harness.utils.process_group exists to prevent for SIGTERM; SIGKILL
    can't be caught, so the test has to reap both groups itself.
    """
    _kill_pgid(fake_pid)
    _kill_pgid(proc.pid)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    assert _wait_until(lambda: not _pid_alive(fake_pid), timeout=5), (
        "fake CLI child process survived the kill -9"
    )


class ResumeAfterSessionCrashTest(unittest.TestCase):
    def test_crash_after_session_capture_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            node_id = "writer_1"
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            pidfile = root / "writer_1.pid"

            proc = _launch_runner_subprocess(
                run_dir=run_dir,
                node_id=node_id,
                pidfile=pidfile,
                prompt_dir=prompt_dir,
                startup_delay=0.0,
                work_delay=3.0,
            )
            try:
                session_event = _wait_for_event(
                    run_dir / "events.jsonl", node_id, "session_captured", timeout=10
                )
                self.assertIsNotNone(session_event, "session_captured never appeared before timeout")
                original_session_id = session_event["session_id"]

                fake_pid = _read_pid(pidfile)
                self.assertTrue(_pid_alive(fake_pid), "fake CLI process should be alive right after capture")

                _crash_and_verify_dead(proc, fake_pid)
            finally:
                if proc.poll() is None:
                    _kill_pgid(proc.pid)
                    proc.wait(timeout=10)

            log = EventLog(run_dir / "events.jsonl")
            node_events = [e for e in log.read_all() if e.get("node_id") == node_id]
            self.assertTrue(any(e["type"] == "session_captured" for e in node_events))
            self.assertFalse(any(e["type"] == "episode_completed" for e in node_events))

            # Resume in-process, pointed at the same run_dir/node_id.
            adapter = FakeStreamAgentAdapter(
                script_path=str(FAKE_CLI),
                pidfile=str(root / "writer_1_resume.pid"),
                prompt_dir=str(prompt_dir),
                workspace_path=str(run_dir),
            )
            env = LocalEnvironment(tmp_dir=str(prompt_dir))
            budget = EpisodeBudget(max_duration_seconds=30)
            result = asyncio.run(run_node(run_dir, node_id, "do the task", adapter, env, budget))

            self.assertEqual(result.status, "done")

            all_events = [e for e in log.read_all() if e.get("node_id") == node_id]
            completed = [e for e in all_events if e["type"] == "episode_completed"]
            self.assertEqual(len(completed), 1, f"expected exactly one episode_completed, got {completed}")

            redispatches = [e for e in all_events if e["type"] == "node_redispatched"]
            self.assertTrue(
                any(e.get("reason") == "resumed_session" for e in redispatches),
                f"expected a resumed_session redispatch, got {redispatches}",
            )

            artifact_path = run_dir / "out" / f"{node_id}.md"
            self.assertTrue(artifact_path.exists())
            artifact_text = artifact_path.read_text()
            # Proves continuation actually happened, not a lucky fresh redispatch:
            # only the fake CLI's --resume branch prints these.
            self.assertIn("resume_acknowledged", artifact_text)
            self.assertIn(original_session_id, artifact_text)


class ResumeBeforeSessionCrashTest(unittest.TestCase):
    def test_crash_before_session_capture_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run2")
            node_id = "writer_2"
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            pidfile = root / "writer_2.pid"

            proc = _launch_runner_subprocess(
                run_dir=run_dir,
                node_id=node_id,
                pidfile=pidfile,
                prompt_dir=prompt_dir,
                startup_delay=3.0,
                work_delay=0.0,
            )
            try:
                # The pidfile is written before the startup-delay sleep, so it
                # appears almost immediately — well before any stdout line
                # could exist, giving a reliable "no output yet" kill window.
                fake_pid = _read_pid(pidfile, timeout=5)
                self.assertTrue(_pid_alive(fake_pid))

                _crash_and_verify_dead(proc, fake_pid)
            finally:
                if proc.poll() is None:
                    _kill_pgid(proc.pid)
                    proc.wait(timeout=10)

            log = EventLog(run_dir / "events.jsonl")
            node_events = [e for e in log.read_all() if e.get("node_id") == node_id]
            self.assertTrue(any(e["type"] == "node_dispatched" for e in node_events))
            self.assertFalse(any(e["type"] == "session_captured" for e in node_events))
            self.assertFalse(any(e["type"] == "episode_completed" for e in node_events))

            adapter = FakeStreamAgentAdapter(
                script_path=str(FAKE_CLI),
                pidfile=str(root / "writer_2_resume.pid"),
                prompt_dir=str(prompt_dir),
                workspace_path=str(run_dir),
            )
            env = LocalEnvironment(tmp_dir=str(prompt_dir))
            budget = EpisodeBudget(max_duration_seconds=30)
            result = asyncio.run(run_node(run_dir, node_id, "do the task", adapter, env, budget))

            self.assertEqual(result.status, "done")

            all_events = [e for e in log.read_all() if e.get("node_id") == node_id]
            completed = [e for e in all_events if e["type"] == "episode_completed"]
            self.assertEqual(len(completed), 1, f"expected exactly one episode_completed, got {completed}")

            redispatches = [e for e in all_events if e["type"] == "node_redispatched"]
            self.assertTrue(
                any(e.get("reason") == "no_session_captured" for e in redispatches),
                f"expected a no_session_captured redispatch, got {redispatches}",
            )

            artifact_path = run_dir / "out" / f"{node_id}.md"
            self.assertTrue(artifact_path.exists())
            self.assertIn("session_id", artifact_path.read_text())


class ResumeAfterCompleteIsNoopTest(unittest.TestCase):
    def test_resume_after_complete_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run3")
            node_id = "writer_3"
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            adapter = FakeStreamAgentAdapter(
                script_path=str(FAKE_CLI),
                pidfile=str(root / "writer_3.pid"),
                prompt_dir=str(prompt_dir),
                workspace_path=str(run_dir),
            )
            env = LocalEnvironment(tmp_dir=str(prompt_dir))
            budget = EpisodeBudget(max_duration_seconds=30)

            first = asyncio.run(run_node(run_dir, node_id, "do the task", adapter, env, budget))
            self.assertEqual(first.status, "done")

            log = EventLog(run_dir / "events.jsonl")
            events_after_first = log.read_all()
            completed_first = [
                e for e in events_after_first
                if e.get("node_id") == node_id and e["type"] == "episode_completed"
            ]
            self.assertEqual(len(completed_first), 1)

            artifact_path = run_dir / "out" / f"{node_id}.md"
            artifact_mtime_before = artifact_path.stat().st_mtime_ns
            artifact_text_before = artifact_path.read_text()

            second = asyncio.run(run_node(run_dir, node_id, "do the task", adapter, env, budget))

            self.assertEqual(second.status, first.status)
            self.assertEqual(second.metadata.get("replayed_from_event_log"), True)

            events_after_second = log.read_all()
            self.assertEqual(
                len(events_after_second),
                len(events_after_first),
                "resume-after-complete must not append any new events",
            )
            self.assertEqual(artifact_path.stat().st_mtime_ns, artifact_mtime_before)
            self.assertEqual(artifact_path.read_text(), artifact_text_before)


class EventLogFsyncTest(unittest.TestCase):
    def test_append_calls_fsync(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            path = Path(root_str) / "events.jsonl"
            log = EventLog(path)

            with mock.patch("lh_harness.v0.events.os.fsync") as fsync_mock:
                log.append({"node_id": "n1", "role": "writer", "round": 0, "type": "node_dispatched"})

            fsync_mock.assert_called_once()
            self.assertTrue(path.exists())
            lines = path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["type"], "node_dispatched")
            self.assertIn("ts", record)

            with mock.patch("lh_harness.v0.events.os.fsync") as fsync_mock_2:
                log.append(
                    {"node_id": "n1", "role": "writer", "round": 0, "type": "session_captured", "session_id": "s1"}
                )
            self.assertEqual(fsync_mock_2.call_count, 1)

            lines = path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
