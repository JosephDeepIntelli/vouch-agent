"""Business task execution contracts: TaskSpec and the TaskRun lifecycle.

Vouch is one shared runtime with two mode families (owner steering 2026-09-28):

* **business task execution** — TaskSpec → TaskRun: the runtime actually
  performs bounded work (plan, tool calls, model calls, checkpoints,
  finalize) under budget, timeouts and the Vouch Gate. Not merely scoring
  fixtures.
* **controlled improvement** — Candidate/EvaluationRun/AcceptanceDecision
  (separate modules) compare bounded changes on task packs.

TaskRun is also the recovery unit: state is persisted before side effects are
attempted and after their result is known, so a crash leaves either a known
outcome or ``needs_reconciliation`` — never a blind replay (design §10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import (
    ContractRecord,
    RunMode,
    new_id,
    require_str,
    utc_now_iso,
)
from vouch_agent.errors import ContractError, InvalidStateTransitionError


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_RECONCILIATION = "needs-reconciliation"


#: v1 transition table. Unknown side-effect state may only leave
#: NEEDS_RECONCILIATION through an explicit recorded reconciliation action.
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PAUSED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.NEEDS_RECONCILIATION,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.NEEDS_RECONCILIATION}
    ),
    TaskStatus.NEEDS_RECONCILIATION: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    # Terminal states have no outgoing edges.
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def transition_task_status(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    if target not in TASK_TRANSITIONS[current]:
        raise InvalidStateTransitionError(
            f"task run cannot transition {current.value} -> {target.value}"
        )
    return target


class StepKind(StrEnum):
    PLAN = "plan"
    MODEL_CALL = "model-call"
    TOOL_CALL = "tool-call"
    CHECKPOINT = "checkpoint"
    GATE_CHECK = "gate-check"
    FINALIZE = "finalize"


@dataclass(frozen=True)
class RunStep(ContractRecord):
    """One recorded step of a run — the journal's per-step evidence unit."""

    step_id: str
    kind: StepKind
    # ok | failed | skipped | unknown | running (in-flight intent persisted
    # BEFORE the side-effecting call — recovery treats a leftover "running"
    # step as unknown-outcome) | cancelled
    status: str
    started_at: str
    ended_at: str | None = None
    input_digest: str | None = None
    output_digest: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # Marks steps whose side-effect outcome is unknown (crash between persist
    # phases); such steps require reconciliation before any retry.
    side_effect_unknown: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "stepId": self.step_id,
            "kind": self.kind.value,
            "status": self.status,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "inputDigest": self.input_digest,
            "outputDigest": self.output_digest,
            "usage": self.usage,
            "error": self.error,
            "sideEffectUnknown": self.side_effect_unknown,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunStep:
        cls._check_version(data)
        return cls(
            step_id=require_str(data["stepId"], "stepId"),
            kind=StepKind(data["kind"]),
            status=require_str(data["status"], "status"),
            started_at=require_str(data["startedAt"], "startedAt"),
            ended_at=data.get("endedAt"),
            input_digest=data.get("inputDigest"),
            output_digest=data.get("outputDigest"),
            usage=dict(data.get("usage") or {}),
            error=data.get("error"),
            side_effect_unknown=bool(data.get("sideEffectUnknown", False)),
        )


@dataclass(frozen=True)
class TaskSpec(ContractRecord):
    """A bounded unit of business work submitted to the runtime.

    Distinct from improvement contracts: a TaskSpec says *what to do under
    which constraints*, not *what change to compare*. ``success_criteria``
    is structured so completion can be checked deterministically where
    possible (schema, validators), by the requester otherwise.
    """

    spec_id: str
    title: str
    goal: str
    mode: RunMode
    workflow_id: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    # Typed constraints; unknown/missing budget means "no override", the
    # project-level BudgetPolicy still applies.
    max_cost_usd: float | None = None
    max_wall_clock_s: float | None = None
    max_steps: int | None = None
    locale: str = "en"
    market: str | None = None
    success_criteria: dict[str, Any] = field(default_factory=dict)
    created_by: str = "local"
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "specId": self.spec_id,
            "title": self.title,
            "goal": self.goal,
            "mode": self.mode.value,
            "workflowId": self.workflow_id,
            "inputs": self.inputs,
            "maxCostUsd": self.max_cost_usd,
            "maxWallClockS": self.max_wall_clock_s,
            "maxSteps": self.max_steps,
            "locale": self.locale,
            "market": self.market,
            "successCriteria": self.success_criteria,
            "createdBy": self.created_by,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        cls._check_version(data)
        if not isinstance(data.get("inputs", {}), dict):
            raise ContractError("TaskSpec.inputs must be an object")
        return cls(
            spec_id=require_str(data["specId"], "specId"),
            title=require_str(data["title"], "title"),
            goal=require_str(data["goal"], "goal"),
            mode=RunMode(data["mode"]),
            workflow_id=data.get("workflowId"),
            inputs=dict(data.get("inputs") or {}),
            max_cost_usd=data.get("maxCostUsd"),
            max_wall_clock_s=data.get("maxWallClockS"),
            max_steps=data.get("maxSteps"),
            locale=require_str(data.get("locale", "en"), "locale"),
            market=data.get("market"),
            success_criteria=dict(data.get("successCriteria") or {}),
            created_by=require_str(data.get("createdBy", "local"), "createdBy"),
            created_at=require_str(data.get("createdAt", utc_now_iso()), "createdAt"),
        )


