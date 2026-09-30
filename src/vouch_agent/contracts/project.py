"""ProjectSpec: workflows, owners, allowed changes, budget and stop conditions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vouch_agent.contracts.common import (
    ContractRecord,
    Role,
    require_str,
    utc_now_iso,
)
from vouch_agent.errors import ContractError


@dataclass(frozen=True)
class WorkflowDeclaration(ContractRecord):
    """One declared business/evaluation workflow, frozen in the ProjectSpec.

    Mirrors the Choose coverage matrix rows (design §3.1): each declaration
    names its main objective and the regression guardrails that must never be
    traded for the main metric.
    """

    workflow_id: str
    name: str
    main_objective: str
    guardrails: tuple[str, ...] = ()
    locales: tuple[str, ...] = ("en",)
    markets: tuple[str, ...] = ()
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "workflowId": self.workflow_id,
            "name": self.name,
            "mainObjective": self.main_objective,
            "guardrails": list(self.guardrails),
            "locales": list(self.locales),
            "markets": list(self.markets),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowDeclaration:
        cls._check_version(data)
        return cls(
            workflow_id=require_str(data["workflowId"], "workflowId"),
            name=require_str(data["name"], "name"),
            main_objective=require_str(data["mainObjective"], "mainObjective"),
            guardrails=tuple(data.get("guardrails", [])),
            locales=tuple(data.get("locales", ("en",))),
            markets=tuple(data.get("markets", ())),
        )


@dataclass(frozen=True)
class BudgetPolicy(ContractRecord):
    """Full-burden budget rules (design §10).

    The cap covers proposals, baseline, *all* candidates including failed
    ones, evaluation, review, retries, search/tools and isolated execution.
    Concurrency and per-attempt bounds exist so reservation happens before
    work starts, not after the invoice arrives.
    """

    total_usd_cap: float
    max_concurrent_attempts: int = 1
    per_attempt_timeout_s: float = 300.0
    # When a price is unknown: reserve this fraction of remaining budget or
    # refuse scheduling outright (fail closed) rather than book zero.
    unknown_price_policy: str = "refuse"  # refuse | conservative-reserve
    conservative_reserve_usd: float = 1.0
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "totalUsdCap": self.total_usd_cap,
            "maxConcurrentAttempts": self.max_concurrent_attempts,
            "perAttemptTimeoutS": self.per_attempt_timeout_s,
            "unknownPricePolicy": self.unknown_price_policy,
            "conservativeReserveUsd": self.conservative_reserve_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetPolicy:
        cls._check_version(data)
        policy = BudgetPolicy(
            total_usd_cap=float(data["totalUsdCap"]),
            max_concurrent_attempts=int(data.get("maxConcurrentAttempts", 1)),
            per_attempt_timeout_s=float(data.get("perAttemptTimeoutS", 300.0)),
            unknown_price_policy=str(data.get("unknownPricePolicy", "refuse")),
            conservative_reserve_usd=float(data.get("conservativeReserveUsd", 1.0)),
        )
        if policy.total_usd_cap < 0:
            raise ContractError("totalUsdCap must be >= 0")
        if policy.max_concurrent_attempts < 1:
            raise ContractError("maxConcurrentAttempts must be >= 1")
        if policy.unknown_price_policy not in ("refuse", "conservative-reserve"):
            raise ContractError(f"unknown unknownPricePolicy {policy.unknown_price_policy!r}")
        return policy


@dataclass(frozen=True)
class ProjectSpec(ContractRecord):
    project_id: str
    name: str
    workflows: tuple[WorkflowDeclaration, ...]
    owners: dict[Role, str]  # responsibility -> human identity; never implicit
    allowed_change_types: tuple[str, ...]
    # Data authorization + processing location are recorded, not assumed.
    data_authorization: str = "internal-own-workflows"
    processing_location: str = "local"
    #: Workspace mode: ``improvement`` (default; the improvement vertical with
    #: workflows and owners) or ``task-only`` (the native task journey — CSV
    #: reconciliation, inspection, export — with no improvement configuration).
    #: Additive (RC1): absent in legacy specs, which stay improvement mode.
    mode: str = "improvement"
    #: Free-text purpose recorded at initialization (never an authorization).
    purpose: str = ""
    budget: BudgetPolicy = field(default_factory=lambda: BudgetPolicy(total_usd_cap=0.0))
    stop_conditions: tuple[str, ...] = ("budget-exhausted", "guardrail-violation", "timeout")
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = "1"

    def workflow(self, workflow_id: str) -> WorkflowDeclaration:
        for wf in self.workflows:
            if wf.workflow_id == workflow_id:
                return wf
        raise ContractError(f"unknown workflow {workflow_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "projectId": self.project_id,
            "name": self.name,
            "workflows": [w.to_dict() for w in self.workflows],
            "owners": {role.value: who for role, who in self.owners.items()},
            "allowedChangeTypes": list(self.allowed_change_types),
            "dataAuthorization": self.data_authorization,
            "processingLocation": self.processing_location,
            "mode": self.mode,
            "purpose": self.purpose,
            "budget": self.budget.to_dict(),
            "stopConditions": list(self.stop_conditions),
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectSpec:
        cls._check_version(data)
        mode = str(data.get("mode", "improvement"))
        if mode not in ("improvement", "task-only"):
            raise ContractError(f"unknown workspace mode {mode!r}")
        workflows = tuple(WorkflowDeclaration.from_dict(w) for w in data.get("workflows", []))
        owners_raw = data.get("owners", {})
        required = (Role.ACCEPTANCE_OWNER, Role.RELEASE_OWNER)
        owners = {Role(r): str(w) for r, w in owners_raw.items()}
        if mode == "task-only":
            # The native task journey needs no improvement configuration;
            # ownership identities are NEVER manufactured to fill the gap.
            if workflows:
                raise ContractError(
                    "a task-only workspace declares no improvement workflows"
                )
        else:
            if not workflows:
                raise ContractError("ProjectSpec must declare at least one workflow")
            missing = [r.value for r in required if r not in owners]
            if missing:
                raise ContractError(f"ProjectSpec.owners missing required roles: {missing}")
        return cls(
            project_id=require_str(data["projectId"], "projectId"),
            name=require_str(data["name"], "name"),
            workflows=workflows,
            owners=owners,
            allowed_change_types=tuple(data.get("allowedChangeTypes", [])),
            data_authorization=str(data.get("dataAuthorization", "internal-own-workflows")),
            processing_location=str(data.get("processingLocation", "local")),
            mode=mode,
            purpose=str(data.get("purpose", "")),
            budget=BudgetPolicy.from_dict(data.get("budget", {"totalUsdCap": 0.0})),
            stop_conditions=tuple(
                data.get("stopConditions", ("budget-exhausted", "guardrail-violation", "timeout"))
            ),
            created_at=require_str(data.get("createdAt", utc_now_iso()), "createdAt"),
        )
