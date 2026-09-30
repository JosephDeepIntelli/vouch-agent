"""Verdict + acceptance decision tests: fail-closed thresholds, digest bindings."""

from __future__ import annotations

import pytest

from vouch_agent.contracts.candidate import AgentVersion, Candidate, ChangeType
from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import RunMode, digest_of, new_id
from vouch_agent.contracts.evaluation import (
    AttemptRecord,
    AttemptStatus,
    EvaluationRun,
    Side,
)
from vouch_agent.contracts.evaluation import Rubric as RubricRecord
from vouch_agent.contracts.project import WorkflowDeclaration
from vouch_agent.errors import (
    ApprovalInvalidatedError,
    ContractError,
    DigestMismatchError,
)
from vouch_agent.evaluation.comparator import (
    THRESHOLD_MIN_COMPLETE_PAIRS,
    THRESHOLD_MIN_MAIN_IMPROVEMENT,
    compare_run,
)
from vouch_agent.evaluation.verdict import (
    UNRECORDED_ENVIRONMENT_DIGEST,
    binding_for_run,
    decide,
    freeze_rubric,
    verdict_from_summary,
    verify_decision,
    verify_decision_for_run,
)

D = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
D3 = "sha256:" + "c" * 64
ENV = "sha256:" + "d" * 64


def _attempt(case_id: str, side: Side, status=AttemptStatus.OK, *, usage=None, cost=0.01):
    return AttemptRecord(
        attempt_id=new_id("att"),
        run_id="run_1",
        side=side,
        case_id=case_id,
        status=status,
        cost_usd=cost,
        usage=dict(usage or {}),
    )


def _workflow() -> WorkflowDeclaration:
    return WorkflowDeclaration(
        workflow_id="wf-compare",
        name="Compare",
        main_objective="submission quality",
        guardrails=("severe-errors",),
    )


#: Frozen once at import: a rubric's digest includes frozen_at, so building
#: "the same" rubric twice can straddle a millisecond and change the digest.
_DEFAULT_RUBRIC = freeze_rubric(
    RubricRecord(
        main_metric="score",
        direction="increase",
        thresholds={},
        hard_guardrails=("severe-errors",),
        repeats=1,
    ),
    "owner",
)


def _rubric(frozen: bool = True, **overrides) -> RubricRecord:
    if frozen and not overrides:
        return _DEFAULT_RUBRIC
    fields = {
        "main_metric": "score",
        "direction": "increase",
        "thresholds": {},
        "hard_guardrails": ("severe-errors",),
        "repeats": 1,
    }
    fields.update(overrides)
    rubric = RubricRecord(**fields)
    return freeze_rubric(rubric, "owner") if frozen else rubric


def _run(attempts, *, rubric=None, split=CaseSplit.SELECTION_VALIDATION, environment_digest=ENV):
    rubric = rubric or _rubric()
    return EvaluationRun(
        run_id="run_1",
        workflow_id="wf-compare",
        split=split,
        baseline_digest=D,
        candidate_digest=D2,
        case_set_digest=D3,
        rubric_digest=rubric.digest(),
        environment_digest=environment_digest,
        attempts=tuple(attempts),
        mode=RunMode.FIXTURE,
        execution_status="completed",
    )


def _summary(attempts, rubric=None, workflow=None):
    return compare_run(_run(attempts, rubric=rubric), workflow or _workflow(), rubric or _rubric())


#: Synthetic adapters emit explicit checked-pass hard channels (review A4):
#: the frozen rubric below hardens "severe-errors", and a usable candidate
#: attempt that never measured a REQUIRED channel is unknown, never clear.
_CAND = {"guardrail:severe-errors": 0}


def _good_attempts():
    return [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, usage={**_CAND, "score": 0.7}),
        _attempt("case_b", Side.BASELINE, usage={"score": 0.4}),
        _attempt("case_b", Side.CANDIDATE, usage={**_CAND, "score": 0.6}),
    ]


EVIDENCE = "sha256:" + "e" * 64


# -- rubric freezing ------------------------------------------------------------


def test_unfrozen_rubric_is_an_error_not_a_verdict():
    summary = _summary(_good_attempts())
    with pytest.raises(ContractError, match="frozen"):
        verdict_from_summary(summary, _rubric(frozen=False))


def test_freeze_rubric_records_freezer_and_refuses_refreeze():
    rubric = freeze_rubric(RubricRecord(main_metric="score"), "yu-pei")
    assert rubric.frozen and rubric.frozen_by == "yu-pei" and rubric.frozen_at
    with pytest.raises(ContractError, match="already frozen"):
        freeze_rubric(rubric, "someone-else")
    with pytest.raises(ContractError):
        freeze_rubric(RubricRecord(main_metric="score"), "  ")


# -- verdict paths -----------------------------------------------------------------


