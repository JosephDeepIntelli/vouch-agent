"""Client-side authority for Choose change bundles and application receipts.

Interop mirror of the Choose runner's ``scripts/vouch/config-bundle.ts`` rules
(read across the repository boundary for protocol interop ONLY — nothing is
imported from Choose and no Choose source is copied; the digest rules are the
published adapter-protocol contract, and both sides recompute them
independently over canonical JSON).

Two duties, both on Vouch's trusted side:

* BEFORE dispatch — parse and verify the sealed candidate's change bundle so
  an unsupported free-text delta, a tampered bundle, or a stale baseline
  lineage refuses clearly at the public boundary instead of surfacing as a
  failed attempt mid-evaluation. The runner re-validates everything it
  receives; this is the early, honest refusal, not a substitute.
* AFTER dispatch — validate the application receipt the runner returns: the
  requested delta, the actually-applied and observed configuration digests,
  the frozen baseline, and the run/attempt/case/workflow/mode identity. A
  missing, tampered, stale or unrelated receipt cannot promote a candidate.

The bounded change class is deliberate: only whitelisted evaluation fields
(``systemSupplement``, ``quotePolicy``, ``conclusionStyle``) may change, and
control-shaped keys (access/billing/acceptance/credentials) carry their own
rejection. Free text is never converted into executable settings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vouch_agent.contracts.common import DIGEST_PREFIX, canonical_json, digest_of
from vouch_agent.errors import ContractError

#: Change-bundle identity (mirrors the runner's CHANGE_BUNDLE_* constants).
CHANGE_BUNDLE_SCHEMA_VERSION = 1
CHANGE_BUNDLE_KIND = "choose-config-change-bundle"
APPLICATION_RECEIPT_KIND = "choose-config-application-receipt"
APPLICATION_RECEIPT_VERSION = 1

#: Everything a delta may ever set. ``configVersion`` is runner-owned lineage
#: and can never be set by a delta.
ALLOWED_DELTA_FIELDS = frozenset({"systemSupplement", "quotePolicy", "conclusionStyle"})

#: Control categories a delta must never touch, with their own refusal label
#: so the rejection is self-explaining in Vouch's records.
DENIED_DELTA_KEYS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^(access|roles?|permissions?|grants?|admin)$", re.I),
        "access/permission controls",
    ),
    (
        re.compile(
            r"^(billing|billable|credits?|pricing|price|payment|payments|charges?|"
            r"refunds?|spend|budget|budgets)$",
            re.I,
        ),
        "billing/spend controls",
    ),
    (
        re.compile(r"^(acceptance|approval|release|publish|deploy(ment)?)$", re.I),
        "acceptance/release controls",
    ),
    (
        re.compile(
            r"^(credential|credentials|apiKey|api_key|secret|secrets?|token|password)$",
            re.I,
        ),
        "credentials",
    ),
    (re.compile(r"^(credentialRef|providerRef)$", re.I), "credential references"),
)

MAX_SYSTEM_SUPPLEMENT_LENGTH = 2_000

_QUOTE_POLICIES = ("verbatim", "paraphrase")
_CONCLUSION_STYLES = ("standard", "terse")


def _require_digest(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(DIGEST_PREFIX)
        or len(value) != len(DIGEST_PREFIX) + 64
    ):
        raise ContractError(f"change bundle field {field!r} must be a sha256 digest string")
    return value


@dataclass(frozen=True)
class ChooseBaselineConfig:
    """The immutable baseline the runner advertised in its describe payload."""

    config_version: str
    digest: str

    @classmethod
    def from_describe(cls, payload: dict[str, Any]) -> ChooseBaselineConfig:
        baseline = payload.get("baselineConfig")
        if not isinstance(baseline, dict):
            raise ContractError(
                "the Choose runner's describe payload declares no baselineConfig; "
                "an application path without a frozen baseline is refused"
            )
        version = baseline.get("configVersion")
        digest = baseline.get("digest")
        if not isinstance(version, str) or not version:
            raise ContractError("runner baselineConfig.configVersion must be a non-empty string")
        _require_digest(digest, "baselineConfig.digest")
        return cls(config_version=version, digest=str(digest))


@dataclass(frozen=True)
class ChooseChangeBundle:
    """A verified ``choose-config-change-bundle`` (digest recomputed on parse)."""

    bundle_id: str
    baseline_config_version: str
    baseline_digest: str
    delta: dict[str, Any]
    delta_digest: str

    def identity(self) -> dict[str, Any]:
        """The projection the digest covers (self-excluded), mirroring Choose."""
        return {
            "schemaVersion": CHANGE_BUNDLE_SCHEMA_VERSION,
            "kind": CHANGE_BUNDLE_KIND,
            "synthetic": True,
            "bundleId": self.bundle_id,
            "baselineConfigVersion": self.baseline_config_version,
            "baselineDigest": self.baseline_digest,
            "delta": self.delta,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity(), "deltaDigest": self.delta_digest}

    def require_lineage(self, baseline: ChooseBaselineConfig) -> None:
        """Refuse a bundle cut against a different baseline lineage."""
        if (
            self.baseline_config_version != baseline.config_version
            or self.baseline_digest != baseline.digest
        ):
            raise ContractError(
                "change bundle targets baseline "
                f"{self.baseline_config_version}/{self.baseline_digest} but the runner "
                f"enforces {baseline.config_version}/{baseline.digest}; a changed "
                "baseline requires an explicit rebase with a new version lineage "
                "(vouch/config-baseline-mismatch)"
            )


def validate_delta(delta: dict[str, Any]) -> None:
    """Enforce the whitelist and value rules BEFORE anything dispatches."""
    if not isinstance(delta, dict):
        raise ContractError("change bundle delta must be a JSON object")
    for key, value in delta.items():
        for pattern, label in DENIED_DELTA_KEYS:
            if pattern.match(key):
                raise ContractError(
                    f"delta field {key!r} touches {label}, which an evaluation "
                    "change may never alter (vouch/config-field-not-allowed)"
                )
        if key not in ALLOWED_DELTA_FIELDS:
            raise ContractError(
                f"delta field {key!r} is not an allowed evaluation field "
                f"(allowed: {', '.join(sorted(ALLOWED_DELTA_FIELDS))}; "
                "vouch/config-field-not-allowed)"
            )
        if key == "systemSupplement":
            if not isinstance(value, str):
                raise ContractError("systemSupplement must be a string")
            if len(value) > MAX_SYSTEM_SUPPLEMENT_LENGTH:
                raise ContractError(
                    f"systemSupplement exceeds {MAX_SYSTEM_SUPPLEMENT_LENGTH} characters"
                )
        elif key == "quotePolicy" and value not in _QUOTE_POLICIES:
            raise ContractError('quotePolicy must be "verbatim" or "paraphrase"')
        elif key == "conclusionStyle" and value not in _CONCLUSION_STYLES:
            raise ContractError('conclusionStyle must be "standard" or "terse"')


def build_change_bundle(
    *, bundle_id: str, baseline: ChooseBaselineConfig, delta: dict[str, Any]
) -> ChooseChangeBundle:
    """Assemble a valid bundle from parts, computing the content digest."""
    if not isinstance(bundle_id, str) or not bundle_id:
        raise ContractError("bundleId is required")
    validate_delta(delta)
    partial = ChooseChangeBundle(
        bundle_id=bundle_id,
        baseline_config_version=baseline.config_version,
        baseline_digest=baseline.digest,
        delta=delta,
        delta_digest="",
    )
    return ChooseChangeBundle(
        bundle_id=partial.bundle_id,
        baseline_config_version=partial.baseline_config_version,
        baseline_digest=partial.baseline_digest,
        delta=partial.delta,
        delta_digest=digest_of(partial.identity()),
    )


def parse_change_bundle(raw: Any) -> ChooseChangeBundle:
    """Strict parse + digest verification of an imported bundle.

    A tampered bundle (edited delta, swapped baseline lineage) fails closed
    here, before any research code dispatches.
    """
    if not isinstance(raw, dict):
        raise ContractError(
            "change bundle must be a JSON object; the Choose application path "
            "requires the candidate delta to BE the runner's change-bundle JSON "
            "(free text is never converted into executable settings)"
        )
    if raw.get("schemaVersion") != CHANGE_BUNDLE_SCHEMA_VERSION:
        raise ContractError(
            f"unsupported change-bundle schemaVersion {raw.get('schemaVersion')!r}"
        )
    if raw.get("kind") != CHANGE_BUNDLE_KIND:
        raise ContractError(
            f"change bundle kind must be {CHANGE_BUNDLE_KIND!r}, got {raw.get('kind')!r}"
        )
    if raw.get("synthetic") is not True:
        raise ContractError("change bundles must be explicitly synthetic")
    bundle_id = raw.get("bundleId")
    if not isinstance(bundle_id, str) or not bundle_id:
        raise ContractError("bundleId must be a non-empty string")
    version = raw.get("baselineConfigVersion")
    if not isinstance(version, str) or not version:
        raise ContractError("baselineConfigVersion must be a non-empty string")
    baseline_digest = _require_digest(raw.get("baselineDigest"), "baselineDigest")
    delta = raw.get("delta")
    if not isinstance(delta, dict):
        raise ContractError("delta must be a JSON object")
    declared = _require_digest(raw.get("deltaDigest"), "deltaDigest")
    partial = ChooseChangeBundle(
        bundle_id=bundle_id,
        baseline_config_version=version,
        baseline_digest=baseline_digest,
        delta=delta,
        delta_digest="",
    )
    recomputed = digest_of(partial.identity())
    if recomputed != declared:
        raise ContractError(
            f"change bundle digest mismatch: expected {declared}, recomputed "
            f"{recomputed} (vouch/config-bundle-tampered); the sealed candidate's "
            "delta does not verify against its own declared digest"
        )
    # Digest-valid but unsupported contents still refuse before dispatch.
    validate_delta(delta)
    return ChooseChangeBundle(
        bundle_id=bundle_id,
        baseline_config_version=version,
        baseline_digest=baseline_digest,
        delta=delta,
        delta_digest=declared,
    )


def parse_candidate_delta(delta: str) -> ChooseChangeBundle:
    """Parse the sealed candidate's delta string as a verified change bundle."""
    import json

    try:
        raw = json.loads(delta)
    except json.JSONDecodeError as exc:
        raise ContractError(
            "the Choose application path requires the candidate delta to be the "
            "runner's change-bundle JSON (choose-config-change-bundle); this "
            "delta is not valid JSON, and free text is never converted into "
            f"executable settings ({exc})"
        ) from exc
    return parse_change_bundle(raw)


