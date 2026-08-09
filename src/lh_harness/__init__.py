"""Recursive-decomposition harness for long-horizon tasks (gptme backend)."""

from importlib.metadata import PackageNotFoundError, version as _package_version

from .types import EpisodeBudget, EpisodeResult, ExecResult

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
    "HOMEPAGE",
    "ISSUES_URL",
    "__version__",
]