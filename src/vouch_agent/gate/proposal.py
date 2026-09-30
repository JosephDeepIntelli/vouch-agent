"""Structured action proposals and risk classes (design §6)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import ContractRecord, canonical_json, digest_of
from vouch_agent.errors import ContractError


class RiskClass(StrEnum):
    R0 = "r0"  # authorized task pack reads, pure computation
    R1 = "r1"  # writes inside isolated scratch, disposable evaluation
    R2 = "r2"  # real project config changes, outbound data
    R3 = "r3"  # release, payment, permission change, irreversible deletion


@dataclass(frozen=True)
class ConfidenceSignal(ContractRecord):
    """Where a confidence number comes from. Tier 0 = deterministic
    environment feedback, Tier 1 = same-provider verifier or self-consistency,
    Tier 2 = optional external calibrated model. A model's self-report only
    ever ranks; it never authorizes."""

    tier: int
    source: str  # e.g. "environment" | "verifier:model-x" | "self-report"
    value: float | None = None
    calibration_version: str = "unknown"
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.tier not in (0, 1, 2):
            raise ContractError(f"confidence tier must be 0, 1 or 2, got {self.tier}")
        if self.value is not None and not 0.0 <= self.value <= 1.0:
            raise ContractError("confidence value must be within [0, 1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "tier": self.tier,
            "source": self.source,
            "value": self.value,
            "calibrationVersion": self.calibration_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfidenceSignal:
        cls._check_version(data)
        return cls(
            tier=int(data["tier"]),
            source=str(data.get("source", "unknown")),
            value=data.get("value"),
            calibration_version=str(data.get("calibrationVersion", "unknown")),
        )


#: The confidence signal of last resort — honest about knowing nothing.
UNKNOWN_CONFIDENCE = ConfidenceSignal(tier=0, source="unknown", value=None)


@dataclass(frozen=True)
class ActionProposal(ContractRecord):
    action: str  # e.g. "read-case", "write-scratch", "invoke-model", "export-evidence"
    arguments: dict[str, Any]
    risk_class: RiskClass
    evidence_refs: tuple[str, ...] = ()
    confidence: ConfidenceSignal = UNKNOWN_CONFIDENCE
    resource_ids: tuple[str, ...] = ()  # case ids, artifact digests, paths...
    schema_version: str = "1"

    def arguments_digest(self) -> str:
        """Digest over the *normalized* arguments — the anti-swap binding."""
        return digest_of({"action": self.action, "arguments": canonical_json(self.arguments)})

    def proposal_digest(self) -> str:
        """Digest over EVERYTHING the gate decides on (review A1).

        Binds action + normalized arguments *and* the declared risk class and
        resource scope, so an approval cannot be replayed onto the same
        arguments carrying a lower risk class or a different resource set.
        ``arguments_digest`` alone proved too narrow: it authorized
        ``("read", {}, r0, ())`` and then kept authorizing when the very same
        action/arguments were re-presented as R2 over foreign resources.
        """
        return digest_of(
            {
                "action": self.action,
                "arguments": canonical_json(self.arguments),
                "riskClass": self.risk_class.value,
                "resourceIds": sorted(self.resource_ids),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "action": self.action,
            "arguments": self.arguments,
            "riskClass": self.risk_class.value,
            "evidenceRefs": list(self.evidence_refs),
            "confidence": self.confidence.to_dict(),
            "resourceIds": list(self.resource_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionProposal:
        cls._check_version(data)
        return cls(
            action=str(data["action"]),
            arguments=dict(data.get("arguments") or {}),
            risk_class=RiskClass(data["riskClass"]),
            evidence_refs=tuple(data.get("evidenceRefs", [])),
            confidence=(
                ConfidenceSignal.from_dict(data["confidence"])
                if data.get("confidence")
                else UNKNOWN_CONFIDENCE
            ),
            resource_ids=tuple(data.get("resourceIds", [])),
        )
