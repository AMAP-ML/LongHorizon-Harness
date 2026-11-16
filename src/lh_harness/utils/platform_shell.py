"""Platform-aware shell quoting for agent command templates.

Agent command templates are executed through the local default shell
(``asyncio.create_subprocess_shell``): POSIX ``sh`` on macOS/Linux and
``cmd.exe`` on Windows.  The historical templates were POSIX-only: single
quotes, ``VAR=value`` leading assignments, and plain ``cd`` all break under
cmd.exe.  These helpers emit the right syntax per platform so one template
works everywhere.
"""

from __future__ import annotations

import os
import shlex

from .platform_caps import IS_WINDOWS


def shell_quote(value: str) -> str:
    """Quote one token for the platform's default shell."""

    text = str(value)
    if IS_WINDOWS:
        # cmd.exe has no single-quote escaping; double quotes are literal
        # delimiters and embedded quotes double per CRT argument rules.
        return f'"{text.replace(chr(34), chr(34) * 2)}"'
    return shlex.quote(text)


def cd_command(path: str) -> str:
    """Change into ``path`` before the agent command runs."""

    if IS_WINDOWS:
        # /d also switches drives (e.g. workspace on D:, harness started from C:).
        return f"cd /d {shell_quote(path)}"
    return f"cd {shlex.quote(path)}"


def env_prefix(assignments: list[tuple[str, str]]) -> str:
    """Serialize leading environment assignments for the platform's shell."""

    if not assignments:
        return ""
    if IS_WINDOWS:
        # cmd.exe cannot prefix assignments; `set` them in the same line.
        return "".join(f'set "{key}={value}"&& ' for key, value in assignments)
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in assignments) + " "
