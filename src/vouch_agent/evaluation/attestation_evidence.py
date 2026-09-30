"""Attestation evidence authority (M3 Gate A5).

A :class:`~vouch_agent.workflows.manifest.RunnerIntegrationRecord` used to be
constructible from invented fields alone: nonexistent run ids, made-up attempt
digests, an asserted coverage layer and provider mode. Syntax checks are not
authority — a claim that names evidence must be resolved AGAINST that evidence
before it can promote a workflow's coverage.

This module resolves OBSERVED evidence for a promotion request. "Observed"
means every fact is read back out of durable storage and re-derived:

* the run and attempt records exist in the metadata store and the claimed
  attempt digests match the digests of those durable records;
* the sealed output artifact of every verified attempt exists and still
  digests to its name, and the provider mode / runner revision the attestation
  claims are the ones the adapter actually REPORTED in those recorded
  exchanges (``mode`` / ``runnerVersion`` sealed with the outputs);
* the case-pack digest is the one the durable run actually executed
  (``run.case_set_digest``) and a pack with that digest is still stored;
* the runner descriptor digest is recomputed from a descriptor the trusted
  side actually obtained (``describe()``), not from a string in the claim;
* the coverage layer is derivable from observed records: ``logic`` from the
  verified run attempts themselves, ``browser``/``control`` only from their
  OWN observed records of that layer. A logic-layer run never satisfies them.
  Since M4 A2 a matching workflow/pack/mode IDENTITY alone is not enough:
  the durable browser/control record must satisfy the published contract
  below, or it is rejected (and a FAILED run is reported as observed
  execution that is NOT passed coverage — never as coverage, never as
  "nothing ran").

**Durable browser/control record contract (schema 1).** Stored under kinds
``browser-evidence`` / ``control-regression``; validated by
:func:`resolve_attestation_evidence`:

* ``schemaVersion``: ``"1"``; ``workflowId``/``casePackDigest``/``providerMode``
  must match the claim exactly;
* ``sourceRevision``: the TESTED source identity — non-empty and equal to the
  runner revision the claim's recorded exchanges verified;
* ``status``: ``passed`` | ``failed`` (record level);
* ``journeys``: non-empty list of ``{journeyId, locale, viewport, status,
  caseIds[], screenshotDigests[]}`` — ``locale``/``viewport`` and full
  locale x viewport matrix closure are required for BROWSER records;
* ``artifactDigests``: content-addressed artifacts; every cited digest
  (record-level and per-journey screenshots) must resolve in the artifact
  store and still digest to its name;
* passed journeys' ``caseIds`` must cover the case ids of the attempts the
  claim cites (requested cases actually succeeded).

Honesty notes (kept from the review): local identity labels are
accountability, not authentication of an external principal; nothing here
executes a runner or performs a live call — it only decides whether durable
evidence supports a claim. Records are treated as UNVERIFIED CLAIMS until
resolved.

The lead wires ``resolve_attestation_evidence`` into
``workflows/manifest.py`` (the promotion path); this module deliberately does
not import ``workflows`` so that wiring cannot cycle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from vouch_agent.adapters.base import AdapterDescriptor
from vouch_agent.contracts.common import digest_of, require_digest, require_str
from vouch_agent.contracts.evaluation import AttemptRecord, EvaluationRun
from vouch_agent.errors import ContractError, DigestMismatchError, VouchError
from vouch_agent.storage.interfaces import ArtifactStore, MetadataStore

#: Storage kinds the resolver reads (mirrors the controller's persistence).
KIND_EVALUATION = "evaluation"
KIND_ATTEMPT = "attempt"
KIND_TASK_PACK = "task-pack"
#: Observed browser-evidence records (Choose browser exporter, Workstream C).
KIND_BROWSER_EVIDENCE = "browser-evidence"
#: Observed product-control regression records (W-C9 / RG-* families).
KIND_CONTROL_REGRESSION = "control-regression"

#: Coverage layers, spelled exactly like ``workflows.CoverageLayer`` values
#: (importing the enum here would cycle once the manifest wires this module).
LAYER_LOGIC = "logic"
LAYER_BROWSER = "browser"
LAYER_CONTROL = "control"
_LAYERS = (LAYER_LOGIC, LAYER_BROWSER, LAYER_CONTROL)

#: The required journey matrix for BROWSER evidence (M4-D): before a record
#: may claim browser scope, every journey it exercised must have run in BOTH
#: locales on BOTH viewports — a single passing desktop/en case never claims
#: the whole journey scope.
REQUIRED_LOCALES: tuple[str, ...] = ("en", "zh")
REQUIRED_VIEWPORTS: tuple[str, ...] = ("desktop", "mobile")

_LAYER_RECORD_SCHEMA_VERSION = "1"
_JOURNEY_STATUSES = ("passed", "failed")

#: Provider modes, spelled exactly like ``contracts.common.RunMode`` values.
_MODES = ("fixture", "offline-evaluation", "authorized-live")


class AttestationError(ContractError):
    """A promotion request is not supported by observed durable evidence.

    Same code as :class:`ContractError` (``vouch/contract``): the claim is
    well-formed but unsupported — not a distinct transport failure mode.
    """


class ObservedUnpassedCoverageError(AttestationError):
    """Layer records that MATCH the claim's identity recorded FAILED runs.

    The distinction this preserves (M4 A2): a failed browser/control run is
    OBSERVED EXECUTION — the journey really ran, against the claimed source
    revision — but it is not PASSED COVERAGE, so it can never promote a
    workflow to ``runner-integrated``. Reporting may say "observed and
    failed"; it must not say either "covered" or "never executed"."""


