"""PLAN.md §C5 — the fixed-task eval harness.

A cost claim plus a correctness claim, both measured (PLAN.md §C5's own
framing): five fixed tasks x three runs, driving real ``RecursiveDriver``
runs against scripted providers and in-memory writer adapters, and
aggregating calls-by-tier, escalation precision, approval rate by shape,
and per-leaf prompt-segment token means.

- ``eval.tasks``  — the five fixed tasks and their canned responses.
- ``eval.measure`` — pure, disk-based measurement functions.
- ``eval.runner`` — ``run_eval_suite`` / ``run_eval_suite_sync``.
"""

from . import measure  # noqa: F401
from .measure import (  # noqa: F401
    approval_rate_by_shape,
    calls_by_role,
    escalation_events,
    escalation_precision,
    mean_tokens_by_segment,
    per_leaf_segment_tokens,
    role_of_schema,
    summarize_calls_by_tier,
    terminal_events_per_node,
)
from .runner import EvalReport, RunMeasurement, run_eval_suite, run_eval_suite_sync  # noqa: F401
from .tasks import EvalTask, build_tasks  # noqa: F401

__all__ = [
    "EvalReport",
    "EvalTask",
    "RunMeasurement",
    "approval_rate_by_shape",
    "build_tasks",
    "calls_by_role",
    "escalation_events",
    "escalation_precision",
    "mean_tokens_by_segment",
    "per_leaf_segment_tokens",
    "role_of_schema",
    "run_eval_suite",
    "run_eval_suite_sync",
    "summarize_calls_by_tier",
    "terminal_events_per_node",
]
