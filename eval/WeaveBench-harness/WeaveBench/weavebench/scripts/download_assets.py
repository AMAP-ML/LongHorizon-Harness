"""Download WeaveBench runtime tarballs from HuggingFace into ./cache/.

Usage::

    weavebench-download-assets [--dest ./cache] [--harness openclaw,codex,...]

The dataset is public — no HF_TOKEN required. Set HF_ENDPOINT to a mirror
(e.g. https://hf-mirror.com) if you're in a region with slow HF access.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


_HF_REPO = "wanlilll/WeaveBench"
_HARNESS_FILES = {
    "openclaw": ["runtime_assets/openclaw.tar.gz"],
    "codex": ["runtime_assets/codex.tar.gz"],
    "claudecode": ["runtime_assets/claudecode.tar.gz"],
    "hermes": [
        "runtime_assets/hermes.tar.gz",
        "runtime_assets/hermes_mcp_wheels.tar.gz",
    ],
}


def _resolve_token(cli_token: str | None) -> str | None:
    return (cli_token
            or os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="weavebench-download-assets",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    ap.add_argument("--dest", default="./cache",
                    help="Local cache root (default ./cache). Tarballs land in <dest>/runtime_assets/.")
    ap.add_argument("--harness", default="openclaw,codex,claudecode,hermes",
                    help="CSV of harnesses to fetch, or 'all' (shorthand for "
                         "openclaw,codex,claudecode,hermes). Default: all four.")
    ap.add_argument("--token", default=None,
                    help="HF token. Falls back to $HF_TOKEN / $HUGGINGFACE_TOKEN. "
                         "Optional — only needed for higher HF rate limits.")
    ap.add_argument("--revision", default=os.environ.get("WEAVEBENCH_DATASET_REVISION"),
                    help="Pin to a specific commit SHA. Falls back to "
                         "$WEAVEBENCH_DATASET_REVISION. Default: latest on main.")
    args = ap.parse_args(argv)

    try:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError
    except ImportError:
        print("[error] pip install huggingface_hub", file=sys.stderr)
        return 1

    token = _resolve_token(args.token)
    endpoint = os.environ.get("HF_ENDPOINT")
    if endpoint:
        print(f"[hf] using mirror {endpoint}")

    dest = Path(args.dest).expanduser().resolve()
    (dest / "runtime_assets").mkdir(parents=True, exist_ok=True)

    requested = {h.strip() for h in args.harness.split(",") if h.strip()}
    # 'all' is shorthand for every harness (the docs and setup.sh advertise it).
    if "all" in requested:
        requested.discard("all")
        requested.update(_HARNESS_FILES.keys())
    bad = requested - _HARNESS_FILES.keys()
    if bad:
        print(f"[error] unknown harness(es): {sorted(bad)}. "
              f"Supported: {sorted(_HARNESS_FILES.keys())} or 'all'.",
              file=sys.stderr)
        return 2

    try:
        for h in sorted(requested):
            for rel in _HARNESS_FILES[h]:
                print(f"[fetch] {_HF_REPO}::{rel}"
                      + (f" @ {args.revision[:8]}" if args.revision else ""))
                local = hf_hub_download(
                    repo_id=_HF_REPO,
                    repo_type="dataset",
                    filename=rel,
                    local_dir=str(dest),
                    token=token,
                    revision=args.revision,
                )
                print(f"        -> {local}")
    except GatedRepoError:
        print(f"[error] {_HF_REPO} is gated — request access at "
              f"https://huggingface.co/datasets/{_HF_REPO}", file=sys.stderr)
        return 3
    except RepositoryNotFoundError:
        print(f"[error] {_HF_REPO} not found OR your token doesn't have access.",
              file=sys.stderr)
        return 4

    print(f"[done] runtime assets staged under {dest}/runtime_assets/")
    print(f"       export WEAVEBENCH_ASSETS_DIR={dest / 'runtime_assets'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
