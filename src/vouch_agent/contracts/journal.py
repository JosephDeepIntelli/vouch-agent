"""Event & cost journal contracts: events, cost entries, budget reservations."""

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


class EventKind(StrEnum):
    RUN_STARTED = "run-started"
    RUN_COMPLETED = "run-completed"
    RUN_FAILED = "run-failed"
    RUN_CANCELLED = "run-cancelled"
    RUN_PAUSED = "run-paused"
    RUN_RECONCILIATION = "run-reconciliation"
    ATTEMPT_STARTED = "attempt-started"
    ATTEMPT_ENDED = "attempt-ended"
    GATE_DECISION = "gate-decision"
    GATE_DENIED = "gate-denied"
    BUDGET_RESERVED = "budget-reserved"
    BUDGET_SETTLED = "budget-settled"
    BUDGET_RELEASED = "budget-released"
    BUDGET_EXHAUSTED = "budget-exhausted"
    CANDIDATE_STATE = "candidate-state"
    APPROVAL_RECORDED = "approval-recorded"
    APPROVAL_INVALIDATED = "approval-invalidated"
    RELEASE_RECORDED = "release-recorded"
    SKILL_STATE = "skill-state"
    RECOVERY_POINT = "recovery-point"
    ADAPTER_FRAME = "adapter-frame"
    AUDIT_NOTE = "audit-note"


@dataclass(frozen=True)
class EventRecord(ContractRecord):
    """Append-only journal event. The controller appends; workers cannot overwrite."""

    event_id: str
    kind: EventKind
    occurred_at: str = field(default_factory=utc_now_iso)
    actor: str = "controller"
    subject: str = ""  # run/attempt/candidate/skill id this event is about
    data: dict[str, Any] = field(default_factory=dict)
    mode: RunMode = RunMode.FIXTURE
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "eventId": self.event_id,
            "kind": self.kind.value,
            "occurredAt": self.occurred_at,
            "actor": self.actor,
            "subject": self.subject,
            "data": self.data,
            "mode": self.mode.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventRecord:
        cls._check_version(data)
        return cls(
            event_id=require_str(data["eventId"], "eventId"),
            kind=EventKind(data["kind"]),
            occurred_at=require_str(data.get("occurredAt", utc_now_iso()), "occurredAt"),
            actor=require_str(data.get("actor", "controller"), "actor"),
            subject=str(data.get("subject", "")),
            data=dict(data.get("data") or {}),
            mode=RunMode(data.get("mode", "fixture")),
        )


class CostCategory(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    SEARCH = "search"
    EVALUATION = "evaluation"
    HUMAN_REVIEW = "human-review"
    INFRASTRUCTURE = "infrastructure"
    RETRY = "retry"


@dataclass(frozen=True)
class CostEntry(ContractRecord):
    """One booked cost line. Failed attempts are booked too (design §10).

    ``amount_usd is None`` with ``measurable=False`` means the price is
    unknown — the entry is recorded as unmeasurable, never as zero. Human
    effort uses ``human_minutes`` (booked separately from USD).
    """

    entry_id: str
    category: CostCategory
    subject: str
    amount_usd: float | None = None
    human_minutes: float | None = None
    measurable: bool = True
    mode: RunMode = RunMode.FIXTURE
    recorded_at: str = field(default_factory=utc_now_iso)
    note: str = ""
    schema_version: str = "1"

    def __post_init__(self) -> None:
        from vouch_agent.errors import ContractError

        if self.amount_usd is None and self.human_minutes is None and self.measurable:
            # Measurable entries must carry an amount; unmeasurable ones are
            # recorded explicitly with measurable=False (never as zero).
            raise ContractError(
                "cost entry must carry amount_usd or human_minutes, or be marked measurable=false"
            )
        if self.amount_usd is not None and self.amount_usd < 0:
            raise ContractError("amount_usd must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "entryId": self.entry_id,
            "category": self.category.value,
            "subject": self.subject,
            "amountUsd": self.amount_usd,
            "humanMinutes": self.human_minutes,
            "measurable": self.measurable,
            "mode": self.mode.value,
            "recordedAt": self.recorded_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CostEntry:
        cls._check_version(data)
        return cls(
            entry_id=require_str(data["entryId"], "entryId"),
            category=CostCategory(data["category"]),
            subject=require_str(data["subject"], "subject"),
            amount_usd=data.get("amountUsd"),
            human_minutes=data.get("humanMinutes"),
            measurable=bool(data.get("measurable", True)),
            mode=RunMode(data.get("mode", "fixture")),
            recorded_at=require_str(data.get("recordedAt", utc_now_iso()), "recordedAt"),
            note=str(data.get("note", "")),
        )


class ReservationStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    RELEASED = "released"


@dataclass(frozen=True)
class BudgetReservation(ContractRecord):
    """Atomic reservation of future spend for one holder (attempt/run/skill check).

    Reservation happens BEFORE work starts; settlement books the actual
    amount and returns the difference to the pool. This is the mechanism the
    JAZ ADR demands: accounting returned cost is not enough to bound in-flight
    spend under concurrency.
    """

    reservation_id: str
    holder: str
    amount_usd: float
    status: ReservationStatus = ReservationStatus.OPEN
    settled_amount_usd: float | None = None
    created_at: str = field(default_factory=utc_now_iso)
    closed_at: str | None = None
    # v1.1: child allocations carve space out of an OPEN parent reservation
    # and never draw from the project cap directly (see storage.budget).
    parent_reservation_id: str | None = None
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "reservationId": self.reservation_id,
            "holder": self.holder,
            "amountUsd": self.amount_usd,
            "status": self.status.value,
            "settledAmountUsd": self.settled_amount_usd,
            "createdAt": self.created_at,
            "closedAt": self.closed_at,
            "parentReservationId": self.parent_reservation_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetReservation:
        cls._check_version(data)
        return cls(
            reservation_id=require_str(data["reservationId"], "reservationId"),
            holder=require_str(data["holder"], "holder"),
            amount_usd=float(data["amountUsd"]),
            status=ReservationStatus(data.get("status", "open")),
            settled_amount_usd=(
                float(data["settledAmountUsd"])
                if data.get("settledAmountUsd") is not None
                else None
            ),
            created_at=require_str(data.get("createdAt", utc_now_iso()), "createdAt"),
            closed_at=data.get("closedAt"),
            parent_reservation_id=data.get("parentReservationId"),
        )


def new_journal_ids() -> tuple[str, str, str]:
    """(event id, cost entry id, reservation id) generator helper."""
    return new_id("evt"), new_id("cost"), new_id("rsv")
