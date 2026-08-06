"""Resolve an agent CLI and prove it actually runs.

`shutil.which` only answers "is there a file with this name on PATH", which is
not the same question as "can I drive this agent". On Windows the gap is wide
enough to break setup silently: installing Codex Desktop from the Microsoft
Store drops a zero-byte App Execution Alias named `codex.exe` into
`%LOCALAPPDATA%\\Microsoft\\WindowsApps`, so `which` succeeds while every real
invocation fails or opens the Store. Everything here therefore runs
`<binary> --version` and reports the outcome instead of trusting the lookup.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# App Execution Aliases live here; they are reparse points, not real programs.
_WINDOWS_STORE_ALIAS_DIR = os.path.join("Microsoft", "WindowsApps")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+\S*")


@dataclass(frozen=True)
class AgentCli:
    binary: str
    path: str = ""
    version: str = ""
    problem: str = ""

    @property
    def found(self) -> bool:
        return bool(self.path)

    @property
    def usable(self) -> bool:
        return bool(self.path) and not self.problem


def probe_agent_cli(binary: str, *, timeout: int = 15) -> AgentCli:
    """Locate `binary` and confirm `--version` succeeds."""
    path = shutil.which(binary)
    if not path:
        return AgentCli(binary, problem=f"`{binary}` was not found on PATH")

    store_alias = is_windows_store_alias(path)
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AgentCli(binary, path, problem=_stalled_detail(binary, path, store_alias, timeout))
    except OSError as exc:
        return AgentCli(binary, path, problem=_broken_detail(binary, path, store_alias, str(exc)))

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        detail = output.strip().splitlines()
        tail = f": {detail[-1][:160]}" if detail else ""
        return AgentCli(
            binary,
            path,
            problem=_broken_detail(binary, path, store_alias, f"exit {result.returncode}{tail}"),
        )

    version = _parse_version(output)
    if not version:
        return AgentCli(
            binary,
            path,
            problem=_broken_detail(
                binary, path, store_alias, "`--version` printed no recognisable version"
            ),
        )
    return AgentCli(binary, path, version=version)


def is_windows_store_alias(path: str) -> bool:
    """True when `path` looks like a Microsoft Store App Execution Alias."""
    if _WINDOWS_STORE_ALIAS_DIR.lower() not in str(path).replace("/", os.sep).lower():
        return False
    try:
        # Real programs have content; the alias is an empty reparse point.
        return Path(path).stat().st_size == 0
    except OSError:
        return True


def _parse_version(output: str) -> str:
    for line in output.strip().splitlines():
        match = _VERSION_RE.search(line)
        if match:
            return match.group(0)
    return ""


_STORE_PACKAGES = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code"}


def _store_hint(binary: str) -> str:
    package = _STORE_PACKAGES.get(binary)
    install = f"Install the CLI itself (`npm install -g {package}`)" if package else "Install the real CLI"
    return (
        f"`{binary}` resolves to a Microsoft Store App Execution Alias -- the desktop app's "
        f"stub, not the full CLI. {install}, or turn the alias off in Settings > Apps > "
        "Advanced app settings > App execution aliases, so PATH finds the real binary."
    )


def _broken_detail(binary: str, path: str, store_alias: bool, reason: str) -> str:
    if store_alias:
        return f"{_store_hint(binary)} (`{path} --version` failed: {reason})"
    return f"`{path} --version` failed ({reason}); the CLI is on PATH but not usable"


def _stalled_detail(binary: str, path: str, store_alias: bool, timeout: int) -> str:
    if store_alias:
        return f"{_store_hint(binary)} (`{path} --version` hung for {timeout}s)"
    return f"`{path} --version` did not finish within {timeout}s; the CLI is unresponsive"
