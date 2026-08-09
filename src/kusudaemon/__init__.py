"""Kusudaemon: a recursive-decomposition harness for long-horizon tasks
(gptme backend). Forked from and built on LongHorizon-Harness
(arXiv:2608.01964) and gptme (github.com/gptme/gptme) — see README.md's
Credits section.
"""

from importlib.metadata import PackageNotFoundError, version as _package_version

from .types import EpisodeBudget, EpisodeResult, ExecResult

# Points at this fork's actual GitHub location, which has not itself been
# renamed (only the package/CLI/docs have) — update if/when it is.
HOMEPAGE = "https://github.com/OrigamiKoala/LongHorizon-Harness"
ISSUES_URL = f"{HOMEPAGE}/issues"

try:
    __version__ = _package_version("kusudaemon")
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