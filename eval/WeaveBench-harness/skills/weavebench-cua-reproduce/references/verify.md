# Verification Reference

Use this reference before reporting that setup or a run is complete.

## Setup Verification Levels

Use these labels consistently:

```text
configured and verified
configured but not smoke-tested
not configured
blocked awaiting user action
```

Recommended checks:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh status
./skills/weavebench-cua-reproduce/scripts/reproduce.sh doctor
```

`status` is non-strict and summarizes the current setup. `doctor` is strict and exits nonzero on missing required pieces.

## Smoke Test Success

A smoke test is considered successful when:

- The script exits with status 0.
- The log has `EXIT_STATUS=0`.
- At least one task directory exists under `WeaveBench/results/<smoke_name>/gui/qwen3.7-plus/`.
- The task has `score.json`.
- Infrastructure errors such as read-only filesystem, missing VM, missing image proxy URLs, or judge failure are absent from the log.

Useful commands:

```bash
tail -100 WeaveBench/logs/<smoke_name>.log
find WeaveBench/results/<smoke_name>/gui/qwen3.7-plus -name score.json -print
grep -R "No space left on device\\|read-only file system\\|judge_failed\\|Warmup did not complete" WeaveBench/logs/<smoke_name>.log WeaveBench/results/<smoke_name> || true
```

## Full Run Success

A full run is considered complete when:

- The log exits with `EXIT_STATUS=0`.
- Expected task count is present.
- Each task has `score.json`.
- Missing `results.tar.gz` is investigated before trusting scores.
- `run_provenance.json` exists in the run result directory.

Useful commands:

```bash
grep "EXIT_STATUS=" WeaveBench/logs/<run_name>.log
find WeaveBench/results/<run_name>/gui/qwen3.7-plus -name score.json | wc -l
find WeaveBench/results/<run_name>/gui/qwen3.7-plus -mindepth 2 -maxdepth 2 -type d | wc -l
find WeaveBench/results/<run_name>/gui/qwen3.7-plus -mindepth 2 -maxdepth 2 -type d '!' -exec test -f '{}/results.tar.gz' ';' -print
test -f WeaveBench/results/<run_name>/run_provenance.json
```

## Score Summary

Use:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh stats \
  WeaveBench/results/<run_name>/gui/qwen3.7-plus
```

Report task count, average score, PR >= 0.8, zero-score count, and domain breakdown when needed.

## Final Report

A concise final report should include:

```text
Environment: ...
Assets: ...
120G VM: ...
Model API: ...
Image proxy: ...
Judge: ...
Smoke test: ...
Full run: ...
Provenance: ...
Remaining blockers: ...
```
