"""Build a fake agent CLI that behaves the same on POSIX and on Windows.

The tests used to write ``#!/bin/sh`` scripts. Windows cannot execute those at
all -- ``CreateProcess`` answers ``WinError 193`` -- so every adapter probe and
end-to-end test silently reported the backend as unavailable there. Translating
each snippet into a ``.cmd`` twice would let the two platforms drift, so the
body is plain Python instead and only the launcher differs: a shebang script on
POSIX, a ``.cmd`` shim on Windows (which is also how the real agent CLIs ship
when installed from npm).
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path


def fake_cli(path: Path, body: str = "") -> str:
    """Create an executable stand-in at ``path`` running the Python ``body``.

    ``sys`` is already imported for the body. Returns the path to invoke, which
    on Windows carries a ``.cmd`` suffix.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    script = path.with_name(f"{path.name}.impl.py")
    script.write_text("import sys\n" + (body or "pass\n"), encoding="utf-8", newline="\n")
    if sys.platform == "win32":
        launcher = path.with_name(f"{path.name}.cmd")
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
            newline="",
        )
        return str(launcher)
    launcher = path
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    launcher.chmod(0o755)
    return str(launcher)
