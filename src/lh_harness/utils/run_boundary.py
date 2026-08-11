"""Filesystem boundary checks for run-scoped dashboard data.

Run directories are writable by the worker (and, in some deployments, by an
agent process). Treat ``runs/<id>`` and its ``lh_harness``/``control`` children as
ownership boundaries: a symlink, a path that resolves to another run, or a
non-directory must never be accepted as a run layout.

The helpers intentionally return ``None`` instead of raising.  Callers use
that result to hide a concurrently replaced/invalid run from a listing or to
return a normal ``404``/``ValueError`` at their API boundary.
"""

from __future__ import annotations

from pathlib import Path


_MAX_RUN_ID_CHARS = 128
CANONICAL_LOG_DIR = "lh_harness"
CANONICAL_ROLE_DIR = "role_orchestration"
OSWORLD_COMPAT_LOG_DIR = "cua_harness"
LEGACY_LOG_DIR = "logs"
LEGACY_ROLE_DIR = "role_management"


def _role_dir_name(log_name: str) -> str:
    return LEGACY_ROLE_DIR if log_name == LEGACY_LOG_DIR else CANONICAL_ROLE_DIR


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _component_name(value: str | Path) -> str | None:
    text = str(value)
    if (
        not text
        or len(text) > _MAX_RUN_ID_CHARS
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)
    ):
        return None
    return text


def safe_run_dir(runs_root: str | Path, run_id: str | Path) -> Path | None:
    """Return a canonical, real run directory path, or ``None``.

    A non-existent path is accepted so callers can create a fresh run.  An
    existing symlink (including one pointing to a sibling run *inside* the
    configured root) is always rejected.
    """

    name = _component_name(run_id)
    if name is None:
        return None
    root = Path(runs_root).expanduser().resolve(strict=False)
    candidate = root / name
    try:
        if candidate.is_symlink():
            return None
        if candidate.exists() and not candidate.is_dir():
            return None
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    # ``resolve`` can follow a symlink in any component.  Equality is the
    # important check even when the resolved target remains under ``root``.
    if resolved != candidate or not _inside(resolved, root):
        return None
    return resolved


def safe_run_subdir(
    runs_root: str | Path,
    run_dir: str | Path,
    name: str,
    *,
    allow_missing: bool = True,
) -> Path | None:
    """Validate one direct child directory of a real run.

    ``name`` is deliberately a single component.  Existing children must be
    real directories whose canonical path is exactly the lexical path; this
    rejects ``logs -> ../other-run/logs`` as well as links outside the root.
    """

    component = _component_name(name)
    if component is None:
        return None
    root = Path(runs_root).expanduser().resolve(strict=False)
    candidate_run = Path(run_dir).expanduser()
    # Validate the run path itself before inspecting any child.  Passing an
    # absolute path is supported for callers that already resolved it, while
    # still requiring it to be exactly one direct child of ``root``.
    try:
        resolved_run = candidate_run.resolve(strict=False)
        if candidate_run.is_symlink() or resolved_run != candidate_run:
            return None
        resolved_run.relative_to(root)
        if resolved_run.parent != root or not resolved_run.is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return _safe_child(root, candidate_run, component, allow_missing=allow_missing)


def _safe_child(
    root: Path,
    parent: Path,
    component: str,
    *,
    allow_missing: bool,
) -> Path | None:
    """Validate one child below an already validated canonical directory."""

    candidate = parent / component
    try:
        if candidate.is_symlink():
            return None
        if not candidate.exists():
            return candidate if allow_missing else None
        if not candidate.is_dir():
            return None
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != candidate or not _inside(resolved, root) or resolved.parent != parent:
        return None
    return resolved


def safe_run_logs(
    runs_root: str | Path,
    run_dir: str | Path,
    *,
    require_role_management: bool = False,
    allow_missing: bool = True,
) -> Path | None:
    """Validate the canonical OSWorld-style result-log directory.

    New runs use ``lh_harness/role_orchestration``. Runs briefly written under
    ``cua_harness/role_orchestration`` and historical ``logs/role_management``
    runs remain readable during migration.
    """

    root = Path(runs_root).expanduser().resolve(strict=False)
    run = safe_run_dir(root, Path(run_dir).name)
    if run is None or Path(run_dir).resolve(strict=False) != run:
        return None
    selected_log_name = CANONICAL_LOG_DIR
    try:
        canonical_exists = (run / CANONICAL_LOG_DIR).exists() or (run / CANONICAL_LOG_DIR).is_symlink()
        compat_exists = (run / OSWORLD_COMPAT_LOG_DIR).exists() or (run / OSWORLD_COMPAT_LOG_DIR).is_symlink()
        legacy_exists = (run / LEGACY_LOG_DIR).exists() or (run / LEGACY_LOG_DIR).is_symlink()
    except OSError:
        return None
    if canonical_exists:
        selected_log_name = CANONICAL_LOG_DIR
    elif compat_exists:
        selected_log_name = OSWORLD_COMPAT_LOG_DIR
    elif legacy_exists:
        selected_log_name = LEGACY_LOG_DIR
    logs = _safe_child(root, run, selected_log_name, allow_missing=allow_missing)
    if logs is None:
        return None
    role: Path | None = None
    if logs.is_dir():
        role_name = _role_dir_name(logs.name)
        role_path = logs / role_name
        try:
            role_exists = role_path.exists() or role_path.is_symlink()
        except OSError:
            return None
        if role_exists:
            # An existing role child must be a real directory.  Missing role
            # data is allowed for a freshly reserved/partially written run.
            role = _safe_child(root, logs, role_name, allow_missing=False)
            if role is None:
                return None
    if require_role_management and role is None:
        return None
    return logs


def safe_run_role(
    runs_root: str | Path,
    run_dir: str | Path,
    *,
    allow_missing: bool = True,
) -> Path | None:
    """Return the current role ledger or one of its readable legacy layouts."""

    root = Path(runs_root).expanduser().resolve(strict=False)
    run = safe_run_dir(root, Path(run_dir).name)
    if run is None or Path(run_dir).resolve(strict=False) != run:
        return None
    logs = safe_run_logs(root, run, allow_missing=allow_missing)
    if logs is None:
        return None
    role_name = _role_dir_name(logs.name)
    return _safe_child(root, logs, role_name, allow_missing=allow_missing)


def safe_run_rounds(
    runs_root: str | Path,
    run_dir: str | Path,
    *,
    allow_missing: bool = True,
) -> Path | None:
    """Return a validated role-orchestration ``rounds`` directory."""

    root = Path(runs_root).expanduser().resolve(strict=False)
    role = safe_run_role(root, run_dir, allow_missing=allow_missing)
    if role is None:
        return None
    return _safe_child(root, role, "rounds", allow_missing=allow_missing)


def safe_run_control(
    runs_root: str | Path,
    run_dir: str | Path,
    *,
    allow_missing: bool = True,
) -> Path | None:
    """Validate a run's ``control`` directory using the same boundary rule."""

    root = Path(runs_root).expanduser().resolve(strict=False)
    run = safe_run_dir(root, Path(run_dir).name)
    if run is None or Path(run_dir).resolve(strict=False) != run:
        return None
    return _safe_child(root, run, "control", allow_missing=allow_missing)
