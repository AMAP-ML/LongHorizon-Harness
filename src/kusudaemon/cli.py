"""The ``kusudaemon`` entry point: PLAN.md §11's control surface.

This is a thin shim over ``pipeline/cli.py``'s command handlers — the
pipeline group (run / status / approve / amend / resume) *is* the CLI now,
since the classic role-based harness (manager/executor/auditor plus the
claude/codex backends) was removed. Run ``kusudaemon --help`` from a
terminal for the full command list.
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
        parser.print_help()
        return 2
    if written is not None:
        print(f"Created provider config: {written} (edit it to change your provider)")
    return dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
