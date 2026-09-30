"""A5: attestation evidence authority — a claim must resolve against
OBSERVED durable evidence before it can promote coverage.

Every scenario drives :func:`resolve_attestation_evidence` against records a
real controller produced (through the public controller entry points, with
the port fakes from the acceptance regressions) or against deliberately
invented claims. Inverted from the coordinator counterexample
``fabricated-v2-attestation``.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from support import (
    ChannelAdapter,
    make_baseline,
    make_candidate,
    make_controller,
    make_pack,
    rubric,
    seed_inputs,
)

from vouch_agent.adapters.base import AdapterDescriptor
from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import Role, RunMode
from vouch_agent.errors import ContractError, DigestMismatchError
from vouch_agent.evaluation.attestation_evidence import (
    KIND_BROWSER_EVIDENCE,
    AttestationClaim,
    AttestationError,
    build_runner_integration_fields,
    descriptor_digest,
    resolve_attestation_evidence,
)

INVENTED = "sha256:" + "a" * 64


def _genuine_run(cap: float = 1.0):
    """A real controller-produced selection run + its durable records."""
    controller, store, _ledger, _journal, artifacts = make_controller(cap)
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), "wf-compare")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = ChannelAdapter(channels=("unsafe",))
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )
    return controller, store, artifacts, pack, run, adapter


def _claim(store, artifacts, run, *, layer: str = "logic", mode: str = "fixture", **overrides):
    """A claim stating EXACTLY what the durable evidence supports."""
    import json

    from vouch_agent.contracts.evaluation import AttemptRecord  # local: no cycle

    attempt_digests = []
    revision = ""
    for attempt in run.attempts:
        stored = AttemptRecord.from_dict(store.load("attempt", attempt.attempt_id))
        attempt_digests.append(stored.digest())
        sealed = json.loads(artifacts.get(stored.output_digest).decode("utf-8"))
        revision = sealed["runnerVersion"]
    fields = {
        "workflow_id": run.workflow_id,
        "runner_id": "channel-fixture@1",
        "runner_descriptor_digest": descriptor_digest(
            AdapterDescriptor(adapter_id="channel-fixture@1", workflows=("wf-compare",))
        ),
        "runner_revision": revision,
        "case_pack_digest": run.case_set_digest,
        "coverage_layer": layer,
        "provider_mode": mode,
        "verified_run_id": run.run_id,
        "verified_attempt_digests": tuple(attempt_digests),
    }
    fields.update(overrides)
    return AttestationClaim(**fields)


def _observed_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(adapter_id="channel-fixture@1", workflows=("wf-compare",))


def _resolve(store, artifacts, claim):
    return resolve_attestation_evidence(
        claim, store=store, artifacts=artifacts, observed_descriptor=_observed_descriptor()
    )


# -- genuine durable evidence resolves -------------------------------------------


def test_genuine_durable_evidence_resolves() -> None:
    _controller, store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run)
    evidence = _resolve(store, artifacts, claim)
    assert evidence.workflow_id == "wf-compare"
    assert evidence.provider_mode == "fixture"
    assert evidence.coverage_layer == "logic"
    assert evidence.case_pack_digest == pack.digest()
    assert evidence.verified_run_id == run.run_id
    assert evidence.verified_attempt_digests == claim.verified_attempt_digests
    # every fact names the record it came from
    assert f"evaluation:{run.run_id}" in evidence.resolved_from
    assert all(f"attempt:{a.attempt_id}" in evidence.resolved_from for a in run.attempts)


def test_resolved_fields_build_a_manifest_record_and_detect_lies() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run)
    evidence = _resolve(store, artifacts, claim)
    fields = build_runner_integration_fields(
        evidence, verified_by="ana", statement="observed via controller run"
    )
    assert fields["workflowId"] == "wf-compare"
    assert evidence.agrees_with(fields)
    tampered = dict(fields)
    tampered["providerMode"] = "authorized-live"
    assert not evidence.agrees_with(tampered)
    with pytest.raises(AttestationError):
        evidence.assert_agrees(tampered)


# -- invented digests never resolve -----------------------------------------------


def test_invented_run_id_fails() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, verified_run_id="nonexistent-run")
    with pytest.raises(AttestationError, match="no durable evaluation run"):
        _resolve(store, artifacts, claim)


def test_invented_attempt_digest_fails() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(
        store, artifacts, run, verified_attempt_digests=(INVENTED,)
    )
    with pytest.raises(AttestationError, match="invented digests never resolve"):
        _resolve(store, artifacts, claim)


def test_tampered_durable_attempt_fails() -> None:
    """Editing the durable attempt record after the fact surfaces as a
    digest mismatch — the run's embedded copy is not evidence on its own."""
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    victim = run.attempts[0]
    data = store.load("attempt", victim.attempt_id)
    assert data is not None
    data["error"] = "rewritten after the fact"
    store.save("attempt", victim.attempt_id, data)
    claim = _claim(store, artifacts, run)
    with pytest.raises(DigestMismatchError):
        _resolve(store, artifacts, claim)


