"""AcceptanceDecision, approval bindings and ReleaseRecord.

Acceptance and release are two separate events (design §5): ``accepted`` is
"evaluation met the agreed threshold", ``approved`` is a responsible human's
release decision, ``released`` requires deployment evidence. Approval binds a
digest tuple; any change to a bound digest invalidates the approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import (
    ContractRecord,
    require_digest,
    require_str,
    utc_now_iso,
)
from vouch_agent.errors import ApprovalInvalidatedError, ContractError


class Verdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ApprovalBinding(ContractRecord):
    """The digest tuple an approval is only valid for.

    Re-verification: recompute each digest at approval-consumption time and
    compare; a mismatch means the approval no longer applies.
    """

    candidate_digest: str
    rubric_digest: str
    environment_digest: str
    acceptance_case_set_digest: str
    schema_version: str = "1"

    def verify(
        self,
        *,
        candidate_digest: str,
        rubric_digest: str,
        environment_digest: str,
        acceptance_case_set_digest: str,
    ) -> None:
        actual = ApprovalBinding(
            candidate_digest=candidate_digest,
            rubric_digest=rubric_digest,
            environment_digest=environment_digest,
            acceptance_case_set_digest=acceptance_case_set_digest,
        )
        if actual.digest() != self.digest():
            raise ApprovalInvalidatedError(
                "approval binding no longer matches: one of candidate/rubric/"
                "environment/case-set digests changed"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "candidateDigest": self.candidate_digest,
            "rubricDigest": self.rubric_digest,
            "environmentDigest": self.environment_digest,
            "acceptanceCaseSetDigest": self.acceptance_case_set_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalBinding:
        cls._check_version(data)
        return cls(
            candidate_digest=require_digest(data["candidateDigest"], "candidateDigest"),
            rubric_digest=require_digest(data["rubricDigest"], "rubricDigest"),
            environment_digest=require_digest(data["environmentDigest"], "environmentDigest"),
            acceptance_case_set_digest=require_digest(
                data["acceptanceCaseSetDigest"], "acceptanceCaseSetDigest"
            ),
        )


@dataclass(frozen=True)
class AcceptanceDecision(ContractRecord):
    decision_id: str
    verdict: Verdict
    candidate_digest: str
    evidence_digest: str  # digest of the full evidence package backing this verdict
    owner: str  # acceptance owner identity — never the proposer
    decided_at: str = field(default_factory=utc_now_iso)
    allowed_release_scope: str = "none"  # none | project | environment
    binding: ApprovalBinding | None = None
    #: The durable final-acceptance run this decision was computed from.
    #: Approval/release re-resolve this id against trusted storage instead of
    #: trusting any digests supplied by the caller (review A5).
    final_acceptance_run_id: str | None = None
    note: str = ""
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.verdict is Verdict.ACCEPTED and self.binding is None:
            raise ContractError("an accepted verdict requires an explicit approval binding")
        if self.owner == "proposer":
            raise ContractError("the proposer cannot be the acceptance owner")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "decisionId": self.decision_id,
            "verdict": self.verdict.value,
            "candidateDigest": self.candidate_digest,
            "evidenceDigest": self.evidence_digest,
            "owner": self.owner,
            "decidedAt": self.decided_at,
            "allowedReleaseScope": self.allowed_release_scope,
            "binding": self.binding.to_dict() if self.binding else None,
            "finalAcceptanceRunId": self.final_acceptance_run_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AcceptanceDecision:
        cls._check_version(data)
        run_id = data.get("finalAcceptanceRunId")
        return cls(
            decision_id=require_str(data["decisionId"], "decisionId"),
            verdict=Verdict(data["verdict"]),
            candidate_digest=require_digest(data["candidateDigest"], "candidateDigest"),
            evidence_digest=require_digest(data["evidenceDigest"], "evidenceDigest"),
            owner=require_str(data["owner"], "owner"),
            decided_at=require_str(data.get("decidedAt", utc_now_iso()), "decidedAt"),
            allowed_release_scope=str(data.get("allowedReleaseScope", "none")),
            binding=ApprovalBinding.from_dict(data["binding"]) if data.get("binding") else None,
            final_acceptance_run_id=str(run_id) if run_id else None,
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class ReleaseRecord(ContractRecord):
    """The *actual* deployment event — written after release evidence exists.

    Vouch never performs the deployment itself in v1; the target product's own
    release process does, and this record captures what, who, and how to roll
    back (design §5, §7.2).
    """

    release_id: str
    candidate_digest: str
    deployed_version: str
    deployed_by: str
    observed_window: str = ""
    rollback_trigger: str = ""
    side_effects: tuple[str, ...] = ()
    compensation_status: str = "none"  # none | pending | done | not-applicable
    released_at: str = field(default_factory=utc_now_iso)
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "releaseId": self.release_id,
            "candidateDigest": self.candidate_digest,
            "deployedVersion": self.deployed_version,
            "deployedBy": self.deployed_by,
            "observedWindow": self.observed_window,
            "rollbackTrigger": self.rollback_trigger,
            "sideEffects": list(self.side_effects),
            "compensationStatus": self.compensation_status,
            "releasedAt": self.released_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReleaseRecord:
        cls._check_version(data)
        return cls(
            release_id=require_str(data["releaseId"], "releaseId"),
            candidate_digest=require_digest(data["candidateDigest"], "candidateDigest"),
            deployed_version=require_str(data["deployedVersion"], "deployedVersion"),
            deployed_by=require_str(data["deployedBy"], "deployedBy"),
            observed_window=str(data.get("observedWindow", "")),
            rollback_trigger=str(data.get("rollbackTrigger", "")),
            side_effects=tuple(data.get("sideEffects", [])),
            compensation_status=str(data.get("compensationStatus", "none")),
            released_at=require_str(data.get("releasedAt", utc_now_iso()), "releasedAt"),
        )
