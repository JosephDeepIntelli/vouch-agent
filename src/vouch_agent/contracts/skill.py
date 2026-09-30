"""Skill ledger contracts: environment-scoped trust states and reuse rights."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import (
    ContractRecord,
    digest_of,
    new_id,
    require_str,
    utc_now_iso,
)
from vouch_agent.errors import ContractError


class SkillState(StrEnum):
    """distilled -> probation -> trusted; new env/age/drift -> probation;
    harmful outcome -> quarantined (evidence kept)."""

    DISTILLED = "distilled"
    PROBATION = "probation"
    TRUSTED = "trusted"
    QUARANTINED = "quarantined"


SKILL_TRANSITIONS: dict[SkillState, frozenset[SkillState]] = {
    SkillState.DISTILLED: frozenset({SkillState.PROBATION, SkillState.QUARANTINED}),
    SkillState.PROBATION: frozenset(
        {SkillState.TRUSTED, SkillState.QUARANTINED, SkillState.DISTILLED}
    ),
    SkillState.TRUSTED: frozenset(
        {SkillState.PROBATION, SkillState.QUARANTINED}
    ),  # env drift / re-check age
    # Quarantine keeps the failure evidence; leaving it is an explicit human action.
    SkillState.QUARANTINED: frozenset({SkillState.PROBATION}),
}


class ReuseRight(StrEnum):
    NONE = "none"
    PROJECT = "project"
    ENVIRONMENT_AUTHORIZED = "environment-authorized"


@dataclass(frozen=True)
class EnvironmentSignature(ContractRecord):
    """Where a skill's verification is valid (design §8).

    There is no cross-customer universal trust score: model version, tool
    schema, workflow/domain, locale, data policy and acceptance version all
    participate in the signature.
    """

    model_id: str
    tool_schema_version: str
    workflow_id: str
    domain: str
    locale: str
    data_policy: str
    acceptance_version: str
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "modelId": self.model_id,
            "toolSchemaVersion": self.tool_schema_version,
            "workflowId": self.workflow_id,
            "domain": self.domain,
            "locale": self.locale,
            "dataPolicy": self.data_policy,
            "acceptanceVersion": self.acceptance_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnvironmentSignature:
        cls._check_version(data)
        return cls(
            model_id=require_str(data["modelId"], "modelId"),
            tool_schema_version=require_str(data["toolSchemaVersion"], "toolSchemaVersion"),
            workflow_id=require_str(data["workflowId"], "workflowId"),
            domain=require_str(data["domain"], "domain"),
            locale=require_str(data["locale"], "locale"),
            data_policy=require_str(data["dataPolicy"], "dataPolicy"),
            acceptance_version=require_str(data["acceptanceVersion"], "acceptanceVersion"),
        )


@dataclass(frozen=True)
class SkillEntry(ContractRecord):
    """One reusable increment with verification counts and reuse rights."""

    skill_id: str
    content_digest: str
    environment: EnvironmentSignature
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    verification_refs: tuple[str, ...] = ()  # digests of verification evidence
    helpful: int = 0
    harmful: int = 0
    unknown: int = 0
    state: SkillState = SkillState.DISTILLED
    reuse_right: ReuseRight = ReuseRight.NONE
    source: str = "internal"
    first_seen_at: str = field(default_factory=utc_now_iso)
    last_verified_at: str | None = None
    recheck_due_at: str | None = None
    schema_version: str = "1"

    def with_transition(self, target: SkillState) -> SkillEntry:
        if target not in SKILL_TRANSITIONS[self.state]:
            from vouch_agent.errors import InvalidStateTransitionError

            raise InvalidStateTransitionError(
                f"skill cannot transition {self.state.value} -> {target.value}"
            )
        return SkillEntry(
            skill_id=self.skill_id,
            content_digest=self.content_digest,
            environment=self.environment,
            preconditions=self.preconditions,
            postconditions=self.postconditions,
            verification_refs=self.verification_refs,
            helpful=self.helpful,
            harmful=self.harmful,
            unknown=self.unknown,
            state=target,
            reuse_right=self.reuse_right,
            source=self.source,
            first_seen_at=self.first_seen_at,
            last_verified_at=self.last_verified_at,
            recheck_due_at=self.recheck_due_at,
        )

    def usable_in(self, environment: EnvironmentSignature, project_authorized: bool) -> None:
        """Rights-before-reuse check — raises unless explicitly authorized."""
        from vouch_agent.errors import UnauthorizedReuseError

        if self.state in (SkillState.QUARANTINED, SkillState.DISTILLED):
            raise UnauthorizedReuseError(
                f"skill {self.skill_id} is {self.state.value}; verification required first"
            )
        if self.reuse_right is ReuseRight.NONE:
            raise UnauthorizedReuseError(f"skill {self.skill_id} carries no reuse rights")
        if self.reuse_right is ReuseRight.PROJECT and not project_authorized:
            raise UnauthorizedReuseError(
                f"skill {self.skill_id} is project-scoped and this project is not authorized"
            )
        env_match = digest_of(self.environment.to_dict()) == digest_of(environment.to_dict())
        if self.reuse_right is ReuseRight.ENVIRONMENT_AUTHORIZED and not env_match:
            raise UnauthorizedReuseError(
                f"skill {self.skill_id} environment signature does not match this environment"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "skillId": self.skill_id,
            "contentDigest": self.content_digest,
            "environment": self.environment.to_dict(),
            "preconditions": list(self.preconditions),
            "postconditions": list(self.postconditions),
            "verificationRefs": list(self.verification_refs),
            "helpful": self.helpful,
            "harmful": self.harmful,
            "unknown": self.unknown,
            "state": self.state.value,
            "reuseRight": self.reuse_right.value,
            "source": self.source,
            "firstSeenAt": self.first_seen_at,
            "lastVerifiedAt": self.last_verified_at,
            "recheckDueAt": self.recheck_due_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillEntry:
        cls._check_version(data)
        counts = (
            int(data.get("helpful", 0)),
            int(data.get("harmful", 0)),
            int(data.get("unknown", 0)),
        )
        if any(c < 0 for c in counts):
            raise ContractError("skill counts must be >= 0")
        return cls(
            skill_id=require_str(data["skillId"], "skillId"),
            content_digest=require_str(data["contentDigest"], "contentDigest"),
            environment=EnvironmentSignature.from_dict(data["environment"]),
            preconditions=tuple(data.get("preconditions", [])),
            postconditions=tuple(data.get("postconditions", [])),
            verification_refs=tuple(data.get("verificationRefs", [])),
            helpful=counts[0],
            harmful=counts[1],
            unknown=counts[2],
            state=SkillState(data.get("state", "distilled")),
            reuse_right=ReuseRight(data.get("reuseRight", "none")),
            source=str(data.get("source", "internal")),
            first_seen_at=require_str(data.get("firstSeenAt", utc_now_iso()), "firstSeenAt"),
            last_verified_at=data.get("lastVerifiedAt"),
            recheck_due_at=data.get("recheckDueAt"),
        )


def new_skill_id() -> str:
    return new_id("skill")
