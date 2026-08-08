# CLAUDE.md — LongHorizon-Harness (this worktree)

## What this repo is

LongHorizon-Harness (`src/lh_harness/`) is an execution/state-management
harness that shells out to agent CLIs (Claude Code, Codex) to carry
long-horizon tasks to verified completion. Manager/Executor/Auditor role
separation, `AgentAdapter` protocol per backend, isolated `runs/<run-id>/`
directories. See README.md for the shipped product.

This worktree (`rdh-v0-resumability`) is building the **Recursive
Decomposition Harness** described in `PLAN.md` (repo root, one level up —
not checked into this worktree's git history; read it directly if it's
missing here) on top of this codebase. `PLAN.md` §13 defines a build ladder;
this worktree implements **v0**.

## v0 — resumability (`src/lh_harness/v0/`)

The load-bearing property (`PLAN.md` §10): `events.jsonl` is append-only and
fsync'd, and replaying it after a `kill -9` at any point converges to exactly
one artifact and one terminal event per node — no double work, no lost work.

- `v0/events.py` — `EventLog`: `append()` fsyncs every write (not just
  flush); `read_all()` silently drops a torn trailing line (kill -9 mid
  `write()` can only corrupt the line being written, and fsync-per-append
  means that's always the last line at kill time); `last_event()` /
  `has_terminal()` for querying node state.
- `v0/run_dir.py` — `create_run_dir(root, run_id)` (idempotent) plus path
  helpers for `events.jsonl`, `manifest.jsonl`, `scratch/<node>/trace.jsonl`,
  `out/<node>.md`. A minimal slice of the full `PLAN.md` §5 layout — no
  `spine.json`/`tree.json`/`contract.md` yet, those come in v1+.
- `v0/runner.py` — `run_node(run_dir, node_id, prompt, adapter, env, budget)`.
  One idempotent entrypoint for both first-run and resume: it inspects
  `events.jsonl` for the node's furthest-reached state (`episode_completed` →
  no-op replay; `session_captured` → continuation via
  `resume_session_id` if the adapter supports it, else fresh redispatch;
  `node_dispatched` only → fresh redispatch; nothing → first dispatch) and
  proceeds from there. While the episode runs, a concurrent task tails the
  live trajectory file for the first `session_id` and durably records
  `session_captured` *before* `run_episode()` returns, since a crash can
  land any time after that line hits disk. `run`/`resume` are thin aliases
  of the same function.

Event `type` vocabulary: `node_dispatched`, `session_captured` (carries
`session_id`), `episode_completed` (carries `status`, `artifact_path`),
`node_redispatched` (carries `reason`: `resumed_session` |
`no_session_captured` | `resume_unsupported`).

## Adapter changes (additive, backward compatible)

- `adapters/cli_agent.py` — `CommandAgentAdapter.run_episode` gained a
  keyword-only `command_override: str | None = None`, and the class gained a
  `supports_session_resume = False` class attribute. Existing positional/
  kwarg call sites (`manager.py`, `auditor_agent.py` via `manager.py`'s
  `_run_role_episode`) are unaffected.
- `adapters/claude_code.py` — `ClaudeCodeAdapter.supports_session_resume =
  True`. `run_episode` gained a keyword-only `resume_session_id: str | None
  = None`; when set, it splices `--resume <id>` into the same command parts
  used for a fresh dispatch (deny-tools, model, mcp-config all preserved)
  and passes it as `command_override`.
- `adapters/codex.py` — untouched. `codex exec` has no session-continuation
  flag, so `CodexAdapter` inherits `supports_session_resume = False` and
  `v0/runner.py` falls back to fresh redispatch for it rather than erroring.

## Tests

`tests/test_v0_resume.py` — stdlib `unittest`, no pytest, no network, no
real `claude`/`codex` binary. Run:

```
python3 -m unittest discover -s tests -p "test_v0_resume.py" -v
```

~7s, 4 tests, all passing as of the v0 build:

1. `ResumeAfterSessionCrashTest` — launches `run_node` as a real OS
   subprocess (`tests/fixtures/run_node_subprocess.py`), polls for
   `session_captured`, then `SIGKILL`s **both** the runner subprocess *and*
   the actual fake-CLI child process (they end up in different process
   groups because `LocalEnvironment.exec` gives the agent CLI its own
   session — killing only the runner's group would leak the child). Verifies
   the child pid is actually dead, then resumes in-process and asserts
   exactly one `episode_completed`, a `resumed_session` redispatch, and the
   fake CLI's resume-acknowledgment text in the final artifact (proving real
   continuation, not a lucky fresh run).
2. `ResumeBeforeSessionCrashTest` — same mechanics, but kills during a
   `--startup-delay` window before any stdout line exists, so resume must
   fall back to `no_session_captured`.
3. `ResumeAfterCompleteIsNoopTest` — calls `run_node` twice to completion;
   asserts the second call appends zero new events and returns the replayed
   result without touching the artifact.
4. `EventLogFsyncTest` — mocks `os.fsync` to confirm `EventLog.append` truly
   calls it (not just `flush()`), once per append.

Fixtures (`tests/fixtures/`):
- `fake_stream_agent.py` — standalone script mimicking Claude Code's
  `stream-json` output: writes its own pid to `--pidfile` immediately (so a
  test can find and kill it before it emits anything), emits a `session_id`
  line, sleeps `--work-delay`, emits a completion line; with `--resume <id>`
  it emits a resume-acknowledgment line and finishes fast instead.
- `fake_adapter.py` — `FakeStreamAgentAdapter(CommandAgentAdapter)`, the test
  double for `ClaudeCodeAdapter`: `supports_session_resume = True`, same
  `resume_session_id` passthrough.
- `run_node_subprocess.py` — CLI wrapper so `run_node` can be launched as an
  independently-killable OS process.

## Explicitly out of scope for v0 (do not build here)

Orchestrator/Planner/Reviewer roles, `tree.json`/`spine.json`/
`contract.md`, per-node tool restriction, gates. `manager.py`,
`auditor_agent.py`, `cli.py`'s role wiring, and `role_prompts.py` were not
touched — v0 is additive scaffolding, wiring it into the full CLI loop is a
later milestone (`PLAN.md` §13 v1+).
