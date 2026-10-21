"""Default TestClient Host to loopback so Host-header hardening stays on."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urljoin

import fastapi.testclient as fastapi_testclient
import starlette.testclient as starlette_testclient

_Orig = starlette_testclient.TestClient
_Upgrade = starlette_testclient._Upgrade


def write_executable_stub(path: Path, body: str) -> str:
    """Create a fake agent CLI binary for adapter tests.

    POSIX receives an ``sh`` script exactly as before.  Windows cannot exec
    shebang scripts, so the same small sh grammar the suite uses (``echo``,
    ``printf ... >&2``, ``exit N``, ``cat <<'EOF'`` heredocs) is translated
    into a ``.cmd`` batch with equivalent behaviour.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    lines = ["@echo off"]

    def cmd_escape(text: str) -> str:
        """Escape cmd.exe metacharacters so echo prints them literally."""

        for ch in "^&|<>":
            text = text.replace(ch, "^" + ch)
        return text

    consumed = body
    heredoc = re.search(r"cat <<'?EOF'?\n(.*?)\nEOF", body, re.S)
    if heredoc:
        for payload_line in heredoc.group(1).splitlines():
            lines.append("echo " + cmd_escape(payload_line))
        consumed = consumed.replace(heredoc.group(0), "")
    for raw in consumed.splitlines():
        line = raw.strip()
        if not line:
            continue
        conditional = re.match(
            r'if \[ "\$1" = "(.*?)" \]; then echo "(.*?)"; exit 0; fi$', line
        )
        if conditional:
            lines.append(f'if "%~1"=="{conditional.group(1)}" (')
            lines.append(f"    echo {cmd_escape(conditional.group(2))}")
            lines.append("    exit /b 0")
            lines.append(")")
            continue
        if line == "printf '%s\\n' \"$*\"":
            lines.append("echo %*")
            continue
        stderr_printf = re.match(r"printf '(.*)\\n' >&2$", line)
        if stderr_printf:
            lines.append(f"1>&2 echo {stderr_printf.group(1)}")
            continue
        stdout_printf = re.match(r"printf '(.*)\\n'$", line)
        if stdout_printf:
            lines.append(f"echo {stdout_printf.group(1)}")
            continue
        echo = re.match(r"echo \"?(.*?)\"?$", line)
        if echo:
            lines.append(f"echo {echo.group(1)}")
            continue
        exit_code = re.match(r"exit (\d+)$", line)
        if exit_code:
            lines.append(f"exit /b {exit_code.group(1)}")
            continue
        lines.append("rem unsupported stub line: " + line)
    cmd_path = path.with_suffix(".cmd")
    cmd_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return str(cmd_path)


class LoopbackTestClient(_Orig):
    def __init__(self, app, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        super().__init__(app, *args, **kwargs)

    def websocket_connect(self, url, subprotocols=None, **kwargs):
        # Starlette hardcodes ws://testserver; rewrite so Host stays loopback.
        url = urljoin("ws://127.0.0.1", url)
        headers = kwargs.get("headers", {})
        headers.setdefault("connection", "upgrade")
        headers.setdefault("sec-websocket-key", "testserver==")
        headers.setdefault("sec-websocket-version", "13")
        if subprotocols is not None:
            headers.setdefault("sec-websocket-protocol", ", ".join(subprotocols))
        kwargs["headers"] = headers
        try:
            super().request("GET", url, **kwargs)
        except _Upgrade as exc:
            return exc.session
        raise RuntimeError("Expected WebSocket upgrade")


starlette_testclient.TestClient = LoopbackTestClient
fastapi_testclient.TestClient = LoopbackTestClient
