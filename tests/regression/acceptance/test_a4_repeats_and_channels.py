"""A4 regressions (evaluation side): repeat semantics, hard-guardrail
channels, and the expected case matrix.

Converted from the coordinator reproduction `review-acceptance-20260928`
(defects 4 and 5 plus the missing-whole-case follow-up). The pure functions
exercised here are the same trusted computation the controller runs; the
closing tests drive the public controller entry points.
"""

from __future__ import annotations

import pytest
from support import (
    ChannelAdapter,
    frozen_rubric,
    make_baseline,
    make_candidate,
    make_controller,
    make_pack,
    rubric,
    seed_inputs,
)

from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import Role, RunMode
from vouch_agent.contracts.decision import Verdict
from vouch_agent.contracts.evaluation import (
    AttemptRecord,
    AttemptStatus,
    EvaluationRun,
    Side,
)
from vouch_agent.contracts.project import WorkflowDeclaration
from vouch_agent.errors import ContractError
from vouch_agent.evaluation import compare_run, verdict_from_summary

D = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
D3 = "sha256:" + "c" * 64

WORKFLOW = WorkflowDeclaration(workflow_id="wf-compare", name="Compare", main_objective="score")


def _attempt(
    case_id: str,
    side: Side,
    usage: dict,
    *,
    cost: float | None = 0.01,
    status: AttemptStatus = AttemptStatus.OK,
    started_at: str = "2026-01-01T00:00:00.000+00:00",
    index: int = 0,
    repeat: int = 0,
    retry_of: str | None = None,
) -> AttemptRecord:
    """``index`` only disambiguates attempt ids; ``repeat`` is the PLANNED
    repeat the attempt belongs to (A7 pairing is by case+repeat)."""
    return AttemptRecord(
        attempt_id=f"att-{case_id}-{side.value}-{index}",
        run_id="run-r",
        side=side,
        case_id=case_id,
        status=status,
        started_at=started_at,
        ended_at=started_at,
        cost_usd=cost,
        usage=dict(usage),
        repeat_index=repeat,
        retry_of=retry_of,
    )


def _run(attempts, *, rubric, execution_status: str = "completed") -> EvaluationRun:
    return EvaluationRun(
        run_id="run-r",
        workflow_id="wf-compare",
        split=CaseSplit.SELECTION_VALIDATION,
        baseline_digest=D,
        candidate_digest=D2,
        case_set_digest=D3,
        rubric_digest=rubric.digest(),
        attempts=tuple(attempts),
        mode=RunMode.FIXTURE,
        execution_status=execution_status,
    )


def _ts(second: int) -> str:
    return f"2026-01-01T00:00:{second:02d}.000+00:00"


# -- defect 4: an earlier hard violation can never disappear -----------------------------


def test_earlier_hard_violation_survives_a_later_successful_repeat() -> None:
    """Reproduction 4 (inverted): baseline 1,1 vs candidate -100,2 with a hard
    violation on candidate repeat 1 must NOT be accepted."""
    rubric = frozen_rubric(repeats=2)
    clean = {"score": 1, "guardrail:unsafe": 0}
    attempts = (
        _attempt("case-1", Side.BASELINE, clean, index=0, repeat=0, started_at=_ts(0)),
        _attempt("case-1", Side.BASELINE, clean, index=1, repeat=1, started_at=_ts(1)),
        _attempt(
            "case-1",
            Side.CANDIDATE,
            {"score": -100, "guardrail:unsafe": 1},
            index=0,
            repeat=0,
            started_at=_ts(2),
        ),
        _attempt(
            "case-1",
            Side.CANDIDATE,
            {"score": 2, "guardrail:unsafe": 0},
            index=1,
            repeat=1,
            started_at=_ts(3),
        ),
    )
    summary = compare_run(_run(attempts, rubric=rubric), WORKFLOW, rubric)
    assert summary.hard_violations, "the repeat-1 violation was erased"
    assert any("att-case-1-candidate-0" in finding.detail for finding in summary.hard_violations)
    assert verdict_from_summary(summary, rubric) is Verdict.REJECTED