@dataclass(frozen=True)
class TaskRun(ContractRecord):
    """Execution record of a TaskSpec — persisted incrementally, recoverable."""

    run_id: str
    task_digest: str
    # Linkage fields (v1.1): let runs point at their spec and reservation
    # without side records. Optional for backwards compatibility.
    spec_id: str | None = None
    reservation_id: str | None = None
    status: TaskStatus = TaskStatus.QUEUED
    steps: tuple[RunStep, ...] = ()
    result_digest: str | None = None
    error: str | None = None
    mode: RunMode = RunMode.FIXTURE
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def with_transition(self, target: TaskStatus, *, error: str | None = None) -> TaskRun:
        transition_task_status(self.status, target)
        return TaskRun(
            run_id=self.run_id,
            task_digest=self.task_digest,
            spec_id=self.spec_id,
            reservation_id=self.reservation_id,
            status=target,
            steps=self.steps,
            result_digest=self.result_digest,
            error=error or self.error,
            mode=self.mode,
            created_at=self.created_at,
            updated_at=utc_now_iso(),
        )

    def with_step(self, step: RunStep) -> TaskRun:
        return TaskRun(
            run_id=self.run_id,
            task_digest=self.task_digest,
            spec_id=self.spec_id,
            reservation_id=self.reservation_id,
            status=self.status,
            steps=(*self.steps, step),
            result_digest=self.result_digest,
            error=self.error,
            mode=self.mode,
            created_at=self.created_at,
            updated_at=utc_now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "runId": self.run_id,
            "taskDigest": self.task_digest,
            "specId": self.spec_id,
            "reservationId": self.reservation_id,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "resultDigest": self.result_digest,
            "error": self.error,
            "mode": self.mode.value,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRun:
        cls._check_version(data)
        return cls(
            run_id=require_str(data["runId"], "runId"),
            task_digest=require_str(data["taskDigest"], "taskDigest"),
            spec_id=data.get("specId"),
            reservation_id=data.get("reservationId"),
            status=TaskStatus(data["status"]),
            steps=tuple(RunStep.from_dict(s) for s in data.get("steps", [])),
            result_digest=data.get("resultDigest"),
            error=data.get("error"),
            mode=RunMode(data.get("mode", "fixture")),
            created_at=require_str(data.get("createdAt", utc_now_iso()), "createdAt"),
            updated_at=require_str(data.get("updatedAt", utc_now_iso()), "updatedAt"),
        )


def new_task_spec_id() -> str:
    return new_id("task")


def new_run_id() -> str:
    return new_id("run")


@dataclass(frozen=True)
class ResultPackage(ContractRecord):
    """What a delivered run hands back (design §4.4) — a usable deliverable,
    not a call log.

    The trusted supervisor builds this after checking completion conditions;
    a model saying "done" never flips a run to success by itself. Unfinished
    work, uncertainties and external-action states are stated, not hidden.
    """

    run_id: str
    conclusion: str
    artifact_refs: tuple[str, ...] = ()  # digests of delivered artifacts
    done_items: tuple[str, ...] = ()
    not_done_items: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    # External/approval actions this result depends on: id -> state
    external_actions: dict[str, str] = field(default_factory=dict)
    total_cost_usd: float | None = None  # None => unmeasured
    completed_conditions_check: dict[str, bool] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = "1"

    def deliverable(self) -> bool:
        """All declared completion conditions checked true, nothing pending."""
        checks = self.completed_conditions_check
        return (
            bool(checks)
            and all(checks.values())
            and not any(state == "pending" for state in self.external_actions.values())
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "runId": self.run_id,
            "conclusion": self.conclusion,
            "artifactRefs": list(self.artifact_refs),
            "doneItems": list(self.done_items),
            "notDoneItems": list(self.not_done_items),
            "uncertainties": list(self.uncertainties),
            "externalActions": dict(self.external_actions),
            "totalCostUsd": self.total_cost_usd,
            "completedConditionsCheck": dict(self.completed_conditions_check),
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResultPackage:
        cls._check_version(data)
        return cls(
            run_id=require_str(data["runId"], "runId"),
            conclusion=require_str(data["conclusion"], "conclusion"),
            artifact_refs=tuple(data.get("artifactRefs", [])),
            done_items=tuple(data.get("doneItems", [])),
            not_done_items=tuple(data.get("notDoneItems", [])),
            uncertainties=tuple(data.get("uncertainties", [])),
            external_actions=dict(data.get("externalActions") or {}),
            total_cost_usd=data.get("totalCostUsd"),
            completed_conditions_check=dict(data.get("completedConditionsCheck") or {}),
            created_at=require_str(data.get("createdAt", utc_now_iso()), "createdAt"),
        )
