"""The v5 pipeline driver and PLAN.md §11 control surface.

v0-v4 ship composable library modules but no driver: nothing chains intake
-> survey -> plan -> pilot -> contract -> research -> execute -> assemble
into one run, and PLAN.md §11's control surface (``run`` / ``status`` /
``approve`` / ``amend`` / ``resume``) does not exist. This package is that
wiring. It is deliberately thin: v0-v4 are imported unmodified. The only
new on-disk state is a small set of v5 paths (``source.txt``,
``phase.json``, ``approvals.jsonl``, ``halt.flag``, ``run.spec.json``)
layered on top of the existing run-directory layout.
"""

from .run_dir import (  # noqa: F401
    approvals_path,
    halt_path,
    phase_path,
    run_spec_path,
    source_path,
)
from .approvals import Approval, read_all, wait_for_resolution  # noqa: F401
from .driver import PHASES, RecursiveDriver  # noqa: F401