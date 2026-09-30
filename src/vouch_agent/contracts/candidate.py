"""AgentVersion and Candidate — bounded, digest-bound change proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import (
    ContractRecord,
    require_str,
    utc_now_iso,
)
from vouch_agent.errors import ContractError


class ChangeType(StrEnum):
    """Allowed candidate change types (design §3.1). Anything else is out of scope.

    Acceptance labels, fact/permission validation strength, billing, market
    and session constraints are NOT changeable — candidates touching them are
    rejected at seal time.
    """

    PROMPT_DELTA = "prompt-delta"
    CLARIFICATION_STRATEGY = "clarification-strategy"
    RETRIEVAL_PARAMS = "retrieval-params"
    TOOL_SELECTION = "tool-selection"
    SUBMISSION_STRATEGY = "submission-strategy"

    def in_scope(self, allowed: tuple[str, ...]) -> bool:
        return self.value in allowed


class CandidateState(StrEnum):
    """Commit state machine (design §7.2).

    proposed -> sealed -> evaluated -> accepted|rejected|inconclusive
    -> approved -> released -> observed|rolled_back
    Any digest change after sealing moves the candidate to invalidated and it
    must re-enter the corresponding evaluation stage.
    """

    PROPOSED = "proposed"
    SEALED = "sealed"
    EVALUATED = "evaluated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    APPROVED = "approved"
    RELEASED = "released"
    OBSERVED = "observed"
    ROLLED_BACK = "rolled-back"
    INVALIDATED = "invalidated"


CANDIDATE_TRANSITIONS: dict[CandidateState, frozenset[CandidateState]] = {
    CandidateState.PROPOSED: frozenset({CandidateState.SEALED}),
    CandidateState.SEALED: frozenset({CandidateState.EVALUATED, CandidateState.INVALIDATED}),
    CandidateState.EVALUATED: frozenset(
        {
            CandidateState.ACCEPTED,
            CandidateState.REJECTED,
            CandidateState.INCONCLUSIVE,
            CandidateState.INVALIDATED,
        }
    ),
    CandidateState.ACCEPTED: frozenset({CandidateState.APPROVED, CandidateState.INVALIDATED}),
    CandidateState.REJECTED: frozenset(),
    CandidateState.INCONCLUSIVE: frozenset({CandidateState.INVALIDATED}),
    CandidateState.APPROVED: frozenset({CandidateState.RELEASED, CandidateState.INVALIDATED}),
    CandidateState.RELEASED: frozenset({CandidateState.OBSERVED, CandidateState.ROLLED_BACK}),
    CandidateState.OBSERVED: frozenset({CandidateState.ROLLED_BACK}),
    CandidateState.ROLLED_BACK: frozenset(),
    # An invalidated candidate re-enters as a NEW proposed candidate; the old
    # record stays invalidated for the audit trail.
    CandidateState.INVALIDATED: frozenset(),
}

#: States from which any bound-digest change forces invalidation.
DIGEST_BINDING_STATES = frozenset(
    {
        CandidateState.SEALED,
        CandidateState.EVALUATED,
        CandidateState.ACCEPTED,
        CandidateState.APPROVED,
        CandidateState.RELEASED,
        CandidateState.OBSERVED,
    }
)


def transition_candidate_state(current: CandidateState, target: CandidateState) -> CandidateState:
    from vouch_agent.errors import InvalidStateTransitionError

    if target not in CANDIDATE_TRANSITIONS[current]:
        raise InvalidStateTransitionError(
            f"candidate cannot transition {current.value} -> {target.value}"
        )
    return target


@dataclass(frozen=True)
class AgentVersion(ContractRecord):
    """A concrete, reproducible configuration of the system-under-improvement."""

    version_id: str
    source_ref: str  # e.g. git commit of the target repo/config
    prompt_delta_digest: str | None = None
    model_id: str = ""
    tool_schema_version: str = "1"
    dependency_lock_digest: str | None = None
    environment_digest: str | None = None
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "versionId": self.version_id,
            "sourceRef": self.source_ref,
            "promptDeltaDigest": self.prompt_delta_digest,
            "modelId": self.model_id,
            "toolSchemaVersion": self.tool_schema_version,
            "dependencyLockDigest": self.dependency_lock_digest,
            "environmentDigest": self.environment_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentVersion:
        cls._check_version(data)
        return cls(
            version_id=require_str(data["versionId"], "versionId"),
            source_ref=require_str(data["sourceRef"], "sourceRef"),
            prompt_delta_digest=data.get("promptDeltaDigest"),
            model_id=str(data.get("modelId", "")),
            tool_schema_version=str(data.get("toolSchemaVersion", "1")),
            dependency_lock_digest=data.get("dependencyLockDigest"),
            environment_digest=data.get("environmentDigest"),
        )


@dataclass(frozen=True)
class Candidate(ContractRecord):
    """A bounded change proposal with explicit parent, rationale and lineage."""

    candidate_id: str
    parent_version: AgentVersion
    change_type: ChangeType
    delta: str  # the change payload itself (prompt diff, params, policy)
    rationale: str
    expected_impact: str
    proposer: str
    dev_case_refs: tuple[str, ...] = ()  # development-set cases that motivated it
    state: CandidateState = CandidateState.PROPOSED
    state_reason: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = "1"

    def with_transition(self, target: CandidateState, reason: str = "") -> Candidate:
        transition_candidate_state(self.state, target)
        return Candidate(
            candidate_id=self.candidate_id,
            parent_version=self.parent_version,
            change_type=self.change_type,
            delta=self.delta,
            rationale=self.rationale,
            expected_impact=self.expected_impact,
            proposer=self.proposer,
            dev_case_refs=self.dev_case_refs,
            state=target,
            state_reason=reason or self.state_reason,
            created_at=self.created_at,
        )

    def delta_digest(self) -> str:
        from vouch_agent.contracts.common import digest_of

        return digest_of({"changeType": self.change_type.value, "delta": self.delta})

    def content_digest(self) -> str:
        """Digest over the change CONTENT only.

        Excludes lifecycle state, state reason and timestamps: approving and
        releasing the same change must not invalidate its own approval by
        advancing the state machine. Any edit to what the change IS (parent,
        type, delta, rationale, proposer, motivating cases) changes this.
        """
        from vouch_agent.contracts.common import digest_of

        return digest_of(
            {
                "parentVersion": self.parent_version.to_dict(),
                "changeType": self.change_type.value,
                "delta": self.delta,
                "rationale": self.rationale,
                "expectedImpact": self.expected_impact,
                "proposer": self.proposer,
                "devCaseRefs": list(self.dev_case_refs),
            }
        )

    def sealable(self, allowed_change_types: tuple[str, ...]) -> None:
        """Validation performed before sealing (fail closed on scope violations)."""
        if not self.change_type.in_scope(allowed_change_types):
            raise ContractError(
                f"change type {self.change_type.value!r} is not in allowed_change_types"
            )
        if not self.delta.strip():
            raise ContractError("candidate delta must not be empty")
        if not self.rationale.strip():
            raise ContractError("candidate must carry a rationale")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "candidateId": self.candidate_id,
            "parentVersion": self.parent_version.to_dict(),
            "changeType": self.change_type.value,
            "delta": self.delta,
            "rationale": self.rationale,
            "expectedImpact": self.expected_impact,
            "proposer": self.proposer,
            "devCaseRefs": list(self.dev_case_refs),
            "state": self.state.value,
            "stateReason": self.state_reason,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Candidate:
        cls._check_version(data)
        return cls(
            candidate_id=require_str(data["candidateId"], "candidateId"),
            parent_version=AgentVersion.from_dict(data["parentVersion"]),
            change_type=ChangeType(data["changeType"]),
            delta=require_str(data["delta"], "delta"),
            rationale=require_str(data["rationale"], "rationale"),
            expected_impact=str(data.get("expectedImpact", "")),
            proposer=require_str(data["proposer"], "proposer"),
            dev_case_refs=tuple(data.get("devCaseRefs", [])),
            state=CandidateState(data.get("state", "proposed")),
            state_reason=str(data.get("stateReason", "")),
            created_at=require_str(data.get("createdAt", utc_now_iso()), "createdAt"),
        )
