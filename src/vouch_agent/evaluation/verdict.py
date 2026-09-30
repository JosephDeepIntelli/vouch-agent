"""Apply a frozen Rubric to a comparison and build the acceptance decision.

Fail-closed ordering (design §3.1/§7.2 — thresholds freeze after baseline
measurement, before candidate search):

1. The rubric must be frozen. An unfrozen rubric means the standard was not
   agreed before the candidate was measured — error, not a verdict.
2. Any hard guardrail violation => REJECTED. A single violation is never
   averaged away by good means.
3. A candidate-side failure/timeout on a case whose baseline was usable is a
   *measured bad outcome* => REJECTED.
4. Any other incomplete pair (missing side, cancellation, immeasurable),
   unmeasurable cost accounting, or fewer usable repeats than the rubric
   demands => INCONCLUSIVE. Unknown is never pass.
5. The main metric must be present on *all* complete pairs with a
   direction-adjusted improvement of at least ``min-main-improvement``
   (default 0: non-inferiority) => otherwise REJECTED (measured shortfall)
   or INCONCLUSIVE (main metric unmeasurable / partially covered).

An ACCEPTED verdict constructs an :class:`AcceptanceDecision` carrying an
:class:`ApprovalBinding` over the candidate/rubric/environment/case-set
digests. Any later change to a bound digest makes consuming that approval
raise :class:`ApprovalInvalidatedError` — approvals are bound to bytes, not
to intentions.
"""

from __future__ import annotations

from dataclasses import replace

from vouch_agent.contracts.common import digest_of, new_id, require_digest, utc_now_iso
from vouch_agent.contracts.decision import AcceptanceDecision, ApprovalBinding, Verdict
from vouch_agent.contracts.evaluation import EvaluationRun, Rubric
from vouch_agent.errors import ContractError, DigestMismatchError
from vouch_agent.evaluation.comparator import (
    THRESHOLD_MIN_COMPLETE_PAIRS,
    THRESHOLD_MIN_MAIN_IMPROVEMENT,
    ComparisonSummary,
)

#: Deterministic digest used when a run recorded no environment digest —
#: binding "no environment was recorded" is itself a bound state.
UNRECORDED_ENVIRONMENT_DIGEST = digest_of({"environmentDigest": None})

_ALLOWED_RELEASE_SCOPES = ("none", "project", "environment")

#: Candidate attempt statuses that are *measured bad* (as opposed to unknown).
_MEASURED_BAD = ("failed", "timeout")


def _is_measured_bad_candidate(reason: str) -> bool:
    """True when an incomplete reason names a measured-bad CANDIDATE outcome.

    Reasons are ``<side>:<status>`` or, since repeat pairing (A7),
    ``repeat-<index>:<side>:<status>`` — the trailing ``side:status`` pair is
    what the verdict reads, so both spellings mean the same thing.
    """
    parts = reason.split(":")
    if len(parts) < 2:
        return False
    side, status = parts[-2], parts[-1]
    return side == "candidate" and status in _MEASURED_BAD


def freeze_rubric(rubric: Rubric, frozen_by: str) -> Rubric:
    """Freeze a rubric once, recording who froze it.

    Re-freezing or editing a frozen rubric is refused: changing the standard
    means creating a new rubric version (a new digest), which is exactly what
    invalidates approvals bound to the old one.
    """
    if rubric.frozen:
        raise ContractError(
            "rubric is already frozen; create a new rubric version instead of re-freezing"
        )
    if not isinstance(frozen_by, str) or not frozen_by.strip():
        raise ContractError("frozen_by must be a non-empty identity string")
    return replace(rubric, frozen_by=frozen_by, frozen_at=utc_now_iso())


def binding_for_run(run: EvaluationRun, rubric: Rubric) -> ApprovalBinding:
    """The digest tuple an approval for this run/rubric is valid for."""
    return ApprovalBinding(
        candidate_digest=run.candidate_digest,
        rubric_digest=run.rubric_digest,
        environment_digest=run.environment_digest or UNRECORDED_ENVIRONMENT_DIGEST,
        acceptance_case_set_digest=run.case_set_digest,
    )


