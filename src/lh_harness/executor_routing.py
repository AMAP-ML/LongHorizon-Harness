from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import re
from typing import Literal

from .types import ExecutorRoutingConfig, RoleNextStep

RoutingSelectionReason = Literal[
    "default",
    "manager",
    "manager_escalation",
    "automatic_escalation",
    "invalid",
]
RoutingAuditSignal = Literal[
    "pending",
    "complete",
    "incomplete",
    "blocked",
    "not_counted",
]
_EXECUTOR_TIER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class ExecutorRoutingDecision:
    enabled: bool
    task_id: str
    task_present: bool
    requested_tier: str | None
    selected_tier: str | None
    selection_reason: RoutingSelectionReason
    failures_before: int
    failures_after: int
    escalation_active: bool
    audit_signal: RoutingAuditSignal = "pending"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ExecutorRouter:
    def __init__(self, tiers: set[str], policy: ExecutorRoutingConfig) -> None:
        invalid_tiers = [tier for tier in tiers if not is_valid_executor_tier_name(tier)]
        if invalid_tiers:
            raise ValueError(
                "Executor tier names must use 1-64 ASCII letters, digits, '_' or '-': "
                + ", ".join(sorted(repr(tier) for tier in invalid_tiers))
            )
        if policy.default_tier not in tiers or policy.escalation_tier not in tiers:
            raise ValueError("Executor routing policy must reference configured tiers")
        self.tiers = frozenset(tiers)
        self.policy = policy
        self._active_task_id: str | None = None
        self._failure_count = 0
        self._escalation_active = False

    def select(
        self,
        *,
        next_step: RoleNextStep,
        task_text: str,
        requested_tier: str | None,
        round_index: int,
    ) -> ExecutorRoutingDecision:
        task_id, task_present = executor_task_identity(
            next_step=next_step,
            task_text=task_text,
            round_index=round_index,
        )
        same_task = task_id == self._active_task_id
        failures = self._failure_count if same_task else 0
        escalated = self._escalation_active if same_task else False

        if requested_tier == "" or (
            requested_tier is not None and requested_tier not in self.tiers
        ):
            return ExecutorRoutingDecision(
                enabled=True,
                task_id=task_id,
                task_present=task_present,
                requested_tier=requested_tier,
                selected_tier=None,
                selection_reason="invalid",
                failures_before=failures,
                failures_after=failures,
                escalation_active=escalated,
                audit_signal="not_counted",
            )

        if escalated:
            selected_tier = self.policy.escalation_tier
            selection_reason: RoutingSelectionReason = (
                "manager_escalation"
                if requested_tier == self.policy.escalation_tier
                else "automatic_escalation"
            )
        elif requested_tier is None:
            selected_tier = self.policy.default_tier
            selection_reason = "default"
        elif requested_tier == self.policy.escalation_tier:
            selected_tier = requested_tier
            selection_reason = "manager_escalation"
            escalated = True
        else:
            selected_tier = requested_tier
            selection_reason = "manager"

        self._active_task_id = task_id
        self._failure_count = failures
        self._escalation_active = escalated
        return ExecutorRoutingDecision(
            enabled=True,
            task_id=task_id,
            task_present=task_present,
            requested_tier=requested_tier,
            selected_tier=selected_tier,
            selection_reason=selection_reason,
            failures_before=failures,
            failures_after=failures,
            escalation_active=escalated,
        )

    def observe(
        self,
        decision: ExecutorRoutingDecision,
        signal: Literal["complete", "incomplete", "blocked", "not_counted"],
    ) -> ExecutorRoutingDecision:
        if decision.selected_tier is None or decision.task_id != self._active_task_id:
            return replace(decision, audit_signal="not_counted")
        if signal == "complete":
            self._active_task_id = None
            self._failure_count = 0
            self._escalation_active = False
            return replace(
                decision,
                failures_after=0,
                escalation_active=False,
                audit_signal="complete",
            )
        if signal in {"incomplete", "blocked"}:
            self._failure_count += 1
            if self._failure_count >= self.policy.escalate_after_failures:
                self._escalation_active = True
            return replace(
                decision,
                failures_after=self._failure_count,
                escalation_active=self._escalation_active,
                audit_signal=signal,
            )
        return replace(
            decision,
            failures_after=self._failure_count,
            escalation_active=self._escalation_active,
            audit_signal="not_counted",
        )


def executor_task_identity(
    *, next_step: RoleNextStep, task_text: str, round_index: int
) -> tuple[str, bool]:
    normalized = re.sub(r"\s+", " ", str(task_text or "")).strip()
    task_present = bool(normalized)
    identity_text = (
        f"{next_step}\n{normalized}"
        if task_present
        else f"{next_step}\n<missing-task-round-{round_index}>"
    )
    return hashlib.sha256(identity_text.encode("utf-8")).hexdigest(), task_present


def is_valid_executor_tier_name(value: object) -> bool:
    return isinstance(value, str) and _EXECUTOR_TIER_NAME_RE.fullmatch(value) is not None


def disabled_routing_decision(
    *, next_step: RoleNextStep, task_text: str, round_index: int
) -> ExecutorRoutingDecision:
    task_id, task_present = executor_task_identity(
        next_step=next_step,
        task_text=task_text,
        round_index=round_index,
    )
    return ExecutorRoutingDecision(
        enabled=False,
        task_id=task_id,
        task_present=task_present,
        requested_tier=None,
        selected_tier=None,
        selection_reason="default",
        failures_before=0,
        failures_after=0,
        escalation_active=False,
    )


def mark_not_counted(decision: ExecutorRoutingDecision) -> ExecutorRoutingDecision:
    return replace(decision, audit_signal="not_counted")
