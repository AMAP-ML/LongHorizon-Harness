"""Download + install the host-side OpenClaw judge template.

Usage::

    weavebench-download-judge [--judge-home ~/judge_agent_test]

This:
  1. Downloads judge/judge_template.tar.gz from the WeaveBench HF dataset
  2. Unpacks into <judge-home>/template_profile and <judge-home>/template_workspace
  3. Substitutes placeholders in template_profile/openclaw.json:
       - PLACEHOLDER_OPENROUTER_API_KEY  -> $OPENROUTER_API_KEY
       - PLACEHOLDER_GATEWAY_TOKEN       -> a random 96-bit hex string
       - PLACEHOLDER_WORKSPACE_DIR       -> <judge-home>/judge_workspace
  4. Prints the env vars to export for `weavebench-run`

Pre-requisite: install OpenClaw on the host first::

    npm install -g openclaw
    # or: npx -y openclaw onboard   (one-shot, no global install)

Optional env vars:
  HF_TOKEN / HUGGINGFACE_TOKEN  : optional auth token (higher HF rate limits)
  HF_ENDPOINT                   : HF mirror (e.g. https://hf-mirror.com)
  WEAVEBENCH_DATASET_REVISION   : pin commit SHA for reproducibility
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
import tarfile
from pathlib import Path


_HF_REPO = "wanlilll/WeaveBench"
_FILE = "judge/judge_template.tar.gz"


def _resolve_token(cli_token: str | None) -> str | None:
    return (cli_token
            or os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="weavebench-download-judge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    ap.add_argument("--judge-home",
                    default=os.environ.get("WEAVEBENCH_JUDGE_HOME",
                                            str(Path.home() / "judge_agent_test")),
                    help="Where to install the judge template "
                         "(default ~/judge_agent_test or $WEAVEBENCH_JUDGE_HOME).")
    ap.add_argument("--token", default=None,
                    help="HF token. Falls back to $HF_TOKEN / $HUGGINGFACE_TOKEN.")
    ap.add_argument("--revision", default=os.environ.get("WEAVEBENCH_DATASET_REVISION"),
                    help="Pin to a specific dataset commit SHA.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing template_profile/template_workspace.")
    args = ap.parse_args(argv)

    try:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError
    except ImportError:
        print("[error] pip install huggingface_hub", file=sys.stderr)
        return 1

    token = _resolve_token(args.token)
    if os.environ.get("HF_ENDPOINT"):
        print(f"[hf] using mirror {os.environ['HF_ENDPOINT']}")

    judge_home = Path(args.judge_home).expanduser().resolve()
    judge_home.mkdir(parents=True, exist_ok=True)

    print(f"[fetch] {_HF_REPO}::{_FILE}"
          + (f" @ {args.revision[:8]}" if args.revision else ""))
    try:
        local = hf_hub_download(
            repo_id=_HF_REPO,
            repo_type="dataset",
            filename=_FILE,
            token=token,
            revision=args.revision,
        )
    except GatedRepoError:
        print(f"[error] {_HF_REPO} is gated — request access at "
              f"https://huggingface.co/datasets/{_HF_REPO}", file=sys.stderr)
        return 2
    except RepositoryNotFoundError:
        print(f"[error] {_HF_REPO} not found OR your token doesn't have access.",
              file=sys.stderr)
        return 3

    # Unpack into a staging dir so we can compare against any existing install.
    staging = judge_home / ".judge_template_unpack"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    with tarfile.open(local) as t:
        # tarfile filter= kwarg only exists on Python 3.10.12+ / 3.11.4+ / 3.12+.
        # Older 3.10/3.11 (e.g. Ubuntu 22.04 stock 3.10.6) lack it.
        try:
            t.extractall(staging, filter="data")
        except TypeError:
            t.extractall(staging)

    src_profile = staging / "profile"
    src_workspace = staging / "workspace"
    dst_profile = judge_home / "template_profile"
    dst_workspace = judge_home / "template_workspace"

    # Decide whether the existing template still has unsubstituted
    # placeholders. If yes, the user hasn't customised it and we can safely
    # replace. If no placeholders remain, treat the file as "user-owned" and
    # require --force to overwrite — protects manual edits to openclaw.json
    # (provider swap, model swap, gateway token rotation).
    existing_json = dst_profile / "openclaw.json"
    user_edited = (
        existing_json.exists()
        and "PLACEHOLDER_" not in existing_json.read_text(encoding="utf-8")
    )
    if user_edited and not args.force:
        print(f"[skip] {existing_json} appears to have been edited "
              f"(no PLACEHOLDER_* markers remain). Leaving it alone. "
              f"Pass --force to overwrite.", file=sys.stderr)
        # Workspace dir is starter-pack content and harmless to refresh, but
        # to honour the "don't touch this install" intent we leave it too.
        shutil.rmtree(staging)
        # Still print the next-step hint so the user sees the AJ_* exports.
        _print_next_steps(dst_profile, dst_workspace, judge_home,
                           openrouter_key_present=bool(os.environ.get("OPENROUTER_API_KEY")))
        return 0

    for src, dst in [(src_profile, dst_profile), (src_workspace, dst_workspace)]:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))

    shutil.rmtree(staging)

    # Substitute placeholders in openclaw.json — single-pass regex so that
    # if a substitution value happens to contain another placeholder string
    # (paranoid: a key containing the literal "PLACEHOLDER_GATEWAY_TOKEN"),
    # we don't double-replace.
    import re
    openclaw_json = dst_profile / "openclaw.json"
    body = openclaw_json.read_text(encoding="utf-8")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not openrouter_key:
        print("[warn] $OPENROUTER_API_KEY not set — leaving PLACEHOLDER_OPENROUTER_API_KEY "
              "in openclaw.json; you must edit it manually before running the judge.",
              file=sys.stderr)
    substitutions = {
        "PLACEHOLDER_OPENROUTER_API_KEY": openrouter_key or "PLACEHOLDER_OPENROUTER_API_KEY",
        "PLACEHOLDER_GATEWAY_TOKEN": secrets.token_hex(24),  # local loopback only, doesn't leave host
        "PLACEHOLDER_WORKSPACE_DIR": str(judge_home / "judge_workspace"),
    }
    body = re.sub(
        "|".join(re.escape(k) for k in substitutions),
        lambda m: substitutions[m.group(0)],
        body,
    )
    openclaw_json.write_text(body, encoding="utf-8")

    (judge_home / "judge_workspace").mkdir(exist_ok=True)

    _print_next_steps(dst_profile, dst_workspace, judge_home,
                      openrouter_key_present=bool(openrouter_key))
    return 0


def _print_next_steps(dst_profile: Path, dst_workspace: Path, judge_home: Path,
                      openrouter_key_present: bool) -> None:
    # Look up openclaw binary
    bin_path = shutil.which("openclaw")
    if bin_path:
        bin_hint = bin_path
    else:
        bin_hint = "/usr/local/bin/openclaw  # run `npm install -g openclaw` first"

    print(f"[done] judge template under {judge_home}/")
    print(f"       template_profile  : {dst_profile}")
    print(f"       template_workspace: {dst_workspace}")
    print()
    print("[next] export these before running weavebench-run:")
    print(f"  export AJ_OPENCLAW_BIN={bin_hint}")
    print(f"  export AJ_TEMPLATE_PROFILE={dst_profile}")
    print(f"  export AJ_TEMPLATE_WORKSPACE={dst_workspace}")
    if not openrouter_key_present:
        print()
        print("[warn] also edit openclaw.json to replace PLACEHOLDER_OPENROUTER_API_KEY")

if __name__ == "__main__":
    raise SystemExit(main())
