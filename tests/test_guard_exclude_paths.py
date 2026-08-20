"""Validation boundaries for auditor guard snapshot exclusions.

Excluded paths stay readable (and writable, via Bash) to the agents while the
guard stops watching them, so every exclusion is a hole in the audit. The
resolver must keep the holes inside the workspace and away from the paths the
audit exists to protect.
"""

from pathlib import Path

import pytest

from lh_harness.cli import _resolve_guard_exclude_paths
from lh_harness.config import ProjectConfigError, _flatten_run_table


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def test_relative_paths_resolve_against_the_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    resolved = _resolve_guard_exclude_paths(
        ["target", "node_modules"], workspace=workspace, protected=()
    )

    assert resolved == (
        str(workspace / "target"),
        str(workspace / "node_modules"),
    )


def test_duplicate_exclusions_are_collapsed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    resolved = _resolve_guard_exclude_paths(
        ["target", str(workspace / "target")], workspace=workspace, protected=()
    )

    assert resolved == (str(workspace / "target"),)


@pytest.mark.parametrize("escape", ["..", "../sibling", "/etc", "a/../../.."])
def test_paths_outside_the_workspace_are_rejected(tmp_path: Path, escape: str) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(ValueError, match="escapes the workspace"):
        _resolve_guard_exclude_paths([escape], workspace=workspace, protected=())


def test_excluding_the_workspace_itself_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(ValueError, match="disable the read-only guard"):
        _resolve_guard_exclude_paths(["."], workspace=workspace, protected=())


@pytest.mark.parametrize("vcs_path", [".git", ".git/objects", "vendored/.git"])
def test_version_control_state_is_protected(tmp_path: Path, vcs_path: str) -> None:
    """Mutations under an excluded .git would be invisible to the guard."""

    workspace = _workspace(tmp_path)

    with pytest.raises(ValueError, match="version-control state"):
        _resolve_guard_exclude_paths([vcs_path], workspace=workspace, protected=())


def test_harness_state_paths_are_protected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run_dir = workspace / "runs" / "run-1"

    # Excluding the harness path itself, or any parent that covers it, would
    # hide the run's own control/state files from the guard.
    for candidate in ("runs/run-1", "runs"):
        with pytest.raises(ValueError, match="harness state"):
            _resolve_guard_exclude_paths(
                [candidate], workspace=workspace, protected=(run_dir,)
            )


def test_sibling_of_harness_state_is_allowed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    run_dir = workspace / "runs" / "run-1"

    resolved = _resolve_guard_exclude_paths(
        ["target"], workspace=workspace, protected=(run_dir,)
    )

    assert resolved == (str(workspace / "target"),)


def test_config_accepts_a_string_array() -> None:
    defaults = _flatten_run_table({"guard_exclude_paths": ["target", "build"]})

    assert defaults["guard_exclude_path"] == ["target", "build"]


@pytest.mark.parametrize("bad", ["target", [""], [1], [None], {"a": 1}])
def test_config_rejects_non_string_arrays(bad: object) -> None:
    with pytest.raises(ProjectConfigError, match="guard_exclude_paths"):
        _flatten_run_table({"guard_exclude_paths": bad})