def test_accepted_when_clean_and_improving():
    summary = _summary(_good_attempts())
    decision = decide(
        summary, _run(_good_attempts()), _rubric(), owner="yu-pei", evidence_digest=EVIDENCE
    )
    assert decision.verdict.value == "accepted"
    assert decision.binding is not None
    assert decision.evidence_digest == EVIDENCE
    assert "cannot guarantee" in decision.note  # uncertainty travels with the decision


def test_accepted_with_nonzero_improvement_threshold():
    rubric = _rubric(thresholds={THRESHOLD_MIN_MAIN_IMPROVEMENT: 0.05})
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.50}),
        _attempt("case_a", Side.CANDIDATE, usage={**_CAND, "score": 0.56}),
    ]
    assert verdict_from_summary(_summary(attempts, rubric=rubric), rubric).value == "accepted"
    rubric_tight = _rubric(thresholds={THRESHOLD_MIN_MAIN_IMPROVEMENT: 0.1})
    assert (
        verdict_from_summary(_summary(attempts, rubric=rubric_tight), rubric_tight).value
        == "rejected"
    )


def test_decrease_direction_measured_correctly():
    rubric = _rubric(main_metric="cost_per_result", direction="decrease")
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"cost_per_result": 0.10}),
        _attempt("case_a", Side.CANDIDATE, usage={**_CAND, "cost_per_result": 0.08}),
    ]
    assert verdict_from_summary(_summary(attempts, rubric=rubric), rubric).value == "accepted"


def test_hard_violation_rejects_even_with_great_means():
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, usage={"score": 0.99, "guardrail:severe-errors": 1}),
    ]
    assert verdict_from_summary(_summary(attempts), _rubric()).value == "rejected"


def test_candidate_failure_is_rejected_not_inconclusive():
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, AttemptStatus.FAILED, usage={}),
        _attempt("case_b", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_b", Side.CANDIDATE, AttemptStatus.TIMEOUT, usage={}),
    ]
    assert verdict_from_summary(_summary(attempts), _rubric()).value == "rejected"


def test_baseline_problem_is_inconclusive():
    attempts = [
        _attempt("case_a", Side.BASELINE, AttemptStatus.CANCELLED, usage={}),
        _attempt("case_a", Side.CANDIDATE, usage={"score": 0.9}),
    ]
    assert verdict_from_summary(_summary(attempts), _rubric()).value == "inconclusive"


def test_missing_side_is_inconclusive():
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        # no candidate attempt at all
    ]
    assert verdict_from_summary(_summary(attempts), _rubric()).value == "inconclusive"


def test_immeasurable_cost_is_inconclusive():
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, usage={"score": 0.9}, cost=None),
    ]
    assert verdict_from_summary(_summary(attempts), _rubric()).value == "inconclusive"


def test_missing_main_metric_is_inconclusive():
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"other": 1.0}),
        _attempt("case_a", Side.CANDIDATE, usage={"other": 2.0}),
    ]
    assert verdict_from_summary(_summary(attempts), _rubric()).value == "inconclusive"


def test_partial_main_metric_coverage_is_inconclusive():
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, usage={"score": 0.9}),
        _attempt("case_b", Side.BASELINE, usage={"other": 1.0}),
        _attempt("case_b", Side.CANDIDATE, usage={"other": 2.0}),
    ]
    assert verdict_from_summary(_summary(attempts), _rubric()).value == "inconclusive"


def test_below_min_complete_pairs_is_inconclusive():
    rubric = _rubric(thresholds={THRESHOLD_MIN_COMPLETE_PAIRS: 3})
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, usage={"score": 0.9}),
        _attempt("case_b", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_b", Side.CANDIDATE, usage={"score": 0.9}),
    ]
    assert verdict_from_summary(_summary(attempts, rubric=rubric), rubric).value == "inconclusive"


def test_zero_pairs_is_inconclusive():
    summary = _summary([])
    assert verdict_from_summary(summary, _rubric()).value == "inconclusive"


def test_repeats_shortfall_is_inconclusive():
    rubric = _rubric(repeats=2)
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, usage={"score": 0.9}),
    ]
    assert verdict_from_summary(_summary(attempts, rubric=rubric), rubric).value == "inconclusive"


# -- decision construction guards -----------------------------------------------------


def test_decide_refuses_proposer_and_bad_scope():
    summary = _summary(_good_attempts())
    run = _run(_good_attempts())
    with pytest.raises(ContractError, match="proposer"):
        decide(summary, run, _rubric(), owner="proposer", evidence_digest=EVIDENCE)
    with pytest.raises(ContractError, match="release_scope"):
        decide(
            summary,
            run,
            _rubric(),
            owner="yu-pei",
            evidence_digest=EVIDENCE,
            allowed_release_scope="galaxy",
        )
    with pytest.raises(ContractError, match="evidence_digest"):
        decide(summary, run, _rubric(), owner="yu-pei", evidence_digest="not-a-digest")


