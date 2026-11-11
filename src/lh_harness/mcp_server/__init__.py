"""MCP (Model Context Protocol) server for LongHorizon-Harness.

The server is a thin layer over the harness's existing run lifecycle: it
exposes start, status, stop, resume, and feedback, and routes each one
through the same :class:`~lh_harness.supervisor.service.RunSupervisor` and
:class:`~lh_harness.supervisor.control_bus.ControlBus` the dashboard uses.

It deliberately does not implement its own task execution, checkpointing, or
verification. Those belong to the harness, and a second implementation would
either duplicate the ownership rules or drift from them.
"""

from .stdio_server import (
    INJECT_INSTRUCTION_KIND,
    MCP_COMMAND_AUTHOR,
    TOOL_DEFINITIONS,
    LongHorizonMCPServer,
    MCPToolError,
    run_mcp_server,
    serve_stdio,
)

__all__ = [
    "INJECT_INSTRUCTION_KIND",
    "MCP_COMMAND_AUTHOR",
    "TOOL_DEFINITIONS",
    "LongHorizonMCPServer",
    "MCPToolError",
    "run_mcp_server",
    "serve_stdio",
]
