# Backend parity audit — kusudaemon

**Scope:** can a run switch `gptme → claude | codex | opencode` without losing functionality or hitting invalid/missing CLI arguments?

**Verdict:** no. The wiring compiles and every call site type-checks, so nothing raises on construction — but that is exactly the problem. Seven of the losses are **silent**: the harness keeps running, gates keep passing, and the degraded output flows into downstream prompts as if it were real. Two are outright broken paths (opencode prompt delivery, session resume). One is a credential leak.

Files read: `adapters/{base,cli_agent,gptme_adapter,claude_code,codex,opencode,claude_permissions,_agent_worker,_gptme_worker}.py`, `pipeline/{backends,driver,cli,run,prompts}.py`, `v0/runner.py`, `v1/provider.py`, `v4/{research,mcp_research}.py`, `provider_config.py`, `provider.json`.

---

## Part 1 — Findings

### Severity table

| # | Finding | Severity | Silent? |
|---|---|---|---|
| C1 | Probe tool allowlist computed then discarded for 3 of 4 backends | Critical | yes |
| C2 | Probe prompt tells the agent to write into its own hidden path | Critical | yes |
| C3 | Research findings degrade to raw trace JSON on all CLI backends | Critical | yes |
| C4 | `opencode run` is probably never handed the prompt | Critical | no (hang/empty) |
| C5 | `OPENAI_API_KEY` fallback leaks the harness provider key to `api.openai.com` | Critical | yes |
| C6 | Session resume is dead on arrival for claude and opencode | High | yes |
| F1 | `model` silently dropped for claude and codex | High | yes |
| F2 | `node.budget.tokens` → context length is gptme-only | High | yes |
| F3 | `node.tools` never reaches claude/codex/opencode | High | yes |
| F4 | Writer web-search guarantee holds only for gptme | Medium | yes |
| F5 | Hidden-path enforcement drops from tool-level to prose-only | Medium | yes |
| S1–S7 | Dead code, stale docstrings, a wrong factory, asymmetric exports | Low | mixed |

---

### C1 — `build_research_adapter` computes a tool allowlist and throws it away

`pipeline/backends.py:309`

```python
tools = allowed_tools_for(query.kind)   # line 309
...
if backend == "gptme":
    kwargs = dict(..., tool_allowlist=tools, ...)   # line 322 — only use
if backend == "claude":
    return ClaudeCodeAdapter(workspace_path=..., prompt_dir=..., hidden_paths=hidden)  # tools gone
```

Consequences:

