"""Improvement-mode case packs: TaskCase, splits and TaskPack.

Evaluation cases are grouped by customer/task family and split across four
isolated sets (design §7.1): development (proposer-readable),
selection_validation (controller-compared, usage counted into search
history), final_acceptance (acceptance side only — the proposer can never
read its labels, files or credentials) and observation (post-release).
Split visibility is enforced by the storage layer, not by convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import (
    ContractRecord,
    RunMode,
    require_digest,
    require_str,
)
from vouch_agent.errors import ContractError


class CaseSplit(StrEnum):
    DEVELOPMENT = "development"
    SELECTION_VALIDATION = "selection-validation"
    FINAL_ACCEPTANCE = "final-acceptance"
    OBSERVATION = "observation"

    def readable_by(self, role: str) -> bool:
        from vouch_agent.contracts.common import Role

        role_enum = Role(role)
        if self is CaseSplit.FINAL_ACCEPTANCE:
            return role_enum in (Role.ACCEPTANCE_OWNER, Role.EVALUATOR)
        # Observation is post-release reporting, not proposer material either.
        if self is CaseSplit.OBSERVATION:
            return role_enum in (
                Role.ACCEPTANCE_OWNER,
                Role.EVALUATOR,
                Role.RELEASE_OWNER,
                Role.ENGINEER,
            )
        return True


#: The splits that may be used to *select* candidates. Repeated use is
#: counted into search history and can never be reported as unseen.
SELECTION_SPLITS = frozenset({CaseSplit.DEVELOPMENT, CaseSplit.SELECTION_VALIDATION})


@dataclass(frozen=True)
class TaskCase(ContractRecord):
    case_id: str
    workflow_id: str
    split: CaseSplit
    group_id: str  # related customer/task family; kept together across splits
    input_digest: str  # content-addressed input artifact
    source_refs: tuple[str, ...] = ()
    locale: str = "en"
    market: str | None = None
    synthetic: bool = True  # fixture packs are synthetic until explicitly not
    label_version: str = "1"
    authorization_scope: str = "internal"
    parent_case_id: str | None = None  # revision lineage
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "caseId": self.case_id,
            "workflowId": self.workflow_id,
            "split": self.split.value,
            "groupId": self.group_id,
            "inputDigest": self.input_digest,
            "sourceRefs": list(self.source_refs),
            "locale": self.locale,
            "market": self.market,
            "synthetic": self.synthetic,
            "labelVersion": self.label_version,
            "authorizationScope": self.authorization_scope,
            "parentCaseId": self.parent_case_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskCase:
        cls._check_version(data)
        return cls(
            case_id=require_str(data["caseId"], "caseId"),
            workflow_id=require_str(data["workflowId"], "workflowId"),
            split=CaseSplit(data["split"]),
            group_id=require_str(data["groupId"], "groupId"),
            input_digest=require_digest(data["inputDigest"], "inputDigest"),
            source_refs=tuple(data.get("sourceRefs", [])),
            locale=require_str(data.get("locale", "en"), "locale"),
            market=data.get("market"),
            synthetic=bool(data.get("synthetic", True)),
            label_version=require_str(data.get("labelVersion", "1"), "labelVersion"),
            authorization_scope=require_str(
                data.get("authorizationScope", "internal"), "authorizationScope"
            ),
            parent_case_id=data.get("parentCaseId"),
        )


@dataclass(frozen=True)
class TaskPack(ContractRecord):
    """A versioned, content-addressed bundle of cases for one workflow."""

    pack_id: str
    workflow_id: str
    cases: tuple[TaskCase, ...]
    source_snapshot_digest: str | None = None  # fixed source snapshot for reproducibility
    mode: RunMode = RunMode.FIXTURE
    notes: str = ""
    schema_version: str = "1"

    def cases_in(self, split: CaseSplit) -> tuple[TaskCase, ...]:
        return tuple(c for c in self.cases if c.split is split)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "packId": self.pack_id,
            "workflowId": self.workflow_id,
            "cases": [c.to_dict() for c in self.cases],
            "sourceSnapshotDigest": self.source_snapshot_digest,
            "mode": self.mode.value,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskPack:
        cls._check_version(data)
        cases = tuple(TaskCase.from_dict(c) for c in data.get("cases", []))
        if not cases:
            raise ContractError("TaskPack must contain at least one case")
        seen: set[str] = set()
        for case in cases:
            if case.case_id in seen:
                raise ContractError(f"duplicate caseId {case.case_id!r} in pack")
            seen.add(case.case_id)
        return cls(
            pack_id=require_str(data["packId"], "packId"),
            workflow_id=require_str(data["workflowId"], "workflowId"),
            cases=cases,
            source_snapshot_digest=data.get("sourceSnapshotDigest"),
            mode=RunMode(data.get("mode", "fixture")),
            notes=data.get("notes", ""),
        )
