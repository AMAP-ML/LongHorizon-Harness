from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DOCKER_IMAGE      = os.environ.get("DOCKER_IMAGE",      "weavebench-ubuntu:v0.4")
DOCKER_IMAGE_GUI  = os.environ.get("DOCKER_IMAGE_GUI",  "weavebench-gui:v1.8")
TMP_WORKSPACE     = os.environ.get("TMP_WORKSPACE",     "/tmp_workspace")

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
JUDGE_MODEL   = os.environ.get("JUDGE_MODEL", "")
ENABLE_VNC    = os.environ.get("ENABLE_VNC", "0")  # "1" to expose x11vnc inside container

def remove_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)

def start_container(task_id: str, workspace_path: str, extra_env: str = "",
                    tmp_path: str = "", lobster_env: list[str] | None = None,
                    gui: bool = False) -> None:
    proxy_http = os.environ.get('HTTP_PROXY_INNER', '')
    proxy_https = os.environ.get('HTTPS_PROXY_INNER', '')
    env_args = [
        "-e", f"http_proxy={proxy_http}",
        "-e", f"https_proxy={proxy_https}",
        "-e", f"HTTP_PROXY={proxy_http}",
        "-e", f"HTTPS_PROXY={proxy_https}",
        "-e", f"BRAVE_API_KEY={BRAVE_API_KEY}",
        "-e", f"JUDGE_MODEL={JUDGE_MODEL}",
        "-e", f"OPENROUTER_API_KEY={os.environ.get('OPENROUTER_API_KEY','')}",
        "-e", f"OPENROUTER_BASE_URL={os.environ.get('OPENROUTER_BASE_URL','')}",
        "-e", f"no_proxy={'' if not proxy_http else os.environ.get('NO_PROXY_INNER', '')}",
    ]
    for line in extra_env.splitlines():
        key = line.strip()
        if not key or key.startswith("#"):
            continue
        value = os.environ.get(key, "")
        env_args += ["-e", f"{key}={value}"]
        masked = (value[:4] + "***") if value else "(empty)"
        logger.info("[%s] Injecting env var: %s=%s", task_id, key, masked)

    for key in (lobster_env or []):
        value = os.environ.get(key, "")
        if not value:
            logger.warning("[%s] Lobster env key %s not found in environment, skipping", task_id, key)
            continue
        env_args += ["-e", f"{key}={value}"]
        masked = value[:4] + "***"
        logger.info("[%s] Injecting lobster env: %s=%s", task_id, key, masked)
 
    image = DOCKER_IMAGE_GUI if gui else DOCKER_IMAGE
    gui_env = []
    if gui:
        gui_env = [
            "-e", "DISPLAY=:99",
            "-e", f"ENABLE_VNC={ENABLE_VNC}",
            # Larger /dev/shm avoids fluxbox/LibreOffice render glitches
            "--shm-size=1g",
        ]

    cmd = [
        "docker", "run", "-d",
        "--name", task_id,
        *env_args,
        *gui_env,
        "-v", f"{workspace_path}:/app:ro",
        image,
        "/bin/bash", "-c", "tail -f /dev/null",
    ]
    logger.info("[%s] Starting container (image=%s, gui=%s), mounting %s → /app (ro)",
                task_id, image, gui, workspace_path)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Container startup failed:\n{r.stderr}")
    logger.info("[%s] Container ID: %s", task_id, r.stdout.strip()[:12])

    if tmp_path and os.path.exists(tmp_path):
        mkdir_cmd = ["docker", "exec", task_id, "mkdir", "-p", "/tmp_workspace/tmp"]
        subprocess.run(mkdir_cmd, capture_output=True)

        cp_cmd = ["docker", "cp", f"{tmp_path}/.", f"{task_id}:/tmp_workspace/tmp/"]
        
        logger.info("[%s] Copying temp files: %s → /tmp_workspace/tmp", task_id, tmp_path)
        cp_r = subprocess.run(cp_cmd, capture_output=True, text=True)
        
        if cp_r.returncode != 0:
            logger.error("[%s] File copy failed: %s", task_id, cp_r.stderr)
        else:
            logger.info("[%s] Temp file copy complete", task_id)

