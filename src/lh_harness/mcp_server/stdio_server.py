"""A thin MCP (Model Context Protocol) layer over the existing harness.

This module deliberately exposes only the five operations the harness already
owns -- start, status, stop, resume, and feedback -- and routes every one of
them through the same :class:`~lh_harness.supervisor.service.RunSupervisor`
and :class:`~lh_harness.supervisor.control_bus.ControlBus` the dashboard uses.

Two properties are load-bearing:

**No faked success.** Every tool returns the value the underlying call
produced. A run that failed reports the failure; a run that is still alive
reports ``alive``. Nothing here synthesises a ``status="complete"``, a
``verified=True``, or a checkpoint that was never written. Operations the
harness does not support raise instead of pretending.

**No second owner.** The MCP layer never executes rounds, never resolves
approvals, and never writes run state directly. It appends *commands* to the
control bus and reads *receipts* and *status* back, exactly as the dashboard
does. The worker remains the only actor; a command appended here is consumed by
the same blocking wait that consumes a dashboard command, and it lands in the
same append-only log with the same revision and receipt semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Awaitable

from ..supervisor.control_bus import ControlBus
from ..supervisor.service import RunSupervisor
from ..utils.run_boundary import safe_run_dir

logger = logging.getLogger(__name__)

# The author recorded on control-bus commands appended through this layer.
# It identifies the *source*, not the actor: the worker still acts on the
# command, and the dashboard's commands remain "operator".
MCP_COMMAND_AUTHOR = "mcp"

# The control-bus command kind the worker drains for free-form instructions.
# This is the kind ``RunState.add_injection`` appends, so MCP feedback and
# dashboard feedback travel the same path and reach the same prompt.
INJECT_INSTRUCTION_KIND = "inject_instruction"


class MCPToolError(RuntimeError):
    """A tool could not complete. The message is surfaced to the caller."""


def _tool_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Build one MCP tool definition in the shape ``tools/list`` expects."""

    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool_schema(
        "lh_start_run",
        "Start a new long-horizon run. Returns the run id and the supervisor's "
        "own record of the run.",
        {
            "task": {"type": "string", "description": "What the run should accomplish."},
            "workspace": {
                "type": "string",
                "description": "Directory the run may work in. Defaults to the harness default.",
            },
            "agent": {
                "type": "string",
                "description": "Agent CLI to drive (e.g. claude_code).",
                "default": "claude_code",
            },
            "model": {"type": "string", "description": "Model identifier, if the agent needs one."},
            "max_rounds": {
                "type": "integer",
                "description": "Maximum number of manager/executor/auditor rounds.",
                "minimum": 1,
            },
            "idempotency_key": {
                "type": "string",
                "description": "Replay guard. A repeated key returns the first result instead of "
                "starting a second run.",
            },
        },
        required=["task"],
    ),
    _tool_schema(
        "lh_run_status",
        "Read the current status of a run, as recorded by the supervisor and the control bus.",
        {
            "run_id": {"type": "string", "description": "The run to inspect."},
        },
        required=["run_id"],
    ),
    _tool_schema(
        "lh_stop_run",
        "Request that a running run stop. The worker performs the shutdown; the call returns "
        "the supervisor's receipt for the request.",
        {
            "run_id": {"type": "string", "description": "The run to stop."},
        },
        required=["run_id"],
    ),
    _tool_schema(
        "lh_resume_run",
        "Resume an interrupted run. 'continue' reopens the same run directory so the worker "
        "picks up its recorded rounds; 'retry' starts a fresh run from the saved task.",
        {
            "run_id": {"type": "string", "description": "The run to resume."},
            "mode": {
                "type": "string",
                "enum": ["continue", "retry"],
                "default": "continue",
                "description": "How to resume.",
            },
            "extra_rounds": {
                "type": "integer",
                "description": "Additional rounds to grant, when the run needs more budget.",
                "minimum": 1,
            },
            "idempotency_key": {
                "type": "string",
                "description": "Replay guard. A repeated key returns the first result.",
            },
        },
        required=["run_id"],
    ),
    _tool_schema(
        "lh_inject_feedback",
        "Queue a free-form instruction for the next round of a run. The instruction is appended "
        "to the control bus and is drained into the manager's prompt at the next end-of-round "
        "gate, exactly as a dashboard injection is.",
        {
            "run_id": {"type": "string", "description": "The run to send the instruction to."},
            "instructions": {
                "type": "string",
                "description": "The instruction text handed to the manager.",
            },
            "command_id": {
                "type": "string",
                "description": "Optional idempotency key. A repeated id replays instead of "
                "queueing a second instruction.",
            },
        },
        required=["run_id", "instructions"],
    ),
]


