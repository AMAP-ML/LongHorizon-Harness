# Changelog

All notable changes to WeaveBench will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-06

First public release accompanying the paper.

### Added
- **Benchmark corpus** — 114 long-horizon hybrid-interface tasks across 8
  real-world work domains (DAV / DES / DOC / DSK / GAM / OPS / SPA / WEB),
  hosted on HuggingFace at `wanlilll/WeaveBench` (public).
- **Four harness adapters** — OpenClaw, Codex CLI, Claude Code, and Hermes,
  all sharing one GUI computer-tool plugin so cross-harness comparisons
  measure real capability differences instead of harness friction.
- **Trajectory-aware Agent-as-Judge** — a separate host-side OpenClaw that
  reads each rollout's `chat.jsonl` + `results.tar.gz` and produces
  per-clause evidence-based scoring in `score.json`. Catches shortcut
  behaviors (fabricated screenshots, hard-coded metrics) the file-only
  grader can't.
- **One-command setup** — `bash scripts/setup.sh` runs the full prereq
  check, `pip install -e .`, `npm install -g openclaw`, and pulls the
  dataset (~207 MB) + runtime tarballs (~852 MB total, 514 MB for openclaw
  alone) + qcow2 VM image (28.46 GB) + judge template (~7 KB) from HF.
  Honors `HF_ENDPOINT=https://hf-mirror.com` for users in China.
- **Five download CLIs** — `weavebench-download-{dataset,assets,vm,judge}` +
  `weavebench-quickstart`, all with `--token` / `--revision` / `HF_TOKEN` /
  `HF_ENDPOINT` support.
- **CI** — matrix tests on Python 3.10 / 3.11 / 3.12 via GitHub Actions.
- **Tests** — 55 fast (<1s) tests covering CLI dispatch, task discovery,
  judge bench_root resolution, download-judge placeholder substitution
  (including adversarial cases), and `--mode cli` argparse non-collision.

### Known limitations
- Linux + KVM + Docker only. Hermes harness is CLI-only on Linux
  (no `pyautogui` desktop driver for it).
- Reproducibility — see [`docs/REPRODUCE.md`](./docs/REPRODUCE.md) for the
  exact dataset SHA + model snapshot ids used in the paper.
