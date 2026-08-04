# Asset Reference

Use this reference before downloading or validating local WeaveBench assets.

## Official Source

The base tasks, task workspaces, Claude Code runtime asset, judge templates, and original Ubuntu qcow2 are expected to come from the official HuggingFace dataset:

```text
wanlilll/WeaveBench
```

Use the repository download commands rather than ad hoc manual downloads:

```bash
cd WeaveBench
weavebench-download-dataset --dest ./cache --include "tasks/*"
weavebench-download-assets --dest ./cache --harness claudecode
weavebench-download-judge --judge-home ./judge_agent_test
weavebench-download-vm --dest ./cache
```

If direct HuggingFace access is unstable:

```bash
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
```

## Required Local Paths

After download and VM preparation, these paths should exist:

```text
WeaveBench/cache/tasks/
WeaveBench/cache/runtime_assets/claudecode.tar.gz
WeaveBench/cache/vm/Ubuntu.qcow2
WeaveBench/cache/vm/Ubuntu_120G.qcow2
WeaveBench/judge_agent_test/template_profile
WeaveBench/judge_agent_test/template_workspace
```

The original VM should not be resized in place. Create the 120G copy:

```bash
cd WeaveBench
cp ./cache/vm/Ubuntu.qcow2 ./cache/vm/Ubuntu_120G.qcow2
qemu-img resize ./cache/vm/Ubuntu_120G.qcow2 120G
```

## Integrity Checks

Basic checks:

```bash
find WeaveBench/cache/tasks -type f -name '*.md' | wc -l
sha256sum WeaveBench/cache/runtime_assets/claudecode.tar.gz
qemu-img info WeaveBench/cache/vm/Ubuntu.qcow2
qemu-img info WeaveBench/cache/vm/Ubuntu_120G.qcow2
```

Reference Claude Code tarball hash from our run:

```text
425e58356a4e07c1f7b3dd9d04a331ada7bf14c164fde282a591b5a92a404296
```

If a downloaded asset differs, do not silently claim exact reproduction. Report that assets are configured but not version-matched.

## Do Not Commit

These local assets are intentionally ignored by git:

```text
WeaveBench/cache/
WeaveBench/results/
WeaveBench/logs/
WeaveBench/judge_agent_test/
*.qcow2
*.tar.gz
```