class LongHorizonMCPServer:
    """Serve the harness's run lifecycle over MCP.

    The server owns no run state of its own. It holds a
    :class:`RunSupervisor` for lifecycle operations and opens a
    :class:`ControlBus` per run directory for the command log.
    """

    def __init__(
        self,
        *,
        state_root: str | Path = "~/.lh-harness",
        workspace_root: str | Path | None = None,
        supervisor: RunSupervisor | None = None,
    ) -> None:
        self.state_root = Path(state_root).expanduser()
        self.runs_root = self.state_root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        if supervisor is not None:
            self.supervisor = supervisor
        else:
            # The supervisor is rooted at the state directory; each run still
            # names its own workspace, so this does not restrict where a run
            # may work.
            self.supervisor = RunSupervisor(
                str(self.runs_root),
                workspace_root=str(workspace_root) if workspace_root else None,
            )
        self.tools: dict[str, dict[str, Any]] = {
            definition["name"]: definition for definition in TOOL_DEFINITIONS
        }

    # -- helpers ---------------------------------------------------------

    def _require_run(self, run_id: str) -> Path:
        """Return the run directory, or fail if the run does not exist.

        The supervisor's ``status`` answers ``{"status": "idle", "alive":
        False}`` for a run it has never seen, which is indistinguishable from a
        real idle run. Checking the directory keeps "no such run" from being
        reported as a healthy-but-idle run.

        The run id is validated with the supervisor's own rules before the
        path is built. Joining an unchecked id onto the run root would let
        a caller point at a directory outside it -- ``../../elsewhere`` or
        an absolute path -- and the control bus would then be created there.
        """

        try:
            RunSupervisor._validate_run_id(run_id)
        except ValueError as exc:
            raise MCPToolError(f"invalid run id: {exc}") from exc
        run_dir = safe_run_dir(self.runs_root, run_id)
        if run_dir is None:
            raise MCPToolError(f"invalid run id: {run_id}")
        if not run_dir.is_dir():
            raise MCPToolError(f"run not found: {run_id}")
        return run_dir

    def _control_bus(self, run_id: str) -> ControlBus:
        """Return the control bus for ``run_id``.

        The control bus lives inside the run directory, so the same commands
        and receipts the dashboard reads are the ones this layer writes.
        """

        # ControlBus takes the run directory and appends "control" itself.
        return ControlBus(self._require_run(run_id))

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking supervisor call off the event loop."""

        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: fn(*args, **kwargs)
        )

    # -- tools -----------------------------------------------------------

    async def start_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Start a run through the supervisor and return its real record."""

        task = str(arguments.get("task") or "").strip()
        if not task:
            raise MCPToolError("task is required")
        max_rounds = arguments.get("max_rounds")
        if max_rounds is not None:
            max_rounds = int(max_rounds)
        created = await self._call(
            self.supervisor.create_run,
            task=task,
            workspace=arguments.get("workspace"),
            agent=str(arguments.get("agent") or "claude_code"),
            model=arguments.get("model"),
            max_rounds=max_rounds if max_rounds is not None else _default_max_rounds(),
            idempotency_key=arguments.get("idempotency_key"),
        )
        return {"run_id": str(created.get("id")), "run": created}

    async def run_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Read the supervisor's view of a run."""

        run_id = str(arguments.get("run_id") or "").strip()
        if not run_id:
            raise MCPToolError("run_id is required")
        self._require_run(run_id)
        status = await self._call(self.supervisor.status, run_id)
        return {"run_id": run_id, "status": status}

    async def stop_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Request a stop and return the supervisor's receipt."""

        run_id = str(arguments.get("run_id") or "").strip()
        if not run_id:
            raise MCPToolError("run_id is required")
        self._require_run(run_id)
        receipt = await self._call(self.supervisor.stop, run_id)
        return {"run_id": run_id, "receipt": receipt}

    async def resume_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Resume a run and return the supervisor's real outcome."""

        run_id = str(arguments.get("run_id") or "").strip()
        if not run_id:
            raise MCPToolError("run_id is required")
        mode = str(arguments.get("mode") or "continue")
        extra_rounds = arguments.get("extra_rounds")
        if extra_rounds is not None:
            extra_rounds = int(extra_rounds)
        self._require_run(run_id)
        result = await self._call(
            self.supervisor.resume,
            run_id,
            mode=mode,
            extra_rounds=extra_rounds,
            idempotency_key=arguments.get("idempotency_key"),
        )
        return {"run_id": run_id, "resumed": result}

    async def inject_feedback(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Queue an instruction on the control bus for the next round.

        This appends the same ``inject_instruction`` command the dashboard
        appends, so the worker drains it through the same path.
        """

        run_id = str(arguments.get("run_id") or "").strip()
        if not run_id:
            raise MCPToolError("run_id is required")
        instructions = str(arguments.get("instructions") or "").strip()
        if not instructions:
            raise MCPToolError("instructions is required")
        bus = self._control_bus(run_id)
        try:
            command = bus.append(
                INJECT_INSTRUCTION_KIND,
                {"instructions": instructions},
                created_by=MCP_COMMAND_AUTHOR,
                command_id=arguments.get("command_id"),
            )
        except Exception as exc:  # surfaced verbatim to the caller
            raise MCPToolError(f"could not queue feedback: {exc}") from exc
        return {
            "run_id": run_id,
            "command_id": command.get("command_id"),
            "revision": command.get("revision"),
            "instructions": instructions,
            "queued": True,
        }

    # -- dispatch --------------------------------------------------------

    async def handle_tool_call(
        self, name: str | None, arguments: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Dispatch one tool call and return its result.

        Errors are returned as ``{"success": False, "error": ...}`` rather
        than raised, so a failing tool does not take the stdio loop down.
        """

        arguments = arguments or {}
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "lh_start_run": self.start_run,
            "lh_run_status": self.run_status,
            "lh_stop_run": self.stop_run,
            "lh_resume_run": self.resume_run,
            "lh_inject_feedback": self.inject_feedback,
        }
        handler = handlers.get(str(name or ""))
        if handler is None:
            return {
                "success": False,
                "error": f"unknown tool: {name}",
                "available": sorted(handlers),
            }
        try:
            result = await handler(arguments)
        except MCPToolError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"success": True, **result}

    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(definition) for definition in TOOL_DEFINITIONS]