def test_all_intended_repeats_aggregate_into_the_metric_delta() -> None:
    rubric = frozen_rubric(repeats=2)
    clean = {"score": 1, "guardrail:unsafe": 0}
    attempts = (
        _attempt("case-1", Side.BASELINE, clean, index=0, repeat=0, started_at=_ts(0)),
        _attempt("case-1", Side.BASELINE, clean, index=1, repeat=1, started_at=_ts(1)),
        _attempt(
            "case-1",
            Side.CANDIDATE,
            {"score": 2, "guardrail:unsafe": 0},
            index=0,
            repeat=0,
            started_at=_ts(2),
        ),
        _attempt(
            "case-1",
            Side.CANDIDATE,
            {"score": 4, "guardrail:unsafe": 0},
            index=1,
            repeat=1,
            started_at=_ts(3),
        ),
    )
    summary = compare_run(_run(attempts, rubric=rubric), WORKFLOW, rubric)
    delta = summary.metric_deltas["score"]
    assert delta.n_pairs == 2  # both repeats, not just the latest
    assert delta.mean_delta == pytest.approx(((2 - 1) + (4 - 1)) / 2)
    assert verdict_from_summary(summary, rubric).value == "accepted"


def test_usable_retry_after_failed_repeat_still_counts_the_failure() -> None:
    """A failed repeat is not erased by a later successful one: the run keeps
    it as a measured failure (A7: candidate failed a repeat whose baseline
    was usable => REJECTED, a firmer refusal than inconclusive), and the
    unpriced failure keeps cost accounting immeasurable."""
    rubric = frozen_rubric(repeats=2)
    clean = {"score": 1, "guardrail:unsafe": 0}
    attempts = (
        _attempt("case-1", Side.BASELINE, clean, index=0, repeat=0, started_at=_ts(0)),
        _attempt("case-1", Side.BASELINE, clean, index=1, repeat=1, started_at=_ts(1)),
        _attempt(
            "case-1",
            Side.CANDIDATE,
            {},
            status=AttemptStatus.FAILED,
            cost=None,
            index=0,
            repeat=0,
            started_at=_ts(2),
        ),
        _attempt(
            "case-1",
            Side.CANDIDATE,
            {"score": 2, "guardrail:unsafe": 0},
            index=1,
            repeat=1,
            started_at=_ts(3),
        ),
    )
    summary = compare_run(_run(attempts, rubric=rubric), WORKFLOW, rubric)
    # the failed attempt was real: cost accounting must stay immeasurable
    assert summary.cost is not None and not summary.cost.measurable
    assert summary.incomplete_reasons["case-1"] == "repeat-0:candidate:failed"
    assert verdict_from_summary(summary, rubric).value != "accepted"
    assert verdict_from_summary(summary, rubric).value == "rejected"


# -- defect 5: missing required hard channel is unknown, never pass ------------------------


def test_missing_required_hard_channel_is_inconclusive() -> None:
    """Reproduction 5 (inverted): rubric hard `unsafe`, candidate usage omits
    the channel -> inconclusive, never an implicit pass."""
    rubric = frozen_rubric(repeats=1)
    attempts = (
        _attempt("case-1", Side.BASELINE, {"score": 1, "guardrail:unsafe": 0}, index=0),
        _attempt("case-1", Side.CANDIDATE, {"score": 2}, index=1),  # channel omitted
    )
    summary = compare_run(_run(attempts, rubric=rubric), WORKFLOW, rubric)
    assert summary.missing_guardrail_measurements
    assert any(f.name == "unsafe" for f in summary.missing_guardrail_measurements)
    assert not summary.acceptance_eligible
    assert verdict_from_summary(summary, rubric) is Verdict.INCONCLUSIVE


