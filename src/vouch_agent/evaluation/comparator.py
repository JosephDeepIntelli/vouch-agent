"""Paired comparison over an EvaluationRun (design §7.2 — 信证).

Honesty rules this module exists to enforce:

* **A single hard guardrail violation is never averaged away.** Hard
  violations are kept as findings; any one of them makes the run
  unacceptable regardless of how good the mean deltas look. Every attempt is
  inspected — an earlier repeat's violation cannot disappear behind a later
  successful repeat (review A4).
* **Missing measurements are unknown, never pass.** A REQUIRED (rubric-hard)
  guardrail channel that a usable candidate attempt does not carry is a
  *missing measurement*, recorded explicitly and blocking acceptance.
* **Incomplete pairs and immeasurable costs are inconclusive, not success.**
  A pair is usable only when both sides produced usable attempts
  (``USABLE_STATUSES``); anything else — missing attempt, failure, timeout,
  cancellation, immeasurable — is recorded with a reason and blocks
  acceptance.
* **Unknown attempt statuses are never success.** Only ``ok`` counts; the
  contract layer refuses to even construct unknown status strings.
* **Repeats and uncertainty are recorded.** Attempts pair by (case,
  ``repeat_index``) — never by independent filtering of each side's
  successes, which can reassemble a "pair" that was never measured together
  (A7). A repeat is complete only when the DECIDING attempt on both sides is
  usable; a repeat with a failed or missing side is a failed/incomplete
  repeat and is recorded with its reason. Fewer complete repeats than the
  frozen rubric demands, partial metric coverage and unmeasured costs all
  land in the uncertainty note instead of being rounded away. Retries do not
  forgive a repeat (see the repeat policy below).
* **The expected case matrix is authoritative.** When callers supply
  ``expected_case_ids`` (the trusted path always derives them from the bound
  pack), a case whose attempts were deleted from the run surfaces as a
  missing case instead of silently shrinking the denominator.

Repeat pairing policy (A7, v1.2 — explicit, no silent forgiveness):

* Every attempt carries the planned repeat it belongs to
  (``AttemptRecord.repeat_index``). Pairs are formed per (case,
  repeat_index) only; successes from DIFFERENT repeats are never combined
  into one comparison.
* A repeat's DECIDING attempt on one side is its planned attempt — the
  unique attempt with ``retry_of=None``. When a repeat carries several
  attempts and none declares lineage (records predating repeat identity),
  the earliest attempt is treated as the planned one and the rest as
  undeclared retries, so legacy data stays deterministic.
* A repeat is COMPLETE only when the deciding attempt on both sides is
  usable. Otherwise it is a failed/incomplete repeat, recorded as
  ``repeat-<index>:<side>:<status>`` (or ``...:<side>:missing``). An explicit
  retry (``retry_of`` set) never changes its repeat's outcome: retries are
  kept for guardrail inspection and surfaced in the uncertainty note. There
  is no forgiveness — a verdict changes only through a new evaluation run.
* A case is complete only when EVERY repeat observed on it is complete; one
  failed repeat is a measured fact the verdict must retain even when the
  rubric demanded fewer repeats than were scheduled.

Cost accounting here is deliberately stricter than
``EvaluationRun.total_cost_usd``: a comparison total is measurable only when
*every* attempt on both sides (failed ones included — design §10: "不能只对
成功候选计费") carries a measured cost. One unpriced attempt makes the whole
comparison's cost immeasurable.

Conventions (v1, kept local to this module):

* Per-attempt metric values live in ``AttemptRecord.usage`` under their plain
  metric name (e.g. ``score``, ``latency_s``). Keys prefixed
  ``guardrail:`` are guardrail channels, not metrics.
* A guardrail ``<name>`` declared by the workflow (or frozen as hard in the
  rubric) is violated on a case when an attempt's
  ``usage["guardrail:<name>"]`` is a positive number or otherwise truthy.
  Severity is ``hard`` exactly when the name is in ``rubric.hard_guardrails``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, TypeGuard

from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import ContractRecord, RunMode
from vouch_agent.contracts.evaluation import (
    USABLE_STATUSES,
    AttemptRecord,
    EvaluationRun,
    GuardrailFinding,
    PairedOutcome,
    Rubric,
    Side,
)
from vouch_agent.contracts.project import WorkflowDeclaration
from vouch_agent.errors import ContractError, DigestMismatchError

GUARDRAIL_USAGE_PREFIX = "guardrail:"

#: Threshold key: required direction-adjusted improvement of the main metric.
THRESHOLD_MIN_MAIN_IMPROVEMENT = "min-main-improvement"
#: Threshold key: minimum number of complete pairs for a conclusive verdict.
THRESHOLD_MIN_COMPLETE_PAIRS = "min-complete-pairs"
#: Threshold key: allowed candidate cost increase in USD (soft economics check).
THRESHOLD_MAX_COST_INCREASE_USD = "max-cost-increase-usd"

#: Incomplete-pair reason recorded when the expected matrix names a case the
#: run's attempts do not contain at all (review A4: deleting both sides of a
#: case must surface, not shrink the denominator).
REASON_CASE_MISSING = "case:missing-from-run"

#: Separator used in repeat-tagged incomplete reasons:
#: ``repeat-<index>:<side>:<status|missing>`` (A7).
REASON_REPEAT_PREFIX = "repeat-"

_SAMPLE_SIZE_CAVEAT = (
    "finite case sample: passing cannot guarantee absence of regressions on "
    "unseen tasks (design §7.1)"
)


def _is_number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_violation(value: Any) -> bool:
    if value is None:
        return False
    if _is_number(value):
        return value > 0
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _is_measured_channel(value: Any) -> bool:
    """A guardrail channel reading the comparison can interpret.

    Numbers, booleans and strings carry a measurement (violating or not);
    ``None``/absent means unmeasured, and non-scalar junk is invalid — both
    are *unknown*, never an implicit pass (review A4).
    """
    return value is not None and not isinstance(value, (list, dict, set))


@dataclass(frozen=True)
class MetricDelta(ContractRecord):
    """Aggregated per-metric delta over complete repeat pairs."""

    metric: str
    n_pairs: int
    mean_baseline: float
    mean_candidate: float
    mean_delta: float  # candidate - baseline
    schema_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "metric": self.metric,
            "nPairs": self.n_pairs,
            "meanBaseline": self.mean_baseline,
            "meanCandidate": self.mean_candidate,
            "meanDelta": self.mean_delta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetricDelta:
        cls._check_version(data)
        return MetricDelta(
            metric=str(data["metric"]),
            n_pairs=int(data["nPairs"]),
            mean_baseline=float(data["meanBaseline"]),
            mean_candidate=float(data["meanCandidate"]),
            mean_delta=float(data["meanDelta"]),
        )


@dataclass(frozen=True)
class CostSummary(ContractRecord):
    """Full-burden cost of the comparison, or an explicit immeasurable verdict."""

    baseline_usd: float | None
    candidate_usd: float | None
    total_usd: float | None
    #: Attempt ids whose cost is unmeasured — the reason a None total exists.
    immeasurable_attempt_ids: tuple[str, ...] = ()
    schema_version: str = "1"

    @property
    def measurable(self) -> bool:
        return self.total_usd is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "baselineUsd": self.baseline_usd,
            "candidateUsd": self.candidate_usd,
            "totalUsd": self.total_usd,
            "immeasurableAttemptIds": list(self.immeasurable_attempt_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CostSummary:
        cls._check_version(data)
        return CostSummary(
            baseline_usd=data.get("baselineUsd"),
            candidate_usd=data.get("candidateUsd"),
            total_usd=data.get("totalUsd"),
            immeasurable_attempt_ids=tuple(data.get("immeasurableAttemptIds", [])),
        )


@dataclass(frozen=True)
class ComparisonSummary(ContractRecord):
    """Everything an acceptance decision needs, with nothing averaged away."""

    run_id: str
    workflow_id: str
    mode: RunMode
    split: CaseSplit
    baseline_digest: str
    candidate_digest: str
    case_set_digest: str
    rubric_digest: str
    pairs: tuple[PairedOutcome, ...]
    total_pairs: int
    complete_pairs: int
    incomplete_pairs: int
    #: case_id -> why the pair is not usable ("candidate:failed", "baseline:missing", ...)
    incomplete_reasons: dict[str, str] = field(default_factory=dict)
    repeats_requested: int = 1
    pairs_below_requested_repeats: tuple[str, ...] = ()
    #: Usable baseline/candidate repeat pairs formed across complete cases —
    #: the denominator main-metric coverage is checked against (repeats
    #: aggregate; one attempt per case is no longer the whole story). Pairs
    #: are formed per (case, repeat_index); successes from different repeats
    #: are never combined (A7).
    complete_repeat_pairs: int = 0
    #: Attempts recorded as explicit retries (``retry_of`` set) or carried as
    #: undeclared extra attempts within one repeat. Retries never change a
    #: repeat's outcome; they are counted here so forgiveness can never be
    #: silent (A7).
    retry_attempts: int = 0
    metric_deltas: dict[str, MetricDelta] = field(default_factory=dict)
    hard_violations: tuple[GuardrailFinding, ...] = ()
    soft_violations: tuple[GuardrailFinding, ...] = ()
    #: REQUIRED (rubric-hard) channels a usable candidate attempt never
    #: measured — unknown, blocking, never an implicit pass (review A4).
    missing_guardrail_measurements: tuple[GuardrailFinding, ...] = ()
    cost: CostSummary | None = None
    uncertainty_note: str = ""
    schema_version: str = "1"

    @property
    def acceptance_eligible(self) -> bool:
        """Facts-only gate: nothing known-bad, nothing unknown, fully priced.

        Thresholds are applied separately (see ``vouch_agent.evaluation.verdict``)
        — this property only says "the evidence is complete and clean enough
        to be acceptable at all".
        """
        return (
            self.total_pairs > 0
            and self.incomplete_pairs == 0
            and not self.hard_violations
            and not self.missing_guardrail_measurements
            and not self.pairs_below_requested_repeats
            and self.cost is not None
            and self.cost.measurable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "runId": self.run_id,
            "workflowId": self.workflow_id,
            "mode": self.mode.value,
            "split": self.split.value,
            "baselineDigest": self.baseline_digest,
            "candidateDigest": self.candidate_digest,
            "caseSetDigest": self.case_set_digest,
            "rubricDigest": self.rubric_digest,
            "pairs": [p.to_dict() for p in self.pairs],
            "totalPairs": self.total_pairs,
            "completePairs": self.complete_pairs,
            "incompletePairs": self.incomplete_pairs,
            "incompleteReasons": dict(self.incomplete_reasons),
            "repeatsRequested": self.repeats_requested,
            "pairsBelowRequestedRepeats": list(self.pairs_below_requested_repeats),
            "completeRepeatPairs": self.complete_repeat_pairs,
            "retryAttempts": self.retry_attempts,
            "metricDeltas": {k: v.to_dict() for k, v in self.metric_deltas.items()},
            "hardViolations": [v.to_dict() for v in self.hard_violations],
            "softViolations": [v.to_dict() for v in self.soft_violations],
            "missingGuardrailMeasurements": [
                v.to_dict() for v in self.missing_guardrail_measurements
            ],
            "cost": self.cost.to_dict() if self.cost else None,
            "uncertaintyNote": self.uncertainty_note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComparisonSummary:
        cls._check_version(data)
        return ComparisonSummary(
            run_id=str(data["runId"]),
            workflow_id=str(data["workflowId"]),
            mode=RunMode(data["mode"]),
            split=CaseSplit(data["split"]),
            baseline_digest=str(data["baselineDigest"]),
            candidate_digest=str(data["candidateDigest"]),
            case_set_digest=str(data["caseSetDigest"]),
            rubric_digest=str(data["rubricDigest"]),
            pairs=tuple(PairedOutcome.from_dict(p) for p in data.get("pairs", [])),
            total_pairs=int(data["totalPairs"]),
            complete_pairs=int(data["completePairs"]),
            incomplete_pairs=int(data["incompletePairs"]),
            incomplete_reasons=dict(data.get("incompleteReasons", {})),
            repeats_requested=int(data.get("repeatsRequested", 1)),
            pairs_below_requested_repeats=tuple(data.get("pairsBelowRequestedRepeats", [])),
            complete_repeat_pairs=int(data.get("completeRepeatPairs", 0)),
            retry_attempts=int(data.get("retryAttempts", 0)),
            metric_deltas={
                k: MetricDelta.from_dict(v) for k, v in data.get("metricDeltas", {}).items()
            },
            hard_violations=tuple(
                GuardrailFinding.from_dict(v) for v in data.get("hardViolations", [])
            ),
            soft_violations=tuple(
                GuardrailFinding.from_dict(v) for v in data.get("softViolations", [])
            ),
            missing_guardrail_measurements=tuple(
                GuardrailFinding.from_dict(v) for v in data.get("missingGuardrailMeasurements", [])
            ),
            cost=CostSummary.from_dict(data["cost"]) if data.get("cost") else None,
            uncertainty_note=str(data.get("uncertaintyNote", "")),
        )


# -- pairing -----------------------------------------------------------------


def _ordered(attempts: list[AttemptRecord]) -> list[AttemptRecord]:
    return sorted(attempts, key=lambda a: (a.started_at, a.attempt_id))


@dataclass(frozen=True)
class RepeatSides:
    """One planned repeat of one case: the deciding attempt per side (A7)."""

    repeat_index: int
    baseline: AttemptRecord | None
    candidate: AttemptRecord | None
    #: Attempts beyond the planned one on either side — explicit retries
    #: (``retry_of`` set) or undeclared extras. Kept for inspection; they
    #: never change this repeat's outcome.
    retries: tuple[AttemptRecord, ...] = ()

    @property
    def complete(self) -> bool:
        return (
            self.baseline is not None
            and self.candidate is not None
            and self.baseline.status in USABLE_STATUSES
            and self.candidate.status in USABLE_STATUSES
        )

    def reason(self) -> str:
        """Why this repeat is not complete, as ``repeat-<i>:<side>:<status>``."""
        if self.baseline is None:
            return f"{REASON_REPEAT_PREFIX}{self.repeat_index}:{Side.BASELINE.value}:missing"
        if self.baseline.status not in USABLE_STATUSES:
            return (
                f"{REASON_REPEAT_PREFIX}{self.repeat_index}:"
                f"{Side.BASELINE.value}:{self.baseline.status.value}"
            )
        if self.candidate is None:
            return f"{REASON_REPEAT_PREFIX}{self.repeat_index}:{Side.CANDIDATE.value}:missing"
        assert self.candidate.status not in USABLE_STATUSES
        return (
            f"{REASON_REPEAT_PREFIX}{self.repeat_index}:"
            f"{Side.CANDIDATE.value}:{self.candidate.status.value}"
        )


def _deciding_and_retries(
    attempts: list[AttemptRecord],
) -> tuple[AttemptRecord | None, tuple[AttemptRecord, ...]]:
    """Split one (case, side, repeat) group into its deciding attempt + retries.

    The deciding attempt is the unique planned attempt (``retry_of=None``).
    With several and none declaring lineage — records predating repeat
    identity — the earliest is planned and the rest are undeclared retries,
    so legacy data stays deterministic. See the module repeat policy.
    """
    if not attempts:
        return None, ()
    ordered = _ordered(attempts)
    planned = [a for a in ordered if a.retry_of is None]
    if len(planned) == 1:
        return planned[0], tuple(a for a in ordered if a.attempt_id != planned[0].attempt_id)
    return ordered[0], tuple(ordered[1:])


def _repeat_groups(
    sides: dict[Side, list[AttemptRecord]],
) -> tuple[list[RepeatSides], int]:
    """Group one case's attempts into per-repeat deciding pairs (A7).

    Returns the repeats in index order plus the number of retry attempts on
    the case. Repeats are keyed by ``repeat_index`` — attempts of different
    repeats are never merged, and a repeat observed on only one side still
    surfaces (its other side is ``None``).
    """
    indices = sorted(
        {a.repeat_index for a in (*sides[Side.BASELINE], *sides[Side.CANDIDATE])}
    )
    groups: list[RepeatSides] = []
    retries = 0
    for index in indices:
        base_group = [a for a in sides[Side.BASELINE] if a.repeat_index == index]
        cand_group = [a for a in sides[Side.CANDIDATE] if a.repeat_index == index]
        base, base_retries = _deciding_and_retries(base_group)
        cand, cand_retries = _deciding_and_retries(cand_group)
        retries += len(base_retries) + len(cand_retries)
        groups.append(
            RepeatSides(
                repeat_index=index,
                baseline=base,
                candidate=cand,
                retries=(*base_retries, *cand_retries),
            )
        )
    return groups, retries


def _pair_metrics(baseline: AttemptRecord, candidate: AttemptRecord) -> dict[str, float]:
    """Per-metric delta of ONE measured (same-repeat) pair — never mixed."""
    metrics: dict[str, float] = {}
    for key in sorted(baseline.usage):
        if key.startswith(GUARDRAIL_USAGE_PREFIX):
            continue
        base_value, cand_value = baseline.usage.get(key), candidate.usage.get(key)
        if _is_number(base_value) and _is_number(cand_value):
            metrics[key] = float(cand_value) - float(base_value)
    return metrics


def build_paired_outcomes(run: EvaluationRun) -> tuple[PairedOutcome, ...]:
    """Group a run's attempts into per-case baseline-vs-candidate pairs.

    The representatives are the deciding attempts of the case's FIRST
    observed repeat; ``PairedOutcome.metrics`` is computed from that single
    same-repeat pair only. A delta is never assembled from attempts of
    different repeats (A7) — when no complete repeat exists the metrics are
    empty and the outcome says why through its attempt statuses.
    """
    by_case: dict[str, dict[Side, list[AttemptRecord]]] = {}
    for attempt in run.attempts:
        by_case.setdefault(attempt.case_id, {Side.BASELINE: [], Side.CANDIDATE: []})[
            Side(attempt.side)
        ].append(attempt)
    outcomes: list[PairedOutcome] = []
    for case_id in sorted(by_case):
        repeats, _retries = _repeat_groups(by_case[case_id])
        first = repeats[0] if repeats else RepeatSides(
            repeat_index=0, baseline=None, candidate=None
        )
        metrics = (
            _pair_metrics(first.baseline, first.candidate)
            if first.complete and first.baseline is not None and first.candidate is not None
            else {}
        )
        outcomes.append(
            PairedOutcome(
                case_id=case_id,
                baseline=first.baseline,
                candidate=first.candidate,
                metrics=metrics,
            )
        )
    return tuple(outcomes)


# -- guardrails ----------------------------------------------------------------


def _guardrail_names(workflow: WorkflowDeclaration, rubric: Rubric) -> tuple[str, ...]:
    names: list[str] = list(workflow.guardrails)
    for name in rubric.hard_guardrails:
        if name not in names:
            names.append(name)
    return tuple(names)


def _channel_key(name: str) -> str:
    return GUARDRAIL_USAGE_PREFIX + name


def _inspect_guardrails(
    *,
    case_id: str,
    candidate_attempts: list[AttemptRecord],
    baseline_attempts: list[AttemptRecord],
    names: tuple[str, ...],
    rubric: Rubric,
) -> tuple[
    tuple[GuardrailFinding, ...],
    tuple[GuardrailFinding, ...],
    tuple[GuardrailFinding, ...],
    int,
]:
    """Inspect EVERY attempt of one case.

    Returns (hard findings, soft findings, missing REQUIRED-channel
    findings, baseline violation count). A violation on ANY candidate
    attempt — including an earlier repeat that a later successful attempt
    might hide — is a finding; a rubric-hard channel absent or invalid on a
    USABLE candidate attempt is a missing measurement (unknown).
    """
    hard: list[GuardrailFinding] = []
    soft: list[GuardrailFinding] = []
    missing: list[GuardrailFinding] = []
    baseline_hits = 0
    for name in names:
        key = _channel_key(name)
        severity = "hard" if name in rubric.hard_guardrails else "soft"
        for attempt in candidate_attempts:
            if key not in attempt.usage:
                if severity == "hard" and attempt.status in USABLE_STATUSES:
                    missing.append(
                        GuardrailFinding(
                            name=name,
                            severity="unknown",
                            detail=(
                                f"case {case_id}: attempt {attempt.attempt_id} reports no "
                                f"measurement for required hard channel {key!r}; unknown is "
                                "never an implicit pass"
                            ),
                        )
                    )
                continue
            value = attempt.usage[key]
            if not _is_measured_channel(value):
                if severity == "hard" and attempt.status in USABLE_STATUSES:
                    missing.append(
                        GuardrailFinding(
                            name=name,
                            severity="unknown",
                            detail=(
                                f"case {case_id}: attempt {attempt.attempt_id} reports an "
                                f"invalid {key!r} measurement {value!r}"
                            ),
                        )
                    )
                continue
            if _is_violation(value):
                finding = GuardrailFinding(
                    name=name,
                    severity=severity,
                    detail=f"case {case_id}: attempt {attempt.attempt_id} {key}={value!r}",
                )
                (hard if severity == "hard" else soft).append(finding)
        for attempt in baseline_attempts:
            value = attempt.usage.get(key)
            if _is_measured_channel(value) and _is_violation(value):
                baseline_hits += 1
    return tuple(hard), tuple(soft), tuple(missing), baseline_hits


# -- cost -------------------------------------------------------------------------


def _side_cost(attempts: list[AttemptRecord]) -> tuple[float | None, list[str]]:
    total = 0.0
    unmeasured: list[str] = []
    for attempt in attempts:
        if attempt.cost_usd is None:
            unmeasured.append(attempt.attempt_id)
        else:
            total += attempt.cost_usd
    return (None if unmeasured else total), unmeasured


def _cost_summary(run: EvaluationRun) -> CostSummary:
    baseline_attempts = [a for a in run.attempts if a.side is Side.BASELINE]
    candidate_attempts = [a for a in run.attempts if a.side is Side.CANDIDATE]
    baseline_usd, base_unmeasured = _side_cost(baseline_attempts)
    candidate_usd, cand_unmeasured = _side_cost(candidate_attempts)
    immeasurable = tuple(sorted(base_unmeasured + cand_unmeasured))
    total = None if immeasurable else float((baseline_usd or 0.0) + (candidate_usd or 0.0))
    return CostSummary(
        baseline_usd=baseline_usd,
        candidate_usd=candidate_usd,
        total_usd=total,
        immeasurable_attempt_ids=immeasurable,
    )


# -- repeat pairing -------------------------------------------------------------------


def _repeat_pairs(
    repeats: list[RepeatSides],
) -> list[tuple[AttemptRecord, AttemptRecord]]:
    """The COMPLETE same-repeat pairs of one case, in repeat order (A7).

    Only repeats whose deciding attempt on BOTH sides is usable form a pair;
    a repeat with a failed or missing side contributes nothing. Successes of
    different repeats are never zipped together — that is exactly how an
    invented "complete pair" used to appear.
    """
    return [
        (repeat.baseline, repeat.candidate)
        for repeat in repeats
        if repeat.complete
        and repeat.baseline is not None
        and repeat.candidate is not None
    ]


# -- comparison ---------------------------------------------------------------------


def compare_run(
    run: EvaluationRun,
    workflow: WorkflowDeclaration,
    rubric: Rubric,
    *,
    expected_case_ids: tuple[str, ...] | None = None,
) -> ComparisonSummary:
    """Build the full comparison summary for one finished run.

    Refuses (fail closed) when the run is still in flight, belongs to another
    workflow, or was not executed under this exact rubric.

    ``expected_case_ids`` is the authoritative case universe derived from the
    pack bound to the run (the trusted path always passes it). Cases named
    there but absent from the run's attempts surface as
    ``case:missing-from-run`` instead of shrinking the denominator. When it
    is omitted the universe falls back to the observed cases (pure-function
    callers that have no bound pack).
    """
    if run.workflow_id != workflow.workflow_id:
        raise ContractError(
            f"run {run.run_id!r} belongs to workflow {run.workflow_id!r}, "
            f"not {workflow.workflow_id!r}"
        )
    if run.execution_status not in ("completed", "failed"):
        raise ContractError(
            f"run {run.run_id!r} has execution status {run.execution_status!r}; "
            "only finished runs can be compared"
        )
    if run.rubric_digest != rubric.digest():
        raise DigestMismatchError(
            f"run {run.run_id!r} was executed under rubric {run.rubric_digest}, "
            f"not {rubric.digest()}"
        )

    by_case: dict[str, dict[Side, list[AttemptRecord]]] = {}
    for attempt in run.attempts:
        by_case.setdefault(attempt.case_id, {Side.BASELINE: [], Side.CANDIDATE: []})[
            Side(attempt.side)
        ].append(attempt)
    universe = sorted(expected_case_ids) if expected_case_ids is not None else sorted(by_case)
    names = _guardrail_names(workflow, rubric)

    pairs: list[PairedOutcome] = []
    hard: list[GuardrailFinding] = []
    soft: list[GuardrailFinding] = []
    missing: list[GuardrailFinding] = []
    incomplete_reasons: dict[str, str] = {}
    below_repeats: list[str] = []
    baseline_hits = 0
    complete = 0
    retry_attempts = 0
    repeat_pairs: list[tuple[AttemptRecord, AttemptRecord]] = []

    for case_id in universe:
        sides = by_case.get(case_id, {Side.BASELINE: [], Side.CANDIDATE: []})
        repeats, case_retries = _repeat_groups(sides)
        retry_attempts += case_retries
        case_pairs = _repeat_pairs(repeats)

        case_hard, case_soft, case_missing, case_baseline_hits = _inspect_guardrails(
            case_id=case_id,
            candidate_attempts=sides[Side.CANDIDATE],
            baseline_attempts=sides[Side.BASELINE],
            names=names,
            rubric=rubric,
        )
        hard.extend(case_hard)
        soft.extend(case_soft)
        missing.extend(case_missing)
        baseline_hits += case_baseline_hits

        # Display pair: the deciding attempts of the FIRST observed repeat.
        # Deltas come from that one same-repeat pair only — never a mix.
        first = repeats[0] if repeats else RepeatSides(0, None, None)
        baseline, candidate = first.baseline, first.candidate
        if first.complete and baseline is not None and candidate is not None:
            metrics = _pair_metrics(baseline, candidate)
        else:
            metrics = {}

        pairs.append(
            PairedOutcome(
                case_id=case_id,
                baseline=baseline,
                candidate=candidate,
                metrics=metrics,
                guardrail_violations=tuple(case_hard) + tuple(case_soft),
            )
        )

        # Measured same-repeat pairs always aggregate (A7): a genuinely
        # measured repeat on a case whose OTHER repeat failed is data, and
        # the case-level incompleteness below still blocks the verdict.
        repeat_pairs.extend(case_pairs)

        if not sides[Side.BASELINE] and not sides[Side.CANDIDATE]:
            incomplete_reasons[case_id] = REASON_CASE_MISSING
        elif repeats and all(repeat.complete for repeat in repeats):
            complete += 1
        else:
            # A7: name the most serious non-complete repeat — a measured-bad
            # CANDIDATE repeat outranks a baseline-side gap (the verdict
            # distinguishes them) — and never let a failed repeat disappear
            # behind a later good one.
            offenders = [repeat for repeat in repeats if not repeat.complete]
            measured_bad = [
                repeat
                for repeat in offenders
                if repeat.candidate is not None
                and repeat.candidate.status.value in ("failed", "timeout")
            ]
            incomplete_reasons[case_id] = (
                measured_bad[0] if measured_bad else offenders[0]
            ).reason()

        if len(case_pairs) < rubric.repeats:
            below_repeats.append(case_id)

    metric_deltas = _aggregate_metrics(repeat_pairs)
    cost = _cost_summary(run)
    if cost.measurable:
        max_increase = rubric.thresholds.get(THRESHOLD_MAX_COST_INCREASE_USD)
        if (
            max_increase is not None
            and cost.candidate_usd is not None
            and cost.baseline_usd is not None
            and cost.candidate_usd - cost.baseline_usd > float(max_increase)
        ):
            soft.append(
                GuardrailFinding(
                    name="cost-increase",
                    severity="soft",
                    detail=(
                        f"candidate cost exceeds baseline by "
                        f"${cost.candidate_usd - cost.baseline_usd:.4f} > "
                        f"${float(max_increase):.4f}"
                    ),
                )
            )

    note = _uncertainty_note(
        run=run,
        total=len(universe),
        complete=complete,
        incomplete_reasons=incomplete_reasons,
        below_repeats=below_repeats,
        missing_measurements=tuple(missing),
        cost=cost,
        soft_count=len(soft),
        baseline_hits=baseline_hits,
        retry_attempts=retry_attempts,
    )

    return ComparisonSummary(
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        mode=run.mode,
        split=run.split,
        baseline_digest=run.baseline_digest,
        candidate_digest=run.candidate_digest,
        case_set_digest=run.case_set_digest,
        rubric_digest=run.rubric_digest,
        pairs=tuple(pairs),
        total_pairs=len(universe),
        complete_pairs=complete,
        incomplete_pairs=len(incomplete_reasons),
        incomplete_reasons=incomplete_reasons,
        repeats_requested=rubric.repeats,
        pairs_below_requested_repeats=tuple(sorted(set(below_repeats))),
        complete_repeat_pairs=len(repeat_pairs),
        retry_attempts=retry_attempts,
        metric_deltas=metric_deltas,
        hard_violations=tuple(hard),
        soft_violations=tuple(soft),
        missing_guardrail_measurements=tuple(missing),
        cost=cost,
        uncertainty_note=note,
    )


def _aggregate_metrics(
    repeat_pairs: list[tuple[AttemptRecord, AttemptRecord]],
) -> dict[str, MetricDelta]:
    """Mean per-metric deltas over every usable repeat pair (both sides usable).

    Aggregates ALL intended repeats rather than a single representative
    attempt (review A4): the mean covers every repeat the plan asked for.
    """
    sums: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for baseline, candidate in repeat_pairs:
        for key in sorted(set(baseline.usage) & set(candidate.usage)):
            if key.startswith(GUARDRAIL_USAGE_PREFIX):
                continue
            base_value, cand_value = baseline.usage[key], candidate.usage[key]
            if not (_is_number(base_value) and _is_number(cand_value)):
                continue
            delta = float(cand_value) - float(base_value)
            sums.setdefault(key, [0.0, 0.0, 0.0])
            sums[key][0] += float(base_value)
            sums[key][1] += float(cand_value)
            sums[key][2] += delta
            counts[key] = counts.get(key, 0) + 1
    return {
        key: MetricDelta(
            metric=key,
            n_pairs=counts[key],
            mean_baseline=values[0] / counts[key],
            mean_candidate=values[1] / counts[key],
            mean_delta=values[2] / counts[key],
        )
        for key, values in sums.items()
    }


def _uncertainty_note(
    *,
    run: EvaluationRun,
    total: int,
    complete: int,
    incomplete_reasons: dict[str, str],
    below_repeats: list[str],
    missing_measurements: tuple[GuardrailFinding, ...],
    cost: CostSummary,
    soft_count: int,
    baseline_hits: int,
    retry_attempts: int = 0,
) -> str:
    parts: list[str] = []
    if incomplete_reasons:
        rendered = ", ".join(
            f"{case} ({reason})" for case, reason in sorted(incomplete_reasons.items())
        )
        parts.append(f"{complete}/{total} pairs complete; incomplete: {rendered}")
    else:
        parts.append(f"all {total} pairs complete")
    if run.uncertainty_note:
        parts.append(run.uncertainty_note)
    if below_repeats:
        parts.append(
            "fewer complete repeats than the rubric demands on cases: "
            f"{', '.join(sorted(set(below_repeats)))}"
        )
    if retry_attempts:
        parts.append(
            f"{retry_attempts} retry attempt(s) recorded; retries never change a "
            "repeat's outcome (explicit policy, no silent forgiveness)"
        )
    if missing_measurements:
        channels = sorted({f.name for f in missing_measurements})
        parts.append(
            "required hard guardrail channels unmeasured on usable candidate "
            f"attempts: {', '.join(channels)}; unknown is never an implicit pass"
        )
    if cost.measurable and cost.total_usd is not None:
        parts.append(f"measured total cost ${cost.total_usd:.4f}")
    else:
        parts.append(
            "cost accounting immeasurable: attempts "
            f"{', '.join(cost.immeasurable_attempt_ids)} have no measured price"
        )
    if baseline_hits:
        parts.append(
            f"baseline violates guardrails on {baseline_hits} case-channel(s); "
            "guardrail comparison against a violating baseline is context, not clearance"
        )
    if soft_count:
        parts.append(f"{soft_count} soft guardrail finding(s) recorded")
    parts.append(_SAMPLE_SIZE_CAVEAT)
    return "; ".join(parts)


def run_with_outcomes(run: EvaluationRun, summary: ComparisonSummary) -> EvaluationRun:
    """Attach a comparison's annotated pairs back onto the run record."""
    return replace(run, outcomes=summary.pairs)
