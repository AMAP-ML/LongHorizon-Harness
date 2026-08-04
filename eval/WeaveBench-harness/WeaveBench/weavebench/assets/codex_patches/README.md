# codex_patches/

In-VM patches applied at runtime by `weavebench/agents/codex_agent.py`
after the Codex CLI tarball is extracted inside the OSWorld VM.

## Convention (mirrors `openclaw_patches/`)

For each patched file ship a pair:

- `<name>.original.js` (or `.py`) — verbatim copy from the upstream
  install (kept for diff / audit).
- `<name>.patched.js` — our modified version. The agent class copies this
  over the upstream path inside the VM during `bootstrap()`.

This directory is currently empty. Patches will be added on demand if
upstream Codex CLI surfaces SSRF / sandbox / endpoint / log-noise issues
that prevent rollouts from completing.