@dataclass(frozen=True)
class AttestationClaim:
    """What a promotion request ASSERTS. Nothing here is trusted until
    :func:`resolve_attestation_evidence` resolves it against durable records.

    Field names/spellings mirror ``RunnerIntegrationRecord`` so the lead can
    build one directly from a promotion request.
    """

    workflow_id: str
    runner_id: str
    runner_descriptor_digest: str
    runner_revision: str
    case_pack_digest: str
    coverage_layer: str
    provider_mode: str
    verified_run_id: str
    verified_attempt_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        require_str(self.workflow_id, "workflowId")
        require_str(self.runner_id, "runnerId")
        require_digest(self.runner_descriptor_digest, "runnerDescriptorDigest")
        require_str(self.runner_revision, "runnerRevision")
        require_digest(self.case_pack_digest, "casePackDigest")
        if self.coverage_layer not in _LAYERS:
            raise AttestationError(f"unknown coverage layer {self.coverage_layer!r}")
        if self.provider_mode not in _MODES:
            raise AttestationError(f"unknown provider mode {self.provider_mode!r}")
        require_str(self.verified_run_id, "verifiedRunId")
        if not self.verified_attempt_digests:
            raise AttestationError(
                "a promotion claim must cite the attempt digests it is based on"
            )
        for digest in self.verified_attempt_digests:
            require_digest(digest, "verifiedAttemptDigests[*]")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttestationClaim:
        return cls(
            workflow_id=str(data["workflowId"]),
            runner_id=str(data["runnerId"]),
            runner_descriptor_digest=str(data["runnerDescriptorDigest"]),
            runner_revision=str(data.get("runnerRevision", "")),
            case_pack_digest=str(data.get("casePackDigest", "")),
            coverage_layer=str(data.get("coverageLayer", "")),
            provider_mode=str(data.get("providerMode", "fixture")),
            verified_run_id=str(data.get("verifiedRunId", "")),
            verified_attempt_digests=tuple(data.get("verifiedAttemptDigests", ())),
        )


