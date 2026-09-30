"""Comparator honesty tests: pairs, guardrails, costs, uncertainty."""

from __future__ import annotations

import pytest

from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import RunMode, digest_of, new_id
from vouch_agent.contracts.decision import Verdict
from vouch_agent.contracts.evaluation import (
    USABLE_STATUSES,
    AttemptRecord,
    AttemptStatus,
    EvaluationRun,
    Side,
)
from vouch_agent.contracts.evaluation import Rubric as RubricRecord
from vouch_agent.contracts.project import WorkflowDeclaration
from vouch_agent.errors import ContractError, DigestMismatchError
from vouch_agent.evaluation.comparator import (
    THRESHOLD_MAX_COST_INCREASE_USD,
    build_paired_outcomes,
    compare_run,
    run_with_outcomes,
)
from vouch_agent.evaluation.verdict import freeze_rubric, verdict_from_summary

D = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64


def _attempt(
    case_id: str,
    side: Side,
    status: AttemptStatus = AttemptStatus.OK,
    *,
    cost: float | None = 0.01,
    usage: dict | None = None,
    started_at: str = "2026-09-28T10:00:00.000+00:00",
    repeat_index: int = 0,
    retry_of: str | None = None,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=new_id("att"),
        run_id="run_1",
        side=side,
        case_id=case_id,
        status=status,
        started_at=started_at,
        ended_at=started_at,
        cost_usd=cost,
        usage=dict(usage or {}),
        repeat_index=repeat_index,
        retry_of=retry_of,
    )


def _workflow(guardrails: tuple[str, ...] = ("severe-errors",)) -> WorkflowDeclaration:
    return WorkflowDeclaration(
        workflow_id="wf-compare",
        name="Compare",
        main_objective="submission quality",
        guardrails=guardrails,
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


def _rubric(**overrides) -> RubricRecord:
    if not overrides:
        return _DEFAULT_RUBRIC
    fields = {
        "main_metric": "score",
        "direction": "increase",
        "thresholds": {},
        "hard_guardrails": ("severe-errors",),
        "repeats": 1,
        "frozen_by": "owner",
    }
    fields.update(overrides)
    return freeze_rubric(RubricRecord(**fields), "owner")


def _run(
    attempts,
    *,
    rubric: RubricRecord | None = None,
    execution_status: str = "completed",
    split: CaseSplit = CaseSplit.SELECTION_VALIDATION,
) -> EvaluationRun:
    rubric = rubric or _rubric()
    return EvaluationRun(
        run_id="run_1",
        workflow_id="wf-compare",
        split=split,
        baseline_digest=D,
        candidate_digest=D2,
        case_set_digest=D,
        rubric_digest=rubric.digest(),
        attempts=tuple(attempts),
        mode=RunMode.FIXTURE,
        execution_status=execution_status,
    )


def _pair(case_id: str, base_usage: dict, cand_usage: dict, base_cost=0.01, cand_cost=0.01):
    # Synthetic adapters emit explicit checked-pass hard channels (review A4):
    # the rubric below freezes "severe-errors" as hard, and a usable candidate
    # attempt that never measured a REQUIRED channel is unknown, not clear.
    cand_usage = {"guardrail:severe-errors": 0, **cand_usage}
    return [
        _attempt(case_id, Side.BASELINE, usage=base_usage, cost=base_cost),
        _attempt(case_id, Side.CANDIDATE, usage=cand_usage, cost=cand_cost),
    ]


# -- pairing ------------------------------------------------------------------


def test_paired_outcomes_group_by_case_and_compute_deltas():
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.7}),
        *_pair("case_b", {"score": 0.4}, {"score": 0.4}),
    ]
    outcomes = build_paired_outcomes(_run(attempts))
    assert [o.case_id for o in outcomes] == ["case_a", "case_b"]
    assert outcomes[0].metrics == {"score": pytest.approx(0.2)}
    assert outcomes[1].metrics == {"score": pytest.approx(0.0)}
    assert all(o.complete_pair() for o in outcomes)


