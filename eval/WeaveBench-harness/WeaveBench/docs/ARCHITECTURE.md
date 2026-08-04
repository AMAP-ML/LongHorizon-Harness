# Architecture

This document explains the runtime data flow: where an LLM HTTP body comes from, how the four harness adapters share one GUI plugin, where the grader's score comes from, and how the bundled desktop_env hosts the VM.

## 1. One round-trip

```
LLM (OpenRouter)  ◄──── HTTPS ────  Harness CLI (in-VM)  ◄── stdio ──  WeaveBench computer_tool_plugin
   model="openai/gpt-5.5"               openclaw / codex /                   (TypeScript, 9 actions)
                                        claude / hermes
```

For every step:

1. The harness CLI (e.g. `openclaw agent` running inside the VM) sends an LLM request via OpenRouter. The request body has four sections:
   - `instructions` — harness-defined system prompt
   - `input` — user task + accumulated tool results
   - `tools` — the JSON schema for each tool, **including** the `__computer__` tool injected from `weavebench/assets/computer_tool_plugin/index.ts`
   - `model`, `tool_choice`, …
2. The LLM responds with tool calls.
3. The harness executes them. GUI tool calls (`__computer__`) flow to our plugin, which translates `{action: "click", x, y}` etc. into PyAutoGUI ops on the live `:0` display and captures a 1920×1080 screenshot to return.
4. Loop until the LLM emits a final `assistant` text or hits a step budget.

## 2. Why four harnesses share one plugin

The paper's **harness sweep** (Table 3) requires that each agent runtime exposes the **same** GUI primitives, otherwise comparisons reflect harness friction rather than model capability. We achieve this by:

- Defining the GUI tool **once** in [`weavebench/assets/computer_tool_plugin/index.ts`](../weavebench/assets/computer_tool_plugin/index.ts) (9 actions: `screenshot`, `click`, `double_click`, `right_click`, `type`, `key`, `scroll`, `drag`, `wait`).
- Reusing it across harnesses via two mechanisms:
  - **OpenClaw** loads it as a native plugin (`openclaw.plugin.json`).
  - **Codex / Claude Code / Hermes** consume it through an [MCP](https://modelcontextprotocol.io) stdio server ([`weavebench/assets/weavebench_computer_mcp/server.py`](../weavebench/assets/weavebench_computer_mcp/server.py)), so the same 9 actions appear in every harness's tool catalogue.

## 3. Scoring — trajectory-aware Agent-as-Judge

Scoring is done by a **host-side OpenClaw instance** acting as judge — see [`docs/AGENT_JUDGE.md`](./AGENT_JUDGE.md) for the user-facing setup. Mechanically:

- Every task `.md` ships a Python `grade(workspace_path, transcript)` function. We do NOT call it during `weavebench-run` — all six runners under `weavebench/eval/_runners/` monkey-patch `orchestrator.run_one` → `agent_judge.run_one_aj` at import time (see e.g. `responses_gui.py:55`), so the agent's deliverables are tarred up and handed to the host-side judge instead.
- The judge ([`weavebench/eval/agent_judge/judge_runner.py`](../weavebench/eval/agent_judge/judge_runner.py)) reads (a) the task `.md`, (b) the agent's `chat.jsonl` trace, and (c) the unpacked `results.tar.gz`. It produces per-clause evidence + an overall 0..1 score in `score.json`.
- Why a judge instead of `grade(...)`: the file-only grader is spoofable — agents can fabricate PNGs that pass a file-existence + OCR check. The trajectory-aware judge looks at HOW the deliverable was produced, catching shortcut behaviors (synthetic screenshots, hard-coded metrics, mocked services).

The embedded `grade(...)` function is retained in every task `.md` as documentation of the rubric and as an emergency fallback you can call directly if you want a fast file-only check during task development.

## 4. Deliverable archival

Before the VM is destroyed, `_archive_deliverables` (orchestrator.py) tars `/tmp_workspace/results/` to `<task_dir>/results.tar.gz` (≤ 1 GB). This is what the host-side judge then reads. It also lets you re-judge offline later with [`weavebench/eval/agent_judge/rejudge_batch.py`](../weavebench/eval/agent_judge/rejudge_batch.py) — for example, when the judge prompt changes — without rerunning agents and without paying agent-side LLM cost.

## 5. desktop_env (bundled, OSWorld-derived)

WeaveBench ships its own copy of the OSWorld `desktop_env/` package under [`weavebench/desktop_env/`](../weavebench/desktop_env/). What's in it:

| Subdir | Origin | Purpose |
|---|---|---|
| `desktop_env.py`, `actions.py`, `controllers/` | OSWorld (upstream, slightly patched) | `DesktopEnv` class + PythonController + SetupController |
| `providers/base.py`, `providers/__init__.py` | OSWorld + WeaveBench rewrite | the VMManager / Provider abstraction + the (slimmed) factory |
| `providers/docker/` | OSWorld | KVM/qcow2 provider (the default for GUI mode) |
| `providers/docker_lite/` | **WeaveBench-original** | headless Docker variant (no X server) used for CLI ablation |
| `server/` | OSWorld | in-VM Flask shim |

What's been **stripped** from upstream OSWorld:
- The entire `desktop_env/evaluators/` subpackage — WeaveBench tasks embed their own `## Grader`. A small inlined `compare_urls` keeps `SetupController.setup_chromium` working without pulling in `formulas` / `openpyxl` / `lxml.cssselect` etc.
- The cloud / hypervisor providers (`aws`, `azure`, `aliyun`, `gcp`, `virtualbox`, `vmware`, `volcengine`) — only `docker` (KVM) and `docker_lite` (headless) are bundled. Requesting any stripped provider raises a clear `NotImplementedError` pointing at upstream OSWorld.
- Optional top-level imports (`playwright`, `pydrive`, `dotenv`, `requests_toolbelt`, `aws.proxy_pool`) in `controllers/setup.py` are now `try`/`except ImportError` so missing optional deps don't break `import`.

If you want the full OSWorld evaluator pipeline or any of the stripped providers, install upstream OSWorld separately (`pip install git+https://github.com/xlang-ai/OSWorld.git`) — its namespace is `desktop_env`, ours is `weavebench.desktop_env`, so they coexist cleanly.