def verdict_from_summary(summary: ComparisonSummary, rubric: Rubric) -> Verdict:
    """Pure verdict computation — no decision record, no side effects."""
    if not rubric.frozen:
        raise ContractError(
            "rubric thresholds are not frozen; refusing to verdict (fail closed) — "
            "freeze the rubric before candidate measurement"
        )
    if summary.total_pairs == 0:
        return Verdict.INCONCLUSIVE

    # (1) hard guardrails: one violation on ANY attempt (earlier repeats
    # included) is enough, means are irrelevant.
    if summary.hard_violations:
        return Verdict.REJECTED

    # (2) measured-bad candidate outcomes on otherwise measurable repeats.
    # Reasons may carry a repeat prefix since A7 (repeat-<i>:candidate:failed).
    for reason in summary.incomplete_reasons.values():
        if _is_measured_bad_candidate(reason):
            return Verdict.REJECTED

    # (3) anything still unknown => inconclusive, never pass. Missing
    # REQUIRED hard-guardrail measurements are unknown, not clear.
    if summary.incomplete_pairs:
        return Verdict.INCONCLUSIVE
    if summary.missing_guardrail_measurements:
        return Verdict.INCONCLUSIVE
    if summary.cost is None or not summary.cost.measurable:
        return Verdict.INCONCLUSIVE
    if summary.pairs_below_requested_repeats:
        return Verdict.INCONCLUSIVE

    # (4) main metric: unmeasurable or partially covered => inconclusive.
    # Coverage is checked against every usable repeat pair, not one
    # representative attempt per case (review A4).
    main = summary.metric_deltas.get(rubric.main_metric)
    expected_pairs = summary.complete_repeat_pairs or summary.complete_pairs
    if main is None or main.n_pairs < expected_pairs:
        return Verdict.INCONCLUSIVE

    # (5) measured shortfall against the frozen threshold => rejected.
    improvement = main.mean_delta if rubric.direction == "increase" else -main.mean_delta
    required = float(rubric.thresholds.get(THRESHOLD_MIN_MAIN_IMPROVEMENT, 0.0))
    if improvement < required - 1e-12:
        return Verdict.REJECTED

    min_pairs = rubric.thresholds.get(THRESHOLD_MIN_COMPLETE_PAIRS)
    if min_pairs is not None and summary.complete_pairs < int(min_pairs):
        return Verdict.INCONCLUSIVE

    return Verdict.ACCEPTED


def decide(
    summary: ComparisonSummary,
    run: EvaluationRun,
    rubric: Rubric,
    *,
    owner: str,
    evidence_digest: str,
    allowed_release_scope: str = "none",
    note: str = "",
) -> AcceptanceDecision:
    """Apply the frozen rubric and construct the acceptance decision.

    ``owner`` is the acceptance owner's identity — never the proposer (the
    contract enforces this). ``evidence_digest`` is the digest of the evidence
    package backing the verdict (see ``vouch_agent.export.evidence``); the
    decision is only as trustworthy as the bundle it points at.
    """
    if not isinstance(owner, str) or not owner.strip():
        raise ContractError("owner must be a non-empty identity string")
    if owner == "proposer":
        raise ContractError("the proposer cannot be the acceptance owner")
    evidence_digest = require_digest(evidence_digest, "evidence_digest")
    if allowed_release_scope not in _ALLOWED_RELEASE_SCOPES:
        raise ContractError(
            f"allowed_release_scope must be one of {_ALLOWED_RELEASE_SCOPES}, "
            f"got {allowed_release_scope!r}"
        )
    if summary.run_id != run.run_id:
        raise ContractError(
            f"summary is for run {summary.run_id!r}, decision requested for {run.run_id!r}"
        )
    if run.rubric_digest != rubric.digest():
        raise DigestMismatchError(
            f"run {run.run_id!r} was executed under rubric {run.rubric_digest}, "
            f"not the supplied rubric {rubric.digest()}"
        )
    if summary.rubric_digest != rubric.digest():
        raise DigestMismatchError(
            f"summary was computed under rubric {summary.rubric_digest}, "
            f"not the supplied rubric {rubric.digest()}"
        )

    verdict = verdict_from_summary(summary, rubric)
    combined_note = " ".join(part for part in (note.strip(), summary.uncertainty_note) if part)
    return AcceptanceDecision(
        decision_id=new_id("dec"),
        verdict=verdict,
        candidate_digest=run.candidate_digest,
        evidence_digest=evidence_digest,
        owner=owner,
        decided_at=utc_now_iso(),
        allowed_release_scope=allowed_release_scope,
        binding=binding_for_run(run, rubric) if verdict is Verdict.ACCEPTED else None,
        final_acceptance_run_id=run.run_id,
        note=combined_note,
    )


def verify_decision(
    decision: AcceptanceDecision,
    *,
    candidate_digest: str,
    rubric_digest: str,
    environment_digest: str,
    acceptance_case_set_digest: str,
) -> None:
    """Re-verify an approval against the digests as they are *now*.

    Raises :class:`ApprovalInvalidatedError` when any bound digest changed —
    the approval no longer applies and the candidate must re-enter evaluation.
    """
    if decision.binding is None:
        raise ContractError(
            f"decision {decision.decision_id!r} carries no approval binding to verify"
        )
    decision.binding.verify(
        candidate_digest=candidate_digest,
        rubric_digest=rubric_digest,
        environment_digest=environment_digest,
        acceptance_case_set_digest=acceptance_case_set_digest,
    )


def verify_decision_for_run(
    decision: AcceptanceDecision, run: EvaluationRun, rubric: Rubric
) -> None:
    """Convenience wrapper: verify against the run/rubric as currently loaded."""
    verify_decision(
        decision,
        candidate_digest=run.candidate_digest,
        rubric_digest=run.rubric_digest,
        environment_digest=run.environment_digest or UNRECORDED_ENVIRONMENT_DIGEST,
        acceptance_case_set_digest=run.case_set_digest,
    )


__all__ = [
    "UNRECORDED_ENVIRONMENT_DIGEST",
    "binding_for_run",
    "decide",
    "freeze_rubric",
    "verdict_from_summary",
    "verify_decision",
    "verify_decision_for_run",
]