@dataclass(frozen=True)
class ResolvedAttestationEvidence:
    """The observed facts behind a promotion, and where each came from.

    Every field is RE-DERIVED from durable records (or recomputed from the
    observed descriptor) — none is copied from the claim without checking.
    ``resolved_from`` names the records consulted so a reviewer can walk the
    same path.
    """

    workflow_id: str
    runner_id: str
    runner_descriptor_digest: str
    runner_revision: str
    case_pack_digest: str
    coverage_layer: str
    provider_mode: str
    verified_run_id: str
    verified_attempt_digests: tuple[str, ...]
    #: Durable record ids actually consulted while resolving.
    resolved_from: tuple[str, ...] = field(default=())
    #: ``(record_id, status)`` for each validated non-logic layer record that
    #: backs this evidence. A successful resolution only ever carries
    #: ``passed`` entries here; FAILED records surface through
    #: :class:`ObservedUnpassedCoverageError` instead — the observed-execution
    #: vs passed-coverage distinction stays visible (M4 A2).
    layer_records: tuple[tuple[str, str], ...] = field(default=())
    #: Content digests of the COMPLETE validated layer records (journeys,
    #: matrix cells, case ids, every cited artifact digest). Bound into the
    #: evidence digest so replaced proof under the same record id invalidates
    #: the claim instead of re-verifying (M4 review, digest-binding fix).
    layer_record_digests: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflowId": self.workflow_id,
            "runnerId": self.runner_id,
            "runnerDescriptorDigest": self.runner_descriptor_digest,
            "runnerRevision": self.runner_revision,
            "casePackDigest": self.case_pack_digest,
            "coverageLayer": self.coverage_layer,
            "providerMode": self.provider_mode,
            "verifiedRunId": self.verified_run_id,
            "verifiedAttemptDigests": list(self.verified_attempt_digests),
            "resolvedFrom": list(self.resolved_from),
            "layerRecords": [list(pair) for pair in self.layer_records],
            "layerRecordDigests": list(self.layer_record_digests),
        }

    def agrees_with(self, record: dict[str, Any]) -> bool:
        """True when a ``RunnerIntegrationRecord`` (as ``to_dict``) states
        exactly what the evidence supports — the check the promotion path
        applies before stamping an entry ``runner-integrated``."""
        expected = self.to_dict()
        keys = (
            "workflowId",
            "runnerId",
            "runnerDescriptorDigest",
            "runnerRevision",
            "casePackDigest",
            "coverageLayer",
            "providerMode",
            "verifiedRunId",
            "verifiedAttemptDigests",
        )
        return all(record.get(key) == expected[key] for key in keys)

    def assert_agrees(self, record: dict[str, Any]) -> None:
        if not self.agrees_with(record):
            raise AttestationError(
                "runner-integration record disagrees with the resolved evidence; "
                f"evidence: {self.to_dict()}; record: "
                f"{ {k: record.get(k) for k in ('workflowId', 'runnerId', 'verifiedRunId')} }"
            )


def descriptor_digest(descriptor: AdapterDescriptor) -> str:
    """Content digest of an OBSERVED descriptor (the describe() handshake)."""
    return digest_of(
        {
            "adapterId": descriptor.adapter_id,
            "protocolVersion": descriptor.protocol_version,
            "workflows": list(descriptor.workflows),
            "actions": list(descriptor.actions),
            "enforcedModes": [mode.value for mode in descriptor.enforced_modes],
            "notes": descriptor.notes,
        }
    )


def _stored_run(store: MetadataStore, run_id: str) -> EvaluationRun:
    data = store.load(KIND_EVALUATION, run_id)
    if data is None:
        raise AttestationError(
            f"no durable evaluation run {run_id!r}: a promotion claim must cite a run "
            "this project actually executed and persisted (invented run ids never resolve)"
        )
    return EvaluationRun.from_dict(data)


def _durable_attempts(store: MetadataStore, run: EvaluationRun) -> dict[str, AttemptRecord]:
    """The run's attempts as independently durable records, digest-verified.

    A claim is never satisfied by the run's own embedded copy alone: each
    attempt must also exist as its own stored record digesting to the same
    bytes, so editing either copy surfaces as a mismatch.
    """
    durable: dict[str, AttemptRecord] = {}
    for attempt in run.attempts:
        data = store.load(KIND_ATTEMPT, attempt.attempt_id)
        if data is None:
            raise AttestationError(
                f"attempt {attempt.attempt_id!r} of run {run.run_id!r} has no durable "
                "attempt record; the run's embedded copy is not evidence on its own"
            )
        stored = AttemptRecord.from_dict(data)
        if stored.digest() != attempt.digest():
            raise DigestMismatchError(
                f"durable attempt {attempt.attempt_id!r} digests to {stored.digest()} "
                f"but run {run.run_id!r} carries {attempt.digest()}; the records "
                "disagree — refusing to attest either"
            )
        durable[attempt.digest()] = stored
    if not durable:
        raise AttestationError(
            f"run {run.run_id!r} recorded no attempts; there is no observed execution "
            "to attest"
        )
    return durable