def test_failed_planned_attempt_decides_its_repeat_even_after_a_retry():
    """A7 repeat policy: the PLANNED attempt decides a repeat. A retry (here
    an undeclared extra attempt in the same repeat) never resurrects the
    failed repeat nor invents a cross-attempt delta."""
    attempts = [
        _attempt(
            "case_a",
            Side.BASELINE,
            AttemptStatus.FAILED,
            usage={"score": 0.1},
            started_at="2026-09-28T10:00:01.000+00:00",
        ),
        _attempt(
            "case_a",
            Side.BASELINE,
            usage={"score": 0.5},
            started_at="2026-09-28T10:00:02.000+00:00",
        ),
        _attempt("case_a", Side.CANDIDATE, usage={"score": 0.8}),
    ]
    run = _run(attempts)
    outcomes = build_paired_outcomes(run)
    assert outcomes[0].baseline is not None
    # the planned (failed) attempt is the honest representative...
    assert outcomes[0].baseline.status is AttemptStatus.FAILED
    # ...and no delta is invented from the retry + the candidate attempt
    assert outcomes[0].metrics == {}
    assert not outcomes[0].complete_pair()

    summary = compare_run(run, _workflow(), _rubric())
    assert summary.complete_repeat_pairs == 0
    assert summary.retry_attempts == 1
    assert "no silent forgiveness" in summary.uncertainty_note
    assert not summary.acceptance_eligible


def test_explicit_retry_lineage_is_recorded_and_does_not_forgive():
    """retry_of is persisted lineage: the retry is kept for inspection, the
    failed repeat stays failed."""
    rubric = _rubric()
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, AttemptStatus.FAILED, cost=None, usage={}),
    ]
    run = _run(attempts)
    failed_id = run.attempts[1].attempt_id
    attempts = [
        *attempts,
        _attempt(
            "case_a",
            Side.CANDIDATE,
            usage={"score": 0.9, "guardrail:severe-errors": 0},
            retry_of=failed_id,
        ),
    ]
    summary = compare_run(_run(attempts), _workflow(), rubric)
    assert summary.incomplete_reasons["case_a"] == "repeat-0:candidate:failed"
    assert summary.retry_attempts == 1
    assert verdict_from_summary(summary, rubric) is Verdict.REJECTED


def test_side_with_no_usable_attempt_keeps_failure_status_as_representative():
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, AttemptStatus.TIMEOUT, usage={"score": 0.9}),
    ]
    outcomes = build_paired_outcomes(_run(attempts))
    assert outcomes[0].candidate is not None
    assert outcomes[0].candidate.status is AttemptStatus.TIMEOUT
    assert not outcomes[0].complete_pair()


@pytest.mark.parametrize(
    "status", sorted(set(AttemptStatus) - USABLE_STATUSES, key=lambda s: s.value)
)
def test_every_non_ok_status_is_not_success(status):
    """Unknown/non-usable attempt statuses never form a usable pair."""
    attempts = [
        _attempt("case_a", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_a", Side.CANDIDATE, status, usage={"score": 0.9}),
    ]
    outcomes = build_paired_outcomes(_run(attempts))
    assert not outcomes[0].complete_pair()


def test_unknown_status_string_fails_closed_at_contract_layer():
    with pytest.raises(ValueError):
        AttemptRecord.from_dict(
            {
                "schemaVersion": "1",
                "attemptId": "att_x",
                "runId": "run_1",
                "side": "candidate",
                "caseId": "case_a",
                "status": "banana",
            }
        )


# -- comparison summary ---------------------------------------------------------


def test_metric_deltas_are_means_over_complete_pairs():
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.7}),
        *_pair("case_b", {"score": 0.4}, {"score": 0.6}),
        *_pair("case_c", {"score": 0.4}, {"score": 0.4}),
    ]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    delta = summary.metric_deltas["score"]
    assert delta.n_pairs == 3
    assert delta.mean_baseline == pytest.approx((0.5 + 0.4 + 0.4) / 3)
    assert delta.mean_candidate == pytest.approx((0.7 + 0.6 + 0.4) / 3)
    assert delta.mean_delta == pytest.approx((0.2 + 0.2 + 0.0) / 3)
    assert summary.acceptance_eligible


def test_single_hard_violation_is_never_averaged_away():
    """One violating case among spotless others must block acceptance."""
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.9}),
        *_pair("case_b", {"score": 0.5}, {"score": 0.9}),
        *_pair("case_c", {"score": 0.5}, {"score": 0.9, "guardrail:severe-errors": 1}),
    ]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert len(summary.hard_violations) == 1
    assert summary.hard_violations[0].name == "severe-errors"
    assert summary.hard_violations[0].severity == "hard"
    assert "case_c" in summary.hard_violations[0].detail
    assert not summary.acceptance_eligible  # means look great; the violation still blocks


