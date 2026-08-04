# Troubleshooting Reference

Use this reference when reproduction fails or produces suspicious results.

## Environment Check Fails

Run:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh doctor
```

Common failures:

```text
docker daemon not reachable     Add the user to the docker group or start Docker.
/dev/kvm missing                Use a KVM-capable Linux host.
openclaw not found              Run npm install -g openclaw or set AJ_OPENCLAW_BIN.
weavebench import fails         Run python3 -m pip install -e ./WeaveBench.
tasks missing                   Run the download step.
VM missing                      Run the download and vm120g steps.
image proxy vars missing        Configure proxy URLs or set WEAVEBENCH_IMAGE_PROXY=0.
```

## VM Disk Full or Read-Only

Symptoms:

```text
No space left on device
read-only file system
EROFS
results.tar.gz missing
archive command failed
```

Checks:

```bash
qemu-img info WeaveBench/cache/vm/Ubuntu_120G.qcow2
grep -R "No space left on device\\|read-only file system\\|EROFS\\|archive command failed" WeaveBench/results/<run_name> WeaveBench/logs/<run_name>.log
```

Fix:

```bash
./skills/weavebench-cua-reproduce/scripts/reproduce.sh vm120g
export OSWORLD_LOCAL_QCOW2_PATH="$PWD/WeaveBench/cache/vm/Ubuntu_120G.qcow2"
export WEAVEBENCH_GROW_ROOTFS=1
```

Then rerun the affected task or experiment.

## API or Gateway Errors

Common classes:

```text
429                         Rate limit. Usually retryable.
500/502/503                 Provider or gateway transient failure. Usually retryable.
400 input is too long        Context/request too large. Reduce history or image payloads.
400 output_config.format     Provider incompatibility with unsupported fields.
404 model                    Model alias or gateway routing problem.
```

Actions:

- Confirm `WEAVEBENCH_LITELLM_BASE_URL` and model name.
- Run a minimal API smoke request outside the benchmark if the provider is new.
- Enable image proxy if large screenshots cause request-size errors.
- Do not treat "input is too long" as an ordinary transient retry; reduce context or image payload.

## Image Proxy Problems

Symptoms:

```text
request body too large
413
400 with large image payload
provider cannot fetch image URL
```

Checklist:

- Upload URL works from the host.
- Public show URL works from the provider side.
- `WEAVEBENCH_IMAGE_PROXY_UPLOAD_MODE` matches the backend (`raw` or `multipart`).
- If no public image backend is available, set `WEAVEBENCH_IMAGE_PROXY=0` only when the provider accepts large base64 images.

## Warmup Timeout

Warmup timeout means the task initialization script did not finish in time. It is not the same as agent failure.

Useful checks:

```bash
grep -R "Warmup did not complete\\|warmup" WeaveBench/results/<run_name> WeaveBench/logs/<run_name>.log
```

Likely causes include slow package downloads, apt/dpkg locks, blocked external mirrors, or a task service that failed to start. Prefer rerunning the task after confirming network and mirrors rather than editing task files.

## Judge Problems

Symptoms:

```text
judge_failed
openclaw exited rc=1
no score.json written
score.json missing
```

Checks:

```bash
find WeaveBench/results/<run_name> -name score.json | wc -l
grep -R "judge_failed\\|openclaw exited\\|no score.json" WeaveBench/results/<run_name> WeaveBench/logs/<run_name>.log
```

The judge stages each case under:

```text
WeaveBench/judge_agent_test/<run_name>/_eval/<case_id>/
```

Use separate `AJ_JUDGE_WORKSPACE` values for concurrent experiments to avoid collisions.

## Partial or Suspicious Task Results

A clean task result should normally include:

```text
score.json
results.tar.gz
chat.jsonl
agent.log
```

If `score.json` exists but `results.tar.gz` is missing, inspect logs before trusting the score.

## Safe Operating Rules

- Do not delete results or judge workspaces unless the user explicitly asks.
- Do not overwrite official `Ubuntu.qcow2`; create `Ubuntu_120G.qcow2`.
- Use smoke test before a full run on new infrastructure.
- If a full run is already active, do not start another high-concurrency run without checking Docker/qemu load.
