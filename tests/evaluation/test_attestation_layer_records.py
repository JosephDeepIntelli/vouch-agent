"""Durable browser/control layer-record contract (M4 A2).

Inverted from the M3 review proof ``browser-layer-minimal-failed-record``: a
FAILED browser record with no case ids, no artifact digests and an unrelated
source revision used to resolve as browser coverage on workflow/pack/mode
match alone. Presence is not validated coverage — every test here drives the
public resolver against durable records a real controller produced.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ACCEPTANCE = Path(__file__).resolve().parents[1] / "regression" / "acceptance"
if str(_ACCEPTANCE) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE))

from support import (  # noqa: E402
    ChannelAdapter,
    make_baseline,
    make_candidate,
    make_controller,
    make_pack,
    rubric,
    seed_inputs,
)

from vouch_agent.contracts.cases import CaseSplit  # noqa: E402
from vouch_agent.contracts.common import Role  # noqa: E402
from vouch_agent.errors import ContractError, DigestMismatchError  # noqa: E402
from vouch_agent.evaluation.attestation_evidence import (  # noqa: E402
    KIND_BROWSER_EVIDENCE,
    KIND_CONTROL_REGRESSION,
    REQUIRED_LOCALES,
    REQUIRED_VIEWPORTS,
    AttestationClaim,
    AttestationError,
    ObservedUnpassedCoverageError,
    descriptor_digest,
    resolve_attestation_evidence,
)


def _genuine_run():
    """A real controller-produced selection run + its durable records."""
    controller, store, _ledger, _journal, artifacts = make_controller()
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), "wf-compare")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = ChannelAdapter()
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )
    return store, artifacts, pack, run, adapter


def _claim(store: Any, artifacts: Any, run: Any, *, layer: str = "browser") -> AttestationClaim:
    import json

    from vouch_agent.contracts.evaluation import AttemptRecord

    attempt_digests = []
    revision = ""
    for attempt in run.attempts:
        stored = AttemptRecord.from_dict(store.load("attempt", attempt.attempt_id))
        attempt_digests.append(stored.digest())
        sealed = json.loads(artifacts.get(stored.output_digest).decode("utf-8"))
        revision = sealed["runnerVersion"]
    from vouch_agent.adapters.base import AdapterDescriptor

    return AttestationClaim(
        workflow_id=run.workflow_id,
        runner_id="channel-fixture@1",
        runner_descriptor_digest=descriptor_digest(
            AdapterDescriptor(adapter_id="channel-fixture@1", workflows=("wf-compare",))
        ),
        runner_revision=revision,
        case_pack_digest=run.case_set_digest,
        coverage_layer=layer,
        provider_mode="fixture",
        verified_run_id=run.run_id,
        verified_attempt_digests=tuple(attempt_digests),
    )


def _observed(store, artifacts, claim):
    from vouch_agent.adapters.base import AdapterDescriptor

    return resolve_attestation_evidence(
        claim,
        store=store,
        artifacts=artifacts,
        observed_descriptor=AdapterDescriptor(
            adapter_id="channel-fixture@1", workflows=("wf-compare",)
        ),
    )


def _browser_record(
    store: Any,
    artifacts: Any,
    record_id: str,
    *,
    workflow_id: str,
    pack: Any,
    case_ids: list[str],
    revision: str,
    **overrides: Any,
) -> None:
    artifact_digest = artifacts.put(f"screenshot-{record_id}".encode())
    journeys = [
        {
            "journeyId": "primary",
            "locale": locale,
            "viewport": viewport,
            "status": "passed",
            "caseIds": list(case_ids),
            "screenshotDigests": [artifact_digest],
        }
        for locale in REQUIRED_LOCALES
        for viewport in REQUIRED_VIEWPORTS
    ]
    record: dict[str, Any] = {
        "schemaVersion": "1",
        "workflowId": workflow_id,
        "casePackDigest": pack.digest(),
        "providerMode": "fixture",
        "sourceRevision": revision,
        "status": "passed",
        "artifactDigests": [artifact_digest],
        "journeys": journeys,
    }
    record.update(overrides)
    store.save(KIND_BROWSER_EVIDENCE, record_id, record)


def _control_record(
    store: Any,
    artifacts: Any,
    record_id: str,
    *,
    workflow_id: str,
    pack: Any,
    case_ids: list[str],
    revision: str,
    **overrides: Any,
) -> None:
    artifact_digest = artifacts.put(f"regression-log-{record_id}".encode())
    record: dict[str, Any] = {
        "schemaVersion": "1",
        "workflowId": workflow_id,
        "casePackDigest": pack.digest(),
        "providerMode": "fixture",
        "sourceRevision": revision,
        "status": "passed",
        "artifactDigests": [artifact_digest],
        "journeys": [
            {
                "journeyId": "regression-suite",
                "status": "passed",
                "caseIds": list(case_ids),
                "screenshotDigests": [],
            }
        ],
    }
    record.update(overrides)
    store.save(KIND_CONTROL_REGRESSION, record_id, record)


# -- the review proof, inverted --------------------------------------------------


def test_failed_browser_record_with_unrelated_revision_is_not_coverage() -> None:
    """The exact ``browser-layer-minimal-failed-record`` shape from the M3
    review: failed status, no case ids, no artifacts, unrelated old revision.
    It must NOT resolve as browser coverage (it used to)."""
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    store.save(
        KIND_BROWSER_EVIDENCE,
        "failed-unrelated-revision",
        {
            "workflowId": run.workflow_id,
            "casePackDigest": pack.digest(),
            "providerMode": "fixture",
            "sourceRevision": "git:unrelated-old-revision",
            "caseIds": [],
            "status": "failed",
            "passed": False,
            "screenshotDigests": [],
        },
    )
    with pytest.raises(AttestationError) as excinfo:
        _observed(store, artifacts, claim)
    # the refusal names the tested-source mismatch, not a vague failure
    assert "sourceRevision" in str(excinfo.value) or "schemaVersion" in str(excinfo.value)


def test_failed_record_is_observed_execution_not_passed_coverage() -> None:
    """A well-formed record whose status is ``failed`` (or whose journeys
    failed) preserves the distinction: observed execution, never coverage."""
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    _browser_record(
        store,
        artifacts,
        "be-failed",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision=claim.runner_revision,
        status="failed",
    )
    with pytest.raises(ObservedUnpassedCoverageError, match="observed execution"):
        _observed(store, artifacts, claim)

    store2, artifacts2, pack2, run2, _adapter2 = _genuine_run()
    claim2 = _claim(store2, artifacts2, run2, layer="browser")
    _browser_record(
        store2,
        artifacts2,
        "be-journey-failed",
        workflow_id="wf-compare",
        pack=pack2,
        case_ids=[a.case_id for a in run2.attempts],
        revision=claim2.runner_revision,
    )
    # one journey in the matrix failed
    data = store2.load(KIND_BROWSER_EVIDENCE, "be-journey-failed")
    assert data is not None
    data["journeys"][0]["status"] = "failed"
    store2.save(KIND_BROWSER_EVIDENCE, "be-journey-failed", data)
    with pytest.raises(ObservedUnpassedCoverageError, match="failed journeys"):
        _observed(store2, artifacts2, claim2)


def test_full_contract_browser_record_resolves_and_reports_its_records() -> None:
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    _browser_record(
        store,
        artifacts,
        "be-good",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision=claim.runner_revision,
    )
    evidence = _observed(store, artifacts, claim)
    assert evidence.coverage_layer == "browser"
    assert "browser-evidence:be-good" in evidence.resolved_from
    assert dict(evidence.layer_records) == {"be-good": "passed"}


# -- rejection axes ---------------------------------------------------------------


def test_wrong_revision_record_rejected() -> None:
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    _browser_record(
        store,
        artifacts,
        "be-old-rev",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision="git:choose@older-commit",
    )
    with pytest.raises(AttestationError, match="sourceRevision"):
        _observed(store, artifacts, claim)


def test_missing_artifact_digest_rejected() -> None:
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    _browser_record(
        store,
        artifacts,
        "be-no-artifacts",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision=claim.runner_revision,
        artifactDigests=["sha256:" + "9" * 64],
    )
    with pytest.raises(AttestationError, match="does not resolve"):
        _observed(store, artifacts, claim)


def test_tampered_artifact_rejected() -> None:
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    _browser_record(
        store,
        artifacts,
        "be-tampered",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision=claim.runner_revision,
    )
    cited = store.load(KIND_BROWSER_EVIDENCE, "be-tampered")
    assert cited is not None
    digest = cited["artifactDigests"][0]
    artifacts.blobs[digest] = b"rewritten-after-the-fact"
    with pytest.raises(DigestMismatchError):
        _observed(store, artifacts, claim)


def test_wrong_case_coverage_rejected() -> None:
    """Requested cases the record never passed are not coverage."""
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    _browser_record(
        store,
        artifacts,
        "be-wrong-cases",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=["some-other-case"],
        revision=claim.runner_revision,
    )
    with pytest.raises(ObservedUnpassedCoverageError, match="requested cases not passed"):
        _observed(store, artifacts, claim)


def test_incomplete_journey_matrix_rejected() -> None:
    """M4-D: browser scope requires every journey in every locale x viewport;
    a single desktop/en cell never claims the whole journey scope."""
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    artifact_digest = artifacts.put(b"partial-matrix-screenshot")
    store.save(
        KIND_BROWSER_EVIDENCE,
        "be-partial-matrix",
        {
            "schemaVersion": "1",
            "workflowId": "wf-compare",
            "casePackDigest": pack.digest(),
            "providerMode": "fixture",
            "sourceRevision": claim.runner_revision,
            "status": "passed",
            "artifactDigests": [artifact_digest],
            "journeys": [
                {
                    "journeyId": "primary",
                    "locale": "en",
                    "viewport": "desktop",
                    "status": "passed",
                    "caseIds": [a.case_id for a in run.attempts],
                    "screenshotDigests": [artifact_digest],
                }
            ],
        },
    )
    with pytest.raises(AttestationError, match="matrix incomplete"):
        _observed(store, artifacts, claim)


def test_malformed_records_rejected_not_skipped() -> None:
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    cases = [a.case_id for a in run.attempts]
    _browser_record(
        store,
        artifacts,
        "be-bad-schema",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=cases,
        revision=claim.runner_revision,
        schemaVersion="99",
    )
    with pytest.raises(AttestationError, match="schemaVersion"):
        _observed(store, artifacts, claim)

    store2, artifacts2, pack2, run2, _a = _genuine_run()
    claim2 = _claim(store2, artifacts2, run2, layer="browser")
    _browser_record(
        store2,
        artifacts2,
        "be-no-journeys",
        workflow_id="wf-compare",
        pack=pack2,
        case_ids=cases,
        revision=claim2.runner_revision,
        journeys=[],
    )
    with pytest.raises(AttestationError, match="no journeys"):
        _observed(store2, artifacts2, claim2)


def test_identity_matching_but_empty_record_rejected() -> None:
    """A record that claims this workflow/pack/mode but carries nothing else
    (no schema, no status, no journeys) is rejected — it is not evidence."""
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    store.save(
        KIND_BROWSER_EVIDENCE,
        "be-bare-identity",
        {
            "workflowId": "wf-compare",
            "casePackDigest": pack.digest(),
            "providerMode": "fixture",
        },
    )
    with pytest.raises(AttestationError, match="schemaVersion"):
        _observed(store, artifacts, claim)


def test_record_for_other_identity_is_not_considered() -> None:
    """A record for a different workflow/pack/mode never even enters
    validation — it cannot be evidence for THIS claim (and cannot crash it)."""
    store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    store.save(KIND_BROWSER_EVIDENCE, "be-other-identity", {"workflowId": "W-C2"})
    with pytest.raises(AttestationError, match="no observed browser-evidence records"):
        _observed(store, artifacts, claim)


# -- control layer -----------------------------------------------------------------


def test_control_record_full_contract_resolves() -> None:
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="control")
    _control_record(
        store,
        artifacts,
        "cr-good",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision=claim.runner_revision,
    )
    evidence = _observed(store, artifacts, claim)
    assert evidence.coverage_layer == "control"
    assert dict(evidence.layer_records) == {"cr-good": "passed"}


def test_failed_control_record_is_observed_not_passed() -> None:
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="control")
    _control_record(
        store,
        artifacts,
        "cr-failed",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision=claim.runner_revision,
        status="failed",
    )
    with pytest.raises(ObservedUnpassedCoverageError, match="observed execution"):
        _observed(store, artifacts, claim)


def test_control_record_requires_source_binding_too() -> None:
    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="control")
    _control_record(
        store,
        artifacts,
        "cr-old-rev",
        workflow_id="wf-compare",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision="git:an-old-control-revision",
    )
    with pytest.raises(ContractError, match="sourceRevision"):
        _observed(store, artifacts, claim)


def test_replaced_layer_proof_invalidates_the_resolved_evidence_digest() -> None:
    """M4 review, digest-binding fix: swapping a validated record's cited
    artifacts for different VALID bytes under the same record id used to leave
    the resolved evidence digest unchanged, so the old claim re-verified after
    a fresh reload. The resolved digest now binds the complete validated layer
    record content and refuses."""
    from vouch_agent.contracts.common import digest_of

    store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    _browser_record(
        store,
        artifacts,
        "be-replaceable",
        workflow_id=run.workflow_id,
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision=claim.runner_revision,
    )
    first = _observed(store, artifacts, claim)
    assert first.layer_record_digests, "resolved evidence binds no layer content"

    original = store.load(KIND_BROWSER_EVIDENCE, "be-replaceable")
    assert original is not None
    replacement = artifacts.put(b"completely-different-screenshot-content")
    original["artifactDigests"] = [replacement]
    for journey in original["journeys"]:
        journey["screenshotDigests"] = [replacement]
    store.save(KIND_BROWSER_EVIDENCE, "be-replaceable", original)

    # The record still validates (the replacement resolves and digests to its
    # own name) — but the resolved evidence is a DIFFERENT fact set now.
    second = _observed(store, artifacts, claim)
    assert digest_of(first.to_dict()) != digest_of(second.to_dict()), (
        "replacing cited proof under the same record id left the evidence "
        "digest unchanged"
    )