- **The "no write" invariant is now false for 3 of 4 backends.** `v4/research.py`'s docstring is explicit: *"a research adapter of any kind has never included gptme's save/patch tools… 'no write' (§A6's table) is enforced by omission from the tool allowlist."* A `workspace` probe on claude/codex/opencode gets the CLI's full default toolset — Bash, Write, Edit — pointed at a real repo it was only supposed to read. Codex additionally runs under `--dangerously-bypass-approvals-and-sandbox`.
- **Nothing wires web search in.** Codex's web search is off by default (needs `-c tools.web_search=true` / `--search`); OpenCode's depends on the provider. Only Claude Code has `WebSearch` on by default. So a `web` probe on codex answers from parametric memory and nobody finds out.
- **The values aren't portable anyway.** `allowed_tools_for("web")` returns `(str(SEARXNG_TOOL_PATH),)` — a filesystem path to a gptme `ToolSpec` module. Passing it straight through to `--disallowedTools` would be nonsense. This needs a per-backend translation table, not a plumbing fix.

Also: `_RESEARCH_CAPABLE` (line 73) is defined and never referenced anywhere.

### C2 — the probe prompt contradicts the probe's own hidden-paths notice

`_hidden_run_dir_subtree_for_probe` (backends.py:103) deliberately ships **without an exceptions half**, and the docstring says why:

> `workspace`/`corpus` probes never get a write tool at all… so there is no "its own artifact/scratch dir" to carve back out.

That premise is now false. Claude/codex/opencode probes *do* have write tools, and `research_prompt` (research.py:188) says:

> `Write your answer to {raw_path} as a JSON object…`

`raw_path` is `<run_dir>/…/raw.json` — inside the hidden subtree, whose notice reads *"Never read, list, search, or modify them."* The agent receives both instructions in one prompt. Claude's `path_deny_rules` only cover `Read`/`Grep`/`Glob`, so a write may squeak through; on any refusal you land in C3.

### C3 — research findings degrade to raw trace JSON

`ClaudeCodeAdapter`, `CodexAdapter`, `OpenCodeAdapter` all call `super().__init__()` **without** `visible_output_parser`. So `cli_agent.py:140`:

```python
visible_output = (self.visible_output_parser(stdout_log).strip()
                  if self.visible_output_parser is not None else "")
```

is permanently `""`. Then `v4/research.py:230`:

```python
text = result.metadata.get("assistant_visible_output") or result.actions_log or ""
```

falls through to `actions_log` — the **entire translated trace JSONL**. `cap_promotion(..., 300)` then truncates it to roughly the first 300 tokens, which begins with `_agent_worker.py`'s bootstrap line `{"type":"logdir","logdir":"/tmp/kusudaemon-claude-xxxx"}` followed by early thinking blocks.

That string is written to `finding_path`, passes the `nonempty` gate (`_GATES = ["nonempty"]`), and is injected into a downstream Writer's prompt as research evidence. gptme is immune because it *has* a parser; every CLI backend is exposed.

`v0/runner.py`'s artifact fallback is safe here only because all three set `has_file_tools = True` — that guard is doing real work and should not be relaxed.

### C4 — `opencode run` is probably never handed the prompt

`OpenCodeAdapter._template` (opencode.py:172):

```
python _agent_worker.py --format opencode -- opencode run --format json --auto < {prompt_path}
```

`_agent_worker.py:481` passes `stdin=0` to the child, so the prompt reaches `opencode` **only via stdin**. That works for the other two by design:

- `claude --print` reads stdin — fine.
- `codex exec … -` — the trailing `-` is explicitly commented in codex.py:124 as *"`-` makes Codex read the prompt from stdin"* — fine.
- `opencode run` takes the message as a **positional argument**. The documented flag list has no stdin form, the docs show no piping example, and the non-interactive issue thread discusses hanging on permission prompts rather than stdin input.

Most likely outcome: an episode with an empty prompt, or a hang until `budget.max_duration_seconds`. **Verify before building anything else** — I could not reach an `opencode` binary from this session. Run `opencode run --help` and test `echo "say hi" | opencode run --format json --auto`.

Second-order: `_OPENCODE_TYPES` in `_agent_worker.py:107` is a guessed union (`step-start`/`step_start`, `text`, `message.part.updated`, …). Capture one real `opencode run --format json` transcript and diff it against that set — unknown types pass through as raw lines, so a mismatch shows up as an unreadable Chat tab rather than an error.

### C5 — the `OPENAI_API_KEY` fallback walks around the credential-leak guard

`codex.py:78`:

```python
key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY")
```

Your `.env` sets `OPENAI_API_KEY` for the *harness provider* (that's `DEFAULT_API_KEY_ENV` in `provider_config.py:80`). Then `_config_overrides(base_url=None, api_key=key, …)`:

```python
if not base_url and not api_key:
    return []          # ← not taken: key is truthy
...
"base_url": _normalize_base_url(None)   # → "https://api.openai.com/v1"
```

Net effect: switching to the codex backend takes your NVIDIA / OpenCode-Zen key and points codex at `api.openai.com` with it. That is precisely what `claude_code.py`'s docstring says the design forbids — *"a zen/opencode key handed to `api.openai.com`-bound tooling is a credential leak"* — but the guard lives at the `backends.py` layer only, and the constructor's env fallback goes around it.

`opencode.py:92` has the same shape (`… or os.getenv("OPENAI_API_KEY")`) and then exports the value as **both** `OPENCODE_API_KEY` and `OPENAI_API_KEY`.

This is the same change as the provider.json work in Part 3: once each backend names its own `api_key_env`, these fallbacks get deleted.

### C6 — session resume is dead on arrival for claude and opencode

`_agent_worker.py:507` prints a bootstrap line **before** the CLI's first record:

```python
print(json.dumps({"type": "logdir", "logdir": session_dir}), flush=True)   # no session_id
```

`v0/runner.py:_watch_for_session_id` reads the trace line by line:

```python
session_id = record.get("session_id")
if session_id: ... return
if record.get("type") == "logdir":
    log.append({... "type": "session_captured", "logdir": record.get("logdir")})
    return          # ← watcher stops here, on the very first line
```

The first line always matches the second branch, so the watcher records a `session_captured` event carrying **only a logdir** and returns. The real session id — emitted later, when `init` (claude) / `thread.started` (codex) / `step-start` (opencode) arrives — is never captured.

On the next dispatch, `session.get("session_id")` is `None`, `resume_session_id` stays `None`, and the episode restarts from scratch. No crash, no log line saying resume was skipped. `supports_session_resume = True` on both adapters is advertising a capability that cannot fire.

(The `logdir` branch was added per PLAN-AUDIT §E13 for a hypothetical future adapter whose continuity token *is* a logdir. It now shadows the branch that matters.)

### F1 — `model` is silently dropped for claude and codex

`build_writer_adapter` (backends.py:247–276) passes `model=` to `GptmeAdapter` and `OpenCodeAdapter`, and **not** to `ClaudeCodeAdapter` or `CodexAdapter`. `--model`, `RunOptions.model`, and the dashboard model selector all no-op on two of four backends.

Note this is not just a missing keyword: threading `options.model` through as-is would also be wrong, because `deepseek-ai/deepseek-v4-flash-0731` is not a valid `claude --model` value. The backend needs its *own* model namespace — which is what you asked for in Part 3.

`build_research_adapter` has the same gap.

### F2 — `node.budget.tokens` is gptme-only

backends.py:236, inside the gptme branch:

```python
kwargs["context_length"] = node.budget.tokens   # → GPTME_CONTEXT_LENGTH
```

The planner sets this per node and `leaf_gate` validates it. On claude/codex/opencode a node budgeted at 8k silently runs at the CLI's full context window — the budget stops being a budget.

### F3 — `node.tools` never reaches the CLI backends

backends.py:227 `base_tools = tuple(node.tools) if node and node.tools else DEFAULT_TOOL_ALLOWLIST` is inside the gptme branch. Meanwhile:

- `ClaudeCodeAdapter.supports_tool_restriction = True` — but `role` is always the default `cli_executor`, whose policy denies only `("Agent",)`. Nothing maps `node.tools`.
- `OpenCodeAdapter.supports_tool_restriction = True` — and it has a fully built `permissions` parameter (`OPENCODE_PERMISSION`) that `backends.py` never passes.
- `CodexAdapter.supports_tool_restriction = False` — honest, at least.

So two adapters advertise a capability the factory never uses.

### F4 — the Writer web-search guarantee holds only for gptme

`backends.py`'s module docstring, lines 31–35:

> **Every Writer also gets the same `websearch` tool directly** … a Writer can now call it mid-episode, at will.

Implemented at line 228–231, inside `if backend == "gptme":`. On codex the tool isn't enabled at all; on opencode it depends on provider config. Switching backends quietly removes a capability the prompt layer assumes exists.

### F5 — hidden-path enforcement degrades to prose

Only Claude gets `path_deny_rules(hidden_paths, base=workspace_path)` → `--disallowedTools Read(//…) Grep(//…) Glob(//…)`. Codex and OpenCode get the prompt notice and nothing else. For codex specifically — full sandbox bypass, no deny rules — §2 invariant 6 (cross-agent isolation: a Writer must not read a sibling's `out/ch03.md`) rests entirely on the model choosing to obey prose. gptme at least enforced it structurally, by having no tool registered under the name.

OpenCode's `permissions` parameter is the obvious lever and is unused.

### Smaller items

- **S1** `_RESEARCH_CAPABLE` (backends.py:73) — dead. Delete it or gate on it.
- **S2** Stale docstrings, and they're the map people navigate by: `backends.py` still says *"three backends"* and *"`build_research_adapter` remains gptme-only"*; `adapters/__init__.py:1` says *"gptme is the only backend"*; `types.py:1` says *"gptme backend"*.
- **S3** `adapters/__init__.py` exports `OpenCodeAdapter` but not `ClaudeCodeAdapter` / `CodexAdapter`.
- **S4** `pipeline/cli.py:598 _writer_factory` — used by `cmd_revalidate`'s repair path (line 423). It reads `options.backend` directly, **bypassing `backend_override.json`**, and omits `run_dir=`, so in `kind="workspace"` mode it computes hidden paths against the wrong root. `driver._default_writer_factory` gets both right; this one should delegate to it.
- **S5** `codex.py:120` emits `--add-dir`. Verify `codex exec` accepts it on your installed version — if not, any operator with `KUSUDAEMON_MCP_ADD_DIRS` set gets a hard CLI parse error rather than a harness-level one.
- **S6** `OpenCodeAdapter` has no `mcp_config` parameter while claude/codex do. No TypeError today (the factory doesn't pass it), but it's an asymmetry to close.
- **S7** `pipeline/prompts.py:162` says *"using your file tools (e.g. gptme's save/patch)"*. Harmless but worth generalizing.

---

## Part 2 — Implementation plan (parity)

Ordered so each phase is independently shippable and testable.

### Phase 0 — verify before building (half a day)

Nothing below is worth writing until these three facts are pinned down on your machine.

1. `opencode run --help`; then `echo "reply with OK" | opencode run --format json --auto`. Does it read stdin, or does it need the message as `argv`? → decides C4's fix shape.
2. Capture one real `opencode run --format json` transcript to a file; diff its `type` values against `_OPENCODE_TYPES` (`_agent_worker.py:107`).
3. `codex exec --help` — confirm `--add-dir`, `--skip-git-repo-check`, `--dangerously-bypass-approvals-and-sandbox`, and that `-` still means stdin.

Record the answers as a fixture file under `tests/fixtures/` so the translator tests stop being guesses.

### Phase 1 — stop the bleeding (critical, no new abstractions)

**1a. C4 — prompt delivery.** If opencode needs `argv`, add a `prompt_delivery` class attribute to `CommandAgentAdapter` (`"stdin"` default, `"argv"` for opencode) and have `run_episode` substitute a new `{prompt_arg}` placeholder — `shlex.quote(prompt)` — instead of `< {prompt_path}`. Keep writing the prompt file regardless: `--file`/`-f` may be the better delivery and the file is also the audit record.

**1b. C5 — kill the cross-backend key fallbacks.** In `codex.py:78` and `opencode.py:92`, delete `os.getenv("OPENAI_API_KEY")` from the chain. Codex reads `CODEX_API_KEY` only; opencode reads `OPENCODE_API_KEY` only; both otherwise fall through to the CLI's own auth. Add a regression test: `OPENAI_API_KEY` set in the environment must not appear in `CodexAdapter(...).command_template`.

**1c. C6 — fix session capture.** In `_watch_for_session_id`, only treat a `logdir` record as terminal when it carries no session-capable successor — simplest correct form: keep polling until either a record with a non-empty `session_id` arrives **or** `stop` is set, and record the logdir-only line as a non-terminal `session_captured` update rather than returning on it. Add a test that feeds the real two-line sequence (bootstrap logdir, then `init` logdir with `session_id`) and asserts the id is captured.

**1d. C3 — give the CLI adapters a visible-output parser.** `_agent_worker.py` already emits gptme-shaped lines, so `gptme_visible_output` works verbatim on their stdout. Pass `visible_output_parser=gptme_visible_output` in all three adapters (move it to a shared `adapters/trace_output.py` so `gptme_adapter` isn't importing from a sibling backend). This makes `assistant_visible_output` real, kills the raw-JSON-as-finding path, and correctly sets `actions_log_diagnostics_only`.

**1e. C2 — carve out the probe's raw-finding path.** Give `_hidden_run_dir_subtree_for_probe` an exceptions half returning the probe's own `research_raw_finding_path`, and thread it as `hidden_path_exceptions` for every backend. Cheap, and removes a live contradiction from the prompt.

### Phase 2 — per-backend capability translation (the real fix for C1, F2–F5)

Introduce one table instead of four `if backend ==` ladders. New module `adapters/capabilities.py`:

```python
@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    # canonical capability -> backend-native construction
    tool_map: dict[str, ToolSpec]        # "read" | "write" | "shell" | "websearch" | "grep" | "list"
    supports_context_length: bool
    supports_tool_allowlist: bool
    supports_tool_denylist: bool
    path_deny_builder: Callable[...] | None
```

Then rewrite `build_writer_adapter` / `build_research_adapter` as: resolve capabilities → project the canonical request (`node.tools` + `websearch`, `node.budget.tokens`, `hidden_paths`) onto the backend → construct.

Concrete per-backend projections:

| Canonical | gptme | claude | codex | opencode |
|---|---|---|---|---|
| tool allowlist | `--tool-allowlist` | invert → `--disallowedTools` over the known tool set | *unsupported* — log a `capability_unavailable` event | `OPENCODE_PERMISSION` JSON |
| `websearch` | searxng tool path | built-in `WebSearch` (ensure not denied) | `-c tools.web_search=true` | provider-dependent; verify |
| context length | `GPTME_CONTEXT_LENGTH` | *unsupported* → event | *unsupported* → event | *unsupported* → event |
| hidden paths | omit from allowlist + notice | `path_deny_rules` + notice | notice only → event | `OPENCODE_PERMISSION` deny + notice |

The load-bearing rule: **when a backend cannot honor a capability, emit a structured `capability_unavailable` event to `events.jsonl`** (`{node_id, backend, capability, reason}`) rather than dropping it silently. That single change converts every one of F1–F5 from invisible to auditable, and it's what makes "no functionality is lost when I switch" a claim you can check after the fact instead of a hope.

Then delete `_RESEARCH_CAPABLE` (S1) or make `build_research_adapter` actually gate on it.

### Phase 3 — cleanup

- S4: `cli.py:_writer_factory` delegates to the driver's factory (picks up `backend_override.json` + `run_dir`).
- S2/S3/S7: docstrings, `adapters/__init__.py` exports, generalize the prompts.py tool hint.
- S6: add `mcp_config` to `OpenCodeAdapter`.
- Extend `tests/test_backend_toggle.py` into a parametrized matrix: for each of the four backends × `{writer, web probe, workspace probe, corpus probe}`, assert the constructed `command_template` contains the expected flags and **no** harness provider credential.

---

## Part 3 — `provider.json` backend customization

### Requested shape

Four top-level backend objects alongside the existing `providers` map (which keeps serving the direct-API roles — orchestrator, reviewer, planner, survey — via `v1/provider.py`; those are text-in/JSON-out HTTP calls and are not affected by any of this).

```jsonc
{
  // ── unchanged: direct-API roles ──────────────────────────────
  "default": "nvidia",
  "providers": {
    "nvidia":    { "base_url": "https://integrate.api.nvidia.com/v1",
                   "model": "deepseek-ai/deepseek-v4-flash-0731",
                   "api_key_env": "NVIDIA_API_KEY" },
    "llama.cpp": { "base_url": "http://localhost:8080/v1",
                   "model": "qwen", "api_key_env": "LLAMACPP_API_KEY" }
  },
  "fallbacks": { "deepseek-ai/deepseek-v4-flash-0731": "qwen" },

  // ── new: per-backend subagent config ─────────────────────────
  "backends": {
    "claude": {
      "model": "claude-sonnet-4-6",
      "api_key_env": "ANTHROPIC_API_KEY",
      "base_url": null
    },

    "codex": {
      "model": "gpt-5.2-codex",
      "api_key_env": "CODEX_API_KEY",
      "base_url": null,
      "wire_api": "responses"
    },

    "opencode": {
      "provider": "zen",
      "providers": {
        "zen":   { "base_url": "https://opencode.ai/zen/v1",
                   "model": "opencode/deepseek-v4-flash-free",
                   "models": ["opencode/deepseek-v4-flash-free", "opencode/qwen3-coder"],
                   "api_key_env": "OPENCODE_API_KEY" },
        "local": { "base_url": "http://localhost:8080/v1",
                   "model": "qwen",
                   "api_key_env": "LLAMACPP_API_KEY" }
      }
    },

    "gptme": {
      "provider": "nvidia",          // name from the top-level "providers" map
      "model": "deepseek-ai/deepseek-v4-flash-0731"
    }
  }
}
```

I'd nest under `"backends"` rather than putting the four names at the file's top level, for one concrete reason: `read_config_file` currently treats *any* object without a `"providers"` key as the legacy flat single-provider shape (`provider_config.py:195`). Four bare top-level keys named after backends is one refactor away from being mis-parsed as a provider entry. `"backends"` is unambiguous and keeps the two namespaces visibly separate. If you'd rather have them literally top-level, the change is mechanical — reserve the four names in `read_config_file` before the legacy-shape branch.

### Semantics per backend

- **claude / codex** — CLI-auth backends. `model` is the default `--model` value. `api_key_env` names the env var (never the key itself, matching the existing file contract). `base_url` is optional; `null` means *use the CLI's own config untouched*, preserving `codex.py`'s deliberate "don't inject a default endpoint" deviation.
- **opencode / gptme** — provider-capable backends. Either an inline `providers` sub-map (opencode, since its providers are its own namespace) or a `provider` reference into the top-level map (gptme, since it speaks the same OpenAI-compatible protocol the direct-API roles do). `model` picks the default within the selected provider.
- **`models` array** on any entry feeds a per-backend model picker (see below).

### Code changes

**1. New `provider_config.read_backend_config(name)` → `BackendSettings`:**

```python
@dataclass(frozen=True)
class BackendSettings:
    backend: str
    model: str | None          # None = omit the --model flag entirely
    base_url: str | None
    api_key: str               # resolved from api_key_env, may be ""
    api_key_env: str
    extra: dict[str, object]   # wire_api, permissions, ...
    source: str
```

Precedence per field, mirroring the existing `resolve()` ladder — highest first:

1. explicit constructor argument
2. `KUSUDAEMON_<BACKEND>_MODEL` / `_BASE_URL` / `_API_KEY` (e.g. `KUSUDAEMON_CLAUDE_MODEL`)
3. run-level `model_override.json`, **only when it names a model in this backend's own `models` list** — otherwise ignored with a logged event (this is what stops the dashboard selector from feeding `deepseek-ai/…` to `claude --model`)
4. the `backends.<name>` block in `provider.json`
5. **omit** — let the CLI use its own config

Rung 5 being *omit* rather than a hardcoded default is the important one. It matches `resolve()`'s existing "there is no step 5 that silently falls back to a hardcoded endpoint" rule, and it's the behavior `codex.py`'s docstring already argues for.

**2. Hard isolation between backend key namespaces.** `read_backend_config` reads *only* the `api_key_env` its own block names. No cross-backend fallback, ever. This is C5's fix and this feature landing as one change — which is why they belong in the same PR.

**3. `backends.py` consumes it.** `build_writer_adapter` / `build_research_adapter` call `read_backend_config(backend)` and pass `model` / `base_url` / `api_key` / `extra` into every adapter uniformly — fixing F1 as a side effect and removing the current asymmetry where opencode gets `model=` and claude/codex don't.

**4. `list_models_for_backend(name)`** alongside the existing `list_available_models()`, so the dashboard's model selector is scoped to the *currently selected backend* instead of offering direct-API provider models for a `claude` run.

**5. `provider.example.json` + README** get the full four-backend example, and `ensure_user_config`'s `SAMPLE_SETTINGS` grows a `"backends"` block with `null` models (i.e. "use each CLI's own default") so a fresh install behaves exactly as today.

**6. Validation.** `read_backend_config` raises `ProviderConfigError` naming the file and the offending key for: an unknown backend name, an `opencode.provider` / `gptme.provider` that isn't defined, or a `model` not present in a declared `models` list. Loud at startup, consistent with how `resolve()` already handles a bad `default`.

### Suggested sequencing

Phase 1 (critical fixes) → Phase 3 provider.json work → Phase 2 (capability table). Phase 2 is the largest change and it wants `BackendSettings` to already exist, since `extra` is the natural carrier for `permissions` / `wire_api` / `tools.web_search`.
