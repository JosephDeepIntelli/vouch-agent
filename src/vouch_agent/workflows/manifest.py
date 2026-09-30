"""WorkflowManifest — the versioned registry of Choose workflows (design §3.1).

Scope note (honesty): the workflow identifiers W-C1..W-C9 are assigned here
by splitting the design doc's §3.1 eight-row workflow table — its last row
("异常与产品控制") covers both exception journeys and product-control
boundaries, and the paragraph after the table explicitly moves login,
payment, account and browser-storage behavior into product-level regression
guardrails. Delivery-plan §5 references the same eight-row list and does not
define literal W-C* ids; this mapping is the canonical assignment for Vouch
and every status below is traceable to it.

Coverage honesty rules (design §3.1 / milestone 4):

* A manifest entry existing is *declaration*, not integration. Statuses are
  ``declared`` < ``fixture-covered`` < ``runner-integrated``.
* ``fixture-covered`` proves the Vouch pipeline on SYNTHETIC journeys only —
  never model or product improvement.
* ``runner-integrated`` requires a real, product-owned runner and **cannot
  be reached from fixture data alone**: the only path is an attested
  :class:`RunnerIntegrationRecord` (controller-side code with a real
  describe() handshake), and fixture-shaped runner ids are rejected
  structurally.
* Access/payment+credits/security/storage are regression guardrail families:
  they are never autonomous optimization targets, so they are excluded from
  ``optimization_target_ids()`` by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import ContractRecord, digest_of, require_digest, require_str
from vouch_agent.errors import ContractError

#: Runner ids that only replay synthetic fixtures. Attesting one of these as
#: a "real runner" integration is structurally impossible.
FIXTURE_RUNNER_IDS = frozenset({"fixture-choose-v1"})
_FIXTURE_RUNNER_PREFIX = "fixture"


class CoverageLayer(StrEnum):
    """The EVIDENCE LAYER actually verified for a workflow (B2: coverage and
    execution mode are represented separately — real Choose logic can be
    connected while providers remain deterministic fixtures)."""

    LOGIC = "logic"  # real product code, deterministic providers, no browser
    BROWSER = "browser"  # Choose-owned Playwright journey evidence
    CONTROL = "control"  # product regression suites (access/payments/security/storage)


class CoverageStatus(StrEnum):
    DECLARED = "declared"
    FIXTURE_COVERED = "fixture-covered"
    RUNNER_INTEGRATED = "runner-integrated"


@dataclass(frozen=True)
class WorkflowSpec:
    """A declared workflow: what evidence a real integration must produce."""

    workflow_id: str
    name: str
    required_evidence: tuple[str, ...]
    regression_only: bool = False
    notes: str = ""


#: The nine declared Choose workflows (design §3.1; see module docstring).
DECLARED_CHOOSE_WORKFLOWS: tuple[WorkflowSpec, ...] = (
    WorkflowSpec(
        workflow_id="W-C1",
        name="Requirement preparation and clarification",
        required_evidence=(
            "ambiguous budget currency and purchasing market explicitly confirmed",
            "draft recovery after interruption returns to the reached preparation step",
            "preparation never triggers paid research",
        ),
        notes="Market is asked inline and never inferred from locale/IP.",
    ),
    WorkflowSpec(
        workflow_id="W-C2",
        name="Find shortlist",
        required_evidence=(
            "every mandatory condition carries a meets/fails/unknown verdict",
            "shortlist entries match source, model and market",
            "no unsupported claims; rejected options cite their failing condition",
            "valid candidate coverage (eligible before provisional)",
        ),
    ),
    WorkflowSpec(
        workflow_id="W-C3",
        name="Compare",
        required_evidence=(
            "every requested candidate appears exactly once, in input order",
            "comparable conditions with citation support",
            "missing information expressed as unknown, never fabricated",
            "blocked sources leave placeholders without substitution",
        ),
    ),
    WorkflowSpec(
        workflow_id="W-C4",
        name="Setup plan",
        required_evidence=(
            "connection support carries a sourced or rule basis",
            "no omitted roles and no duplicated items",
            "whole-set budget verdict with named unknown fees and currency",
            "optional extras and buying order present",
        ),
    ),
    WorkflowSpec(
        workflow_id="W-C5",
        name="Follow-up and revision",
        required_evidence=(
            "new version linked to its parent result and parent input digest",
            "failed revision preserves the prior answer",
            "task input versioned, not silently overwritten",
        ),
    ),
    WorkflowSpec(
        workflow_id="W-C6",
        name="Cross-task handoff",
        required_evidence=(
            "Find selection prefills Compare exactly",
            "Compare rejects become Find exclusions (instructions, not examined options)",
            "old-result permissions are never inherited by the new task",
        ),
    ),
    WorkflowSpec(
        workflow_id="W-C7",
        name="Save, reopen and resume",
        required_evidence=(
            "results keep their own run capability after reopen",
            "storage failure is visible and exportable, never silent loss",
            "resume continues from the persisted checkpoint",
            "locale switch preserves the original report language",
        ),
    ),
    WorkflowSpec(
        workflow_id="W-C8",
        name="Exception journeys",
        required_evidence=(
            "insufficient evidence yields an honest unbilled partial",
            "timeout/cancel stops descendant work and releases holds",
            "budget exhaustion saves a limited answer without endless resume",
            "partial/unknown is never presented as completed",
        ),
    ),
    WorkflowSpec(
        workflow_id="W-C9",
        name="Product-control boundaries",
        required_evidence=(
            "login, credits and payment boundaries do not regress",
            "idempotency and market limits hold under replay",
            "browser-storage failure stays visible and bounded",
        ),
        regression_only=True,
        notes="Regression guardrail workflows: verified by guardrail families below.",
    ),
)

#: The four regression guardrail families (regression-only, never optimization targets).
DECLARED_GUARDRAIL_FAMILIES: tuple[WorkflowSpec, ...] = (
    WorkflowSpec(
        workflow_id="RG-ACCESS",
        name="Access and account boundaries",
        required_evidence=("auth gating, account linking and permission checks do not regress",),
        regression_only=True,
    ),
    WorkflowSpec(
        workflow_id="RG-PAYMENTS-CREDITS",
        name="Payment and credit invariants",
        required_evidence=(
            "holds released on failure; idempotent settlement; no debit for unbilled outcomes",
        ),
        regression_only=True,
    ),
    WorkflowSpec(
        workflow_id="RG-SECURITY",
        name="Security and capability isolation",
        required_evidence=(
            "run capabilities, public-source validation and media transport checks do not regress",
        ),
        regression_only=True,
    ),
    WorkflowSpec(
        workflow_id="RG-STORAGE",
        name="Browser storage integrity",
        required_evidence=(
            "session/draft persistence bounds and failure visibility do not regress",
        ),
        regression_only=True,
    ),
)

_GUARDRAIL_PARENT = "W-C9"


@dataclass(frozen=True)
class WorkflowEntry(ContractRecord):
    workflow_id: str
    name: str
    status: CoverageStatus
    family: str = "journey"  # journey | guardrail
    required_evidence: tuple[str, ...] = ()
    regression_only: bool = False
    locales: tuple[str, ...] = ()
    markets: tuple[str, ...] = ()
    notes: str = ""
    fixture_ids: tuple[str, ...] = ()
    # B2: what layer of evidence exists and under which provider mode —
    # coverage status and execution mode are separate facts.
    coverage_layer: str | None = None  # logic | browser | control
    provider_mode: str | None = None  # fixture | offline-evaluation | authorized-live
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "workflowId": self.workflow_id,
            "name": self.name,
            "status": self.status.value,
            "family": self.family,
            "requiredEvidence": list(self.required_evidence),
            "regressionOnly": self.regression_only,
            "locales": list(self.locales),
            "markets": list(self.markets),
            "notes": self.notes,
            "fixtureIds": list(self.fixture_ids),
            "coverageLayer": self.coverage_layer,
            "providerMode": self.provider_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowEntry:
        cls._check_version(data)
        return cls(
            workflow_id=require_str(data["workflowId"], "workflowId"),
            name=require_str(data["name"], "name"),
            status=CoverageStatus(data["status"]),
            family=require_str(data.get("family", "journey"), "family"),
            required_evidence=tuple(data.get("requiredEvidence", [])),
            regression_only=bool(data.get("regressionOnly", False)),
            locales=tuple(data.get("locales", [])),
            markets=tuple(data.get("markets", [])),
            notes=str(data.get("notes", "")),
            fixture_ids=tuple(data.get("fixtureIds", [])),
            coverage_layer=data.get("coverageLayer"),
            provider_mode=data.get("providerMode"),
        )


@dataclass(frozen=True)
class RunnerIntegrationRecord(ContractRecord):
    """Attestation that a workflow runs against a real product-owned runner.

    This is the *only* way ``runner-integrated`` can be reached. The record
    itself is a CLAIM: fixture-shaped runner ids are rejected structurally,
    every digest must be well-formed, a human verifier must be named — but
    since M4 A2 the record only promotes through
    :meth:`WorkflowManifest.with_runner_integration` /
    :meth:`WorkflowManifest.with_regression_coverage`, which resolve the
    durable evidence (``evaluation.attestation_evidence``) against the actual
    stores and STAMP ``evidence_digest`` with the digest of the evidence
    document that resolved. Hand-invented run ids, contradictory
    revision/mode/layer claims and failed layer runs can never resolve.
    """

    workflow_id: str
    runner_id: str
    runner_descriptor_digest: str
    verified_by: str
    statement: str
    # v2 (coordinator review B2): a name + statement no longer promotes
    # coverage — the record must cite controller-observed runner revision,
    # the case pack it ran, and DURABLE execution evidence (run id + attempt
    # digests), plus the evidence layer and provider mode actually used.
    runner_revision: str
    case_pack_digest: str
    coverage_layer: CoverageLayer
    provider_mode: str = "fixture"
    verified_run_id: str = ""
    verified_attempt_digests: tuple[str, ...] = ()
    evidence_digest: str = ""
    verified_at: str = ""
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.runner_id in FIXTURE_RUNNER_IDS or self.runner_id.startswith(
            _FIXTURE_RUNNER_PREFIX
        ):
            raise ContractError(
                f"runner {self.runner_id!r} is a fixture runner; fixture coverage can never "
                "be recorded as runner-integrated"
            )
        require_digest(self.runner_descriptor_digest, "runnerDescriptorDigest")
        require_str(self.verified_by, "verifiedBy")
        require_str(self.statement, "statement")
        require_str(self.runner_revision, "runnerRevision")
        require_digest(self.case_pack_digest, "casePackDigest")
        require_str(self.verified_run_id, "verifiedRunId")
        if not self.verified_attempt_digests:
            raise ContractError(
                "runner integration requires durable execution evidence: at least one "
                "verified attempt digest"
            )
        for digest in self.verified_attempt_digests:
            require_digest(digest, "verifiedAttemptDigests[*]")
        require_digest(self.evidence_digest, "evidenceDigest")
        if self.provider_mode not in ("fixture", "offline-evaluation", "authorized-live"):
            raise ContractError(f"unknown provider mode {self.provider_mode!r}")
        declared = {s.workflow_id for s in DECLARED_CHOOSE_WORKFLOWS} | {
            g.workflow_id for g in DECLARED_GUARDRAIL_FAMILIES
        }
        if self.workflow_id not in declared:
            raise ContractError(f"unknown workflow {self.workflow_id!r} for runner integration")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "workflowId": self.workflow_id,
            "runnerId": self.runner_id,
            "runnerDescriptorDigest": self.runner_descriptor_digest,
            "verifiedBy": self.verified_by,
            "statement": self.statement,
            "runnerRevision": self.runner_revision,
            "casePackDigest": self.case_pack_digest,
            "coverageLayer": self.coverage_layer.value,
            "providerMode": self.provider_mode,
            "verifiedRunId": self.verified_run_id,
            "verifiedAttemptDigests": list(self.verified_attempt_digests),
            "evidenceDigest": self.evidence_digest,
            "verifiedAt": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunnerIntegrationRecord:
        cls._check_version(data)
        return cls(
            workflow_id=require_str(data["workflowId"], "workflowId"),
            runner_id=require_str(data["runnerId"], "runnerId"),
            runner_descriptor_digest=require_digest(
                data["runnerDescriptorDigest"], "runnerDescriptorDigest"
            ),
            verified_by=require_str(data["verifiedBy"], "verifiedBy"),
            statement=require_str(data["statement"], "statement"),
            runner_revision=require_str(data.get("runnerRevision", ""), "runnerRevision"),
            case_pack_digest=require_digest(data.get("casePackDigest", ""), "casePackDigest"),
            coverage_layer=CoverageLayer(data.get("coverageLayer", "")),
            provider_mode=str(data.get("providerMode", "fixture")),
            verified_run_id=require_str(data.get("verifiedRunId", ""), "verifiedRunId"),
            verified_attempt_digests=tuple(
                require_digest(d, "verifiedAttemptDigests[*]")
                for d in data.get("verifiedAttemptDigests", [])
            ),
            evidence_digest=require_digest(data.get("evidenceDigest", ""), "evidenceDigest"),
            verified_at=str(data.get("verifiedAt", "")),
        )


@dataclass(frozen=True)
class WorkflowManifest(ContractRecord):
    """Versioned, content-addressed workflow coverage registry.

    Build from a fixture pack via :meth:`from_fixture_pack` (statuses cap at
    ``fixture-covered``), then promote workflows with
    :meth:`with_runner_integration` only after a real runner handshake.
    """

    entries: tuple[WorkflowEntry, ...]
    guardrails: tuple[WorkflowEntry, ...]
    fixture_pack_digest: str | None = None
    runner_integrations: tuple[RunnerIntegrationRecord, ...] = ()
    # Workflow ids whose runner-integration records were RESOLVED against
    # durable evidence: at promotion time (evidence was controller-observed)
    # or via verify_integrations() after a load. A freshly DESERIALIZED
    # manifest has none — imported records are unverified claims (M3 A5).
    verified_integrations: frozenset[str] = frozenset()
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "fixturePackDigest": self.fixture_pack_digest,
            "entries": [e.to_dict() for e in self.entries],
            "guardrails": [g.to_dict() for g in self.guardrails],
            "runnerIntegrations": [r.to_dict() for r in self.runner_integrations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowManifest:
        cls._check_version(data)
        manifest = cls(
            entries=tuple(WorkflowEntry.from_dict(e) for e in data.get("entries", [])),
            guardrails=tuple(WorkflowEntry.from_dict(g) for g in data.get("guardrails", [])),
            fixture_pack_digest=data.get("fixturePackDigest"),
            runner_integrations=tuple(
                RunnerIntegrationRecord.from_dict(r) for r in data.get("runnerIntegrations", [])
            ),
        )
        manifest._validate_integrations()
        return manifest

    # -- queries ---------------------------------------------------------------

    def entry(self, workflow_id: str) -> WorkflowEntry:
        for candidate in self.entries:
            if candidate.workflow_id == workflow_id:
                return candidate
        raise ContractError(f"workflow {workflow_id!r} not in manifest")

    def guardrail(self, family_id: str) -> WorkflowEntry:
        for candidate in self.guardrails:
            if candidate.workflow_id == family_id:
                return candidate
        raise ContractError(f"guardrail family {family_id!r} not in manifest")

    def effective_status(self, workflow_id: str) -> str:
        """The status that may be ACTED upon.

        A runner-integrated entry whose record has not been resolved against
        durable evidence (a freshly deserialized manifest) reports
        ``unverified-integration``: imported claims are not coverage until
        :meth:`verify_integrations` resolves them (M3 A5).
        """
        entry = (
            self.entry(workflow_id)
            if any(e.workflow_id == workflow_id for e in self.entries)
            else self.guardrail(workflow_id)
        )
        if (
            entry.status is CoverageStatus.RUNNER_INTEGRATED
            and workflow_id not in self.verified_integrations
        ):
            return "unverified-integration"
        return entry.status.value

    def verify_integrations(
        self, store: Any, artifacts: Any, observed_descriptor: Any
    ) -> WorkflowManifest:
        """Resolve EVERY imported integration record against durable state.

        Uses the acceptance resolver (``evaluation.attestation_evidence``):
        for each record, rebuild the claim, resolve the OBSERVED evidence
        (durable run/attempt/artifact records, descriptor, mode, layer), and
        require agreement with the record. Agreement marks the integration
        verified; disagreement refuses. Returns a new manifest with the
        verified set updated.

        M4 A2: there is deliberately NO skip for already-verified workflows —
        an in-memory flag never bypasses revalidation. Every call re-resolves
        every record against the durable store, so changed, corrupted or
        removed evidence downgrades or refuses even for a manifest that was
        verified earlier in the same process.
        """
        verified: set[str] = set()
        for record in self.runner_integrations:
            resolved = _resolve_record(record, store, artifacts, observed_descriptor)
            expected_digest = digest_of(resolved.to_dict())
            if record.evidence_digest != expected_digest:
                raise ContractError(
                    f"{record.workflow_id} integration record binds evidence digest "
                    f"{record.evidence_digest} but the durable evidence resolves to "
                    f"{expected_digest}; the record and the evidence disagree — "
                    "re-resolve before trusting this claim"
                )
            verified.add(record.workflow_id)
        return replace(self, verified_integrations=frozenset(verified))

    def coverage_summary(self) -> dict[str, list[str]]:
        """EFFECTIVE coverage summary — the only view consumers may act on.

        Reports :meth:`effective_status` per workflow: a ``runner-integrated``
        entry whose integration record has not been RESOLVED against durable
        evidence in this process (a freshly deserialized manifest) counts
        under ``unverified-integration``, never under ``runner-integrated``
        (M4 A2: the persisted raw status is a claim, not coverage).
        """
        summary: dict[str, list[str]] = {status.value: [] for status in CoverageStatus}
        summary["unverified-integration"] = []
        for entry in (*self.entries, *self.guardrails):
            summary[self.effective_status(entry.workflow_id)].append(entry.workflow_id)
        return summary

    def coverage_dimensions(self) -> dict[str, dict[str, str | None]]:
        """Per workflow: effective status plus the SEPARATE evidence-layer and
        provider-mode dimensions (B2/M4 A2).

        ``layer``/``mode`` are the dimensions of the integration RECORD (what
        layer was actually verified, under which provider mode) — ``None``
        when no record exists. They are reported independently of status so a
        logic/browser/control claim can never be inferred from one another.
        """
        dimensions: dict[str, dict[str, str | None]] = {}
        for entry in (*self.entries, *self.guardrails):
            record = next(
                (r for r in self.runner_integrations if r.workflow_id == entry.workflow_id),
                None,
            )
            dimensions[entry.workflow_id] = {
                "status": self.effective_status(entry.workflow_id),
                "layer": record.coverage_layer.value if record is not None else None,
                "mode": record.provider_mode if record is not None else None,
            }
        return dimensions

    def optimization_target_ids(self) -> tuple[str, ...]:
        """Workflow ids a candidate proposal may target — guardrails never qualify."""
        return tuple(
            e.workflow_id for e in self.entries if e.family == "journey" and not e.regression_only
        )

    def assert_declared_complete(self) -> None:
        declared = {s.workflow_id for s in DECLARED_CHOOSE_WORKFLOWS}
        present = {e.workflow_id for e in self.entries}
        missing = declared - present
        if missing:
            raise ContractError(f"manifest missing declared workflows: {sorted(missing)}")
        guard_declared = {s.workflow_id for s in DECLARED_GUARDRAIL_FAMILIES}
        guard_present = {g.workflow_id for g in self.guardrails}
        guard_missing = guard_declared - guard_present
        if guard_missing:
            raise ContractError(f"manifest missing guardrail families: {sorted(guard_missing)}")

    # -- construction from fixture packs ----------------------------------------

    @classmethod
    def from_fixture_pack(cls, pack: Any) -> WorkflowManifest:
        """Build a manifest from a loaded fixture pack.

        Statuses derived from fixture data can only be ``declared`` or
        ``fixture-covered`` — this method has no code path that produces
        ``runner-integrated``.
        """
        entries: list[WorkflowEntry] = []
        for spec in DECLARED_CHOOSE_WORKFLOWS:
            coverage = pack.workflow_coverage.get(spec.workflow_id, {})
            fixture_ids = tuple(coverage.get("fixtureIds", ()))
            status = CoverageStatus.FIXTURE_COVERED if fixture_ids else CoverageStatus.DECLARED
            entries.append(
                WorkflowEntry(
                    workflow_id=spec.workflow_id,
                    name=spec.name,
                    status=status,
                    required_evidence=spec.required_evidence,
                    regression_only=spec.regression_only,
                    locales=tuple(coverage.get("locales", ())),
                    markets=tuple(coverage.get("markets", ())),
                    notes=f"{spec.notes} {coverage.get('notes', '')}".strip(),
                    fixture_ids=fixture_ids,
                )
            )
        guardrails: list[WorkflowEntry] = []
        for family in pack.guardrail_families:
            family_spec: WorkflowSpec | None = next(
                (s for s in DECLARED_GUARDRAIL_FAMILIES if s.workflow_id == family.family_id),
                None,
            )
            if family_spec is None:
                raise ContractError(f"unknown guardrail family {family.family_id!r} in pack")
            guardrails.append(
                WorkflowEntry(
                    workflow_id=family.family_id,
                    name=family.name,
                    status=CoverageStatus.FIXTURE_COVERED
                    if family.fixture_ids
                    else CoverageStatus.DECLARED,
                    family="guardrail",
                    required_evidence=family_spec.required_evidence,
                    regression_only=True,
                    notes=f"{family.notes} (regression-only, parent workflow {_GUARDRAIL_PARENT})",
                    fixture_ids=family.fixture_ids,
                )
            )
        missing_families = {s.workflow_id for s in DECLARED_GUARDRAIL_FAMILIES} - {
            f.family_id for f in pack.guardrail_families
        }
        if missing_families:
            raise ContractError(
                f"fixture pack missing guardrail families: {sorted(missing_families)}"
            )
        return cls(
            entries=tuple(entries),
            guardrails=tuple(guardrails),
            fixture_pack_digest=cls._pack_digest(pack),
        )

    @staticmethod
    def _pack_digest(pack: Any) -> str:
        normalized = {
            "packId": pack.pack_id,
            "workflowCoverage": {
                wid: {"fixtureIds": list(entry.get("fixtureIds", []))}
                for wid, entry in pack.workflow_coverage.items()
            },
            "guardrailFamilies": [
                {"familyId": f.family_id, "fixtureIds": list(f.fixture_ids)}
                for f in pack.guardrail_families
            ],
        }
        return digest_of(normalized)

    # -- runner integration (the only promotion path) ----------------------------

    def with_runner_integration(
        self,
        record: RunnerIntegrationRecord,
        *,
        store: Any,
        artifacts: Any,
        observed_descriptor: Any,
    ) -> WorkflowManifest:
        """Promote one OPTIMIZATION-TARGET workflow to ``runner-integrated``.

        M4 A2 — one promotion authority: the ONLY way a record promotes is by
        resolving the durable evidence through
        ``evaluation.attestation_evidence.resolve_attestation_evidence`` against
        the actual stores (run/attempt/artifact records, the observed
        describe() descriptor, validated browser/control layer records) and
        requiring the record to state exactly what that evidence supports.
        Two caller-constructed records that merely AGREE with each other prove
        nothing — the in-memory ``evidence`` cross-check of M3 is gone because
        it was itself caller-constructible.
        """
        target = self.entry(record.workflow_id)
        if target.regression_only:
            raise ContractError(
                f"{record.workflow_id} is a regression-only workflow; use "
                "with_regression_coverage to record verified regression evidence"
            )
        resolved = _resolve_record(record, store, artifacts, observed_descriptor)
        return self._record_integration(record, resolved)

    def with_regression_coverage(
        self,
        record: RunnerIntegrationRecord,
        *,
        store: Any,
        artifacts: Any,
        observed_descriptor: Any,
    ) -> WorkflowManifest:
        """Record VERIFIED regression coverage for a guardrail family or a
        regression-only journey (W-C9). The entry is marked runner-integrated
        WITH its evidence layer, but stays permanently excluded from
        optimization targets (B2).

        M4 A2: exactly like :meth:`with_runner_integration`, promotion resolves
        durable evidence through the attestation resolver — the claim's
        revision/run/mode/layer must all be supported by the store, and a
        contradictory record refuses instead of promoting.
        """
        is_guardrail = any(g.workflow_id == record.workflow_id for g in self.guardrails)
        target = (
            self.guardrail(record.workflow_id) if is_guardrail else self.entry(record.workflow_id)
        )
        if not is_guardrail and not target.regression_only:
            raise ContractError(
                f"{record.workflow_id} is an optimization target; use with_runner_integration"
            )
        if record.coverage_layer is not CoverageLayer.CONTROL and is_guardrail:
            raise ContractError("guardrail families record CONTROL-layer regression evidence only")
        resolved = _resolve_record(record, store, artifacts, observed_descriptor)
        return self._record_integration(record, resolved)

    def _record_integration(
        self, record: RunnerIntegrationRecord, resolved: Any
    ) -> WorkflowManifest:
        if any(r.workflow_id == record.workflow_id for r in self.runner_integrations):
            raise ContractError(f"{record.workflow_id} already has a runner integration record")

        # The stored record binds the digest of the evidence document that was
        # actually resolved — the caller's declared digest is overwritten, so a
        # later verify_integrations() can detect a record edited after the fact.
        bound = replace(record, evidence_digest=digest_of(resolved.to_dict()))

        def stamped(entry: WorkflowEntry) -> WorkflowEntry:
            if entry.workflow_id != record.workflow_id:
                return entry
            return WorkflowEntry(
                workflow_id=entry.workflow_id,
                name=entry.name,
                status=CoverageStatus.RUNNER_INTEGRATED,
                family=entry.family,
                required_evidence=entry.required_evidence,
                regression_only=entry.regression_only,
                locales=entry.locales,
                markets=entry.markets,
                notes=entry.notes,
                fixture_ids=entry.fixture_ids,
                coverage_layer=record.coverage_layer.value,
                provider_mode=record.provider_mode,
            )

        updated = tuple(stamped(e) for e in self.entries)
        updated_guardrails = tuple(stamped(g) for g in self.guardrails)
        manifest = WorkflowManifest(
            entries=updated,
            guardrails=updated_guardrails,
            fixture_pack_digest=self.fixture_pack_digest,
            runner_integrations=(*self.runner_integrations, bound),
            verified_integrations=self.verified_integrations | {record.workflow_id},
        )
        manifest._validate_integrations()
        return manifest

    def _validate_integrations(self) -> None:
        """Every ``runner-integrated`` entry must have a valid evidence-bound
        attestation whose layer/mode match the entry stamp.

        This runs on every load (``from_dict``) too: a hand-edited manifest
        claiming integration without a real attestation fails closed.
        """
        attested = {r.workflow_id: r for r in self.runner_integrations}
        for entry in (*self.entries, *self.guardrails):
            if entry.status is not CoverageStatus.RUNNER_INTEGRATED:
                continue
            record = attested.get(entry.workflow_id)
            if record is None:
                raise ContractError(
                    f"{entry.workflow_id} claims runner-integrated without a valid "
                    "RunnerIntegrationRecord; manifest completeness is not integration"
                )
            if (
                entry.coverage_layer != record.coverage_layer.value
                or entry.provider_mode != record.provider_mode
            ):
                raise ContractError(
                    f"{entry.workflow_id} coverage layer/provider mode disagree with its "
                    "attestation record"
                )


def _resolve_record(
    record: RunnerIntegrationRecord,
    store: Any,
    artifacts: Any,
    observed_descriptor: Any,
) -> Any:
    """Resolve one integration record against durable evidence (the ONE
    promotion authority, M4 A2).

    Builds the :class:`~vouch_agent.evaluation.attestation_evidence.AttestationClaim`
    the record asserts and resolves it through
    :func:`~vouch_agent.evaluation.attestation_evidence.resolve_attestation_evidence`
    against the actual stores; the record must state exactly what the durable
    evidence supports. Raises on any disagreement — invented records, missing
    durable state, contradictory mode/revision/layer, or layer records that
    fail the durable-record contract.
    """
    from vouch_agent.evaluation.attestation_evidence import (
        AttestationClaim,
        resolve_attestation_evidence,
    )

    claim = AttestationClaim(
        workflow_id=record.workflow_id,
        runner_id=record.runner_id,
        runner_descriptor_digest=record.runner_descriptor_digest,
        runner_revision=record.runner_revision,
        case_pack_digest=record.case_pack_digest,
        coverage_layer=record.coverage_layer.value,
        provider_mode=record.provider_mode,
        verified_run_id=record.verified_run_id,
        verified_attempt_digests=tuple(record.verified_attempt_digests),
    )
    resolved = resolve_attestation_evidence(
        claim, store=store, artifacts=artifacts, observed_descriptor=observed_descriptor
    )
    resolved.assert_agrees(record.to_dict())
    return resolved


__all__ = [
    "DECLARED_CHOOSE_WORKFLOWS",
    "DECLARED_GUARDRAIL_FAMILIES",
    "FIXTURE_RUNNER_IDS",
    "CoverageLayer",
    "CoverageStatus",
    "RunnerIntegrationRecord",
    "WorkflowEntry",
    "WorkflowManifest",
    "WorkflowSpec",
]
