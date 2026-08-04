"""Download the WeaveBench Ubuntu qcow2 VM image from HuggingFace.

Usage::

    weavebench-download-vm [--dest ./cache]

The qcow2 is 28.46 GB (`vm/Ubuntu.qcow2` on the dataset repo) — the
paper-canonical v3_eyeson_apps image. After download, set
``OSWORLD_LOCAL_QCOW2_PATH=<dest>/vm/Ubuntu.qcow2`` (or symlink the file
into the location your launcher expects) before running ``weavebench-run``.

Optional env vars (same contract as `weavebench-download-dataset`):
  HF_TOKEN / HUGGINGFACE_TOKEN  : optional auth token (higher HF rate limits)
  HF_ENDPOINT                   : HF mirror (e.g. https://hf-mirror.com)
  WEAVEBENCH_DATASET_REVISION   : pin to a specific commit SHA
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path


_HF_REPO = "wanlilll/WeaveBench"
_FILE = "vm/Ubuntu.qcow2"

# Pinned sha256 of the paper-canonical v3_eyeson_apps qcow2.
# Set to None to skip verification.
_EXPECTED_SHA256: str | None = "3c1caf41cb75b8482a30d1a251545393aee8175375ab405e0d6870f8a07fa3f8"


def _resolve_token(cli_token: str | None) -> str | None:
    return (cli_token
            or os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN"))


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="weavebench-download-vm",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    ap.add_argument("--dest", default="./cache",
                    help="Local cache root (default ./cache). qcow2 lands in <dest>/vm/Ubuntu.qcow2.")
    ap.add_argument("--token", default=None,
                    help="HF token. Falls back to $HF_TOKEN / $HUGGINGFACE_TOKEN. "
                         "Optional — only needed for higher HF rate limits.")
    ap.add_argument("--revision", default=os.environ.get("WEAVEBENCH_DATASET_REVISION"),
                    help="Pin to a specific commit SHA.")
    ap.add_argument("--skip-verify", action="store_true",
                    help="Skip sha256 verification (default: verify if a pinned hash is available).")
    args = ap.parse_args(argv)

    try:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import (
            GatedRepoError, RepositoryNotFoundError, HfHubHTTPError,
        )
    except ImportError:
        print("[error] pip install huggingface_hub", file=sys.stderr)
        return 1

    token = _resolve_token(args.token)
    endpoint = os.environ.get("HF_ENDPOINT")
    if endpoint:
        print(f"[hf] using mirror {endpoint}")

    dest = Path(args.dest).expanduser().resolve()
    (dest / "vm").mkdir(parents=True, exist_ok=True)

    print(f"[fetch] {_HF_REPO}::{_FILE}  (28.46 GB, may take a while)"
          + (f" @ {args.revision[:8]}" if args.revision else ""))
    # hf_hub_download is resumable: a re-run continues a partial 28 GB file
    # rather than restarting. Mirrors (notably hf-mirror.com) rate-limit large
    # transfers with 429, so we retry with backoff — each retry resumes where
    # the last left off.
    import time
    local = None
    for attempt in range(1, 6):
        try:
            local = hf_hub_download(
                repo_id=_HF_REPO,
                repo_type="dataset",
                filename=_FILE,
                local_dir=str(dest),
                token=token,
                revision=args.revision,
            )
            break
        except GatedRepoError:
            print(f"[error] {_HF_REPO} is gated — request access at "
                  f"https://huggingface.co/datasets/{_HF_REPO}", file=sys.stderr)
            return 2
        except RepositoryNotFoundError:
            print(f"[error] {_HF_REPO} not found OR your token doesn't have access.",
                  file=sys.stderr)
            return 3
        except HfHubHTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status != 429 or attempt == 5:
                if status == 429:
                    print(f"[error] {_HF_REPO}: still rate-limited (429) after "
                          f"{attempt} attempts. Re-run the command to resume the "
                          f"download (the partial file is kept and continued).",
                          file=sys.stderr)
                    return 5
                raise
            backoff = 5 * attempt
            print(f"[retry] HTTP 429 from the Hub/mirror (attempt {attempt}/5); "
                  f"resuming in {backoff}s...", file=sys.stderr)
            time.sleep(backoff)

    local_path = Path(local)
    print(f"[done] qcow2 staged at {local_path}")

    if _EXPECTED_SHA256 and not args.skip_verify:
        print(f"[verify] computing sha256 (this takes a few minutes for 28 GB)…")
        got = _sha256(local_path)
        if got != _EXPECTED_SHA256:
            print(f"[error] sha256 mismatch:\n  expected {_EXPECTED_SHA256}\n  got      {got}",
                  file=sys.stderr)
            return 4
        print(f"[verify] sha256 OK ({got[:16]}…)")

    print()
    print(f"[next] export OSWORLD_LOCAL_QCOW2_PATH={local_path}")
    print(f"[next] then run: weavebench-run --harness openclaw --model openai/gpt-5.5 \\")
    print(f"                   --tasks_root ./cache/tasks --domains WEB \\")
    print(f"                   --task_filter task_1 --result_dir ./results/smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