def _sealed_exchange(
    artifacts: ArtifactStore, attempt: AttemptRecord, run_id: str
) -> dict[str, Any]:
    """The sealed output artifact of one attempt: the recorded exchange facts
    (provider mode, runner revision) attestation derives its claims from."""
    if not attempt.output_digest:
        raise AttestationError(
            f"attempt {attempt.attempt_id!r} of run {run_id!r} sealed no output "
            "artifact; its provider mode and runner revision cannot be resolved"
        )
    payload = artifacts.get(attempt.output_digest)  # verifies the bytes
    try:
        sealed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttestationError(
            f"sealed output of attempt {attempt.attempt_id!r} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(sealed, dict):
        raise AttestationError(f"sealed output of attempt {attempt.attempt_id!r} is not an object")
    return sealed


def _pack_exists(store: MetadataStore, case_pack_digest: str) -> bool:
    for pack_id in store.list_ids(KIND_TASK_PACK):
        data = store.load(KIND_TASK_PACK, pack_id)
        if data is None:  # pragma: no cover - id came from list_ids
            continue
        from vouch_agent.contracts.cases import TaskPack

        if TaskPack.from_dict(data).digest() == case_pack_digest:
            return True
    return False


def _observed_layer_records(
    store: MetadataStore, kind: str, claim: AttestationClaim
) -> list[tuple[str, dict[str, Any]]]:
    """Durable records of a NON-logic coverage layer whose IDENTITY matches
    the claim (workflow / pack / mode). Validation happens separately in
    :func:`_validated_layer_records` — a matching identity alone proves
    nothing (M4 A2 review: presence is not validated coverage).
    """
    matches: list[tuple[str, dict[str, Any]]] = []
    for record_id in store.list_ids(kind):
        data = store.load(kind, record_id)
        if not isinstance(data, dict):
            continue
        if str(data.get("workflowId", "")) != claim.workflow_id:
            continue
        if str(data.get("casePackDigest", "")) != claim.case_pack_digest:
            continue
        if str(data.get("providerMode", "")) != claim.provider_mode:
            continue
        matches.append((record_id, data))
    return matches


@dataclass(frozen=True)
class _ValidatedLayerRecord:
    """One durable browser/control record after full contract validation.

    ``content_digest`` binds the COMPLETE validated record content — journeys
    (ids, matrix cells, statuses, case ids, screenshot digests) and every
    cited artifact digest — so the resolved evidence digest cannot stay
    unchanged when the cited proof is replaced under the same record id
    (M4 review: replacing screenshot references with different valid bytes
    used to re-verify because the digest bound only record ids/status).
    """

    record_id: str
    source_revision: str
    passed_case_ids: frozenset[str]
    journeys: tuple[dict[str, Any], ...]
    artifact_digests: tuple[str, ...]
    content_digest: str = ""

    def bind_content_digest(self) -> _ValidatedLayerRecord:
        content = digest_of(
            {
                "recordId": self.record_id,
                "sourceRevision": self.source_revision,
                "journeys": [
                    {
                        "journeyId": journey["journeyId"],
                        "locale": journey["locale"],
                        "viewport": journey["viewport"],
                        "status": journey["status"],
                        "caseIds": list(journey["caseIds"]),
                        "screenshotDigests": list(journey["screenshotDigests"]),
                    }
                    for journey in self.journeys
                ],
                "artifactDigests": list(self.artifact_digests),
            }
        )
        return replace(self, content_digest=content)


def _reject_layer_record(record_id: str, kind: str, reason: str) -> AttestationError:
    return AttestationError(
        f"durable {kind} record {record_id!r} is not valid evidence: {reason}; "
        "rejected rather than counted as coverage"
    )


def _validated_layer_records(
    store: MetadataStore,
    artifacts: ArtifactStore,
    kind: str,
    claim: AttestationClaim,
    requested_case_ids: frozenset[str],
    *,
    require_matrix: bool,
) -> tuple[list[_ValidatedLayerRecord], list[tuple[str, str]]]:
    """Validate every identity-matching layer record against the durable
    record contract (M4 A2) and split them into (passed, observed-unpassed).

    The contract a browser/control record must satisfy to count as PASSED
    coverage — anything less is rejected, never silently accepted:

    * schema: ``schemaVersion`` 1, ``status`` ``passed``|``failed``, a
      non-empty ``journeys`` list; every journey states ``journeyId``,
      ``locale``, ``viewport``, ``status`` and its ``caseIds``;
    * tested source identity: a non-empty ``sourceRevision`` equal to the
      runner revision the claim's own recorded exchanges verified — evidence
      from an unrelated old revision never covers the claimed source;
    * content-addressed artifacts: every digest the record cites
      (``artifactDigests`` + per-journey ``screenshotDigests``) must resolve
      in the artifact store and still digest to its name;
    * actual successful requested cases: the union of ``caseIds`` over
      PASSED journeys must cover the case ids of the attempts the claim
      cites — requested-but-absent or requested-but-failed cases are not
      coverage;
    * (browser only) journey matrix closure: for EVERY journeyId present,
      all locale x viewport combinations of the required matrix must appear
      exactly once.

    Records that match the identity but record ``failed`` (or whose journeys
    failed / whose matrix is incomplete) come back in the SECOND list with a
    reason: they are observed execution, not passed coverage — the caller
    reports them through :class:`ObservedUnpassedCoverageError`.
    """
    passed: list[_ValidatedLayerRecord] = []
    unpassed: list[tuple[str, str]] = []
    for record_id, data in _observed_layer_records(store, kind, claim):
        # -- schema (malformed identity-matching records are REJECTED) -------
        if str(data.get("schemaVersion", "")) != _LAYER_RECORD_SCHEMA_VERSION:
            raise _reject_layer_record(
                record_id, kind, f"schemaVersion must be {_LAYER_RECORD_SCHEMA_VERSION!r}"
            )
        record_status = str(data.get("status", ""))
        if record_status not in _JOURNEY_STATUSES:
            raise _reject_layer_record(
                record_id, kind, f"status must be one of {list(_JOURNEY_STATUSES)}"
            )
        revision = str(data.get("sourceRevision", ""))
        if not revision:
            raise _reject_layer_record(record_id, kind, "empty tested sourceRevision")
        journeys_raw = data.get("journeys")
        if not isinstance(journeys_raw, list) or not journeys_raw:
            raise _reject_layer_record(record_id, kind, "no journeys recorded")

        journeys: list[dict[str, Any]] = []
        for journey in journeys_raw:
            if not isinstance(journey, dict):
                raise _reject_layer_record(record_id, kind, "journey entry is not an object")
            journey_id = str(journey.get("journeyId", ""))
            if not journey_id:
                raise _reject_layer_record(record_id, kind, "journey entry has no journeyId")
            journey_status = str(journey.get("status", ""))
            if journey_status not in _JOURNEY_STATUSES:
                raise _reject_layer_record(
                    record_id, kind, f"journey {journey_id!r} status must be passed|failed"
                )
            case_ids = journey.get("caseIds", [])
            if not isinstance(case_ids, list) or any(not isinstance(c, str) for c in case_ids):
                raise _reject_layer_record(
                    record_id, kind, f"journey {journey_id!r} caseIds must be a list of strings"
                )
            if require_matrix:
                locale = str(journey.get("locale", ""))
                viewport = str(journey.get("viewport", ""))
                if locale not in REQUIRED_LOCALES:
                    raise _reject_layer_record(
                        record_id, kind, f"journey {journey_id!r} locale {locale!r} not in "
                        f"{list(REQUIRED_LOCALES)}"
                    )
                if viewport not in REQUIRED_VIEWPORTS:
                    raise _reject_layer_record(
                        record_id, kind, f"journey {journey_id!r} viewport {viewport!r} not in "
                        f"{list(REQUIRED_VIEWPORTS)}"
                    )
            screenshots = journey.get("screenshotDigests", [])
            if not isinstance(screenshots, list):
                raise _reject_layer_record(
                    record_id, kind, f"journey {journey_id!r} screenshotDigests must be a list"
                )
            journeys.append(
                {
                    "journeyId": journey_id,
                    "locale": str(journey.get("locale", "")),
                    "viewport": str(journey.get("viewport", "")),
                    "status": journey_status,
                    "caseIds": tuple(case_ids),
                    "screenshotDigests": tuple(str(d) for d in screenshots),
                }
            )

        # -- tested source identity ------------------------------------------
        if revision != claim.runner_revision:
            raise _reject_layer_record(
                record_id,
                kind,
                f"tested sourceRevision {revision!r} is not the runner revision the "
                f"recorded exchanges verified ({claim.runner_revision!r})"
            )

        # -- content-addressed artifacts must resolve ------------------------
        artifact_digests = data.get("artifactDigests", [])
        if not isinstance(artifact_digests, list):
            raise _reject_layer_record(record_id, kind, "artifactDigests must be a list")
        cited = [str(d) for d in artifact_digests]
        for journey in journeys:
            cited.extend(journey["screenshotDigests"])
        for digest in cited:
            require_digest(digest, f"{kind}:{record_id} artifactDigest")
            try:
                artifacts.get(digest)  # verifies the bytes against the name
            except DigestMismatchError:
                raise  # tampered artifact: refuse, never downgrade silently
            except VouchError as exc:
                raise _reject_layer_record(
                    record_id, kind, f"cited artifact {digest} does not resolve ({exc})"
                ) from exc

        # -- browser journey matrix closure ----------------------------------
        if require_matrix:
            seen_combos: set[tuple[str, str, str]] = set()
            by_journey: dict[str, set[tuple[str, str]]] = {}
            for journey in journeys:
                combo = (journey["journeyId"], journey["locale"], journey["viewport"])
                if combo in seen_combos:
                    raise _reject_layer_record(
                        record_id, kind, f"duplicate matrix cell {combo[:1] + combo[1:]}"
                    )
                seen_combos.add(combo)
                by_journey.setdefault(journey["journeyId"], set()).add(
                    (journey["locale"], journey["viewport"])
                )
            required = {
                (locale, viewport)
                for locale in REQUIRED_LOCALES
                for viewport in REQUIRED_VIEWPORTS
            }
            for journey_id, combos in sorted(by_journey.items()):
                missing = required - combos
                if missing:
                    raise _reject_layer_record(
                        record_id,
                        kind,
                        f"journey {journey_id!r} matrix incomplete: missing "
                        f"{sorted(missing)} of {sorted(required)} (M4-D requires every "
                        "journey in every locale x viewport)"
                    )

        # -- passed journeys must cover the requested cases -------------------
        passed_cases = frozenset(
            case_id
            for journey in journeys
            if journey["status"] == "passed"
            for case_id in journey["caseIds"]
        )
        record = _ValidatedLayerRecord(
            record_id=record_id,
            source_revision=revision,
            passed_case_ids=passed_cases,
            journeys=tuple(journeys),
            artifact_digests=tuple(cited),
        ).bind_content_digest()
        if record_status != "passed":
            unpassed.append((record_id, f"record status {record_status!r}"))
            continue
        failed_journeys = [j["journeyId"] for j in journeys if j["status"] != "passed"]
        if failed_journeys:
            unpassed.append((record_id, f"failed journeys {failed_journeys}"))
            continue
        uncovered = sorted(requested_case_ids - passed_cases)
        if uncovered:
            unpassed.append((record_id, f"requested cases not passed: {uncovered}"))
            continue
        passed.append(record)
    return passed, unpassed


def resolve_attestation_evidence(
    claim: AttestationClaim,
    *,
    store: MetadataStore,
    artifacts: ArtifactStore,
    observed_descriptor: AdapterDescriptor,
) -> ResolvedAttestationEvidence:
    """Resolve a promotion claim against OBSERVED durable evidence.

    Raises :class:`AttestationError` (or :class:`DigestMismatchError`) when
    any fact the claim asserts is not supported; returns the re-derived
    evidence on success. Fail-closed on every dimension:

    * invented run ids / attempt digests -> no durable record resolves;
    * runner revision or provider mode the recorded exchanges do not show;
    * a case-pack digest the run did not execute, or whose pack is gone;
    * a descriptor digest that is not the observed descriptor's digest;
    * a browser/control layer without its own observed records.
    """
    resolved_from: list[str] = []

    # (1) the run must be durable, finished, and belong to the workflow.
    run = _stored_run(store, claim.verified_run_id)
    resolved_from.append(f"{KIND_EVALUATION}:{run.run_id}")
    if run.workflow_id != claim.workflow_id:
        raise AttestationError(
            f"run {run.run_id!r} belongs to workflow {run.workflow_id!r}, the claim "
            f"names {claim.workflow_id!r}"
        )
    if run.execution_status != "completed":
        raise AttestationError(
            f"run {run.run_id!r} has execution status {run.execution_status!r}; only a "
            "completed run is observed evidence"
        )

    # (2) the descriptor digest is recomputed from the OBSERVED descriptor.
    observed_digest = descriptor_digest(observed_descriptor)
    if observed_digest != claim.runner_descriptor_digest:
        raise AttestationError(
            f"claimed runner descriptor digest {claim.runner_descriptor_digest} is not "
            f"the observed descriptor's digest {observed_digest} "
            f"({observed_descriptor.adapter_id!r})"
        )
    if claim.runner_id != observed_descriptor.adapter_id:
        raise AttestationError(
            f"claimed runner id {claim.runner_id!r} is not the observed adapter "
            f"{observed_descriptor.adapter_id!r}"
        )

    # (3) every cited attempt digest must match a durable attempt of this run.
    durable = _durable_attempts(store, run)
    for digest in claim.verified_attempt_digests:
        if digest not in durable:
            raise AttestationError(
                f"cited attempt digest {digest} matches no durable attempt of run "
                f"{run.run_id!r} (durable: {sorted(durable)}); invented digests never "
                "resolve"
            )

    # (4) provider mode and runner revision derive from the RECORDED
    # exchanges of the cited attempts — never from the claim.
    mode = run.mode.value
    revision = ""
    for digest in claim.verified_attempt_digests:
        attempt = durable[digest]
        resolved_from.append(f"{KIND_ATTEMPT}:{attempt.attempt_id}")
        sealed = _sealed_exchange(artifacts, attempt, run.run_id)
        sealed_mode = str(sealed.get("mode", ""))
        if sealed_mode != run.mode.value:
            raise AttestationError(
                f"attempt {attempt.attempt_id!r} recorded mode {sealed_mode!r} while its "
                f"run recorded {run.mode.value!r}; contradictory mode never attests"
            )
        sealed_revision = str(sealed.get("runnerVersion", ""))
        if not sealed_revision:
            raise AttestationError(
                f"attempt {attempt.attempt_id!r} recorded no runner revision; the "
                "claimed revision cannot be derived from evidence"
            )
        if revision and sealed_revision != revision:
            raise AttestationError(
                f"cited attempts of run {run.run_id!r} report different runner "
                f"revisions ({revision!r} vs {sealed_revision!r})"
            )
        revision = sealed_revision
    if mode != claim.provider_mode:
        raise AttestationError(
            f"claimed provider mode {claim.provider_mode!r} but run {run.run_id!r} and "
            f"its recorded exchanges say {mode!r}; a fixture run never attests a "
            "higher mode"
        )
    if revision != claim.runner_revision:
        raise AttestationError(
            f"claimed runner revision {claim.runner_revision!r} but the recorded "
            f"exchanges of run {run.run_id!r} report {revision!r}"
        )

    # (5) the case pack: the run executed it and it is still stored.
    if run.case_set_digest != claim.case_pack_digest:
        raise AttestationError(
            f"claimed case-pack digest {claim.case_pack_digest} is not what run "
            f"{run.run_id!r} executed ({run.case_set_digest})"
        )
    if not _pack_exists(store, claim.case_pack_digest):
        raise AttestationError(
            f"no stored task pack digests to {claim.case_pack_digest}; the pack the "
            "claim cites cannot be re-read"
        )

    # (6) the coverage layer must be derivable from observed records.
    layer = claim.coverage_layer
    layer_records: tuple[tuple[str, str], ...] = ()
    layer_record_digests: tuple[str, ...] = ()
    if layer == LAYER_BROWSER:
        identity = _observed_layer_records(store, KIND_BROWSER_EVIDENCE, claim)
        if not identity:
            raise AttestationError(
                f"no observed {KIND_BROWSER_EVIDENCE} records for workflow "
                f"{claim.workflow_id!r} under pack {claim.case_pack_digest} and mode "
                f"{claim.provider_mode!r}; browser coverage requires its own observed "
                "records — a logic-layer run never satisfies it"
            )
        requested = frozenset(durable[digest].case_id for digest in claim.verified_attempt_digests)
        passed, unpassed = _validated_layer_records(
            store,
            artifacts,
            KIND_BROWSER_EVIDENCE,
            claim,
            requested,
            require_matrix=True,
        )
        if not passed:
            raise ObservedUnpassedCoverageError(
                f"observed {KIND_BROWSER_EVIDENCE} records for workflow "
                f"{claim.workflow_id!r} record executions that are NOT passed coverage "
                f"({unpassed or 'no passing record'}); observed execution never promotes "
                "coverage until a record passes the full journey matrix, cases and "
                "artifact checks"
            )
        layer_records = tuple((record.record_id, "passed") for record in passed)
        layer_record_digests = tuple(record.content_digest for record in passed)
        resolved_from.extend(f"{KIND_BROWSER_EVIDENCE}:{record.record_id}" for record in passed)
    elif layer == LAYER_CONTROL:
        identity = _observed_layer_records(store, KIND_CONTROL_REGRESSION, claim)
        if not identity:
            raise AttestationError(
                f"no observed {KIND_CONTROL_REGRESSION} records for workflow "
                f"{claim.workflow_id!r}; control coverage requires its own observed "
                "records — a logic-layer run never satisfies it"
            )
        requested = frozenset(durable[digest].case_id for digest in claim.verified_attempt_digests)
        passed, unpassed = _validated_layer_records(
            store,
            artifacts,
            KIND_CONTROL_REGRESSION,
            claim,
            requested,
            require_matrix=False,
        )
        if not passed:
            raise ObservedUnpassedCoverageError(
                f"observed {KIND_CONTROL_REGRESSION} records for workflow "
                f"{claim.workflow_id!r} record executions that are NOT passed coverage "
                f"({unpassed or 'no passing record'}); observed execution never promotes "
                "coverage"
            )
        layer_records = tuple((record.record_id, "passed") for record in passed)
        layer_record_digests = tuple(record.content_digest for record in passed)
        resolved_from.extend(f"{KIND_CONTROL_REGRESSION}:{record.record_id}" for record in passed)
    # LAYER_LOGIC needs nothing beyond the verified run attempts themselves.

    return ResolvedAttestationEvidence(
        workflow_id=run.workflow_id,
        runner_id=observed_descriptor.adapter_id,
        runner_descriptor_digest=observed_digest,
        runner_revision=revision,
        case_pack_digest=run.case_set_digest,
        coverage_layer=layer,
        provider_mode=mode,
        verified_run_id=run.run_id,
        verified_attempt_digests=tuple(claim.verified_attempt_digests),
        resolved_from=tuple(resolved_from),
        layer_records=layer_records,
        layer_record_digests=layer_record_digests,
    )


def build_runner_integration_fields(
    evidence: ResolvedAttestationEvidence,
    *,
    verified_by: str,
    statement: str,
    verified_at: str = "",
) -> dict[str, Any]:
    """The ``RunnerIntegrationRecord`` fields a promotion may carry.

    This is the bridge for the lead-owned manifest wiring: a promoting record
    can only be built FROM resolved evidence, so a bare deserialized claim and
    a promoted entry can be compared field by field
    (``ResolvedAttestationEvidence.assert_agrees``).
    """
    require_str(verified_by, "verifiedBy")
    require_str(statement, "statement")
    fields = evidence.to_dict()
    fields.pop("resolvedFrom", None)
    fields.pop("layerRecords", None)
    fields["verifiedBy"] = verified_by
    fields["statement"] = statement
    fields["verifiedAt"] = verified_at
    fields["schemaVersion"] = "1"
    return fields


__all__ = [
    "KIND_BROWSER_EVIDENCE",
    "KIND_CONTROL_REGRESSION",
    "LAYER_BROWSER",
    "LAYER_CONTROL",
    "LAYER_LOGIC",
    "REQUIRED_LOCALES",
    "REQUIRED_VIEWPORTS",
    "AttestationClaim",
    "AttestationError",
    "ObservedUnpassedCoverageError",
    "ResolvedAttestationEvidence",
    "build_runner_integration_fields",
    "descriptor_digest",
    "resolve_attestation_evidence",
]
