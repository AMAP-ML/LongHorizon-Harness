"""Tests for the thin MCP layer over the supervisor and control bus.

The point of these tests is that they assert *observable* state. A previous
implementation of this module returned hard-coded ``status="complete"``,
``verified=True``, and success-shaped dictionaries for checkpoints that were
never written or restored; its tests passed because they only checked the shape
of the returned dictionary. Each test here therefore checks the underlying
artifact -- a run directory, a control-bus command, a receipt, a status
record -- rather than the shape of the response.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lh_harness.mcp_server import (
    INJECT_INSTRUCTION_KIND,
    MCP_COMMAND_AUTHOR,
    LongHorizonMCPServer,
)
from lh_harness.supervisor.control_bus import ControlBus
from lh_harness.supervisor.service import RunSupervisor


class FakeProcess:
    """Stand-in for a supervised worker process.

    The pid must belong to a *live* process. On Windows the supervisor stops a
    run by asking ``tasklist`` whether the pid exists, so a fabricated pid
    makes the stop path report "worker is no longer running" -- the same
    outcome as a worker that had already exited. A real child keeps the
    stop path exercised for what it is.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def worker(monkeypatch, request):
    """Prevent the supervisor from actually launching an agent CLI."""

    # A real, long-lived child stands in for the agent CLI, so the supervisor
    # sees a live pid. ``cmd /c ping -t`` is used because it is present on
    # every Windows host and stays alive until it is killed.
    child = subprocess.Popen(
        [os.environ.get("COMSPEC", "cmd.exe"), "/c", "ping", "-t", "127.0.0.1", "-n", "600"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process = FakeProcess(child.pid)
    real_popen = subprocess.Popen

    def _popen(cmd, *args, **kwargs):
        # ``tasklist`` must run for real: it is how Windows asks whether a pid
        # is still alive, and faking it would make every stop look like a
        # worker that had already exited. Only the agent launch is faked.
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] in {"tasklist", "taskkill"}:
            return real_popen(cmd, *args, **kwargs)
        return process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    def _cleanup():
        try:
            child.kill()
        except OSError:
            pass

    request.addfinalizer(_cleanup)

    # os.killpg exists only on POSIX, so it is supplied rather than patched.
    monkeypatch.setattr(
        "lh_harness.supervisor.service.os.killpg", lambda *args, **kwargs: None, raising=False
    )
    # The supervisor confirms a worker's identity by reading the process
    # command line, which it does through ``ps`` -- a POSIX tool. On Windows
    # that probe returns nothing, so a run can never be shown alive and every
    # stop fails with "worker is no longer running". (The harness's own stop
    # test fails here for the same reason.) Answering the probe keeps these
    # tests about the MCP layer rather than about that platform gap.
    monkeypatch.setattr(
        "lh_harness.supervisor.service._ps_query", _fake_ps_query
    )
    return process


# The supervisor's identity check requires the worker's command line to
# contain its run id. The probe reads that id off disk, so it does not depend
# on the order in which the fixtures run.
_RUNS_ROOT: list[Path] = []
_START_MARKER = "start-marker"


def _fake_ps_query(pid: int, field: str) -> str:
    if field.startswith("lstart"):
        return _START_MARKER
    run_id = ""
    root = _RUNS_ROOT[0] if _RUNS_ROOT else None
    if root is not None and root.is_dir():
        for candidate in sorted(root.iterdir()):
            if (candidate / "control" / "owner.json").is_file():
                run_id = candidate.name
    return f"{pid} lh-harness run --run-id {run_id}"


@pytest.fixture
def server(tmp_path: Path) -> LongHorizonMCPServer:
    instance = LongHorizonMCPServer(state_root=tmp_path / "state")
    _RUNS_ROOT.clear()
    _RUNS_ROOT.append(instance.runs_root)
    return instance


# -- the tool surface --------------------------------------------------


def test_only_the_five_lifecycle_tools_are_exposed(server):
    """The review asked for a small layer: start, status, stop, resume, feedback."""

    assert sorted(server.tools) == [
        "lh_inject_feedback",
        "lh_resume_run",
        "lh_run_status",
        "lh_start_run",
        "lh_stop_run",
    ]


def test_tool_definitions_are_valid_mcp_schemas(server):
    for definition in server.list_tools():
        assert definition["name"].startswith("lh_")
        assert definition["description"]
        schema = definition["inputSchema"]
        assert schema["type"] == "object"
        for required in schema["required"]:
            assert required in schema["properties"]


def test_unknown_tool_reports_an_error(server):
    result = _call_sync(server, "lh_does_not_exist")
    assert result["success"] is False
    assert "unknown tool" in result["error"]
    assert "lh_start_run" in result["available"]


def _call_sync(server: LongHorizonMCPServer, name: str, **arguments):
    """Call one tool and return its result.

    The tools are async, but every assertion here is about the artifact the
    tool produced, so a synchronous driver keeps the tests readable.
    """

    import asyncio

    return asyncio.run(server.handle_tool_call(name, arguments))


# -- start -------------------------------------------------------------


def test_start_run_creates_a_real_run_directory(server, worker):
    result = _call_sync(server, "lh_start_run", task="inspect the project", max_rounds=2)

    assert result["success"] is True
    run_id = result["run_id"]
    assert run_id

    # The run directory and its control state exist on disk.
    run_dir = server.runs_root / run_id
    assert run_dir.is_dir()
    assert (run_dir / "control" / "owner.json").is_file()

    # The supervisor agrees the run is alive, rather than the tool asserting it.
    assert server.supervisor.status(run_id)["alive"] is True


def test_start_run_requires_a_task(server, worker):
    result = _call_sync(server, "lh_start_run", task="   ")
    assert result["success"] is False
    assert "task is required" in result["error"]


def test_start_run_is_idempotent_under_a_repeated_key(server, worker):
    first = _call_sync(
        server, "lh_start_run", task="do the thing", max_rounds=1, idempotency_key="k1"
    )
    second = _call_sync(
        server, "lh_start_run", task="do the thing", max_rounds=1, idempotency_key="k1"
    )
    assert first["run_id"] == second["run_id"]


# -- status ------------------------------------------------------------


def test_run_status_reflects_the_supervisor(server, worker):
    started = _call_sync(server, "lh_start_run", task="watch the clock", max_rounds=1)
    run_id = started["run_id"]

    result = _call_sync(server, "lh_run_status", run_id=run_id)
    assert result["success"] is True
    assert result["status"]["alive"] is True
    assert result["status"]["status"]


def test_run_status_of_an_unknown_run_is_an_error(server):
    result = _call_sync(server, "lh_run_status", run_id="no-such-run")
    assert result["success"] is False
    assert "no-such-run" in result["error"]


def test_run_status_requires_a_run_id(server):
    result = _call_sync(server, "lh_run_status", run_id="")
    assert result["success"] is False
    assert "run_id is required" in result["error"]


# -- feedback ----------------------------------------------------------


def test_inject_feedback_appends_a_real_control_command(server, worker):
    started = _call_sync(server, "lh_start_run", task="write the parser", max_rounds=1)
    run_id = started["run_id"]

    result = _call_sync(
        server,
        "lh_inject_feedback",
        run_id=run_id,
        instructions="prefer python 3.11",
    )
    assert result["success"] is True

    # The command is on the control bus, which is what the worker drains.
    bus = ControlBus(server.runs_root / run_id)
    commands = [c for c in bus.commands() if c["kind"] == INJECT_INSTRUCTION_KIND]
    assert len(commands) == 1
    assert commands[0]["payload"]["instructions"] == "prefer python 3.11"
    assert commands[0]["created_by"] == MCP_COMMAND_AUTHOR

    # And the worker-side drain path returns the text. The dashboard state
    # opens the control bus of the *run* directory, so this exercises the same
    # path the worker's end-of-round gate uses.
    from lh_harness.dashboard.state import DashboardState

    state = DashboardState(
        server.runs_root / run_id / "lh_harness", control_enabled=True
    )
    assert state.drain_injections() == ["prefer python 3.11"]


def test_inject_feedback_replays_under_a_repeated_command_id(server, worker):
    started = _call_sync(server, "lh_start_run", task="write the parser", max_rounds=1)
    run_id = started["run_id"]

    first = _call_sync(
        server, "lh_inject_feedback", run_id=run_id, instructions="one", command_id="c1"
    )
    second = _call_sync(
        server, "lh_inject_feedback", run_id=run_id, instructions="one", command_id="c1"
    )
    assert first["command_id"] == second["command_id"]

    bus = ControlBus(server.runs_root / run_id)
    commands = [c for c in bus.commands() if c["kind"] == INJECT_INSTRUCTION_KIND]
    assert len(commands) == 1, "a repeated command id must not queue twice"


def test_inject_feedback_requires_instructions(server, worker):
    started = _call_sync(server, "lh_start_run", task="write the parser", max_rounds=1)
    result = _call_sync(
        server, "lh_inject_feedback", run_id=started["run_id"], instructions="  "
    )
    assert result["success"] is False
    assert "instructions is required" in result["error"]


def test_inject_feedback_to_an_unknown_run_is_an_error(server):
    result = _call_sync(
        server, "lh_inject_feedback", run_id="missing-run", instructions="hello"
    )
    assert result["success"] is False
    assert "not found" in result["error"]


# -- stop --------------------------------------------------------------


def test_stop_run_records_a_real_stop_request(server, worker):
    started = _call_sync(server, "lh_start_run", task="long job", max_rounds=1)
    run_id = started["run_id"]

    result = _call_sync(server, "lh_stop_run", run_id=run_id)
    assert result["success"] is True
    assert result["receipt"]

    # The stop is visible in the control-plane record, not just the response.
    bus = ControlBus(server.runs_root / run_id)
    kinds = {c["kind"] for c in bus.commands()}
    assert "stop" in kinds


def test_stop_run_of_an_unknown_run_is_an_error(server):
    result = _call_sync(server, "lh_stop_run", run_id="missing-run")
    assert result["success"] is False


# -- resume ------------------------------------------------------------


def test_resume_run_reports_the_supervisor_outcome(server, worker):
    started = _call_sync(server, "lh_start_run", task="interrupted job", max_rounds=1)
    run_id = started["run_id"]

    # A run can only be resumed once it is no longer active, so stop it
    # first. The worker is not present to write a final report, so the
    # supervisor reconciles the run to a terminal state itself.
    stopped = _call_sync(server, "lh_stop_run", run_id=run_id)
    assert stopped["success"] is True
    worker.returncode = -15

    result = _call_sync(server, "lh_resume_run", run_id=run_id, mode="continue")
    assert result["success"] is True
    assert result["resumed"], "resume must return the supervisor's own record"


def test_resume_run_refuses_an_active_run(server, worker):
    """The supervisor's own precondition must surface, not be swallowed."""

    started = _call_sync(server, "lh_start_run", task="still running", max_rounds=1)
    result = _call_sync(server, "lh_resume_run", run_id=started["run_id"], mode="continue")
    assert result["success"] is False
    assert "active run" in result["error"]


def test_resume_run_rejects_an_invalid_mode(server, worker):
    started = _call_sync(server, "lh_start_run", task="interrupted job", max_rounds=1)
    result = _call_sync(server, "lh_resume_run", run_id=started["run_id"], mode="rewind")
    assert result["success"] is False
    assert "continue or retry" in result["error"]


# -- stdio transport ---------------------------------------------------


def test_stdio_dispatch_lists_the_tools(server):
    response = _call_sync_dispatch(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == set(server.tools)
    assert response["id"] == 1
    assert response["jsonrpc"] == "2.0"


def _call_sync_dispatch(server, request):
    import asyncio

    from lh_harness.mcp_server.stdio_server import _dispatch

    return asyncio.run(_dispatch(server, request))


def test_stdio_dispatch_calls_a_tool(server, worker):
    response = _call_sync_dispatch(
        server,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "lh_start_run", "arguments": {"task": "via stdio", "max_rounds": 1}},
        },
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert (server.runs_root / payload["run_id"]).is_dir()


def test_stdio_dispatch_reports_an_unknown_method(server):
    response = _call_sync_dispatch(server, {"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert response["error"]["code"] == -32601


def test_stdio_dispatch_marks_a_failing_tool_as_an_error(server):
    response = _call_sync_dispatch(
        server,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "lh_run_status", "arguments": {"run_id": "missing"}},
        },
    )
    assert response["result"]["isError"] is True


def test_initialize_reports_tool_capability(server):
    response = _call_sync_dispatch(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response["result"]["capabilities"]["tools"] == {}
    assert response["result"]["serverInfo"]["name"] == "long-horizon-harness"


# -- the real CLI over stdio -------------------------------------------


def test_cli_serves_the_protocol_over_stdio(tmp_path: Path):
    """Drive the shipped command the way an MCP client would.

    This is the end-to-end check: the process is the real ``lh-harness
    mcp-server`` entry point, the transport is the real stdio loop, and the
    answers come back over the pipe.
    """

    import subprocess
    import sys

    state_root = tmp_path / "state"
    proc = subprocess.Popen(
        [sys.executable, "-m", "lh_harness", "mcp-server", "--state-root", str(state_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    def send(request: dict) -> dict:
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, "server closed stdout without answering"
        return json.loads(line)

    try:
        initialize = send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert initialize["result"]["serverInfo"]["name"] == "long-horizon-harness"

        listed = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in listed["result"]["tools"]} == set(
            {
                "lh_start_run",
                "lh_run_status",
                "lh_stop_run",
                "lh_resume_run",
                "lh_inject_feedback",
            }
        )

        failed = send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "lh_run_status", "arguments": {"run_id": "nope"}},
            }
        )
        assert failed["result"]["isError"] is True
        payload = json.loads(failed["result"]["content"][0]["text"])
        assert payload["success"] is False
        assert "nope" in payload["error"]
    finally:
        proc.stdin.close()
        try:
            exit_code = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            raise

    # Closing stdin ends the session. The server must exit cleanly: a
    # non-zero status or a traceback on stderr means the entry point and the
    # transport disagree about who owns the event loop.
    assert exit_code == 0, f"server exited with {exit_code}"
    stderr = proc.stderr.read() or ""
    assert "Traceback" not in stderr, stderr
    assert "coroutine was expected" not in stderr, stderr
