"""EvaluationRun, attempts, paired outcomes and rubrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import (
    ContractRecord,
    RunMode,
    new_id,
    require_digest,
    require_str,
    utc_now_iso,
)
from vouch_agent.errors import ContractError


class Side(StrEnum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"


class AttemptStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    #: Usage/cost could not be measured — never silently rounded to zero.
    IMMEASURABLE = "immeasurable"


#: Statuses that may be counted as a usable result. Everything else —
#: including unknown/immeasurable — must NOT be treated as success.
USABLE_STATUSES = frozenset({AttemptStatus.OK})


@dataclass(frozen=True)
class AttemptRecord(ContractRecord):
    attempt_id: str
    run_id: str
    side: Side
    case_id: str
    status: AttemptStatus
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str | None = None
    output_digest: str | None = None
    error: str | None = None
    cost_usd: float | None = None  # None == unmeasured, never zero-by-default
    usage: dict[str, Any] = field(default_factory=dict)
    adapter: str = ""
    # v1.2 (M3 Gate A7): planned-repeat identity pairs baseline/candidate
    # observations per case+repeat; filtering successes independently must
    # never reassemble a pair that was not measured. Retry lineage is explicit.
    repeat_index: int = 0
    retry_of: str | None = None
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "attemptId": self.attempt_id,
            "runId": self.run_id,
            "side": self.side.value,
            "caseId": self.case_id,
            "status": self.status.value,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "outputDigest": self.output_digest,
            "error": self.error,
            "costUsd": self.cost_usd,
            "usage": self.usage,
            "adapter": self.adapter,
            "repeatIndex": self.repeat_index,
            "retryOf": self.retry_of,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttemptRecord:
        cls._check_version(data)
        return cls(
            attempt_id=require_str(data["attemptId"], "attemptId"),
            run_id=require_str(data["runId"], "runId"),
            side=Side(data["side"]),
            case_id=require_str(data["caseId"], "caseId"),
            status=AttemptStatus(data["status"]),
            started_at=require_str(data.get("startedAt", utc_now_iso()), "startedAt"),
            ended_at=data.get("endedAt"),
            output_digest=data.get("outputDigest"),
            error=data.get("error"),
            cost_usd=data.get("costUsd"),
            usage=dict(data.get("usage") or {}),
            adapter=str(data.get("adapter", "")),
            repeat_index=int(data.get("repeatIndex", 0)),
            retry_of=data.get("retryOf"),
        )


@dataclass(frozen=True)
class GuardrailFinding(ContractRecord):
    name: str
    severity: str  # hard | soft
    detail: str = ""
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "severity": self.severity,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuardrailFinding:
        cls._check_version(data)
        return cls(
            name=require_str(data["name"], "name"),
            severity=require_str(data.get("severity", "hard"), "severity"),
            detail=str(data.get("detail", "")),
        )


@dataclass(frozen=True)
class PairedOutcome(ContractRecord):
    """Per-case paired comparison: baseline vs candidate on the same case."""

    case_id: str
    baseline: AttemptRecord | None
    candidate: AttemptRecord | None
    metrics: dict[str, float] = field(default_factory=dict)
    guardrail_violations: tuple[GuardrailFinding, ...] = ()
    schema_version: str = "1"

    def complete_pair(self) -> bool:
        """A pair is usable only when both sides produced usable attempts."""
        return (
            self.baseline is not None
            and self.candidate is not None
            and self.baseline.status in USABLE_STATUSES
            and self.candidate.status in USABLE_STATUSES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "caseId": self.case_id,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "metrics": self.metrics,
            "guardrailViolations": [g.to_dict() for g in self.guardrail_violations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PairedOutcome:
        cls._check_version(data)
        return cls(
            case_id=require_str(data["caseId"], "caseId"),
            baseline=AttemptRecord.from_dict(data["baseline"]) if data.get("baseline") else None,
            candidate=AttemptRecord.from_dict(data["candidate"]) if data.get("candidate") else None,
            metrics=dict(data.get("metrics") or {}),
            guardrail_violations=tuple(
                GuardrailFinding.from_dict(g) for g in data.get("guardrailViolations", [])
            ),
        )


@dataclass(frozen=True)
class Rubric(ContractRecord):
    """Frozen evaluation standard. Thresholds freeze AFTER baseline measurement,
    BEFORE candidate search (design §3.1). Rubric changes create a new version
    and invalidate approvals bound to the old digest."""

    main_metric: str
    direction: str = "increase"  # increase | decrease
    thresholds: dict[str, float] = field(default_factory=dict)
    hard_guardrails: tuple[str, ...] = ()
    repeats: int = 1
    frozen_by: str = ""
    frozen_at: str | None = None
    schema_version: str = "1"

    @property
    def frozen(self) -> bool:
        return bool(self.frozen_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "mainMetric": self.main_metric,
            "direction": self.direction,
            "thresholds": self.thresholds,
            "hardGuardrails": list(self.hard_guardrails),
            "repeats": self.repeats,
            "frozenBy": self.frozen_by,
            "frozenAt": self.frozen_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rubric:
        cls._check_version(data)
        rubric = Rubric(
            main_metric=require_str(data["mainMetric"], "mainMetric"),
            direction=str(data.get("direction", "increase")),
            thresholds=dict(data.get("thresholds") or {}),
            hard_guardrails=tuple(data.get("hardGuardrails", [])),
            repeats=int(data.get("repeats", 1)),
            frozen_by=str(data.get("frozenBy", "")),
            frozen_at=data.get("frozenAt"),
        )
        if rubric.direction not in ("increase", "decrease"):
            raise ContractError(f"invalid rubric direction {rubric.direction!r}")
        if rubric.repeats < 1:
            raise ContractError("repeats must be >= 1")
        return rubric


@dataclass(frozen=True)
class EvaluationRun(ContractRecord):
    """One paired comparison run over a case set, with every attempt kept."""

    run_id: str
    workflow_id: str
    split: CaseSplit
    baseline_digest: str
    candidate_digest: str
    case_set_digest: str
    rubric_digest: str
    environment_digest: str | None = None
    attempts: tuple[AttemptRecord, ...] = ()
    outcomes: tuple[PairedOutcome, ...] = ()
    mode: RunMode = RunMode.FIXTURE
    execution_status: str = "pending"  # pending | running | completed | failed
    uncertainty_note: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = "1"

    def total_cost_usd(self) -> float | None:
        """Sum of measured attempt costs; None if any attempt is unmeasured."""
        total = 0.0
        for attempt in self.attempts:
            if attempt.cost_usd is None:
                if attempt.status in USABLE_STATUSES:
                    return None  # usable but unpriced -> immeasurable total
                continue  # failed attempts without cost still count as 0 booked
            total += attempt.cost_usd
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "runId": self.run_id,
            "workflowId": self.workflow_id,
            "split": self.split.value,
            "baselineDigest": self.baseline_digest,
            "candidateDigest": self.candidate_digest,
            "caseSetDigest": self.case_set_digest,
            "rubricDigest": self.rubric_digest,
            "environmentDigest": self.environment_digest,
            "attempts": [a.to_dict() for a in self.attempts],
            "outcomes": [o.to_dict() for o in self.outcomes],
            "mode": self.mode.value,
            "executionStatus": self.execution_status,
            "uncertaintyNote": self.uncertainty_note,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationRun:
        cls._check_version(data)
        return cls(
            run_id=require_str(data["runId"], "runId"),
            workflow_id=require_str(data["workflowId"], "workflowId"),
            split=CaseSplit(data["split"]),
            baseline_digest=require_digest(data["baselineDigest"], "baselineDigest"),
            candidate_digest=require_digest(data["candidateDigest"], "candidateDigest"),
            case_set_digest=require_digest(data["caseSetDigest"], "caseSetDigest"),
            rubric_digest=require_digest(data["rubricDigest"], "rubricDigest"),
            environment_digest=data.get("environmentDigest"),
            attempts=tuple(AttemptRecord.from_dict(a) for a in data.get("attempts", [])),
            outcomes=tuple(PairedOutcome.from_dict(o) for o in data.get("outcomes", [])),
            mode=RunMode(data.get("mode", "fixture")),
            execution_status=str(data.get("executionStatus", "pending")),
            uncertainty_note=str(data.get("uncertaintyNote", "")),
            created_at=require_str(data.get("createdAt", utc_now_iso()), "createdAt"),
        )


def new_run_ids() -> tuple[str, str]:
    """(evaluation run id, attempt id) generator helper."""
    return new_id("eval"), new_id("att")
