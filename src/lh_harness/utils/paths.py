"""Filesystem paths that survive Windows' MAX_PATH limit.

Windows rejects paths longer than 260 characters unless they carry the `\\\\?\\`
extended-length prefix. The harness reaches that limit easily on its own: the
run layout `<runs_root>/<run-id>/logs/role_management/rounds/round_NNN/
<role>_raw_trajectory.jsonl` spends roughly 100 characters before any of the
user's project path is counted.

The failure is also silent-looking -- `mkdir` succeeds for the shorter directory
while `open` on the file inside it raises ``FileNotFoundError`` -- so it reads
like a missing directory rather than a length limit.
"""

from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform == "win32"

_EXTENDED_PREFIX = "\\\\?\\"
# Well under 260 so a caller appending a filename to a directory we hand back
# does not slip over the limit on its own.
_SAFE_LENGTH = 200


def os_path(path) -> str:
    """Path string safe to hand to ``open``/``mkdir`` on this platform.

    Left untouched everywhere except long Windows paths, so relative paths stay
    relative and log records keep reading the way an operator expects.
    """
    text = os.fspath(path)
    if not IS_WINDOWS or text.startswith(_EXTENDED_PREFIX):
        return text
    absolute = os.path.abspath(text)
    if len(absolute) <= _SAFE_LENGTH:
        return text
    if absolute.startswith("\\\\"):
        # \\server\share -> \\?\UNC\server\share
        return f"{_EXTENDED_PREFIX}UNC\\{absolute[2:]}"
    return f"{_EXTENDED_PREFIX}{absolute}"


def makedirs(path, *, exist_ok: bool = True) -> None:
    os.makedirs(os_path(path), exist_ok=exist_ok)


def write_text(path, content: str, *, encoding: str = "utf-8") -> None:
    makedirs(os.path.dirname(os.path.abspath(os.fspath(path))))
    with open(os_path(path), "w", encoding=encoding, newline="") as handle:
        handle.write(content)


def read_text(path, *, encoding: str = "utf-8", errors: str = "strict") -> str:
    with open(os_path(path), "r", encoding=encoding, errors=errors) as handle:
        return handle.read()


def append_line(path, line: str, *, encoding: str = "utf-8") -> None:
    makedirs(os.path.dirname(os.path.abspath(os.fspath(path))))
    with open(os_path(path), "a", encoding=encoding, newline="") as handle:
        handle.write(line)