@dataclass(frozen=True)
class ChooseApplicationReceipt:
    """The validated application receipt bound to one attempt."""

    receipt_version: int
    requested_delta_digest: str
    applied_config_digest: str
    baseline_config_digest: str
    baseline_config_version: str
    no_op: bool
    changed_fields: tuple[str, ...]
    transport_observed: bool
    observed_config_digest: str | None
    runner_version: str
    adapter_id: str
    run_id: str
    attempt_id: str
    case_id: str
    workflow_id: str
    mode: str
    choose_run_id: str
    outcome: str | None
    supported_claim_count: int | None
    report_artifact_digest: str | None
    evidence_artifact_digests: tuple[str, ...]
    usage_artifact_digest: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "receiptVersion": self.receipt_version,
            "kind": APPLICATION_RECEIPT_KIND,
            "synthetic": True,
            "requestedDeltaDigest": self.requested_delta_digest,
            "appliedConfigDigest": self.applied_config_digest,
            "baselineConfigDigest": self.baseline_config_digest,
            "baselineConfigVersion": self.baseline_config_version,
            "noOp": self.no_op,
            "changedFields": list(self.changed_fields),
            "transportObserved": self.transport_observed,
            "observedConfigDigest": self.observed_config_digest,
            "runnerVersion": self.runner_version,
            "adapterId": self.adapter_id,
            "runId": self.run_id,
            "attemptId": self.attempt_id,
            "caseId": self.case_id,
            "workflowId": self.workflow_id,
            "mode": self.mode,
            "chooseRunId": self.choose_run_id,
            "outcome": self.outcome,
            "supportedClaimCount": self.supported_claim_count,
            "reportArtifactDigest": self.report_artifact_digest,
            "evidenceArtifactDigests": list(self.evidence_artifact_digests),
            "usageArtifactDigest": self.usage_artifact_digest,
        }