def _default_max_rounds() -> int:
    from ..types import DEFAULT_MAX_ROUNDS

    return int(DEFAULT_MAX_ROUNDS)


# -- stdio transport ---------------------------------------------------


async def serve_stdio(server: LongHorizonMCPServer) -> None:
    """Serve MCP over stdio using newline-delimited JSON-RPC.

    The harness has no MCP SDK dependency, so the protocol is spoken directly:
    one JSON object per line in, one JSON object per line out.
    """

    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except Exception:  # pragma: no cover - stdin closed underneath us
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _write_response(
                _error(None, _JSONRPC_PARSE_ERROR, f"invalid JSON: {exc}")
            )
            continue
        try:
            response = await _dispatch(server, request)
        except Exception as exc:  # a bad message must not end the session
            logger.exception("dispatch failed")
            response = _error(
                None if not isinstance(request, dict) else request.get("id"),
                _JSONRPC_INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
            )
        # A notification is not answered.
        if response is not None:
            _write_response(response)


_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response."""

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


async def _dispatch(
    server: LongHorizonMCPServer, request: Any
) -> dict[str, Any] | None:
    """Handle one JSON-RPC message.

    Returns ``None`` for a notification. A notification carries no id, and
    the protocol requires that no response is written for one -- replying to
    ``notifications/initialized`` with "method not found" is a protocol
    violation that makes a real client fail the handshake.
    """

    if not isinstance(request, dict):
        return _error(None, _JSONRPC_INVALID_REQUEST, "request must be a JSON object")

    method = request.get("method")
    request_id = request.get("id")
    # A message without an id is a notification and must not be answered.
    is_notification = "id" not in request

    if not isinstance(method, str) or not method:
        if is_notification:
            return None
        return _error(request_id, _JSONRPC_INVALID_REQUEST, "method must be a string")

    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "long-horizon-harness", "version": "1"},
        }
    elif method in {"notifications/initialized", "initialized", "notifications/cancelled"}:
        # A lifecycle notification. The client has nothing to wait for.
        return None
    elif method in {"tools/list", "listTools"}:
        result = {"tools": server.list_tools()}
    elif method in {"tools/call", "callTool"}:
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if is_notification:
                return None
            return _error(
                request_id, _JSONRPC_INVALID_PARAMS, "params must be an object"
            )
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            if is_notification:
                return None
            return _error(
                request_id, _JSONRPC_INVALID_PARAMS, "arguments must be an object"
            )
        if not isinstance(name, str) or not name:
            if is_notification:
                return None
            return _error(request_id, _JSONRPC_INVALID_PARAMS, "name is required")
        payload = await server.handle_tool_call(name, arguments)
        result = {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": not payload.get("success", True),
        }
    else:
        if is_notification:
            return None
        return _error(request_id, _JSONRPC_METHOD_NOT_FOUND, f"method not found: {method}")

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _write_response(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def run_mcp_server(
    *,
    state_root: str = "~/.lh-harness",
    transport: str = "stdio",
) -> None:
    """Serve the MCP server until stdin closes.

    This is a coroutine so the caller owns the event loop: the CLI wraps it in
    ``asyncio.run``. Logging is the caller's business, because the caller
    decides where the handler goes -- stdout belongs to the protocol.
    """

    if transport != "stdio":
        raise ValueError(f"unsupported transport: {transport}")
    server = LongHorizonMCPServer(state_root=state_root)
    await serve_stdio(server)


if __name__ == "__main__":  # pragma: no cover - manual entry point
    asyncio.run(
        run_mcp_server(state_root=os.environ.get("LH_HARNESS_STATE_ROOT", "~/.lh-harness"))
    )
