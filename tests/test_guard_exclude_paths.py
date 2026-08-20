"""Validation boundaries for auditor guard snapshot exclusions.

Excluded paths stay readable (and writable, via Bash) to the agents while the
guard stops watching them, so every exclusion is a hole in the audit. The
resolver must keep the holes inside the workspace and away from the paths the
audit exists to protect.
"""

import argparse
from pathlib import Path

import pytest

import lh_harness.cli as cli
from lh_harness.cli import _apply_repeatable_defaults, _resolve_guard_exclude_paths
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


# --- repeatable options: the command line replaces the project config --------
#
# ``action="append"`` appends to whatever default it is handed, so passing the
# config list straight to argparse made ``--guard-exclude-path`` *extend* the
# config rather than override it. That silently widened the audit hole: the
# operator narrowing exclusions to one directory still got every configured
# directory excluded too.

_REPEATABLE = ("mcp_add_dir", "guard_exclude_path")


def _namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {name: None for name in _REPEATABLE}
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize("name", _REPEATABLE)
def test_the_command_line_replaces_the_configured_list(name: str) -> None:
    args = _namespace(**{name: ["from-cli"]})

    _apply_repeatable_defaults(args, {name: ["from-config-a", "from-config-b"]})

    assert getattr(args, name) == ["from-cli"]


@pytest.mark.parametrize("name", _REPEATABLE)
def test_the_configured_list_is_used_when_the_command_line_is_silent(name: str) -> None:
    args = _namespace()

    _apply_repeatable_defaults(args, {name: ["from-config"]})

    assert getattr(args, name) == ["from-config"]


@pytest.mark.parametrize("name", _REPEATABLE)
def test_an_absent_option_and_config_becomes_an_empty_list(name: str) -> None:
    args = _namespace()

    _apply_repeatable_defaults(args, {})

    assert getattr(args, name) == []


@pytest.mark.parametrize("name", _REPEATABLE)
def test_the_cached_defaults_are_never_aliased(name: str) -> None:
    configured = ["from-config"]
    args = _namespace()

    _apply_repeatable_defaults(args, {name: configured})
    getattr(args, name).append("mutated-later")

    assert configured == ["from-config"]


@pytest.mark.parametrize("bad", ["not-a-list", 7, None, {"a": 1}])
def test_a_malformed_configured_value_does_not_leak_into_argv(bad: object) -> None:
    # load_run_defaults validates these, but a Namespace built by another caller
    # must not turn a stray string into a list of characters.
    args = _namespace()

    _apply_repeatable_defaults(args, {"guard_exclude_path": bad})

    assert args.guard_exclude_path == []


def test_repeating_the_option_still_accumulates() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guard-exclude-path", action="append", default=None)

    args = parser.parse_args(["--guard-exclude-path=a", "--guard-exclude-path=b"])
    _apply_repeatable_defaults(args, {"guard_exclude_path": ["from-config"]})

    assert args.guard_exclude_path == ["a", "b"]


def test_run_parses_repeatable_options_without_inheriting_the_config(monkeypatch) -> None:
    """End-to-end through ``main``: the flag overrides, the config fills in."""

    captured: list[tuple[list[str], list[str]]] = []
    monkeypatch.setattr(
        cli,
        "load_run_defaults",
        lambda: {"guard_exclude_path": ["cfg-guard"], "mcp_add_dir": ["cfg-dir"]},
    )
    monkeypatch.setattr(
        cli,
        "_run_command",
        lambda args: captured.append((list(args.guard_exclude_path), list(args.mcp_add_dir))) or 0,
    )

    assert cli.main(["run", "--task=t"]) == 0
    assert captured[-1] == (["cfg-guard"], ["cfg-dir"])

    assert cli.main(["run", "--task=t", "--guard-exclude-path=cli-guard"]) == 0
    assert captured[-1] == (["cli-guard"], ["cfg-dir"]), "only the given option is overridden"

    # A second call in the same process must not accumulate.
    assert cli.main(["run", "--task=t", "--guard-exclude-path=cli-guard"]) == 0
    assert captured[-1] == (["cli-guard"], ["cfg-dir"])
