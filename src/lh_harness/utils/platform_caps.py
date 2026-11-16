"""Single home for platform capability detection.

``supervisor/control_bus.py`` previously owned the anchored-walk capability
flag and the best-effort symlink scan; the dashboard and adapters grew their
own ``os.name`` checks.  Centralising them here keeps the fallback semantics
from drifting between modules.  POSIX behaviour is never changed by anything
in this module — every guard degrades only where the capability is absent.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Anchored no-follow walking needs O_NOFOLLOW/O_DIRECTORY plus dir-fd variants
# of open/mkdir.  POSIX platforms provide them; Windows does not.  Callers on
# those platforms degrade to plain path operations guarded by a best-effort
# symlink scan instead of failing closed, because the run tree is owned by the
# same operator who started the process.
SECURE_DIRFD = bool(
    getattr(os, "O_NOFOLLOW", 0)
    and getattr(os, "O_DIRECTORY", 0)
    and os.open in getattr(os, "supports_dir_fd", set())
    and os.mkdir in getattr(os, "supports_dir_fd", set())
)


def validate_no_symlink_chain(path: Path) -> None:
    """Reject any symlinked component below the anchor (best-effort platforms)."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    anchor_parts = len(Path(absolute.anchor).parts)
    current = Path(*absolute.parts[:anchor_parts])
    for component in absolute.parts[anchor_parts:]:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(f"symlinked path component rejected: {current}")
