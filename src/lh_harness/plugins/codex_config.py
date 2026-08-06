"""Read-only helpers for Codex's own configuration format.

Nothing here writes to `~/.codex/config.toml`. The harness renders MCP blocks
into its own files under `~/.lh-harness/plugins/` and replays them per run, so
installing a plugin never changes what an unrelated `codex` invocation can do.
The path helper exists only so the harness can *report* pre-existing entries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def codex_config_path() -> Path:
    home = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    return Path(home).expanduser() / "config.toml"


def mcp_server_block(name: str, command: str, args: list[str]) -> str:
    """Render one `[mcp_servers.<name>]` table in Codex's TOML format."""
    lines = [f"[mcp_servers.{name}]", f"command = {json.dumps(command)}"]
    if args:
        lines.append("args = [" + ", ".join(json.dumps(arg) for arg in args) + "]")
    return "\n".join(lines) + "\n"
