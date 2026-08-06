"""Global npm package checks and installs for the community GUI plugins.

Both community computer-use servers ship on npm, so the harness only needs to
know whether a global package is present and how to add or remove it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from .errors import PluginError


@dataclass(frozen=True)
class NpmPackageState:
    name: str
    installed: bool
    version: str = ""


def npm_binary() -> str | None:
    return shutil.which("npm")


def _tool_version(binary: str) -> str:
    """Best-effort `<binary> --version`; an unusable tool must not raise."""
    path = shutil.which(binary)
    if not path:
        return ""
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip().lstrip("v") if result.returncode == 0 else ""


def node_version() -> str:
    return _tool_version("node")


def npm_version() -> str:
    return _tool_version("npm")


def require_npm() -> str:
    npm = npm_binary()
    if not npm:
        raise PluginError(
            "npm was not found on PATH. Install Node.js 20 or later "
            "(https://nodejs.org) and re-run this command."
        )
    return npm


def global_package_state(name: str, *, npm: str | None = None) -> NpmPackageState:
    """Read one global package's installed state from `npm ls --json`."""
    binary = npm or require_npm()
    # `npm ls` exits 1 when the package is absent, so the JSON body decides.
    result = _run(
        [binary, "ls", "--global", "--depth=0", "--json", name],
        timeout=120,
        operation=f"query global npm package {name}",
        allow_failure=True,
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise PluginError(f"npm returned invalid JSON while listing {name}.") from exc
    dependencies = payload.get("dependencies") if isinstance(payload, dict) else None
    entry = dependencies.get(name) if isinstance(dependencies, dict) else None
    if not isinstance(entry, dict):
        return NpmPackageState(name, installed=False)
    return NpmPackageState(name, installed=True, version=str(entry.get("version") or ""))


def install_global_package(name: str, *, npm: str | None = None) -> NpmPackageState:
    binary = npm or require_npm()
    _run(
        [binary, "install", "--global", name],
        timeout=900,
        operation=f"install npm package {name}",
    )
    return global_package_state(name, npm=binary)


def uninstall_global_package(name: str, *, npm: str | None = None) -> NpmPackageState:
    binary = npm or require_npm()
    _run(
        [binary, "uninstall", "--global", name],
        timeout=300,
        operation=f"uninstall npm package {name}",
    )
    return global_package_state(name, npm=binary)


def _run(
    command: list[str],
    *,
    timeout: int,
    operation: str,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PluginError(f"npm disappeared while trying to {operation}.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PluginError(f"Timed out while trying to {operation}.") from exc
    except OSError as exc:
        raise PluginError(f"Could not {operation}: {exc}") from exc
    if result.returncode != 0 and not allow_failure:
        raise PluginError(f"Failed to {operation}{_detail(result)}")
    return result


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout).strip()
    if not text:
        return ""
    return f": {text[-800:]}"