def receipt_from_payload(payload: dict[str, Any]) -> ChooseApplicationReceipt:
    """Decode the apply-config response receipt (shape-strict)."""
    raw = payload.get("receipt")
    if not isinstance(raw, dict):
        raise ContractError("apply-config response carries no application receipt")
    if raw.get("kind") != APPLICATION_RECEIPT_KIND:
        raise ContractError(
            f"application receipt kind must be {APPLICATION_RECEIPT_KIND!r}, "
            f"got {raw.get('kind')!r}"
        )
    if raw.get("receiptVersion") != APPLICATION_RECEIPT_VERSION:
        raise ContractError(
            f"unsupported application receipt version {raw.get('receiptVersion')!r}"
        )
    if raw.get("synthetic") is not True:
        raise ContractError("application receipts must be explicitly synthetic")
    try:
        return ChooseApplicationReceipt(
            receipt_version=int(raw["receiptVersion"]),
            requested_delta_digest=_require_digest(
                raw["requestedDeltaDigest"], "requestedDeltaDigest"
            ),
            applied_config_digest=_require_digest(
                raw["appliedConfigDigest"], "appliedConfigDigest"
            ),
            baseline_config_digest=_require_digest(
                raw["baselineConfigDigest"], "baselineConfigDigest"
            ),
            baseline_config_version=str(raw["baselineConfigVersion"]),
            no_op=bool(raw["noOp"]),
            changed_fields=tuple(str(field) for field in raw.get("changedFields", [])),
            transport_observed=bool(raw["transportObserved"]),
            observed_config_digest=(
                _require_digest(raw["observedConfigDigest"], "observedConfigDigest")
                if raw.get("observedConfigDigest") is not None
                else None
            ),
            runner_version=str(raw["runnerVersion"]),
            adapter_id=str(raw["adapterId"]),
            run_id=str(raw["runId"]),
            attempt_id=str(raw["attemptId"]),
            case_id=str(raw["caseId"]),
            workflow_id=str(raw["workflowId"]),
            mode=str(raw["mode"]),
            choose_run_id=str(raw["chooseRunId"]),
            outcome=(str(raw["outcome"]) if raw.get("outcome") is not None else None),
            supported_claim_count=(
                int(raw["supportedClaimCount"])
                if raw.get("supportedClaimCount") is not None
                else None
            ),
            report_artifact_digest=(
                _require_digest(raw["reportArtifactDigest"], "reportArtifactDigest")
                if raw.get("reportArtifactDigest") is not None
                else None
            ),
            evidence_artifact_digests=tuple(
                _require_digest(digest, "evidenceArtifactDigests[*]")
                for digest in raw.get("evidenceArtifactDigests", [])
            ),
            usage_artifact_digest=(
                _require_digest(raw["usageArtifactDigest"], "usageArtifactDigest")
                if raw.get("usageArtifactDigest") is not None
                else None
            ),
        )
    except KeyError as exc:
        raise ContractError(f"application receipt is missing field {exc}") from exc