def test_soft_guardrail_recorded_but_not_blocking():
    attempts = [
        *_pair(
            "case_a",
            {"score": 0.5, "guardrail:latency-budget": 0},
            {"score": 0.9, "guardrail:latency-budget": 3.2},
        ),
    ]
    workflow = _workflow(guardrails=("severe-errors", "latency-budget"))
    summary = compare_run(_run(attempts), workflow, _rubric())
    assert not summary.hard_violations
    assert len(summary.soft_violations) == 1
    assert summary.soft_violations[0].name == "latency-budget"
    assert summary.soft_violations[0].severity == "soft"
    assert not summary.missing_guardrail_measurements
    assert summary.acceptance_eligible


def test_hard_guardrail_in_rubric_counted_even_if_workflow_omits_it():
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.9, "guardrail:billing-touch": "yes"}),
    ]
    workflow = _workflow(guardrails=())  # workflow forgot to declare it
    rubric = _rubric(hard_guardrails=("billing-touch",))
    summary = compare_run(_run(attempts, rubric=rubric), workflow, rubric)
    assert len(summary.hard_violations) == 1
    assert summary.hard_violations[0].name == "billing-touch"


def test_baseline_violations_are_context_not_clearance():
    attempts = [
        *_pair("case_a", {"score": 0.5, "guardrail:severe-errors": 2}, {"score": 0.9}),
    ]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert not summary.hard_violations
    assert "baseline violates guardrails" in summary.uncertainty_note


def test_incomplete_pairs_recorded_with_reasons():
    attempts = [
        _attempt("case_ok", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_ok", Side.CANDIDATE, usage={"score": 0.9}),
        # candidate missing entirely
        _attempt("case_missing", Side.BASELINE, usage={"score": 0.5}),
        # candidate timed out (baseline fine)
        _attempt("case_timeout", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_timeout", Side.CANDIDATE, AttemptStatus.TIMEOUT, usage={"score": 0.9}),
        # baseline cancelled
        _attempt("case_basebad", Side.BASELINE, AttemptStatus.CANCELLED, usage={"score": 0.5}),
        _attempt("case_basebad", Side.CANDIDATE, usage={"score": 0.9}),
    ]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert summary.total_pairs == 4
    assert summary.complete_pairs == 1
    assert summary.incomplete_pairs == 3
    # reasons carry the repeat identity since A7 (repeat-<index>:<side>:<status>)
    assert summary.incomplete_reasons["case_missing"] == "repeat-0:candidate:missing"
    assert summary.incomplete_reasons["case_timeout"] == "repeat-0:candidate:timeout"
    assert summary.incomplete_reasons["case_basebad"] == "repeat-0:baseline:cancelled"
    assert not summary.acceptance_eligible
    assert "incomplete" in summary.uncertainty_note


def test_immeasurable_cost_makes_total_none_and_lists_attempts():
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.9}),
        *_pair("case_b", {"score": 0.5}, {"score": 0.9}, cand_cost=None),
    ]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert summary.cost is not None
    assert summary.cost.total_usd is None
    assert summary.cost.measurable is False
    assert len(summary.cost.immeasurable_attempt_ids) == 1
    assert not summary.acceptance_eligible
    assert "immeasurable" in summary.uncertainty_note


def test_failed_attempt_without_cost_is_also_immeasurable():
    """Design §10: failed attempts are billed too — an unpriced failure is a hole."""
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.9}),
        _attempt("case_b", Side.BASELINE, usage={"score": 0.5}),
        _attempt("case_b", Side.CANDIDATE, AttemptStatus.FAILED, cost=None, usage={}),
    ]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert summary.cost is not None and summary.cost.measurable is False


def test_measasurable_costs_summed_per_side():
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.9}, base_cost=0.10, cand_cost=0.20),
        *_pair("case_b", {"score": 0.4}, {"score": 0.6}, base_cost=0.10, cand_cost=0.30),
    ]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert summary.cost is not None
    assert summary.cost.baseline_usd == pytest.approx(0.20)
    assert summary.cost.candidate_usd == pytest.approx(0.50)
    assert summary.cost.total_usd == pytest.approx(0.70)
    assert "measured total cost" in summary.uncertainty_note


def test_cost_increase_beyond_threshold_emits_soft_finding():
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.9}, base_cost=0.10, cand_cost=0.50),
    ]
    rubric = _rubric(thresholds={THRESHOLD_MAX_COST_INCREASE_USD: 0.10})
    summary = compare_run(_run(attempts, rubric=rubric), _workflow(), rubric)
    assert any(v.name == "cost-increase" for v in summary.soft_violations)