def test_missing_durable_attempt_record_fails() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run)  # built while records were intact
    store.data.pop(("attempt", run.attempts[0].attempt_id), None)
    with pytest.raises(AttestationError, match="no durable attempt record"):
        _resolve(store, artifacts, claim)


# -- mode / layer / revision must be DERIVED, not asserted ------------------------


def test_mode_mismatch_fails_fixture_run_cannot_attest_live() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, provider_mode="authorized-live")
    with pytest.raises(AttestationError, match="never attests a higher mode"):
        _resolve(store, artifacts, claim)


def test_claimed_runner_revision_must_match_recorded_exchanges() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, runner_revision="git:never-existed")
    with pytest.raises(AttestationError, match="recorded"):
        _resolve(store, artifacts, claim)


def test_descriptor_digest_must_be_the_observed_one() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, runner_descriptor_digest=INVENTED)
    with pytest.raises(AttestationError, match="observed descriptor"):
        _resolve(store, artifacts, claim)


def test_claimed_runner_id_must_be_the_observed_adapter() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, runner_id="invented-nonfixture-runner")
    with pytest.raises(AttestationError, match="observed adapter"):
        _resolve(store, artifacts, claim)


def test_case_pack_digest_must_be_what_the_run_executed() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, case_pack_digest=INVENTED)
    with pytest.raises(AttestationError, match="not what run"):
        _resolve(store, artifacts, claim)


def test_wrong_workflow_fails() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, workflow_id="W-C9")
    with pytest.raises(AttestationError, match="belongs to workflow"):
        _resolve(store, artifacts, claim)


def test_unfinished_run_is_not_evidence() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    data = store.load("evaluation", run.run_id)
    assert data is not None
    data["executionStatus"] = "running"
    store.save("evaluation", run.run_id, data)
    claim = _claim(store, artifacts, run)
    with pytest.raises(AttestationError, match="only a completed run"):
        _resolve(store, artifacts, claim)


# -- browser / control layers need their OWN observed records ----------------------


def test_logic_layer_run_never_satisfies_browser_layer() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    with pytest.raises(AttestationError, match="logic-layer run never satisfies"):
        _resolve(store, artifacts, claim)


def test_browser_layer_resolves_only_with_matching_browser_evidence() -> None:
    _controller, store, artifacts, pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="browser")
    artifact_digest = artifacts.put(b"browser-journey-screenshot")
    # a browser record for a DIFFERENT workflow/pack/mode still does not count
    store.save(
        KIND_BROWSER_EVIDENCE,
        "be-other",
        {
            "schemaVersion": "1",
            "workflowId": "W-C2",
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
                    "screenshotDigests": [],
                }
            ],
        },
    )
    with pytest.raises(AttestationError):
        _resolve(store, artifacts, claim)
    # the matching observed record does — full journey matrix, verified artifacts
    journeys = [
        {
            "journeyId": "primary",
            "locale": locale,
            "viewport": viewport,
            "status": "passed",
            "caseIds": [a.case_id for a in run.attempts],
            "screenshotDigests": [artifact_digest],
        }
        for locale in ("en", "zh")
        for viewport in ("desktop", "mobile")
    ]
    store.save(
        KIND_BROWSER_EVIDENCE,
        "be-wf",
        {
            "schemaVersion": "1",
            "workflowId": "wf-compare",
            "casePackDigest": pack.digest(),
            "providerMode": "fixture",
            "sourceRevision": claim.runner_revision,
            "status": "passed",
            "artifactDigests": [artifact_digest],
            "journeys": journeys,
        },
    )
    evidence = _resolve(store, artifacts, claim)
    assert evidence.coverage_layer == "browser"
    assert "browser-evidence:be-wf" in evidence.resolved_from
    assert dict(evidence.layer_records) == {"be-wf": "passed"}


def test_control_layer_requires_control_records() -> None:
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run, layer="control")
    with pytest.raises(AttestationError, match="control coverage requires"):
        _resolve(store, artifacts, claim)


# -- the original counterexample, end to end ---------------------------------------


def test_fabricated_v2_attestation_does_not_resolve() -> None:
    """The exact shape of the coordinator's ``fabricated-v2-attestation``
    proof: a bare record with invented digests promoting W-C3 to
    browser/authorized-live. Resolution refuses on every axis."""
    _controller, store, artifacts, _pack, _run, _adapter = _genuine_run()
    fabricated = AttestationClaim(
        workflow_id="W-C3",
        runner_id="invented-nonfixture-runner",
        runner_descriptor_digest="sha256:" + "a" * 64,
        runner_revision="git:never-existed",
        case_pack_digest="sha256:" + "b" * 64,
        coverage_layer="browser",
        provider_mode="authorized-live",
        verified_run_id="nonexistent-run",
        verified_attempt_digests=("sha256:" + "c" * 64,),
    )
    with pytest.raises(AttestationError):
        _resolve(store, artifacts, fabricated)


