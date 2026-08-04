"""WeaveBench quickstart — one command that gets a new user to "ready to run".

What it does:
  1. Pre-flight checks for $HF_TOKEN + $OPENROUTER_API_KEY (early exit if missing)
  2. Downloads the 114 task dataset (~207 MB) into ./cache/tasks/
  3. Downloads the openclaw runtime tarball (~514 MB) into ./cache/runtime_assets/
  4. Downloads the judge template (~7 KB) into ~/judge_agent_test/ and
     substitutes $OPENROUTER_API_KEY into the openclaw config
  5. Downloads the Ubuntu qcow2 VM image (28.46 GB) into ./cache/vm/
     (skip with --skip-vm if you already have a qcow2)
  6. Prints the next `weavebench-run` command to copy-paste

Env vars consumed:
  OPENROUTER_API_KEY            : required
  HF_TOKEN                      : optional — only needed for higher HF rate limits
  HF_ENDPOINT                   : optional HF mirror (e.g. https://hf-mirror.com)
  WEAVEBENCH_CACHE              : cache root (default ./cache)
  WEAVEBENCH_JUDGE_HOME         : judge install dir (default ~/judge_agent_test)

Note: weavebench-quickstart does NOT install the OpenClaw CLI for you (it's
a Node binary, outside our pip install scope). Run `npm install -g openclaw`
yourself first, or use `bash scripts/setup.sh` which handles npm too.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _have(*names: str) -> bool:
    return any(os.environ.get(n) for n in names)


def _check_node_ge_22() -> tuple[bool, str]:
    """Return (ok, version_or_reason)."""
    node = shutil.which("node")
    if not node:
        return False, "not installed"
    try:
        raw = subprocess.run([node, "--version"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
        # e.g. "v22.10.0"
        major = int(raw.lstrip("v").split(".")[0])
        if major < 22:
            return False, f"{raw} (need 22+)"
        return True, raw
    except Exception as e:
        return False, f"check failed: {e}"


def _check_system_prereqs() -> list[str]:
    """Return a list of missing-prereq messages. Empty list = all OK."""
    missing: list[str] = []
    if not shutil.which("docker"):
        missing.append("docker  (apt install docker.io / docker.com)")
    # qemu-system-x86_64 is NOT a hard prerequisite: the default docker GUI
    # provider boots the qcow2 inside the happysixd/osworld-docker container,
    # which ships its own QEMU — the host needs only docker + /dev/kvm. A host
    # qemu is only needed if you bypass docker and run QEMU directly, so we just
    # inform rather than fail (matches scripts/setup.sh).
    if not shutil.which("qemu-system-x86_64"):
        print("  [info]    qemu-system-x86_64 not on host — fine for the docker "
              "GUI provider (the container provides QEMU); only needed if you run "
              "QEMU directly outside docker.")
    ok, ver = _check_node_ge_22()
    if not ok:
        missing.append(f"node 22+  ({ver})")
    if not shutil.which("npm"):
        missing.append("npm  (ships with Node)")
    return missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="weavebench-quickstart",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--dest", default=os.environ.get("WEAVEBENCH_CACHE", "./cache"),
                    help="Cache root (default ./cache or $WEAVEBENCH_CACHE).")
    ap.add_argument("--skip-vm", action="store_true",
                    help="Skip the 28 GB qcow2 download (use if you already have one).")
    ap.add_argument("--harness", default="openclaw",
                    help="Which harness runtime to fetch (default openclaw). "
                         "CSV for multiple, or 'all'.")
    ap.add_argument("--ignore-system-prereqs", action="store_true",
                    help="Don't fail when docker/qemu/node/npm are missing. "
                         "Use this if you intend to download the assets on one "
                         "machine and run on another.")
    args = ap.parse_args(argv)
    dest = Path(args.dest).expanduser().resolve()
    judge_home = Path(os.environ.get("WEAVEBENCH_JUDGE_HOME",
                                      str(Path.home() / "judge_agent_test"))).expanduser().resolve()

    print("=" * 68)
    print("WeaveBench quickstart")
    print("=" * 68)

    # 1. Pre-flight credentials + system prereqs
    n_total = 4 if args.skip_vm else 5
    print(f"\n[1/{n_total}] checking prerequisites")
    missing_env = []
    if not _have("OPENROUTER_API_KEY"):
        missing_env.append("OPENROUTER_API_KEY  (https://openrouter.ai/keys)")
    if missing_env:
        print("[error] missing env vars:")
        for m in missing_env:
            print(f"  - {m}")
        print("\nExport and retry:")
        print("  export OPENROUTER_API_KEY=sk-or-...")
        print("  # users in China:  export HF_ENDPOINT=https://hf-mirror.com")
        return 1
    print("       ✓ OPENROUTER_API_KEY set")
    if _have("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        print("       ✓ HF_TOKEN set (will use for higher HF rate limits)")
    if os.environ.get("HF_ENDPOINT"):
        print(f"       ✓ using HF mirror: {os.environ['HF_ENDPOINT']}")

    sys_missing = _check_system_prereqs()
    if sys_missing:
        msg = ("[error] missing system prereqs (needed at runtime, "
               "not for download):")
        for m in sys_missing:
            msg += f"\n  - {m}"
        if args.ignore_system_prereqs:
            print(msg.replace("[error]", "[warn]"))
            print("       (--ignore-system-prereqs: continuing anyway)")
        else:
            print(msg)
            print("\n  Install the above and retry, OR pass --ignore-system-prereqs "
                  "if you're staging downloads on a different machine.")
            print("  See README §Requirements for install hints.")
            return 1
    else:
        print("       ✓ docker, node 22+, npm all present")
    if not shutil.which("openclaw"):
        print("       ⚠  OpenClaw CLI not on PATH — run `npm install -g openclaw` "
              "before the first weavebench-run (or use scripts/setup.sh which "
              "handles npm too).")

    # 2. dataset
    print(f"\n[2/{n_total}] downloading 114 tasks -> {dest}/tasks/")
    from weavebench.scripts.download_dataset import main as dl_dataset
    rc = dl_dataset(["--dest", str(dest)])
    if rc:
        print("[error] dataset download failed", file=sys.stderr)
        return rc

    # 3. runtime assets
    print(f"\n[3/{n_total}] downloading {args.harness} runtime -> {dest}/runtime_assets/")
    from weavebench.scripts.download_assets import main as dl_assets
    rc = dl_assets(["--dest", str(dest), "--harness", args.harness])
    if rc:
        print("[error] asset download failed", file=sys.stderr)
        return rc

    # 4. judge template (cheap; runs before the qcow2 so a failure here doesn't waste 28 GB)
    # NOTE: no --force — the script auto-detects user edits and no-ops if it
    # finds an already-customised openclaw.json.
    print(f"\n[4/{n_total}] downloading judge template -> {judge_home}/")
    from weavebench.scripts.download_judge import main as dl_judge
    rc = dl_judge(["--judge-home", str(judge_home)])
    if rc:
        print("[error] judge template install failed", file=sys.stderr)
        return rc

    # 5. qcow2 VM image (the big one)
    if not args.skip_vm:
        print(f"\n[5/{n_total}] downloading 28 GB Ubuntu qcow2 -> {dest}/vm/")
        print("       (this takes a while; use --skip-vm if you already have one)")
        from weavebench.scripts.download_vm import main as dl_vm
        rc = dl_vm(["--dest", str(dest)])
        if rc:
            print("[error] qcow2 download failed", file=sys.stderr)
            return rc

    # next-step
    print("\n" + "=" * 68)
    print("Ready. Next:")
    print("=" * 68)
    qcow = dest / "vm" / "Ubuntu.qcow2"
    openclaw_bin = shutil.which("openclaw") or "/usr/local/bin/openclaw  # npm install -g openclaw first"
    print(f"""
    {'export OSWORLD_LOCAL_QCOW2_PATH=' + str(qcow) if not args.skip_vm else '# point OSWORLD_LOCAL_QCOW2_PATH at your local qcow2'}
    export AJ_OPENCLAW_BIN={openclaw_bin}
    export AJ_TEMPLATE_PROFILE={judge_home}/template_profile
    export AJ_TEMPLATE_WORKSPACE={judge_home}/template_workspace

    weavebench-run \\
        --harness openclaw \\
        --model openai/gpt-5.5 \\
        --tasks_root {dest / "tasks"} \\
        --domains WEB \\
        --task_filter task_1_ \\
        --result_dir ./results/smoke
""")
    print("Need KVM/Docker on the host first — see README §Requirements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