def test_decide_refuses_summary_run_mismatch_and_rubric_mismatch():
    attempts = _good_attempts()
    summary = _summary(attempts)
    run = _run(attempts)
    other_rubric = _rubric(thresholds={THRESHOLD_MIN_MAIN_IMPROVEMENT: 0.2})
    with pytest.raises(DigestMismatchError):
        decide(summary, run, other_rubric, owner="yu-pei", evidence_digest=EVIDENCE)


def test_rejected_and_inconclusive_decisions_carry_no_binding():
    bad = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, usage={"score": 0.99, "guardrail:severe-errors": 1}),
    ]
    rejected = decide(_summary(bad), _run(bad), _rubric(), owner="yu-pei", evidence_digest=EVIDENCE)
    assert rejected.verdict.value == "rejected" and rejected.binding is None
    with pytest.raises(ContractError, match="no approval binding"):
        verify_decision(
            rejected,
            candidate_digest=D2,
            rubric_digest=rejected.candidate_digest,
            environment_digest=ENV,
            acceptance_case_set_digest=D3,
        )


# -- approval invalidation (the load-bearing attack) ------------------------------------


def _accepted_decision():
    attempts = _good_attempts()
    rubric = _rubric()
    run = _run(attempts, rubric=rubric)
    summary = compare_run(run, _workflow(), rubric)
    decision = decide(summary, run, rubric, owner="yu-pei", evidence_digest=EVIDENCE)
    return decision, run, rubric


def _candidate(delta: str) -> Candidate:
    return Candidate(
        candidate_id=new_id("cand"),
        parent_version=AgentVersion(version_id=new_id("ver"), source_ref="git:abc123"),
        change_type=ChangeType.PROMPT_DELTA,
        delta=delta,
        rationale="clarify submission format",
        expected_impact="fewer reworks",
        proposer="model-worker",
    )


def test_approval_invalidated_when_candidate_changes():
    decision, run, rubric = _accepted_decision()
    verify_decision_for_run(decision, run, rubric)  # still valid

    original = _candidate("Use bullet lists.")
    tampered = _candidate("Use bullet lists AND skip verification.")  # changed delta
    assert original.delta_digest() != tampered.delta_digest()

    # The approval was bound to the candidate digest; consuming it against the
    # changed candidate must fail loudly, not pass silently.
    with pytest.raises(ApprovalInvalidatedError):
        verify_decision(
            decision,
            candidate_digest=tampered.delta_digest(),
            rubric_digest=run.rubric_digest,
            environment_digest=ENV,
            acceptance_case_set_digest=run.case_set_digest,
        )


def test_approval_invalidated_when_rubric_changes():
    decision, _old_run, rubric = _accepted_decision()
    new_rubric = _rubric(thresholds={THRESHOLD_MIN_MAIN_IMPROVEMENT: 0.9})
    assert new_rubric.digest() != rubric.digest()
    # A changed rubric means re-measurement: the new run carries the new
    # rubric digest while the approval is still bound to the old one.
    rerun = _run(_good_attempts(), rubric=new_rubric)
    with pytest.raises(ApprovalInvalidatedError):
        verify_decision_for_run(decision, rerun, new_rubric)


def test_approval_invalidated_when_environment_or_case_set_changes():
    decision, run, _rubric = _accepted_decision()
    with pytest.raises(ApprovalInvalidatedError):
        verify_decision(
            decision,
            candidate_digest=run.candidate_digest,
            rubric_digest=run.rubric_digest,
            environment_digest="sha256:" + "f" * 64,
            acceptance_case_set_digest=run.case_set_digest,
        )
    with pytest.raises(ApprovalInvalidatedError):
        verify_decision(
            decision,
            candidate_digest=run.candidate_digest,
            rubric_digest=run.rubric_digest,
            environment_digest=ENV,
            acceptance_case_set_digest="sha256:" + "9" * 64,
        )


def test_unrecorded_environment_digest_is_deterministic():
    attempts = _good_attempts()
    run = _run(attempts, environment_digest=None)
    binding = binding_for_run(run, _rubric())
    assert binding.environment_digest == UNRECORDED_ENVIRONMENT_DIGEST
    # same inputs -> same sentinel, so verification is stable
    assert binding_for_run(run, _rubric()).environment_digest == UNRECORDED_ENVIRONMENT_DIGEST
    decision = decide(_summary(attempts), run, _rubric(), owner="yu-pei", evidence_digest=EVIDENCE)
    verify_decision_for_run(
        decision, run, _rubric()
    )  # None environment still verifies against itself


def test_binding_digests_come_from_the_run():
    attempts = _good_attempts()
    run = _run(attempts)
    binding = binding_for_run(run, _rubric())
    assert binding.candidate_digest == run.candidate_digest
    assert binding.rubric_digest == run.rubric_digest
    assert binding.acceptance_case_set_digest == run.case_set_digest
    assert binding.digest() == digest_of(binding.to_dict())