def test_repeats_below_requested_recorded_and_blocking():
    rubric = _rubric(repeats=2)
    attempts = [
        *_pair("case_a", {"score": 0.5}, {"score": 0.9}),
        *_pair("case_b", {"score": 0.5}, {"score": 0.9}),
        # case_c does have two complete same-repeat pairs (repeat 0 and 1)
        _attempt(
            "case_c",
            Side.BASELINE,
            usage={"score": 0.5},
            started_at="2026-09-28T10:00:00.000+00:00",
            repeat_index=0,
        ),
        _attempt(
            "case_c",
            Side.BASELINE,
            usage={"score": 0.5},
            started_at="2026-09-28T10:00:01.000+00:00",
            repeat_index=1,
        ),
        _attempt(
            "case_c",
            Side.CANDIDATE,
            usage={"score": 0.9},
            started_at="2026-09-28T10:00:00.000+00:00",
            repeat_index=0,
        ),
        _attempt(
            "case_c",
            Side.CANDIDATE,
            usage={"score": 0.9},
            started_at="2026-09-28T10:00:01.000+00:00",
            repeat_index=1,
        ),
    ]
    summary = compare_run(_run(attempts, rubric=rubric), _workflow(), rubric)
    assert summary.pairs_below_requested_repeats == ("case_a", "case_b")
    assert summary.complete_repeat_pairs == 4  # case_a 1 + case_b 1 + case_c 2
    assert not summary.acceptance_eligible
    assert "repeats" in summary.uncertainty_note


def test_uncertainty_note_carries_sample_size_caveat():
    attempts = [*_pair("case_a", {"score": 0.5}, {"score": 0.9})]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert "cannot guarantee" in summary.uncertainty_note


# -- A7: pairs are formed per (case, repeat), never across repeats ---------------


def _repeat_pair(case_id, base_usage, cand_usage, repeat, *, base_status=None, cand_status=None):
    """One planned repeat: both sides carry the SAME repeat_index."""
    return [
        _attempt(
            case_id,
            Side.BASELINE,
            base_status or AttemptStatus.OK,
            usage=base_usage,
            repeat_index=repeat,
            started_at=f"2026-09-28T10:00:0{repeat}.000+00:00",
        ),
        _attempt(
            case_id,
            Side.CANDIDATE,
            cand_status or AttemptStatus.OK,
            usage=cand_usage,
            repeat_index=repeat,
            started_at=f"2026-09-28T10:00:1{repeat}.000+00:00",
        ),
    ]


def test_successes_of_different_repeats_never_form_one_pair():
    """The A7 counterexample, inverted: repeat 0 baseline failed/candidate ok
    and repeat 1 baseline ok/candidate failed must NOT reassemble into one
    complete pair — with frozen repeats=1 there is no complete pair at all."""
    rubric = _rubric()  # repeats=1
    attempts = [
        *_repeat_pair(
            "case_a",
            {"score": 1.0, "guardrail:severe-errors": 0},
            {"score": 2.0, "guardrail:severe-errors": 0},
            0,
            base_status=AttemptStatus.FAILED,
        ),
        *_repeat_pair(
            "case_a",
            {"score": 1.0, "guardrail:severe-errors": 0},
            {"score": 2.0, "guardrail:severe-errors": 0},
            1,
            cand_status=AttemptStatus.FAILED,
        ),
    ]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert summary.complete_repeat_pairs == 0  # the invented pair is gone
    assert summary.complete_pairs == 0
    # the measured-bad candidate repeat outranks the baseline-side gap
    assert summary.incomplete_reasons["case_a"] == "repeat-1:candidate:failed"
    assert summary.pairs_below_requested_repeats == ("case_a",)
    assert not summary.acceptance_eligible
    assert verdict_from_summary(summary, rubric) is not Verdict.ACCEPTED
    # the measured candidate failure on repeat 1 is a measured bad outcome
    assert verdict_from_summary(summary, rubric) is Verdict.REJECTED


def test_complete_repeats_aggregate_and_genuine_repeat_pairs_count():
    rubric = _rubric(repeats=2)
    attempts = [
        *_repeat_pair(
            "case_a",
            {"score": 1.0, "guardrail:severe-errors": 0},
            {"score": 2.0, "guardrail:severe-errors": 0},
            0,
        ),
        *_repeat_pair(
            "case_a",
            {"score": 1.0, "guardrail:severe-errors": 0},
            {"score": 4.0, "guardrail:severe-errors": 0},
            1,
        ),
    ]
    summary = compare_run(_run(attempts, rubric=rubric), _workflow(), rubric)
    assert summary.complete_repeat_pairs == 2
    delta = summary.metric_deltas["score"]
    assert delta.n_pairs == 2
    assert delta.mean_delta == pytest.approx(((2 - 1) + (4 - 1)) / 2)
    assert verdict_from_summary(summary, rubric) is Verdict.ACCEPTED


