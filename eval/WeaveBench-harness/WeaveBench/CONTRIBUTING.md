# Contributing to WeaveBench

Thanks for your interest in WeaveBench! This project is the paper-companion
code release for *WeaveBench: A Long-Horizon, Real-World Benchmark for
Computer-Use Agents with Hybrid Interfaces*. Contributions of any size are
welcome — from typo fixes to new harness adapters.

## Quick links

- **Bug reports** → [GitHub Issues](https://github.com/weavebench/WeaveBench/issues)
- **Security issues** → please email the corresponding author privately rather than filing a public issue
- **Discussion** → [GitHub Discussions](https://github.com/weavebench/WeaveBench/discussions) (if enabled), otherwise an issue is fine

## Filing a bug

Please include:

1. **What you ran** — the exact `weavebench-run` (or `bash scripts/setup.sh`)
   command, with all relevant env vars (`HF_TOKEN`, `OPENROUTER_API_KEY`,
   `HF_ENDPOINT`, etc. — **redact actual secrets**).
2. **What you expected vs. what happened** — paste stderr / `agent.log` /
   `score.json` if relevant.
3. **System info** — `python3 --version`, `node --version`, `docker --version`,
   OS, GPU/CPU if relevant.
4. **Repo commit** — `git rev-parse HEAD`.

For reproducibility-relevant bugs (i.e. your numbers diverge from the paper
beyond ±1%), please cross-check against [`docs/REPRODUCE.md`](./docs/REPRODUCE.md)
first — the dataset revision and model snapshot ids matter.

## Submitting a PR

```bash
git clone https://github.com/weavebench/WeaveBench.git && cd WeaveBench
python3 -m pip install -e ".[dev]"
pytest tests/ -q                # 55 tests, ~0.5s
ruff check weavebench tests     # lint (CI runs this in warning-only mode for v0.1)
```

PRs that pass CI and have a clear "why" in the commit message are likely to
land quickly. Larger refactors — please open an issue first to discuss the
shape of the change before investing time in a big patch.

## Architecture pointers

If you're touching the runtime, start here:

| You want to change… | …read this first |
|---|---|
| The agent loop or harness | [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) |
| The scorer (Agent-as-Judge) | [`docs/AGENT_JUDGE.md`](./docs/AGENT_JUDGE.md) |
| The GUI tool surface (visible to all 4 harnesses) | `weavebench/assets/computer_tool_plugin/index.ts` |
| The wire-layer (LLM ↔ openclaw) protocol patches | `weavebench/assets/openclaw_patches/*.patched.js` |
| The task corpus | task `.md` files on the public HF dataset. Each contains its own `## Grader` Python block. |

## Locked-down areas

Please **don't** modify these without an issue first:

- `weavebench/desktop_env/providers/{docker,vmware,virtualbox,aws,…}/` — vendored from
  [OSWorld upstream](https://github.com/xlang-ai/OSWorld). Patches should be
  proposed upstream first.
- `weavebench/assets/openclaw_patches/*.original.js` — verbatim upstream
  copies kept for diffing.
- Pinned VM image sha256 in `weavebench/scripts/download_vm.py:_EXPECTED_SHA256` —
  changing it without bumping the corresponding HF revision will break
  every existing install.

## Coding conventions

- Python ≥ 3.10.12 (we test 3.10/3.11/3.12 in CI).
- Use `pathlib.Path` over `os.path.join` in new code.
- For new CLI scripts, follow the pattern of `weavebench/scripts/download_*.py`:
  `allow_abbrev=False`, honor `HF_TOKEN` + `HF_ENDPOINT`, friendly error on
  `GatedRepoError` / `RepositoryNotFoundError`.

## License + attribution

This project is MIT-licensed (see [LICENSE](./LICENSE)). By submitting a PR
you agree to license your contribution under the same terms. Third-party
attributions live in [NOTICE](./NOTICE) — please add an entry if your PR
vendors new upstream code.