def test_claim_shape_validation_is_fail_closed() -> None:
    with pytest.raises(ContractError):
        AttestationClaim(
            workflow_id="W-C3",
            runner_id="runner",
            runner_descriptor_digest="not-a-digest",
            runner_revision="git:x",
            case_pack_digest=INVENTED,
            coverage_layer="logic",
            provider_mode="fixture",
            verified_run_id="run",
            verified_attempt_digests=(INVENTED,),
        )
    with pytest.raises(ContractError):
        AttestationClaim(
            workflow_id="W-C3",
            runner_id="runner",
            runner_descriptor_digest=INVENTED,
            runner_revision="git:x",
            case_pack_digest=INVENTED,
            coverage_layer="logic",
            provider_mode="fixture",
            verified_run_id="run",
            verified_attempt_digests=(),
        )


def test_live_mode_claim_against_fixture_adapter_modes_fails() -> None:
    """A descriptor that does not even enforce the claimed mode never
    resolves: the observed descriptor is part of the evidence."""
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    claim = _claim(store, artifacts, run)
    live_only = AdapterDescriptor(
        adapter_id="channel-fixture@1",
        workflows=("wf-compare",),
        enforced_modes=(RunMode.AUTHORIZED_LIVE,),
    )
    # the observed descriptor digest no longer matches the claim's
    with pytest.raises(AttestationError, match="observed descriptor"):
        resolve_attestation_evidence(
            claim, store=store, artifacts=artifacts, observed_descriptor=live_only
        )


def test_run_with_no_attempts_is_not_evidence() -> None:
    controller, store, _ledger, _journal, artifacts = make_controller()
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    from vouch_agent.contracts.evaluation import EvaluationRun

    hollow = EvaluationRun(
        run_id="eval_hollow",
        workflow_id="wf-compare",
        split=CaseSplit.SELECTION_VALIDATION,
        baseline_digest=INVENTED,
        candidate_digest=INVENTED,
        case_set_digest=pack.digest(),
        rubric_digest=INVENTED,
        mode=RunMode.FIXTURE,
        execution_status="completed",
        attempts=(),
    )
    store.save("evaluation", hollow.run_id, hollow.to_dict())
    claim = AttestationClaim(
        workflow_id="wf-compare",
        runner_id="channel-fixture@1",
        runner_descriptor_digest=descriptor_digest(_observed_descriptor()),
        runner_revision="channel-fixture@1",
        case_pack_digest=pack.digest(),
        coverage_layer="logic",
        provider_mode="fixture",
        verified_run_id=hollow.run_id,
        verified_attempt_digests=(INVENTED,),
    )
    with pytest.raises(AttestationError, match="no observed execution"):
        _resolve(store, artifacts, claim)


def test_attempt_without_sealed_output_cannot_attest_revision() -> None:
    """An attempt whose recorded exchange carries no runner revision (e.g. a
    pre-A6 record) cannot support a revision claim."""
    _controller, store, artifacts, _pack, run, _adapter = _genuine_run()
    victim = run.attempts[0]
    data = store.load("attempt", victim.attempt_id)
    assert data is not None
    stripped = replace(victim, output_digest=None)
    store.save("attempt", victim.attempt_id, stripped.to_dict())
    # keep the run's embedded copy consistent so the mismatch is the point
    from vouch_agent.contracts.evaluation import AttemptRecord

    attempts = tuple(
        stripped if a.attempt_id == victim.attempt_id else a for a in run.attempts
    )
    rewritten = replace(run, attempts=attempts)
    store.save("evaluation", run.run_id, rewritten.to_dict())
    digests = tuple(
        AttemptRecord.from_dict(store.load("attempt", a.attempt_id)).digest()
        for a in attempts
    )
    revision = json.loads(
        artifacts.get(next(a.output_digest for a in attempts if a.output_digest)).decode()
    )["runnerVersion"]
    claim = AttestationClaim(
        workflow_id="wf-compare",
        runner_id="channel-fixture@1",
        runner_descriptor_digest=descriptor_digest(_observed_descriptor()),
        runner_revision=revision,
        case_pack_digest=rewritten.case_set_digest,
        coverage_layer="logic",
        provider_mode="fixture",
        verified_run_id=run.run_id,
        verified_attempt_digests=digests,
    )
    with pytest.raises(AttestationError, match="sealed no output artifact"):
        _resolve(store, artifacts, claim)