def test_display_metrics_never_mix_repeats():
    """Per-case display deltas come from ONE same-repeat pair; a baseline
    value from repeat 1 is never subtracted from a repeat-0 candidate."""
    attempts = [
        _attempt(
            "case_a",
            Side.BASELINE,
            usage={"score": 10.0},
            repeat_index=0,
            started_at="2026-09-28T10:00:00.000+00:00",
        ),
        _attempt(
            "case_a",
            Side.CANDIDATE,
            usage={"score": 2.0, "guardrail:severe-errors": 0},
            repeat_index=0,
            started_at="2026-09-28T10:00:01.000+00:00",
        ),
    ]
    outcomes = build_paired_outcomes(_run(attempts))
    assert outcomes[0].metrics == {"score": pytest.approx(-8.0)}  # same repeat only


def test_missing_repeat_side_surfaces_as_incomplete_repeat():
    rubric = _rubric(repeats=2)
    attempts = [
        *_repeat_pair(
            "case_a",
            {"score": 1.0, "guardrail:severe-errors": 0},
            {"score": 2.0, "guardrail:severe-errors": 0},
            0,
        ),
        # repeat 1: baseline ran, candidate never did
        _attempt(
            "case_a",
            Side.BASELINE,
            usage={"score": 1.0, "guardrail:severe-errors": 0},
            repeat_index=1,
            started_at="2026-09-28T10:00:10.000+00:00",
        ),
    ]
    summary = compare_run(_run(attempts, rubric=rubric), _workflow(), rubric)
    assert summary.incomplete_reasons["case_a"] == "repeat-1:candidate:missing"
    assert summary.complete_repeat_pairs == 1  # repeat 0 still measured
    assert verdict_from_summary(summary, rubric) is Verdict.INCONCLUSIVE


def test_summary_roundtrips_through_dict():
    attempts = [*_pair("case_a", {"score": 0.5}, {"score": 0.9})]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    from vouch_agent.evaluation.comparator import ComparisonSummary

    restored = ComparisonSummary.from_dict(summary.to_dict())
    assert restored.to_dict() == summary.to_dict()
    assert restored.digest() == summary.digest()


def test_run_with_outcomes_attaches_annotated_pairs():
    attempts = [*_pair("case_a", {"score": 0.5}, {"score": 0.9, "guardrail:severe-errors": 1})]
    run = _run(attempts)
    summary = compare_run(run, _workflow(), _rubric())
    annotated = run_with_outcomes(run, summary)
    assert len(annotated.outcomes) == 1
    assert annotated.outcomes[0].guardrail_violations == summary.pairs[0].guardrail_violations


# -- fail-closed guards -----------------------------------------------------------


def test_refuses_unfinished_run():
    attempts = [*_pair("case_a", {"score": 0.5}, {"score": 0.9})]
    for status in ("pending", "running"):
        with pytest.raises(ContractError, match="execution status"):
            compare_run(_run(attempts, execution_status=status), _workflow(), _rubric())


def test_refuses_wrong_workflow():
    attempts = [*_pair("case_a", {"score": 0.5}, {"score": 0.9})]
    other = WorkflowDeclaration(workflow_id="wf-find", name="Find", main_objective="coverage")
    with pytest.raises(ContractError, match="workflow"):
        compare_run(_run(attempts), other, _rubric())


def test_refuses_rubric_digest_mismatch():
    attempts = [*_pair("case_a", {"score": 0.5}, {"score": 0.9})]
    run = _run(attempts)  # bound to _rubric() digest
    different = _rubric(thresholds={"min-main-improvement": 0.5})
    with pytest.raises(DigestMismatchError):
        compare_run(run, _workflow(), different)


def test_run_digest_fields_flow_into_summary():
    attempts = [*_pair("case_a", {"score": 0.5}, {"score": 0.9})]
    summary = compare_run(_run(attempts), _workflow(), _rubric())
    assert summary.baseline_digest == D
    assert summary.candidate_digest == D2
    assert summary.mode is RunMode.FIXTURE
    assert digest_of(_rubric().to_dict()) == summary.rubric_digest
