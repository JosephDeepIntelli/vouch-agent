"""Invocation records — the minimal language-driven call contract (design §4.3).

Semantic contract: ``invoke(task, scope, return_schema, policy_ref,
budget_slice, cancellation)``. Every invocation carries
``invocationId, parentId, taskRef, inputRefs, outputSchema, policyDigest,
budgetReservation``. Sub-invocation permissions only narrow; budgets come
from the parent's reservation; depth, concurrency, total calls, context and
wall-clock are all capped. Roles are call policies, not resident agents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import (
    ContractRecord,
    new_id,
    require_str,
)


class InvocationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Budget/reservation invalid or wall clock exceeded — bounded, not fatal
    #: to the parent task unless policy says so.
    BOUNDED_STOP = "bounded-stop"


@dataclass(frozen=True)
class Invocation(ContractRecord):
    """One node in the supervisor's invocation tree (execute or improve side)."""

    invocation_id: str
    task_ref: str  # TaskSpec id/digest or workflow-scoped reference
    parent_id: str | None = None
    input_refs: tuple[str, ...] = ()
    output_schema: dict[str, Any] = field(default_factory=dict)
    policy_digest: str = ""
    budget_reservation_id: str = ""
    # Scope narrowing: a child's capability set must be a subset of parent's.
    granted_capabilities: tuple[str, ...] = ()
    status: InvocationStatus = InvocationStatus.PENDING
    started_at: str | None = None
    ended_at: str | None = None
    result_digest: str | None = None
    error: str | None = None
    depth: int = 0
    schema_version: str = "1"

    def child(self, invocation_id: str, *, capabilities: tuple[str, ...]) -> Invocation:
        """Spawn a child invocation whose capabilities must narrow ours."""
        widened = set(capabilities) - set(self.granted_capabilities)
        if widened:
            from vouch_agent.errors import ContractError

            raise ContractError(f"child invocation cannot widen capabilities: {sorted(widened)}")
        return Invocation(
            invocation_id=invocation_id,
            task_ref=self.task_ref,
            parent_id=self.invocation_id,
            input_refs=(),
            output_schema={},
            policy_digest=self.policy_digest,
            budget_reservation_id="",  # assigned when the parent slices its budget
            granted_capabilities=capabilities,
            depth=self.depth + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "invocationId": self.invocation_id,
            "taskRef": self.task_ref,
            "parentId": self.parent_id,
            "inputRefs": list(self.input_refs),
            "outputSchema": self.output_schema,
            "policyDigest": self.policy_digest,
            "budgetReservationId": self.budget_reservation_id,
            "grantedCapabilities": list(self.granted_capabilities),
            "status": self.status.value,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "resultDigest": self.result_digest,
            "error": self.error,
            "depth": self.depth,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Invocation:
        cls._check_version(data)
        return cls(
            invocation_id=require_str(data["invocationId"], "invocationId"),
            task_ref=require_str(data["taskRef"], "taskRef"),
            parent_id=data.get("parentId"),
            input_refs=tuple(data.get("inputRefs", [])),
            output_schema=dict(data.get("outputSchema") or {}),
            policy_digest=str(data.get("policyDigest", "")),
            budget_reservation_id=str(data.get("budgetReservationId", "")),
            granted_capabilities=tuple(data.get("grantedCapabilities", [])),
            status=InvocationStatus(data.get("status", "pending")),
            started_at=data.get("startedAt"),
            ended_at=data.get("endedAt"),
            result_digest=data.get("resultDigest"),
            error=data.get("error"),
            depth=int(data.get("depth", 0)),
        )


def new_invocation_id() -> str:
    return new_id("inv")
