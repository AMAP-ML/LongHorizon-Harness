"""Role-managed harness for long-horizon computer-use tasks."""

from importlib.metadata import PackageNotFoundError, version as _package_version

from .types import (
    EpisodeBudget,
    EpisodeResult,
    ExecResult,
    HarnessConfig,
    ManagedRound,
    AuditReport,
)

HOMEPAGE = "https://github.com/AMAP-ML/LongHorizon-Harness"
ISSUES_URL = f"{HOMEPAGE}/issues"

try:
    __version__ = _package_version("lh-harness")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "EpisodeBudget",
    "EpisodeResult",
    "ExecResult",
    "HarnessConfig",
    "ManagedRound",
    "AuditReport",
    "HOMEPAGE",
    "ISSUES_URL",
    "__version__",
]
