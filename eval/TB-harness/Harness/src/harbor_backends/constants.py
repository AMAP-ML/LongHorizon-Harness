from __future__ import annotations

from pathlib import Path


CLAUDE_VARIANT_NAME = "cua_harness_claudecode_harbor"

ROLE_BACKEND_CLAUDE_CODE = "claude-code"

CLAUDE_CODE_PROXY_HOST = (
    Path(__file__).resolve().parents[1]
    / "assets"
    / "claudecode_request_proxy.py"
)
CLAUDE_CODE_PROXY_REMOTE = "/usr/local/bin/cua_harness_claudecode_request_proxy.py"

TASK_TOOL_DENYLIST = (
    "Task,TaskCreate,TaskGet,TaskList,TaskOutput,TaskStop,TaskUpdate,"
    "WebFetch,WebSearch"
)
ORCHESTRATOR_DENYLIST = (
    f"{TASK_TOOL_DENYLIST},Bash,Write,Edit,MultiEdit,NotebookEdit"
)
TASK_DENYLIST = TASK_TOOL_DENYLIST
VERIFIER_DENYLIST = f"{TASK_TOOL_DENYLIST},Write,Edit,MultiEdit,NotebookEdit"
VERIFIER_ROLES = {"cli_verifier"}
