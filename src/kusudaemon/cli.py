"""The ``kusudaemon`` entry point: PLAN.md §11's control surface.

This is a thin shim over ``pipeline/cli.py``'s command handlers — the
pipeline group (run / status / approve / amend / resume / serve) *is* the
CLI now, since the classic role-based harness (manager/executor/auditor
plus the claude/codex backends) was removed. Run ``kusudaemon --help``
from a terminal for the full command list. Bare ``kusudaemon`` (no
subcommand) launches the web dashboard directly — the same "just run the
thing" ergonomics as other modern agent CLIs — rather than printing help.
"""

from __future__ import annotations

import sys

from . import HOMEPAGE, __version__
from .provider_config import ensure_user_config, load_env_file

_EPILOG = (
    "Provider: any OpenAI-compatible endpoint; the default is OpenCode Zen. "
    "Configure it in the provider config file (shown on the run command) — "
    "copy provider.example.json from the repo root. See "
    f"{HOMEPAGE}"
)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    from .pipeline.cli import build_pipeline_parser, dispatch

    parser = build_pipeline_parser()
    parser.prog = "kusudaemon"
    parser.description = (
        f"Kusudaemon {__version__}: recursive decomposition harness "
        "(intake -> survey -> plan -> pilot -> execute -> assemble)."
    )
    parser.epilog = _EPILOG

    load_env_file()
    written = ensure_user_config()

    args = parser.parse_args(raw_argv)
    if not getattr(args, "pipeline_command", None):
        if raw_argv:
            parser.print_help()
            return 2
        args.pipeline_command = "serve"
        args.runs_root = "./.kusudaemon/runs"
        args.host = "127.0.0.1"
        args.port = 8765
        args.run_id = None
        args.no_control = False
        # §C4: bare-kusudaemon invocation keeps the auth_token default
        # ("") so the loopback default is anonymous (the same as before
        # this workstream). Setting --auth-token re-enables auth; setting
        # --host to a non-loopback without --auth-token raises in
        # run_forever via _assert_safe_host.
        args.auth_token = None
        args.max_concurrent_runs = None
    if written is not None:
        print(f"Created provider config: {written} (edit it to change your provider)")
    return dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