def start_gui_environment(task_id: str, timeout_seconds: int = 20) -> None:
    """Start Xvfb + fluxbox inside the container and wait until xdpyinfo succeeds.

    Requires the container to be built from DOCKER_IMAGE_GUI (see `start_container(gui=True)`).
    Idempotent: safe to call multiple times.
    """
    logger.info("[%s] Starting GUI environment (Xvfb + fluxbox) ...", task_id)
    r = subprocess.run(
        ["docker", "exec", task_id, "/bin/bash", "-c",
         "/usr/local/bin/start-gui.sh"],
        capture_output=True, text=True, timeout=timeout_seconds + 10,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"GUI startup failed (rc={r.returncode}):\n"
            f"stdout={r.stdout}\nstderr={r.stderr}"
        )
    logger.info("[%s] GUI ready: %s", task_id, r.stdout.strip().splitlines()[-1] if r.stdout else "")

    # Health check — probe xdpyinfo from inside the container
    hc = subprocess.run(
        ["docker", "exec", task_id, "/bin/bash", "-c",
         "DISPLAY=:99 xdpyinfo | head -5"],
        capture_output=True, text=True,
    )
    if hc.returncode != 0:
        raise RuntimeError(f"GUI health check failed:\n{hc.stderr}")
    logger.info("[%s] GUI health check OK", task_id)