def validate_receipt(
    receipt: ChooseApplicationReceipt,
    *,
    bundle: ChooseChangeBundle,
    baseline: ChooseBaselineConfig,
    run_id: str,
    attempt_id: str,
    case_id: str,
    workflow_id: str,
    mode: str,
    runner_version: str,
) -> None:
    """Bind the receipt to THIS attempt and the frozen baseline.

    Missing, tampered, stale or unrelated receipts cannot promote a
    candidate: every binding below must hold exactly.
    """
    problems: list[str] = []
    if receipt.requested_delta_digest != bundle.delta_digest:
        problems.append(
            f"requestedDeltaDigest {receipt.requested_delta_digest} != the sent "
            f"bundle's digest {bundle.delta_digest}"
        )
    if receipt.baseline_config_digest != baseline.digest:
        problems.append(
            f"baselineConfigDigest {receipt.baseline_config_digest} != the runner's "
            f"advertised baseline {baseline.digest}"
        )
    if receipt.baseline_config_version != baseline.config_version:
        problems.append(
            f"baselineConfigVersion {receipt.baseline_config_version!r} != "
            f"{baseline.config_version!r}"
        )
    if receipt.run_id != run_id:
        problems.append(f"runId {receipt.run_id!r} != {run_id!r}")
    if receipt.attempt_id != attempt_id:
        problems.append(f"attemptId {receipt.attempt_id!r} != {attempt_id!r}")
    if receipt.case_id != case_id:
        problems.append(f"caseId {receipt.case_id!r} != {case_id!r}")
    if receipt.workflow_id != workflow_id:
        problems.append(f"workflowId {receipt.workflow_id!r} != {workflow_id!r}")
    if receipt.mode != mode:
        problems.append(f"mode {receipt.mode!r} != {mode!r}")
    if receipt.runner_version != runner_version:
        problems.append(
            f"runnerVersion {receipt.runner_version!r} != the response's "
            f"{runner_version!r}"
        )
    if not receipt.transport_observed:
        problems.append("transportObserved is false: the applied configuration never "
                        "demonstrably reached the model transport")
    if receipt.observed_config_digest != receipt.applied_config_digest:
        problems.append(
            f"observedConfigDigest {receipt.observed_config_digest} != appliedConfigDigest "
            f"{receipt.applied_config_digest}: what the transport observed is not what "
            "was applied"
        )
    if receipt.no_op and receipt.changed_fields:
        problems.append("noOp is true but changedFields is non-empty")
    if problems:
        raise ContractError(
            "application receipt failed validation for attempt "
            f"{attempt_id!r}; the attempt cannot count as applied evidence: "
            + "; ".join(problems)
        )


def baseline_noop_bundle(baseline: ChooseBaselineConfig) -> ChooseChangeBundle:
    """The explicit baseline-side bundle: an empty delta, reported as a no-op.

    Baseline and candidate attempts both travel the SAME application path —
    they differ only in the delta — so a paired comparison's differences come
    from the applied configuration alone.
    """
    return build_change_bundle(
        bundle_id=f"vouch-baseline-noop@{baseline.config_version}",
        baseline=baseline,
        delta={},
    )


__all__ = [
    "ALLOWED_DELTA_FIELDS",
    "APPLICATION_RECEIPT_KIND",
    "APPLICATION_RECEIPT_VERSION",
    "CHANGE_BUNDLE_KIND",
    "CHANGE_BUNDLE_SCHEMA_VERSION",
    "ChooseApplicationReceipt",
    "ChooseBaselineConfig",
    "ChooseChangeBundle",
    "baseline_noop_bundle",
    "build_change_bundle",
    "canonical_json",
    "parse_candidate_delta",
    "parse_change_bundle",
    "receipt_from_payload",
    "validate_delta",
    "validate_receipt",
]
