"""Evaluation package: paired comparison, guardrails, rubric verdicts.

Public surface used by the controller and CLI:

* :func:`vouch_agent.evaluation.comparator.compare_run` — build a
  :class:`~vouch_agent.evaluation.comparator.ComparisonSummary` from a
  finished :class:`~vouch_agent.contracts.evaluation.EvaluationRun`.
* :func:`vouch_agent.evaluation.verdict.decide` — apply a frozen
  :class:`~vouch_agent.contracts.evaluation.Rubric` and construct an
  :class:`~vouch_agent.contracts.decision.AcceptanceDecision` with its
  :class:`~vouch_agent.contracts.decision.ApprovalBinding`.
* :func:`vouch_agent.evaluation.verdict.verify_decision` /
  :func:`vouch_agent.evaluation.verdict.verify_decision_for_run` — re-verify
  an approval against current digests (raises
  :class:`vouch_agent.errors.ApprovalInvalidatedError` on any change).
* :func:`vouch_agent.evaluation.attestation_evidence.resolve_attestation_evidence`
  — resolve a runner-integration promotion claim against OBSERVED durable
  evidence (M3 Gate A5). Imported by the lead-owned manifest promotion path;
  a bare claim never promotes coverage on its own.
"""

from vouch_agent.evaluation.attestation_evidence import (
    AttestationClaim,
    AttestationError,
    ObservedUnpassedCoverageError,
    ResolvedAttestationEvidence,
    build_runner_integration_fields,
    descriptor_digest,
    resolve_attestation_evidence,
)
from vouch_agent.evaluation.comparator import (
    GUARDRAIL_USAGE_PREFIX,
    THRESHOLD_MAX_COST_INCREASE_USD,
    THRESHOLD_MIN_COMPLETE_PAIRS,
    THRESHOLD_MIN_MAIN_IMPROVEMENT,
    ComparisonSummary,
    CostSummary,
    MetricDelta,
    build_paired_outcomes,
    compare_run,
    run_with_outcomes,
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

__all__ = [
    "GUARDRAIL_USAGE_PREFIX",
    "THRESHOLD_MAX_COST_INCREASE_USD",
    "THRESHOLD_MIN_COMPLETE_PAIRS",
    "THRESHOLD_MIN_MAIN_IMPROVEMENT",
    "UNRECORDED_ENVIRONMENT_DIGEST",
    "AttestationClaim",
    "AttestationError",
    "ComparisonSummary",
    "CostSummary",
    "MetricDelta",
    "ObservedUnpassedCoverageError",
    "ResolvedAttestationEvidence",
    "binding_for_run",
    "build_paired_outcomes",
    "build_runner_integration_fields",
    "compare_run",
    "decide",
    "descriptor_digest",
    "freeze_rubric",
    "resolve_attestation_evidence",
    "run_with_outcomes",
    "verdict_from_summary",
    "verify_decision",
    "verify_decision_for_run",
]
