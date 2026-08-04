"""Provider factory for WeaveBench.

WeaveBench only bundles the `docker` (KVM/qcow2 desktop) and `docker_lite`
(headless container, CLI ablation) providers — the OSWorld cloud providers
(`aws`, `azure`, `aliyun`, `gcp`, `virtualbox`, `vmware`, `volcengine`) have
been stripped to keep the dependency surface small. If you need one of those,
install upstream OSWorld separately:

    pip install git+https://github.com/xlang-ai/OSWorld.git

and import from `desktop_env.providers.<name>` rather than
`weavebench.desktop_env.providers.<name>`.
"""
from weavebench.desktop_env.providers.base import VMManager, Provider


_BUNDLED = {"docker", "docker_lite"}
_STRIPPED = {"aws", "amazon web services", "azure", "aliyun", "gcp",
             "virtualbox", "vmware", "volcengine"}


def create_vm_manager_and_provider(provider_name: str, region: str,
                                   use_proxy: bool = False):
    """Return ``(VMManager, Provider)`` for the requested backend.

    Only ``docker`` (KVM/qcow2 desktop) and ``docker_lite`` (headless
    container) are supported in WeaveBench. Use upstream OSWorld for the
    other providers.
    """
    provider_name = provider_name.lower().strip()
    if provider_name == "docker":
        from weavebench.desktop_env.providers.docker.manager import DockerVMManager
        from weavebench.desktop_env.providers.docker.provider import DockerProvider
        return DockerVMManager(), DockerProvider(region)
    if provider_name == "docker_lite":
        from weavebench.desktop_env.providers.docker_lite.lite_env import DockerLiteEnv
        # docker_lite is a fused VMManager + Provider — return it twice so
        # downstream code that unpacks the tuple still works.
        env = DockerLiteEnv  # class, not instance — caller decides
        return env, env
    if provider_name in _STRIPPED:
        raise NotImplementedError(
            f"Provider {provider_name!r} is not bundled in WeaveBench. "
            "Install upstream OSWorld (pip install "
            "git+https://github.com/xlang-ai/OSWorld.git) and use its "
            "`desktop_env.providers` namespace instead."
        )
    raise NotImplementedError(f"Unknown provider: {provider_name!r}")