def test_invalid_hard_channel_value_is_inconclusive() -> None:
    rubric = frozen_rubric(repeats=1)
    attempts = (
        _attempt("case-1", Side.BASELINE, {"score": 1, "guardrail:unsafe": 0}, index=0),
        _attempt(
            "case-1",
            Side.CANDIDATE,
            {"score": 2, "guardrail:unsafe": {"nested": True}},  # non-scalar junk
            index=1,
        ),
    )
    summary = compare_run(_run(attempts, rubric=rubric), WORKFLOW, rubric)
    assert any(f.name == "unsafe" for f in summary.missing_guardrail_measurements)
    assert verdict_from_summary(summary, rubric) is Verdict.INCONCLUSIVE


def test_explicit_checked_pass_channel_accepts_cleanly() -> None:
    rubric = frozen_rubric(repeats=1)
    attempts = (
        _attempt("case-1", Side.BASELINE, {"score": 1, "guardrail:unsafe": 0}, index=0),
        _attempt("case-1", Side.CANDIDATE, {"score": 2, "guardrail:unsafe": 0}, index=1),
    )
    summary = compare_run(_run(attempts, rubric=rubric), WORKFLOW, rubric)
    assert not summary.missing_guardrail_measurements
    assert summary.acceptance_eligible
    assert verdict_from_summary(summary, rubric) is Verdict.ACCEPTED


# -- follow-up: the expected case/side/repeat matrix is authoritative ----------------------


def test_expected_matrix_surfaces_missing_case_instead_of_shrinking() -> None:
    rubric = frozen_rubric(repeats=1)
    attempts = (
        _attempt("case-2", Side.BASELINE, {"score": 1, "guardrail:unsafe": 0}, index=0),
        _attempt("case-2", Side.CANDIDATE, {"score": 2, "guardrail:unsafe": 0}, index=1),
        # case-1 had BOTH sides deleted from the record
    )
    run = _run(attempts, rubric=rubric)
    summary = compare_run(run, WORKFLOW, rubric, expected_case_ids=("case-1", "case-2"))
    assert summary.total_pairs == 2  # denominator comes from the expected matrix
    assert summary.incomplete_pairs == 1
    assert "case-1" in summary.incomplete_reasons
    assert verdict_from_summary(summary, rubric) is Verdict.INCONCLUSIVE


def test_expected_matrix_surfaces_missing_side_and_missing_repeat() -> None:
    rubric = frozen_rubric(repeats=2)
    attempts = (
        _attempt("case-1", Side.BASELINE, {"score": 1, "guardrail:unsafe": 0}, index=0),
        # case-1 baseline repeat 2 missing, candidate side missing entirely
    )
    run = _run(attempts, rubric=rubric)
    summary = compare_run(run, WORKFLOW, rubric, expected_case_ids=("case-1",))
    assert summary.incomplete_reasons["case-1"] == "repeat-0:candidate:missing"
    assert summary.pairs_below_requested_repeats == ("case-1",)


# -- controller-level: repeat violations block qualification --------------------------------


def test_selection_run_with_early_violation_cannot_qualify_candidate() -> None:
    """The same defect driven through the public controller: a candidate
    whose first repeat trips the hard guardrail must not reach 'evaluated'."""
    controller, _store, _ledger, _journal, artifacts = make_controller()
    controller.init()
    pack = make_pack()
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), "wf-compare")
    rubric_digest = controller.freeze_rubric(rubric(repeats=2), "ana")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)

    adapter = ChannelAdapter(
        channels=("unsafe",),
        violate_repeats=frozenset({0}),  # repeat 0 violates; repeat 1 is clean
        baseline_values=(1.0, 1.0),
        candidate_values=(-100.0, 2.0),
    )
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
        repeats=2,
    )
    assert len(run.attempts) == 4  # 1 case x 2 sides x 2 repeats
    with pytest.raises(ContractError):
        controller.mark_evaluated("cand-acc")
    assert controller.candidate("cand-acc").state.value == "sealed"


def test_pending_or_foreign_runs_never_compare() -> None:
    rubric = frozen_rubric(repeats=1)
    with pytest.raises(ContractError, match="execution status"):
        compare_run(_run([], rubric=rubric, execution_status="pending"), WORKFLOW, rubric)
