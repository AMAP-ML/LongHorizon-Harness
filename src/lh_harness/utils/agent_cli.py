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
import sys
from dataclasses import dataclass
from pathlib import Path

# App Execution Aliases live here; they are reparse points, not real programs.
_WINDOWS_STORE_ALIAS_DIR = os.path.join("Microsoft", "WindowsApps")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+\S*")

# The desktop app and the standalone npm CLI can both install a `codex`
# executable.  The desktop binary carries the same authenticated app-server
# runtime as the Codex client, while an older PATH entry may reject newer
# config values (for example `model_reasoning_effort = "ultra"`).  Keep this
# precedence in one place so adapters, doctor output, and the Web API agree.
_CODEX_BINARY_ENV_VARS = ("LH_HARNESS_CODEX_BINARY", "CODEX_CLI_PATH")
_CODEX_DESKTOP_BINARY = "/Applications/ChatGPT.app/Contents/Resources/codex"


def resolve_agent_binary(
    binary: str,
    *,
    environ: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> str | None:
    """Resolve an agent executable using the same policy everywhere.

    Explicit Codex overrides are authoritative, even when the target is
    missing; ``probe_agent_cli`` can then report the concrete path/OS error
    instead of silently falling back to a different installation.  For
    ordinary discovery, only an existing executable desktop binary is selected
    before falling back to ``PATH``.
    """

    env = os.environ if environ is None else environ
    if binary == "codex":
        for name in _CODEX_BINARY_ENV_VARS:
            value = str(env.get(name) or "").strip()
            if value:
                return os.path.expanduser(value)

        if (platform_name or sys.platform) == "darwin":
            desktop_candidates = (_CODEX_DESKTOP_BINARY,)
            # A per-user app install is common on macOS and costs nothing to
            # support while preserving the system /Applications preference.
            desktop_candidates += (
                str(Path.home() / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex"),
            )
            for candidate in desktop_candidates:
                path = Path(candidate)
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)

    return shutil.which(binary)


def resolve_codex_binary(
    *,
    environ: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> str | None:
    """Resolve the Codex executable selected by the harness."""

    return resolve_agent_binary("codex", environ=environ, platform_name=platform_name)


def is_agent_binary_available(path: str | None) -> bool:
    """Return whether a resolved executable can be launched without running it."""

    return bool(path and shutil.which(path))


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
    path = resolve_agent_binary(binary)
    if not path:
        return AgentCli(binary, problem=f"`{binary}` was not found")

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
