# hermes_patches/

In-VM patches applied at runtime by `weavebench/agents/hermes_agent.py`
after the Hermes tarball is extracted inside the OSWorld VM.

## Convention (mirrors `openclaw_patches/`)

For each patched file ship a pair:

- `<name>.original.py` (or `.yaml`) — verbatim copy from the upstream
  install (kept for diff / audit).
- `<name>.patched.py` — our modified version. The agent class copies this
  over the upstream path inside the VM during `bootstrap()`.

This directory is currently empty. Patches will be added on demand if
upstream Hermes surfaces SSRF / sandbox / endpoint / log-noise issues
that prevent rollouts from completing.

## GUI status

`hermes -z` is **bash-only on Linux** — Hermes ships `computer_use` but
its only computer-use backend (`cua-driver`) is macOS only. On Linux it
has the `browser_*` toolset (Camofox / CDP) and shell tools but no
`pyautogui` desktop driver. The Linux Hermes agent therefore runs in
CLI-only mode; the `gui=True` path is `NotImplementedError`.
