"""A4 regressions (controller side): durable-run evidence authority and the
A2 controller-side budget defect.

Converted from the coordinator reproduction `review-acceptance-20260928`
(defects 3 and 6 plus the missing-whole-case follow-up). All scenarios drive
the PUBLIC controller entry points; the port fakes come from ``support``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from support import (
    RUN_EXPORT_KIND,
    ChannelAdapter,
    make_baseline,
    make_candidate,
    make_controller,
    make_pack,
    rubric,
    seed_inputs,
)

from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import Role, digest_of
from vouch_agent.contracts.decision import AcceptanceDecision, ApprovalBinding, Verdict
from vouch_agent.contracts.evaluation import EvaluationRun, Side
from vouch_agent.errors import BudgetExhaustedError, ContractError

KIND_EVALUATION = "evaluation"


def _prepared(cap: float = 1.0):
    controller, store, ledger, journal, artifacts = make_controller(cap)
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), "wf-compare")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    return controller, store, ledger, journal, artifacts, pack


def _development_pack(pack):
    return replace(
        pack,
        pack_id="pack-dev",
        cases=tuple(replace(c, split=CaseSplit.DEVELOPMENT) for c in pack.cases),
    )


def _selection_run(controller, pack, rubric_digest, *, adapter=None):
    return controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter or ChannelAdapter(channels=("unsafe",)),
    )


def _final_run(controller, pack, rubric_digest, *, adapter=None):
    return controller.run_final_acceptance(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        rubric_digest=rubric_digest,
        adapter=adapter or ChannelAdapter(channels=("unsafe",)),
        role=Role.ACCEPTANCE_OWNER,
    )


def _anchored_decision(controller, store, final_run, rubric_digest) -> AcceptanceDecision:
    """decide() over the durable record, anchored to a persisted export."""
    from vouch_agent.evaluation import compare_run, decide

    rubric = controller.rubric(rubric_digest)
    stored = _stored_run(store, final_run.run_id)
    summary = compare_run(
        stored,
        controller.project.workflow("wf-compare"),
        rubric,
        expected_case_ids=_expected_cases(controller, stored),
    )
    evidence_digest = digest_of({"export": final_run.run_id})
    store.save(
        RUN_EXPORT_KIND,
        final_run.run_id,
        {"manifestDigest": evidence_digest, "path": f"/synthetic/{final_run.run_id}"},
    )
    return decide(summary, stored, rubric, owner="ana", evidence_digest=evidence_digest)


def _stored_run(store, run_id: str) -> EvaluationRun:
    data = store.load(KIND_EVALUATION, run_id)
    assert data is not None, f"run {run_id} was never persisted"
    return EvaluationRun.from_dict(data)


def _expected_cases(controller, run: EvaluationRun) -> tuple[str, ...]:
    from vouch_agent.contracts.cases import TaskPack

    for pack_id in controller.store.list_ids("task-pack"):
        data = controller.store.load("task-pack", pack_id)
        assert data is not None
        pack = TaskPack.from_dict(data)
        if pack.digest() == run.case_set_digest:
            return tuple(c.case_id for c in pack.cases_in(run.split))
    raise AssertionError("bound pack not stored")


# -- defect 3: forged run / caller-supplied verdict --------------------------------------


def test_mark_evaluated_requires_a_qualifying_selection_run() -> None:
    controller, _store, _ledger, _journal, artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    with pytest.raises(ContractError, match="selection"):
        controller.mark_evaluated("cand-acc")
    assert controller.candidate("cand-acc").state.value == "sealed"

    # a DEVELOPMENT run, however good, does not qualify either
    dev_pack = _development_pack(pack)
    seed_inputs(artifacts, dev_pack)
    controller.import_pack(dev_pack, Role.EVALUATOR)
    controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=dev_pack,
        split=CaseSplit.DEVELOPMENT,
        rubric_digest=rubric_digest,
        adapter=ChannelAdapter(channels=("unsafe",)),
    )
    with pytest.raises(ContractError, match="selection"):
        controller.mark_evaluated("cand-acc")
    assert controller.candidate("cand-acc").state.value == "sealed"

    # a qualifying selection-validation run does
    _selection_run(controller, pack, rubric_digest)
    moved = controller.mark_evaluated("cand-acc")
    assert moved.state.value == "evaluated"


def test_forged_pending_run_and_decision_cannot_advance_candidate() -> None:
    """Reproduction 3 (inverted): an unpersisted pending final run with zero
    attempts plus a caller-supplied ACCEPTED decision must move NOTHING."""
    controller, _store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    _selection_run(controller, pack, rubric_digest)
    controller.mark_evaluated("cand-acc")

    digest = controller.candidate("cand-acc").content_digest()
    arbitrary = digest_of("arbitrary")
    fake_run = EvaluationRun(
        run_id="never-executed",
        workflow_id="wf-compare",
        split=CaseSplit.FINAL_ACCEPTANCE,
        baseline_digest=arbitrary,
        candidate_digest=digest,
        case_set_digest=arbitrary,
        rubric_digest=arbitrary,
        execution_status="pending",  # forged: never executed, never persisted
    )
    fake_binding = ApprovalBinding(
        candidate_digest=digest,
        rubric_digest=arbitrary,
        environment_digest=arbitrary,
        acceptance_case_set_digest=arbitrary,
    )
    fake_decision = AcceptanceDecision(
        decision_id="forged",
        verdict=Verdict.ACCEPTED,
        candidate_digest=digest,
        evidence_digest=arbitrary,
        owner="ana",
        binding=fake_binding,
    )
    with pytest.raises(ContractError):
        controller.record_decision(
            fake_decision, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=fake_run
        )
    assert controller.candidate("cand-acc").state.value == "evaluated"

    # a durable-looking id nobody ever persisted is refused the same way
    with pytest.raises(ContractError):
        controller.record_decision(
            fake_decision,
            role=Role.ACCEPTANCE_OWNER,
            final_acceptance_run="eval_does_not_exist",
        )
    assert controller.candidate("cand-acc").state.value == "evaluated"

    # approval of the forged binding finds no durable accepted decision
    with pytest.raises(ContractError):
        controller.record_approval("cand-acc", role=Role.RELEASE_OWNER, binding=fake_binding)
    assert controller.candidate("cand-acc").state.value == "evaluated"


def test_caller_cannot_launder_an_improved_in_memory_run() -> None:
    """record_decision recomputes from the DURABLE record: a decision built
    from an in-memory copy with a hard violation scrubbed must be refused."""
    controller, store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    _selection_run(controller, pack, rubric_digest)
    controller.mark_evaluated("cand-acc")

    violating = ChannelAdapter(channels=("unsafe",), violate_repeats=frozenset({0}))
    final_run = _final_run(controller, pack, rubric_digest, adapter=violating)
    # the attacker's improved in-memory copy: violation channel flipped to pass
    laundered = replace(
        final_run,
        attempts=tuple(
            replace(a, usage={**a.usage, "guardrail:unsafe": 0}) for a in final_run.attempts
        ),
    )
    from vouch_agent.evaluation import compare_run, decide

    rubric_obj = controller.rubric(rubric_digest)
    clean_summary = compare_run(laundered, controller.project.workflow("wf-compare"), rubric_obj)
    evidence_digest = digest_of({"export": final_run.run_id})
    store.save(RUN_EXPORT_KIND, final_run.run_id, {"manifestDigest": evidence_digest, "path": "/x"})
    decision = decide(
        clean_summary, laundered, rubric_obj, owner="ana", evidence_digest=evidence_digest
    )
    assert decision.verdict is Verdict.ACCEPTED
    with pytest.raises(ContractError):
        controller.record_decision(
            decision, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=laundered
        )
    assert controller.candidate("cand-acc").state.value == "evaluated"


def test_record_decision_rejects_unanchored_evidence_and_tampered_verdicts() -> None:
    controller, store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    _selection_run(controller, pack, rubric_digest)
    controller.mark_evaluated("cand-acc")
    final_run = _final_run(controller, pack, rubric_digest)

    from vouch_agent.evaluation import compare_run, decide

    rubric_obj = controller.rubric(rubric_digest)
    stored = _stored_run(store, final_run.run_id)
    summary = compare_run(
        stored,
        controller.project.workflow("wf-compare"),
        rubric_obj,
        expected_case_ids=_expected_cases(controller, stored),
    )

    # (a) verdict tampering: claim REJECTED when the trusted path computes ACCEPTED
    honest = decide(
        summary,
        stored,
        rubric_obj,
        owner="ana",
        evidence_digest=digest_of({"export": final_run.run_id}),
    )
    assert honest.verdict is Verdict.ACCEPTED
    tampered = replace(honest, verdict=Verdict.REJECTED, binding=None)
    with pytest.raises(ContractError, match="verdict"):
        controller.record_decision(
            tampered, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=final_run.run_id
        )

    # (b) evidence not anchored to any persisted export of this run
    unanchored = decide(
        summary,
        stored,
        rubric_obj,
        owner="ana",
        evidence_digest=digest_of({"never": "exported"}),
    )
    with pytest.raises(ContractError, match="evidence"):
        controller.record_decision(
            unanchored, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=final_run.run_id
        )
    assert controller.candidate("cand-acc").state.value == "evaluated"


def test_missing_whole_case_in_stored_run_surfaces_as_missing() -> None:
    """Deleting BOTH sides of a case from the durable run must surface a
    missing case (inconclusive), not shrink the denominator."""
    from vouch_agent.evaluation import compare_run, verdict_from_summary
    from vouch_agent.evaluation.verdict import binding_for_run

    controller, store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    _selection_run(controller, pack, rubric_digest)
    controller.mark_evaluated("cand-acc")
    final_run = _final_run(controller, pack, rubric_digest)

    # attacker deletes both sides of the final case from the stored record
    stored = _stored_run(store, final_run.run_id)
    hollowed = replace(stored, attempts=())
    store.save(KIND_EVALUATION, final_run.run_id, hollowed.to_dict())

    rubric_obj = controller.rubric(rubric_digest)
    reloaded = _stored_run(store, final_run.run_id)
    summary = compare_run(
        reloaded,
        controller.project.workflow("wf-compare"),
        rubric_obj,
        expected_case_ids=_expected_cases(controller, reloaded),
    )
    assert summary.total_pairs == 1  # the expected case still counts
    assert summary.incomplete_pairs == 1
    assert verdict_from_summary(summary, rubric_obj) is Verdict.INCONCLUSIVE

    evidence_digest = digest_of({"export": final_run.run_id})
    store.save(RUN_EXPORT_KIND, final_run.run_id, {"manifestDigest": evidence_digest, "path": "/x"})
    forged = AcceptanceDecision(
        decision_id="forged-accept",
        verdict=Verdict.ACCEPTED,
        candidate_digest=reloaded.candidate_digest,
        evidence_digest=evidence_digest,
        owner="ana",
        binding=binding_for_run(reloaded, rubric_obj),
        final_acceptance_run_id=final_run.run_id,
    )
    with pytest.raises(ContractError):
        controller.record_decision(
            forged, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=final_run.run_id
        )
    assert controller.candidate("cand-acc").state.value == "evaluated"


def test_happy_path_decision_still_advances_and_persists_atomically() -> None:
    controller, store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    _selection_run(controller, pack, rubric_digest)
    controller.mark_evaluated("cand-acc")
    final_run = _final_run(controller, pack, rubric_digest)
    decision = _anchored_decision(controller, store, final_run, rubric_digest)

    controller.record_decision(
        decision, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=final_run.run_id
    )
    assert controller.candidate("cand-acc").state.value == "accepted"
    stored_decision = store.load("decision", decision.decision_id)
    assert stored_decision is not None
    assert stored_decision["verdict"] == "accepted"


def test_approval_and_release_pass_without_drift() -> None:
    """The re-resolved verification must still let the honest tail complete."""
    from vouch_agent.contracts.decision import ReleaseRecord

    controller, store, _ledger, _journal, _artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    _selection_run(controller, pack, rubric_digest)
    controller.mark_evaluated("cand-acc")
    final_run = _final_run(controller, pack, rubric_digest)
    decision = _anchored_decision(controller, store, final_run, rubric_digest)
    controller.record_decision(
        decision, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=final_run.run_id
    )
    controller.record_approval("cand-acc", role=Role.RELEASE_OWNER, binding=decision.binding)
    assert controller.candidate("cand-acc").state.value == "approved"
    controller.record_release(
        ReleaseRecord(
            release_id="rel-acc",
            candidate_digest=decision.candidate_digest,
            deployed_version="choose@v9",
            deployed_by="roger",
        ),
        role=Role.RELEASE_OWNER,
    )
    assert controller.candidate("cand-acc").state.value == "released"


# -- defect 6 (A2 controller side): unknown failed spend ---------------------------------


def test_unknown_failed_spend_does_not_replenish_budget() -> None:
    """Reproduction 6 (inverted): a $0.05 cap must not fund eight unknown-cost
    failed attempts. Post-dispatch unknown spend keeps its conservative
    reservation until reconciliation."""
    controller, _store, ledger, journal, artifacts = make_controller(cap=0.05)
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    rubric_digest = controller.freeze_rubric(rubric(repeats=1), "ana")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)

    dev_pack = _development_pack(pack)
    seed_inputs(artifacts, dev_pack)
    controller.import_pack(dev_pack, Role.EVALUATOR)
    with pytest.raises(BudgetExhaustedError):
        controller.run_paired_evaluation(
            workflow_id="wf-compare",
            candidate_id="cand-acc",
            baseline=make_baseline(),
            pack=dev_pack,
            split=CaseSplit.DEVELOPMENT,
            rubric_digest=rubric_digest,
            adapter=ChannelAdapter(ok=False, cost=None),
            repeats=2,  # 1 case x 2 sides x 2 repeats = 8 attempts if budget refills
            per_attempt_reserve_usd=0.05,
        )
    assert ledger.settled_usd() == 0.0  # nothing was booked as measured spend
    assert ledger.outstanding_usd() == pytest.approx(0.05)  # the hold stays
    assert ledger.remaining_usd() == pytest.approx(0.0)
    # the unknown spend is flagged for reconciliation, not silently zeroed
    assert any(
        e.kind.value == "run-reconciliation" and e.data.get("state") == "needed"
        for e in journal.events()
    )
    # explicit reconciliation (verified zero spend) returns the hold
    held = [r for r in ledger.reservations.values() if r.status.value == "open"]
    assert len(held) == 1
    ledger.release(held[0].reservation_id)
    assert ledger.remaining_usd() == pytest.approx(0.05)


def test_pre_dispatch_rejection_settles_at_verified_zero() -> None:
    """Failure BEFORE dispatch provably spent nothing: zero settlement is
    correct there, and such attempts must not hold budget."""
    # gate scope empty: every authorize() is denied before any adapter work
    controller, _store, ledger, _journal, artifacts = make_controller(
        cap=0.05, gate_case_prefixes=()
    )
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    rubric_digest = controller.freeze_rubric(rubric(repeats=1), "ana")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    dev_pack = _development_pack(pack)
    seed_inputs(artifacts, dev_pack)
    controller.import_pack(dev_pack, Role.EVALUATOR)
    adapter = ChannelAdapter(ok=True, cost=None)
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=dev_pack,
        split=CaseSplit.DEVELOPMENT,
        rubric_digest=rubric_digest,
        adapter=adapter,
        repeats=2,  # all 8 attempts are refused pre-dispatch at $0 -> affordable
        per_attempt_reserve_usd=0.05,
    )
    assert len(run.attempts) == 8
    assert all(a.status.value == "failed" for a in run.attempts)
    assert adapter.executed_case_inputs == []  # no adapter work ever happened
    assert ledger.settled_usd() == 0.0
    assert ledger.outstanding_usd() == 0.0
    assert ledger.remaining_usd() == pytest.approx(0.05)


# -- A6, controller side: verified input bytes + sealed outputs ---------------------------


def test_runner_receives_verified_input_bytes_and_sealed_outputs() -> None:
    controller, store, _ledger, _journal, artifacts, pack = _prepared()
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = ChannelAdapter(channels=("unsafe",))
    run = _selection_run(controller, pack, rubric_digest, adapter=adapter)
    assert run.attempts
    for case_input in adapter.executed_case_inputs:
        assert case_input.get("inputDigest")
        # the verified bytes behind the digest travel with the attempt
        assert case_input.get("inputBytes") is not None
    # outputs are sealed into the artifact store with retained digests
    for attempt in run.attempts:
        stored = store.load("attempt", attempt.attempt_id)
        assert stored is not None
        assert stored["outputDigest"]
        assert artifacts.exists(stored["outputDigest"])
    # baseline and candidate both executed every case
    assert {a.case_id for a in run.attempts} == {"case-sel-1"}
    assert {a.side for a in run.attempts} == {Side.BASELINE, Side.CANDIDATE}
