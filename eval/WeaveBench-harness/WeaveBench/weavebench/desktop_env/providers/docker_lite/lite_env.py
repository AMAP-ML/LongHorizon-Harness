"""DockerLiteEnv — lightweight, no-desktop drop-in for ``DesktopEnv``.

Goal: run the SimpAgent CLI agent stack against a plain Docker container
(default ``weavebench-ubuntu:v1.2``) for ablation studies, without
paying the cost of booting an OSWorld KVM VM with a full desktop.

This class quacks like the bits of ``DesktopEnv`` that the agent and
``run_one`` actually touch:

  * ``vm_ip`` / ``server_port``  — host-mapped REST endpoint
  * ``controller``               — no-op stub (only GUI mode would use it)
  * ``close()``                   — docker rm -f
  * ``reset()``                   — no-op (run_one never calls it)
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

LITE_IMAGE = os.environ.get("WEAVEBENCH_LITE_IMAGE", "weavebench-ubuntu:v1.2")
LITE_USER = os.environ.get("WEAVEBENCH_LITE_USER", "user")
LITE_PASSWORD = os.environ.get("WEAVEBENCH_LITE_PASSWORD", "password")
LITE_PROBE_TIMEOUT = int(os.environ.get("WEAVEBENCH_LITE_PROBE_TIMEOUT", "60"))
LITE_DOCKER_NETWORK = os.environ.get("WEAVEBENCH_LITE_DOCKER_NETWORK", "")

# CPU cap to prevent a single CPU-heavy CLI task (colmap mesh
# reconstruction, blender bake, ffmpeg encode, large numpy ops) from
# pinning the entire host. We observed one container pulling 12000%
# CPU on a 128-core host, starving every concurrent KVM VM into 100+
# minute timeouts. Default is 4 cores to match KVM's `-smp 4`.
#
# RAM is intentionally NOT capped: the host has plenty of headroom
# (~250 GB) and Docker's default (unlimited) is fine — over-allocating
# is bounded naturally by the number of concurrent envs, and capping
# would just risk OOM-killing legitimate workloads.
#
# We honor either WEAVEBENCH_LITE_CPU_CORES (lite-specific override) or
# OSWORLD_CPU_CORES (same knob the KVM provider uses) so a single env
# tweak affects both modes consistently.
LITE_CPU_CORES = os.environ.get(
    "WEAVEBENCH_LITE_CPU_CORES", os.environ.get("OSWORLD_CPU_CORES", "4")
)
# Pin OpenMP / MKL / OpenBLAS / NumExpr / VecLib thread pools to the
# cpu cap. Without this, scientific code (colmap eigen, numpy MKL,
# pytorch threadpool) would still spawn `os.cpu_count()` (= 128)
# worker threads inside the 4-core cgroup cap — the kernel ends up
# doing nothing but context-switching, throughput collapses, and all
# neighboring envs starve. Setting these env vars caps the thread
# pools at the source.
LITE_THREAD_CAP = LITE_CPU_CORES

HERE = Path(__file__).resolve().parent
LITE_SERVER_PY = HERE / "lite_server.py"


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _run(cmd, check=False, capture=True, timeout=120):
    return subprocess.run(
        cmd, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True, timeout=timeout,
    )


class _NullController:
    """Stub for ``env.controller``. Only the GUI code path uses it; in
    ``--mode cli`` runs nothing should call these."""
    def get_screenshot(self):
        raise RuntimeError(
            "DockerLiteEnv has no display: cannot take screenshot. "
            "Use --sandbox vm for GUI tasks.")

    def get_terminal_output(self):
        return ""

    def get_accessibility_tree(self):
        return None

    def get_vm_platform(self):
        return "Linux"


class DockerLiteEnv:
    """One short-lived ``weavebench-ubuntu`` container per task, with a
    stdlib HTTP server inside that mimics the OSWorld VM REST API."""

    def __init__(self,
                 image: str | None = None,
                 client_password: str | None = None,
                 task_id: str | None = None,
                 extra_env: dict | None = None,
                 docker_network: str | None = None):
        self.image = image or LITE_IMAGE
        self.client_password = client_password or LITE_PASSWORD
        self.user = LITE_USER
        suffix = uuid.uuid4().hex[:6]
        safe_tid = (task_id or "task").replace("/", "_").replace(" ", "_")
        self.container_name = f"weavebench_lite_{safe_tid}_{suffix}"[:60]
        self.host_port = _free_port()
        self.vm_ip = "127.0.0.1"
        self.server_port = self.host_port
        self.controller = _NullController()
        self.chromium_port = 0
        self.vnc_port = 0
        self.vlc_port = 0
        self.provider_name = "docker_lite"
        self.path_to_vm = None
        self._extra_env = dict(extra_env or {})
        self._docker_network = docker_network or LITE_DOCKER_NETWORK
        self._closed = False
        self._start()

    def _docker_run(self) -> None:
        if not LITE_SERVER_PY.is_file():
            raise RuntimeError(f"lite_server.py missing at {LITE_SERVER_PY}")
        env_args = []
        # The base image bakes a stale http(s)_proxy pointing at a host that
        # is unreachable from this network. Override with empty values so apt
        # and curl inside the container go direct via the host's LAN route.
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env_args += ["-e", f"{k}="]
        # Cap thread pools to the cpu cap so OpenMP/MKL/OpenBLAS-aware
        # libraries don't spawn 128 threads inside the 4-core cgroup.
        for tk in ("OMP_NUM_THREADS", "MKL_NUM_THREADS",
                   "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                   "VECLIB_MAXIMUM_THREADS"):
            env_args += ["-e", f"{tk}={LITE_THREAD_CAP}"]
        for k, v in self._extra_env.items():
            env_args += ["-e", f"{k}={v}"]
        net_args = []
        if self._docker_network:
            net_args += ["--network", self._docker_network]
        # CPU cap mirrors KVM (CPU_CORES=4); RAM is left at Docker default
        # (the host has plenty of headroom and capping risks OOM-killing
        # legitimate workloads).
        cap_args = ["--cpus", str(LITE_CPU_CORES)]
        cmd = [
            "docker", "run", "-d", "--rm",
            *cap_args,
            "--name", self.container_name,
            "-p", f"{self.host_port}:5000",
            "-v", f"{LITE_SERVER_PY}:/opt/lite_server.py:ro",
            *env_args, *net_args,
            self.image,
            "bash", "-c", "tail -f /dev/null",
        ]
        logger.info("[%s] docker run image=%s host_port=%d cpus=%s",
                    self.container_name, self.image, self.host_port,
                    LITE_CPU_CORES)
        r = _run(cmd, check=False, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(
                f"docker run failed (rc={r.returncode}): {r.stderr.strip()}"
            )

    def _exec(self, sh: str, timeout: int = 60):
        return _run(
            ["docker", "exec", self.container_name, "bash", "-lc", sh],
            check=False, capture=True, timeout=timeout,
        )

    def _ensure_user_and_deps(self) -> None:
        """Idempotently provision: sudo, user `user` (uid 1000) with the
        configured password + NOPASSWD sudoers entry, /home/user, python3.

        On a prepped image (e.g. ``weavebench-ubuntu:v1.2-lite`` built
        via ``Dockerfile.lite``) all of this is already baked in and we
        short-circuit after a quick health check, so we never need apt at
        runtime — important when ``archive.ubuntu.com`` is unreachable.
        """
        # Fast-path: prepped image already has everything we need.
        prepped = self._exec(
            "command -v sudo >/dev/null 2>&1 "
            f"&& id {self.user} >/dev/null 2>&1 "
            f"&& [ -f /etc/sudoers.d/99-{self.user} ] "
            "&& command -v python3 >/dev/null 2>&1 "
            "&& chmod 1777 /tmp "
            "&& echo PREPPED || echo NEEDS_INIT",
            timeout=15,
        )
        if "PREPPED" in (prepped.stdout or ""):
            return

        init = (
            "set -e; "
            "export DEBIAN_FRONTEND=noninteractive; "
            "chmod 1777 /tmp; "
            "if ! command -v sudo >/dev/null 2>&1 || [ ! -d /etc/sudoers.d ]; then "
            "  (apt-get update >/dev/null 2>&1 || true) && "
            "  apt-get install -y --no-install-recommends sudo >/dev/null; "
            "fi; "
            "if [ ! -d /etc/sudoers.d ]; then "
            "  install -d -m 0750 /etc/sudoers.d; "
            "fi; "
            f"if ! id {self.user} >/dev/null 2>&1; then "
            f"  (useradd -m -s /bin/bash -u 1000 {self.user} 2>/dev/null) "
            f"   || useradd -m -s /bin/bash {self.user}; "
            "fi; "
            f"echo '{self.user}:{self.client_password}' | chpasswd; "
            "if ! getent group sudo >/dev/null 2>&1; then groupadd sudo; fi; "
            f"usermod -aG sudo {self.user}; "
            f"printf '%s\\n' '{self.user} ALL=(ALL) NOPASSWD: ALL' "
            f"  > /etc/sudoers.d/99-{self.user}; "
            f"chmod 0440 /etc/sudoers.d/99-{self.user}; "
            f"install -d -o {self.user} -g {self.user} -m 0755 /home/{self.user}; "
            "if ! command -v python3 >/dev/null 2>&1; then "
            "  (apt-get update >/dev/null 2>&1 || true) && "
            "  apt-get install -y --no-install-recommends python3 >/dev/null; "
            "fi; "
            "echo INIT_OK"
        )
        r = self._exec(init, timeout=600)
        if "INIT_OK" not in (r.stdout or ""):
            raise RuntimeError(
                "Container init failed.\n"
                f"STDOUT: {r.stdout}\nSTDERR: {r.stderr}"
            )

    def _maybe_short_circuit_bootstrap(self) -> None:
        """If the image already has openclaw on PATH, mark
        ``/home/user/.openclaw_bootstrap.done`` so ``agent.bootstrap()``
        skips the 491MB tarball upload + reinstall.
        """
        sh = (
            "if command -v openclaw >/dev/null 2>&1; then "
            f"  install -d -o {self.user} -g {self.user} "
            f"    /home/{self.user}/.openclaw "
            f"    /home/{self.user}/.openclaw/agents/main/agent "
            f"    /home/{self.user}/.openclaw/agents/main/sessions; "
            f"  touch /home/{self.user}/.openclaw_bootstrap.done; "
            f"  chown {self.user}:{self.user} "
            f"    /home/{self.user}/.openclaw_bootstrap.done; "
            "  echo SHORTCUT_OK; "
            "else echo SHORTCUT_NO; fi"
        )
        r = self._exec(sh, timeout=30)
        if "SHORTCUT_OK" in (r.stdout or ""):
            logger.info(
                "[%s] openclaw pre-installed in image; bootstrap will short-circuit.",
                self.container_name)
        else:
            logger.info(
                "[%s] openclaw not pre-installed; agent will run full bootstrap.",
                self.container_name)

    def _start_lite_server(self) -> None:
        sh = (
            "rm -f /tmp/lite_server.log; "
            f"sudo -u {self.user} -H setsid "
            f"  python3 /opt/lite_server.py "
            "  >/tmp/lite_server.log 2>&1 < /dev/null &"
            " disown || true; sleep 0.2; echo SPAWNED"
        )
        self._exec(sh, timeout=30)

    def _wait_ready(self) -> None:
        import urllib.error
        import urllib.request
        # Build an opener that explicitly bypasses any host-level
        # http(s)_proxy env vars — we always want to talk to 127.0.0.1
        # directly, never through a proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f"http://{self.vm_ip}:{self.server_port}/healthz"
        deadline = time.time() + LITE_PROBE_TIMEOUT
        last_err = None
        while time.time() < deadline:
            try:
                with opener.open(url, timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                last_err = e
            time.sleep(0.5)
        log = self._exec("tail -200 /tmp/lite_server.log 2>/dev/null", timeout=10)
        raise RuntimeError(
            f"DockerLiteEnv: lite_server not ready on {url} within "
            f"{LITE_PROBE_TIMEOUT}s ({last_err}).\n"
            f"--- /tmp/lite_server.log ---\n{log.stdout}"
        )

    def _start(self) -> None:
        # Make sure subsequent `requests.post(...)` calls from the agent /
        # run_one helpers (which target 127.0.0.1:<host_port>) skip any
        # host-level HTTP proxy. Idempotent and process-local.
        cur = os.environ.get("NO_PROXY", "")
        if "127.0.0.1" not in cur:
            os.environ["NO_PROXY"] = (cur + "," if cur else "") + "127.0.0.1,localhost"
        cur_lc = os.environ.get("no_proxy", "")
        if "127.0.0.1" not in cur_lc:
            os.environ["no_proxy"] = (cur_lc + "," if cur_lc else "") + "127.0.0.1,localhost"

        self._docker_run()
        try:
            self._ensure_user_and_deps()
            self._maybe_short_circuit_bootstrap()
            self._start_lite_server()
            self._wait_ready()
            logger.info(
                "[%s] DockerLiteEnv ready at %s:%d (image=%s)",
                self.container_name, self.vm_ip, self.server_port, self.image)
        except Exception:
            self.close()
            raise

    def reset(self, *args, **kwargs):
        return None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            _run(["docker", "rm", "-f", self.container_name],
                 check=False, capture=True, timeout=60)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