def take_init_screenshot(task_id: str, name: str = "_init") -> str | None:
    """Take a desktop screenshot inside the container after warmup.

    Saves to /tmp_workspace/screenshots/<name>.png. Tolerates failure (returns None).
    Returns the in-container screenshot path on success.
    """
    cmd = (
        "mkdir -p /tmp_workspace/screenshots && "
        "DISPLAY=:99 python3 /root/skills/desktop-control/tools/desktop_screenshot.py "
        f"--name {name}"
    )
    r = subprocess.run(
        ["docker", "exec", task_id, "/bin/bash", "-c", cmd],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        logger.warning("[%s] init screenshot failed (rc=%s): %s",
                       task_id, r.returncode, r.stderr.strip())
        return None
    path = f"/tmp_workspace/screenshots/{name}.png"
    logger.info("[%s] init screenshot saved → %s", task_id, path)
    return path


def setup_workspace(task_id: str, thinking: str | None = None) -> None:
    logger.info("[%s] Copying /app → %s", task_id, TMP_WORKSPACE)
    r = subprocess.run(
        ["docker", "exec", task_id, "/bin/bash", "-c",
         f"cp -r /app/. {TMP_WORKSPACE} && chmod -R u+w {TMP_WORKSPACE}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Workspace copy failed:\n{r.stderr}")

    if thinking is not None:
        logger.info("[%s] Setting thinkingDefault to %s", task_id, thinking)
        thinking_result = subprocess.run(
            ["docker", "exec", task_id,
             "openclaw", "config", "set", "agents.defaults.thinkingDefault", thinking],
            capture_output=True, text=True,
        )
        if thinking_result.returncode != 0:
            raise RuntimeError(
                f"Failed to set thinkingDefault to {thinking}:\n{thinking_result.stderr}"
            )

    # Symlink OpenClaw workspace → TMP_WORKSPACE so the image tool's
    # media-local-roots check allows reading files under /tmp_workspace.
    subprocess.run(
        ["docker", "exec", task_id, "/bin/bash", "-c",
         f"rm -rf /root/.openclaw/workspace && ln -s {TMP_WORKSPACE} /root/.openclaw/workspace"],
        capture_output=True, text=True,
    )

    # Disable web search if BRAVE_API_KEY is not set to prevent gateway startup failure.
    if not BRAVE_API_KEY:
        subprocess.run(
            ["docker", "exec", task_id,
             "openclaw", "config", "set", "tools.web.search.enabled", "false"],
            capture_output=True, text=True,
        )
        logger.info("[%s] Web search disabled (no BRAVE_API_KEY)", task_id)

def setup_skills(task_id: str, skills: str, skills_path: str) -> None:
    for line in skills.splitlines():
        line = line.strip()
        if not line:
            continue
        subprocess.run(
            ["docker", "exec", task_id,
             "mkdir", "-p", f"/root/skills/{line}"],
            capture_output=True, text=True,
        )
        r = subprocess.run(
            ["docker", "cp",
             f"{skills_path}/{line}", f"{task_id}:/root/skills"],
            capture_output=True, text=True,
        )


def inject_openclaw_models(task_id: str, models_config: dict) -> None:
    """Inject custom models into ~/.openclaw/openclaw.json."""
    container_tmp_path = "/tmp/openclaw_models.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tmp_file:
        json.dump(models_config, tmp_file, indent=2)
        tmp_file_path = tmp_file.name

    try:
        cp_r = subprocess.run(
            ["docker", "cp", tmp_file_path, f"{task_id}:{container_tmp_path}"],
            capture_output=True, text=True,
        )
        if cp_r.returncode != 0:
            raise RuntimeError(f"Failed to copy models config into container:\n{cp_r.stderr}")

        inject_cmd = f"""python3 - <<'PY'
import json
import pathlib

config_path = pathlib.Path('/root/.openclaw/openclaw.json')
models_path = pathlib.Path('{container_tmp_path}')

config = json.loads(config_path.read_text()) if config_path.exists() else {{}}
models = json.loads(models_path.read_text())
config['models'] = models

config_path.write_text(json.dumps(config, indent=2))
PY"""
        r = subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", inject_cmd],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Failed to inject models config:\n{r.stderr}")
    finally:
        Path(tmp_file_path).unlink(missing_ok=True)

    logger.info("[%s] Injected custom models config", task_id)


def run_warmup(task_id: str, warmup: str) -> None:
    """Execute warmup bash commands line by line inside the container (skip blank lines and comments)."""
    if not warmup.strip():
        return
    commands = [
        line.strip()
        for line in warmup.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not commands:
        return

    logger.info("[%s] Running warmup (%d commands)", task_id, len(commands))
    for cmd in commands:
        logger.info("[%s] warmup: %s", task_id, cmd)
        r = subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", cmd],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Warmup command failed: {cmd!r}\n{r.stderr}")


def run_background(task_id: str, bash_cmd: str, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        ["docker", "exec", task_id, "/bin/bash", "-c",
         f"cd {TMP_WORKSPACE} && {bash_cmd}"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
    )
    proc._log_file = log_file
    logger.info("[%s] Started process PID=%s → %s", task_id, proc.pid, log_path)
    return proc


def close_proc_log(proc: subprocess.Popen) -> None:
    """Close the log file handle created by run_background."""
    log_file = getattr(proc, "_log_file", None)
    if log_file and not log_file.closed:
        log_file.close()


def collect_output_from_container(task_id: str, output_dir: Path) -> None:
    """Collect task output files from the container to output_dir/task_output/.

    Collection strategy:
      1. All files under /tmp/openclaw/ (agent session logs, etc.)
      2. Task output files under /tmp_workspace/results/
      3. Desktop screenshots under /tmp_workspace/screenshots/
      4. OpenClaw agent session trajectories under /root/.openclaw/agents/main/sessions/
    """
    task_output_dir = output_dir / "task_output"
    task_output_dir.mkdir(parents=True, exist_ok=True)

    _copy_dir_from_container(task_id, "/tmp/openclaw/.", str(task_output_dir))

    results_out = task_output_dir / "workspace" / "results"
    results_out.mkdir(parents=True, exist_ok=True)
    ok = _copy_dir_from_container(
        task_id, f"{TMP_WORKSPACE}/results/.", str(results_out),
    )
    if not ok:
        logger.warning("[%s] results/ directory does not exist or is empty", task_id)

    screenshots_out = task_output_dir / "workspace" / "screenshots"
    screenshots_out.mkdir(parents=True, exist_ok=True)
    _copy_dir_from_container(
        task_id, f"{TMP_WORKSPACE}/screenshots/.", str(screenshots_out),
    )

    sessions_out = task_output_dir / "openclaw_sessions"
    sessions_out.mkdir(parents=True, exist_ok=True)
    _copy_dir_from_container(
        task_id, "/root/.openclaw/agents/main/sessions/.", str(sessions_out),
    )


def inject_lobster_workspace(task_id: str, workspace_path: str) -> None:
    """Copy the entire lobster workspace into /root/ (the OpenClaw workspace in the image).

    This brings in everything: SOUL.md, USER.md, MEMORY.md, memory/, skills/, etc.
    """
    r = subprocess.run(
        ["docker", "cp", f"{workspace_path}/.", f"{task_id}:/root/"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        logger.error("[%s] Lobster workspace copy failed: %s", task_id, r.stderr)
    else:
        logger.info("[%s] Lobster workspace copied: %s → /root/", task_id, workspace_path)


def _copy_dir_from_container(task_id: str, src: str, dest: str) -> bool:
    r = subprocess.run(
        ["docker", "cp", f"{task_id}:{src}", dest],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        logger.info("[%s] Collected container directory %s → %s", task_id, src, dest)
        return True
    return False

