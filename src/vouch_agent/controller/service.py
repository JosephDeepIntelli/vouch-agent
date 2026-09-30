"""VouchController — trusted orchestration of the improvement vertical flow.

Baseline -> candidate -> seal -> paired evaluation (dev/selection splits) ->
independent final acceptance -> decision -> release record -> export. Every
attempt (including failures) is budgeted via atomic reservations, gated, and
journaled. No method here performs a production deploy; release records only
capture what the product's own release process did.

The controller is deliberately built against ports (MetadataStore,
ArtifactStore, BudgetLedger, Journal, AdapterClient) so the whole flow is
testable with fakes and deterministic offline adapters.

Evidence authority (review A4/A5/A6):

* Entry points that advance candidate state load DURABLE run records by id
  and recompute the verdict from the frozen rubric inside this trusted path —
  caller-supplied runs, verdicts or bindings never move state on their own.
* ``approve`` and ``release`` re-resolve the candidate/rubric/baseline/
  case-set digests from trusted storage; a binding is never verified against
  its own supplied values, so evaluate -> accept -> drift -> approve/release
  fails at the step after the drift.
* State changes and their decision/release records are written in one
  storage transaction.
* Case-input bytes are digest-verified before any adapter work and travel
  with the attempt; adapter outputs are sealed into the artifact store with
  retained digests.
* Post-dispatch failures with unknown spend keep their conservative
  reservation and are flagged for reconciliation — only verified
  pre-execution rejections settle at zero (review A2).

Repeat and retry policy (A7, explicit — there is no silent forgiveness):

* The controller schedules exactly the requested number of PLANNED repeats
  per case and side; ``repeats`` never means "retry until one passes". Each
  attempt persists the planned repeat it belongs to (``repeat_index``).
* Retries exist only as EXPLICIT operator actions and are recorded as
  lineage (``retry_of``); a retry never changes the outcome of the repeat it
  retries — the comparison decides a repeat by its planned attempt, so a
  failed repeat stays failed and the verdict reflects it. To change a
  verdict, run a new evaluation.
* The candidate's parent baseline is bound at evaluation, decision and
  approval: a baseline change requires an explicit rebase
  (:meth:`rebase_candidate`), which yields a NEW candidate content digest and
  therefore invalidates every prior evaluation/acceptance/approval.
"""

from __future__ import annotations

import base64
import threading
from dataclasses import replace
from typing import Any

from vouch_agent.adapters.base import AdapterClient, AdapterExecution
from vouch_agent.contracts.candidate import AgentVersion, Candidate, CandidateState
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
from vouch_agent.contracts.common import Role, RunMode, canonical_json, new_id, utc_now_iso
from vouch_agent.contracts.decision import (
    AcceptanceDecision,
    ApprovalBinding,
    ReleaseRecord,
    Verdict,
)
from vouch_agent.contracts.evaluation import (
    AttemptRecord,
    AttemptStatus,
    EvaluationRun,
    Rubric,
    Side,
)
from vouch_agent.contracts.journal import (
    BudgetReservation,
    CostCategory,
    CostEntry,
    EventKind,
    EventRecord,
    ReservationStatus,
)
from vouch_agent.contracts.project import ProjectSpec
from vouch_agent.errors import (
    ApprovalInvalidatedError,
    BudgetExhaustedError,
    ContractError,
    DigestMismatchError,
    GateDeniedError,
    ReconciliationRequiredError,
    SplitAccessError,
)
from vouch_agent.evaluation.comparator import ComparisonSummary, compare_run
from vouch_agent.evaluation.verdict import (
    UNRECORDED_ENVIRONMENT_DIGEST,
    binding_for_run,
    verdict_from_summary,
)
from vouch_agent.gate.broker import CapabilityBroker
from vouch_agent.gate.proposal import ActionProposal, RiskClass
from vouch_agent.storage.interfaces import (
    ArtifactStore,
    BudgetLedger,
    Journal,
    MetadataStore,
    enforce_split_visibility,
)

KIND_PROJECT = "project"
KIND_PACK = "task-pack"
KIND_RUBRIC = "rubric"
KIND_BASELINE = "baseline"
KIND_CANDIDATE = "candidate"
KIND_EVALUATION = "evaluation"
KIND_ATTEMPT = "attempt"
KIND_DECISION = "decision"
KIND_RELEASE = "release"
KIND_RESERVATION = "budget-reservation"
#: Workflow id -> {"rubricDigest"} — the CURRENT active rubric (the value the
#: application services keep in sync in ``appservices.kinds``; duplicated as
#: a literal here to keep the controller free of an appservices import).
KIND_WORKFLOW_RUBRIC = "workflow-rubric"
#: Workflow id -> {"versionId", "baselineRecordId"} — the CURRENT baseline.
KIND_WORKFLOW_BASELINE = "workflow-baseline"
#: Run id -> {"manifestDigest", "path"} of an exported evidence package.
KIND_RUN_EXPORT = "run-export"


def _input_fields(input_digest: str, payload: bytes) -> dict[str, Any]:
    """Verified input bytes as JSON-safe case-input fields (A6).

    The digest was verified against these exact bytes before dispatch; the
    runner receives the material itself (UTF-8 text, or base64 for binary),
    never a host path.
    """
    try:
        return {"inputDigest": input_digest, "inputBytes": payload.decode("utf-8")}
    except UnicodeDecodeError:
        return {
            "inputDigest": input_digest,
            "inputBytesB64": base64.b64encode(payload).decode("ascii"),
        }


class VouchController:
    def __init__(
        self,
        project: ProjectSpec,
        store: MetadataStore,
        artifacts: ArtifactStore,
        ledger: BudgetLedger,
        journal: Journal,
        broker: CapabilityBroker,
    ) -> None:
        self.project = project
        self.store = store
        self.artifacts = artifacts
        self.ledger = ledger
        self.journal = journal
        self.broker = broker
        self._concurrency = threading.Semaphore(project.budget.max_concurrent_attempts)

    # --- lifecycle helpers -------------------------------------------------

    def _event(self, kind: EventKind, subject: str, data: dict[str, Any]) -> None:
        self.journal.append(
            EventRecord(
                event_id=new_id("evt"),
                kind=kind,
                subject=subject,
                data=data,
                mode=RunMode.FIXTURE,
            )
        )

    def _persist(self, kind: str, record_id: str, record: Any) -> None:
        self.store.save(kind, record_id, record.to_dict())

    def _load(self, kind: str, record_id: str) -> Any:
        data = self.store.load(kind, record_id)
        if data is None:
            raise ContractError(f"unknown {kind} {record_id!r}")
        return data

    def _candidate(self, candidate_id: str) -> Candidate:
        return Candidate.from_dict(self._load(KIND_CANDIDATE, candidate_id))

    def candidate(self, candidate_id: str) -> Candidate:
        """Public read accessor for review/reporting."""
        return self._candidate(candidate_id)

    # --- durable evidence resolution (A4) -------------------------------------

    def _load_run(self, run_id: str) -> EvaluationRun:
        """Load a DURABLE evaluation run by id; never trust caller objects."""
        return EvaluationRun.from_dict(self._load(KIND_EVALUATION, run_id))

    def _frozen_rubric(self, rubric_digest: str) -> Rubric:
        rubric = Rubric.from_dict(self._load(KIND_RUBRIC, rubric_digest))
        if rubric.digest() != rubric_digest:
            raise ContractError(
                f"stored rubric {rubric_digest!r} no longer digests to its id — "
                "the record was modified after freezing"
            )
        if not rubric.frozen:
            raise ContractError("rubric must be frozen before it can govern evaluation")
        return rubric

    def _pack_by_digest(self, case_set_digest: str) -> TaskPack | None:
        """The durably stored pack behind a run's case-set digest, if any."""
        for pack_id in self.store.list_ids(KIND_PACK):
            data = self.store.load(KIND_PACK, pack_id)
            if data is None:  # pragma: no cover - id came from list_ids
                continue
            pack = TaskPack.from_dict(data)
            if pack.digest() == case_set_digest:
                return pack
        return None

    def _recompute(self, run: EvaluationRun) -> tuple[ComparisonSummary, Verdict]:
        """Trusted-path comparison + verdict for a durable run.

        The expected case matrix comes from the pack bound to the run
        (``run.case_set_digest``), not from the attempts the run happens to
        contain — deleting attempts cannot shrink the denominator.
        """
        workflow = self.project.workflow(run.workflow_id)
        rubric = self._frozen_rubric(run.rubric_digest)
        pack = self._pack_by_digest(run.case_set_digest)
        expected = tuple(c.case_id for c in pack.cases_in(run.split)) if pack else None
        summary = compare_run(run, workflow, rubric, expected_case_ids=expected)
        return summary, verdict_from_summary(summary, rubric)

    def _qualifying_selection_run(self, candidate: Candidate) -> EvaluationRun | None:
        """A durable COMPLETED selection-validation run for this candidate
        whose verdict recomputes to ACCEPTED under the frozen standard."""
        best: EvaluationRun | None = None
        for run_id in self.store.list_ids(KIND_EVALUATION):
            data = self.store.load(KIND_EVALUATION, run_id)
            if data is None:  # pragma: no cover - id came from list_ids
                continue
            try:
                run = EvaluationRun.from_dict(data)
                if run.split is not CaseSplit.SELECTION_VALIDATION:
                    continue
                if run.execution_status != "completed":
                    continue
                if run.candidate_digest != candidate.content_digest():
                    continue
                _summary, verdict = self._recompute(run)
            except (ContractError, DigestMismatchError):
                continue  # corrupt/unusable records never qualify
            if verdict.value == "accepted" and (best is None or run.created_at > best.created_at):
                best = run
        return best

    def mark_evaluated(self, candidate_id: str) -> Candidate:
        """Sealed -> evaluated, after a QUALIFYING selection-validation run.

        A development-split acceptance, however good, does not promote the
        candidate: acceptance-eligibility requires a durable completed
        selection-validation run whose verdict this controller recomputes to
        ACCEPTED from the frozen rubric (review A5).
        """
        candidate = self._candidate(candidate_id)
        qualifying = self._qualifying_selection_run(candidate)
        if qualifying is None:
            raise ContractError(
                f"candidate {candidate_id!r} has no qualifying selection-validation "
                "run: a durable completed selection-validation evaluation with an "
                "accepted verdict is required before 'evaluated' (development-only "
                "evaluation does not qualify)"
            )
        moved = candidate.with_transition(CandidateState.EVALUATED)  # refuses wrong states
        with self.store.transaction():
            self._save_candidate(moved)
        self._event(
            EventKind.CANDIDATE_STATE,
            candidate_id,
            {"state": "evaluated", "selectionRun": qualifying.run_id},
        )
        return moved

    def _save_candidate(self, candidate: Candidate) -> None:
        self._persist(KIND_CANDIDATE, candidate.candidate_id, candidate)

    def _reservation_status(self, reservation_id: str) -> ReservationStatus:
        data = self.store.load(KIND_RESERVATION, reservation_id)
        if data is None:
            raise ContractError(f"unknown reservation {reservation_id!r}")
        return BudgetReservation.from_dict(data).status

    # --- project / pack / rubric -------------------------------------------

    def init(self) -> None:
        self._persist(KIND_PROJECT, self.project.project_id, self.project)
        self._event(EventKind.AUDIT_NOTE, self.project.project_id, {"event": "project-init"})

    def import_pack(self, pack: TaskPack, role: Role) -> str:
        for case in pack.cases:
            enforce_split_visibility(role, case.split.value)
        self.project.workflow(pack.workflow_id)  # must be a declared workflow
        self._persist(KIND_PACK, pack.pack_id, pack)
        self._event(EventKind.AUDIT_NOTE, pack.pack_id, {"imported": len(pack.cases)})
        return pack.pack_id

    def freeze_rubric(self, rubric: Rubric, frozen_by: str) -> str:
        if rubric.frozen:
            raise ContractError("rubric is already frozen; create a new version instead")
        frozen = Rubric(
            main_metric=rubric.main_metric,
            direction=rubric.direction,
            thresholds=rubric.thresholds,
            hard_guardrails=rubric.hard_guardrails,
            repeats=rubric.repeats,
            frozen_by=frozen_by,
            frozen_at=utc_now_iso(),
        )
        self._persist(KIND_RUBRIC, frozen.digest(), frozen)
        self._event(EventKind.AUDIT_NOTE, frozen.digest(), {"event": "rubric-frozen"})
        return frozen.digest()

    def rubric(self, rubric_digest: str) -> Rubric:
        """Public accessor: the frozen rubric record behind a digest."""
        return Rubric.from_dict(self._load(KIND_RUBRIC, rubric_digest))

    def record_baseline(self, version: AgentVersion, workflow_id: str) -> str:
        self.project.workflow(workflow_id)
        record_id = f"{workflow_id}:{version.version_id}"
        self._persist(KIND_BASELINE, record_id, version)
        return record_id

    # --- candidate flow ------------------------------------------------------

    @staticmethod
    def _require_parent_baseline(
        candidate: Candidate, baseline: AgentVersion, *, workflow_id: str
    ) -> None:
        """A7: the baseline a run measures must BE the candidate's parent.

        A candidate based on v0 measured against v1 (or rolled back to v0
        after v1 shipped) silently separates the acceptance evidence from the
        deployment lineage, so the comparison is refused until the candidate
        is EXPLICITLY rebased onto the current baseline — a rebase produces a
        new candidate content digest and therefore requires fresh evidence.
        """
        parent_digest = candidate.parent_version.digest()
        baseline_digest = baseline.digest()
        if parent_digest != baseline_digest:
            raise ContractError(
                f"candidate {candidate.candidate_id!r} is based on parent "
                f"{candidate.parent_version.version_id} ({parent_digest}) but the "
                f"baseline for workflow {workflow_id!r} is {baseline.version_id} "
                f"({baseline_digest}); refusing to evaluate a candidate against a "
                "baseline it was not derived from — rebase the candidate explicitly "
                "(new parent, new content digest, fresh evidence)"
            )

    def rebase_candidate(
        self,
        candidate_id: str,
        *,
        baseline: AgentVersion,
        workflow_id: str,
        note: str = "",
    ) -> Candidate:
        """EXPLICIT rebase (A7): re-parent a candidate onto ``baseline``.

        The result is a NEW candidate (new id, state ``proposed``) carrying the
        same change content; because the parent is part of the content digest,
        the digest necessarily changes and every previous evaluation,
        acceptance and approval of the old candidate stops applying. This is
        the only sanctioned response to a parent/baseline mismatch — the
        alternative (silently measuring an old candidate against a new
        baseline) is what the comparison now refuses.
        """
        old = self._candidate(candidate_id)
        self.project.workflow(workflow_id)
        if old.parent_version.digest() == baseline.digest():
            raise ContractError(
                f"candidate {candidate_id!r} is already based on baseline "
                f"{baseline.version_id}; nothing to rebase"
            )
        rebased = Candidate(
            candidate_id=new_id("cand"),
            parent_version=baseline,
            change_type=old.change_type,
            delta=old.delta,
            rationale=old.rationale,
            expected_impact=old.expected_impact,
            proposer=old.proposer,
            dev_case_refs=old.dev_case_refs,
            state_reason=(
                f"rebased from {candidate_id} onto {baseline.version_id}"
                + (f": {note}" if note else "")
            ),
        )
        rebased.sealable(self.project.allowed_change_types)
        self._save_candidate(rebased)
        self._event(
            EventKind.CANDIDATE_STATE,
            rebased.candidate_id,
            {
                "state": "proposed",
                "rebaseOf": candidate_id,
                "newParent": baseline.version_id,
                "note": note,
            },
        )
        return rebased

    def propose(self, candidate: Candidate) -> str:
        candidate.sealable(self.project.allowed_change_types)
        self._save_candidate(candidate)
        self._event(EventKind.CANDIDATE_STATE, candidate.candidate_id, {"state": "proposed"})
        return candidate.candidate_id

    def seal(self, candidate_id: str) -> Candidate:
        candidate = self._candidate(candidate_id)
        sealed = candidate.with_transition(CandidateState.SEALED)
        self._save_candidate(sealed)
        self._event(
            EventKind.CANDIDATE_STATE, candidate_id, {"state": "sealed", "digest": sealed.digest()}
        )
        return sealed

    def invalidate(self, candidate_id: str, reason: str) -> Candidate:
        candidate = self._candidate(candidate_id)
        invalidated = candidate.with_transition(CandidateState.INVALIDATED, reason=reason)
        self._save_candidate(invalidated)
        self._event(EventKind.APPROVAL_INVALIDATED, candidate_id, {"reason": reason})
        return invalidated

    # --- evaluation ----------------------------------------------------------

    def _verified_inputs(self, cases: tuple[TaskCase, ...]) -> dict[str, bytes]:
        """Digest-verify every case input's BYTES before any adapter work.

        Missing or corrupted artifacts raise here — before a reservation is
        taken, before the gate is asked, before the adapter runs (A6).
        """
        verified: dict[str, bytes] = {}
        for case in cases:
            verified[case.case_id] = self.artifacts.get(case.input_digest)
        return verified

    def run_paired_evaluation(
        self,
        *,
        workflow_id: str,
        candidate_id: str,
        baseline: AgentVersion,
        pack: TaskPack,
        split: CaseSplit,
        rubric_digest: str,
        adapter: AdapterClient,
        mode: RunMode = RunMode.FIXTURE,
        repeats: int = 1,
        per_attempt_reserve_usd: float = 0.05,
        timeout_s: float | None = None,
    ) -> EvaluationRun:
        """Execute baseline+candidate on every case of ``split`` in the pack.

        Budget is reserved before each attempt and settled after; failures and
        timeouts are booked as attempts too. Concurrency is bounded by the
        project policy. The candidate must be sealed.
        """
        candidate = self._candidate(candidate_id)
        if candidate.state not in (CandidateState.SEALED, CandidateState.EVALUATED):
            raise ContractError(
                f"candidate {candidate_id} must be sealed before evaluation "
                f"(state={candidate.state.value})"
            )
        self._require_parent_baseline(candidate, baseline, workflow_id=workflow_id)
        self._frozen_rubric(rubric_digest)
        if split not in (CaseSplit.DEVELOPMENT, CaseSplit.SELECTION_VALIDATION):
            raise SplitAccessError(
                "paired evaluation may only run on development/selection splits; "
                "final acceptance runs through run_final_acceptance"
            )

        cases = pack.cases_in(split)
        if not cases:
            raise ContractError(f"pack has no cases in split {split.value}")
        verified_inputs = self._verified_inputs(cases)

        run = EvaluationRun(
            run_id=new_id("eval"),
            workflow_id=workflow_id,
            split=split,
            baseline_digest=baseline.digest(),
            candidate_digest=candidate.content_digest(),
            case_set_digest=pack.digest(),
            rubric_digest=rubric_digest,
            mode=mode,
            execution_status="running",
        )
        attempts: list[AttemptRecord] = []
        # The side digests the durable run record itself binds to: the
        # baseline's AgentVersion digest and the candidate's CONTENT digest
        # (stable across lifecycle transitions — an attempt's provenance must
        # not drift because the candidate later advanced state).
        side_digests = {
            Side.BASELINE: baseline.digest(),
            Side.CANDIDATE: candidate.content_digest(),
        }
        for repeat in range(repeats):
            for case in cases:
                for side in (Side.BASELINE, Side.CANDIDATE):
                    attempts.append(
                        self._run_attempt(
                            run=run,
                            side=side,
                            case_id=case.case_id,
                            case_input={
                                "caseId": case.case_id,
                                "repeat": repeat,
                                "side": side.value,
                                "inputDigest": case.input_digest,
                                # A6: the controller knows this side's version
                                # digest — it always travels with the attempt so
                                # the adapter identity carries it (protocol v1.1
                                # §1 allows omission only when genuinely unknown).
                                "versionDigest": side_digests[side],
                            },
                            input_bytes=verified_inputs[case.case_id],
                            version_digest=side_digests[side],
                            workflow_id=workflow_id,
                            adapter=adapter,
                            mode=mode,
                            reserve_usd=per_attempt_reserve_usd,
                            timeout_s=timeout_s,
                            repeat_index=repeat,
                        )
                    )
        completed = EvaluationRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            split=run.split,
            baseline_digest=run.baseline_digest,
            candidate_digest=run.candidate_digest,
            case_set_digest=run.case_set_digest,
            rubric_digest=run.rubric_digest,
            environment_digest=run.environment_digest,
            attempts=tuple(attempts),
            outcomes=(),
            mode=run.mode,
            execution_status="completed",
            uncertainty_note=(
                ""
                if all(a.status is AttemptStatus.OK for a in attempts)
                else "contains non-ok attempts"
            ),
        )
        self._persist(KIND_EVALUATION, completed.run_id, completed)
        for attempt in attempts:
            self._persist(KIND_ATTEMPT, attempt.attempt_id, attempt)
        self._event(
            EventKind.RUN_COMPLETED,
            completed.run_id,
            {"attempts": len(attempts), "split": split.value},
        )
        return completed

    def _run_attempt(
        self,
        *,
        run: EvaluationRun,
        side: Side,
        case_id: str,
        case_input: dict[str, Any],
        version_digest: str,
        workflow_id: str,
        adapter: AdapterClient,
        mode: RunMode,
        reserve_usd: float,
        timeout_s: float | None,
        input_bytes: bytes | None = None,
        repeat_index: int = 0,
        retry_of: str | None = None,
    ) -> AttemptRecord:
        from vouch_agent.errors import AdapterExecutionError

        attempt_id = new_id("att")
        if input_bytes is not None:
            digest = str(case_input["inputDigest"])
            case_input = {**case_input, **_input_fields(digest, input_bytes)}
        reservation = self._reserve_or_fail(attempt_id, reserve_usd)
        proposal = ActionProposal(
            action="run-adapter-attempt",
            arguments={
                "runId": run.run_id,
                "attemptId": attempt_id,
                "caseId": case_id,
                "versionDigest": version_digest,
                "workflowId": workflow_id,
            },
            risk_class=RiskClass.R1,
            resource_ids=(case_id, run.run_id),
        )
        try:
            authorization = self.broker.authorize(
                proposal, mode=mode, reservation_id=reservation.reservation_id
            )
        except GateDeniedError as exc:
            # Refused BEFORE dispatch: provably no adapter work, no spend.
            return self._finish_attempt(
                run=run,
                attempt_id=attempt_id,
                side=side,
                case_id=case_id,
                status=AttemptStatus.FAILED,
                started_at=utc_now_iso(),
                ended_at=utc_now_iso(),
                error=str(exc),
                result=None,
                reservation=reservation,
                reserve_usd=reserve_usd,
                dispatched=False,
                adapter=adapter,
                mode=mode,
            )
        started = utc_now_iso()
        status: AttemptStatus = AttemptStatus.OK
        error: str | None = None
        result: AdapterExecution | None = None
        dispatched = True
        try:
            with self._concurrency:
                result = self.broker.execute(
                    authorization,
                    lambda: self._execute_with_timeout(
                        adapter,
                        run_id=run.run_id,
                        attempt_id=attempt_id,
                        workflow_id=workflow_id,
                        case_input=case_input,
                        mode=mode,
                        timeout_s=timeout_s,
                    ),
                    proposal=proposal,
                    mode=mode,
                )
            if result.mode is not mode:
                # A6: the adapter reported a different provider mode than the
                # one that authorized this attempt. NOTHING from this exchange
                # is stored, sealed or billed as an outcome of the run — the
                # result object is dropped entirely (process adapters already
                # kill the child; this covers in-process adapters and any
                # future port).
                status = AttemptStatus.FAILED
                error = (
                    f"adapter reported mode {result.mode.value!r} for an attempt "
                    f"authorized as {mode.value!r}; contradictory identity is refused"
                )
                result = None
            elif not result.ok:
                status = AttemptStatus.FAILED
                error = result.error
        except GateDeniedError as exc:
            # The broker refused before invoking the executor: verified
            # pre-execution rejection, not uncertain post-dispatch spend.
            status = AttemptStatus.FAILED
            error = str(exc)
            dispatched = False
        except AdapterExecutionError as exc:
            status = AttemptStatus.FAILED
            error = str(exc)
            dispatched = True
        except TimeoutError:
            status = AttemptStatus.TIMEOUT
            error = "attempt timed out"
            dispatched = True
        ended = utc_now_iso()
        return self._finish_attempt(
            run=run,
            attempt_id=attempt_id,
            side=side,
            case_id=case_id,
            status=status,
            started_at=started,
            ended_at=ended,
            error=error,
            result=result,
            reservation=reservation,
            reserve_usd=reserve_usd,
            dispatched=dispatched,
            adapter=adapter,
            mode=mode,
            repeat_index=repeat_index,
            retry_of=retry_of,
        )

    def _finish_attempt(
        self,
        *,
        run: EvaluationRun,
        attempt_id: str,
        side: Side,
        case_id: str,
        status: AttemptStatus,
        started_at: str,
        ended_at: str,
        error: str | None,
        result: AdapterExecution | None,
        reservation: BudgetReservation,
        reserve_usd: float,
        dispatched: bool,
        adapter: AdapterClient,
        mode: RunMode,
        repeat_index: int = 0,
        retry_of: str | None = None,
    ) -> AttemptRecord:
        """Settle, journal and seal one attempt (A2 + A6 + A7).

        The attempt's planned repeat identity (``repeat_index``) and explicit
        retry lineage (``retry_of``) are persisted on the record (A7): the
        comparison pairs observations by case+repeat and never merges across
        repeats. The sealed output artifact carries the mode and runner
        version the adapter actually reported, so later attestation resolves
        them from recorded exchanges rather than from claims (A5).

        Settlement policy:

        * measured cost -> settle the actual amount;
        * verified pre-dispatch rejection -> settle 0.0 (provably no spend);
        * post-dispatch failure with unknown cost -> KEEP the reservation
          open (conservative hold) and flag the attempt for reconciliation —
          unknown spend must never replenish the budget;
        * otherwise (no metering) -> conservatively commit the reservation.
        """
        cost = result.usage.get("costUsd") if result and result.usage else None
        actual = float(cost) if isinstance(cost, int | float) else None

        settled_reservation: BudgetReservation | None = None
        if actual is not None:
            settled_reservation = self.ledger.settle(reservation.reservation_id, actual)
        elif not dispatched:
            settled_reservation = self.ledger.settle(reservation.reservation_id, 0.0)
        elif status is AttemptStatus.FAILED:
            settled_reservation = None  # hold; reconciliation decides
        else:
            settled_reservation = self.ledger.settle(reservation.reservation_id, reserve_usd)
        if settled_reservation is not None:
            self._persist(KIND_RESERVATION, settled_reservation.reservation_id, settled_reservation)

        if actual is not None:
            self.journal.append_cost(
                CostEntry(
                    entry_id=new_id("cost"),
                    category=CostCategory.MODEL,
                    subject=attempt_id,
                    amount_usd=actual,
                    measurable=True,
                    mode=mode,
                )
            )
        elif not dispatched:
            self.journal.append_cost(
                CostEntry(
                    entry_id=new_id("cost"),
                    category=CostCategory.MODEL,
                    subject=attempt_id,
                    amount_usd=0.0,
                    measurable=True,
                    mode=mode,
                    note="refused before dispatch; verified no spend",
                )
            )
        elif status is AttemptStatus.FAILED:
            self.journal.append_cost(
                CostEntry(
                    entry_id=new_id("cost"),
                    category=CostCategory.MODEL,
                    subject=attempt_id,
                    amount_usd=None,
                    measurable=False,
                    mode=mode,
                    note=(
                        "post-dispatch failure with unknown spend; reservation held "
                        "pending reconciliation (never booked as zero)"
                    ),
                )
            )
            self._event(
                EventKind.RUN_RECONCILIATION,
                attempt_id,
                {
                    "state": "needed",
                    "reason": (
                        "adapter work began but its cost is unknown; verify actual "
                        "spend and settle or release the reservation"
                    ),
                    "reservationId": reservation.reservation_id,
                },
            )
        else:
            self.journal.append_cost(
                CostEntry(
                    entry_id=new_id("cost"),
                    category=CostCategory.MODEL,
                    subject=attempt_id,
                    amount_usd=None,
                    measurable=False,
                    mode=mode,
                    note="adapter returned no metering; booked unmeasurable",
                )
            )

        output_digest: str | None = None
        if result is not None:
            sealed = {
                "runId": run.run_id,
                "attemptId": attempt_id,
                "side": side.value,
                "repeatIndex": repeat_index,
                "outputs": result.outputs,
                "evidenceRefs": list(result.evidence_refs),
                # Observed exchange facts (A5/A6): the provider mode and the
                # runner revision the adapter REPORTED for this exact attempt
                # are sealed with the outputs, so attestation can resolve them
                # from durable evidence instead of trusting a claim.
                "mode": result.mode.value,
                "runnerVersion": result.runner_version,
            }
            output_digest = self.artifacts.put(canonical_json(sealed).encode("utf-8"))

        attempt = AttemptRecord(
            attempt_id=attempt_id,
            run_id=run.run_id,
            side=side,
            case_id=case_id,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            error=error,
            cost_usd=actual,
            usage=dict(result.usage) if result and result.usage else {},
            adapter=adapter.describe().adapter_id,
            output_digest=output_digest,
            repeat_index=repeat_index,
            retry_of=retry_of,
        )
        self._event(
            EventKind.ATTEMPT_ENDED,
            attempt_id,
            {"status": status.value, "side": side.value, "costUsd": actual},
        )
        return attempt

    def _execute_with_timeout(
        self,
        adapter: AdapterClient,
        *,
        run_id: str,
        attempt_id: str,
        workflow_id: str,
        case_input: dict[str, Any],
        mode: RunMode,
        timeout_s: float | None,
    ) -> AdapterExecution:
        """Attempt-level wall clock. The attempt DIES at the deadline even if
        the underlying thread cannot be interrupted; subprocess adapters
        enforce their own kill-on-overrun on top (design §10 timeouts)."""
        import concurrent.futures

        if timeout_s is None:
            return adapter.execute(
                run_id=run_id,
                attempt_id=attempt_id,
                workflow_id=workflow_id,
                case_input=case_input,
                mode=mode,
            )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                adapter.execute,
                run_id=run_id,
                attempt_id=attempt_id,
                workflow_id=workflow_id,
                case_input=case_input,
                mode=mode,
            )
            try:
                return future.result(timeout=timeout_s)
            except concurrent.futures.TimeoutError as exc:
                raise TimeoutError(f"attempt {attempt_id} exceeded {timeout_s}s") from exc

    def _reserve_or_fail(self, holder: str, amount_usd: float) -> BudgetReservation:
        try:
            reservation = self.ledger.reserve(holder, amount_usd)
        except BudgetExhaustedError:
            self._event(EventKind.BUDGET_EXHAUSTED, holder, {"requested": amount_usd})
            raise
        self._persist(KIND_RESERVATION, reservation.reservation_id, reservation)
        self._event(
            EventKind.BUDGET_RESERVED,
            reservation.reservation_id,
            {"holder": holder, "amountUsd": amount_usd},
        )
        return reservation

    def run_final_acceptance(
        self,
        *,
        workflow_id: str,
        candidate_id: str,
        baseline: AgentVersion,
        pack: TaskPack,
        rubric_digest: str,
        adapter: AdapterClient,
        role: Role,
        mode: RunMode = RunMode.FIXTURE,
        per_attempt_reserve_usd: float = 0.05,
    ) -> EvaluationRun:
        """Acceptance-side evaluation on the final-acceptance split.

        Only the acceptance owner / evaluator role may invoke this; proposer
        and engineer roles are refused regardless of what they can name. The
        candidate must carry a durable qualifying selection-validation run
        (re-resolved from storage, not assumed from the state field), and the
        run executes the frozen rubric's actual ``repeats``.
        """
        if role not in (Role.ACCEPTANCE_OWNER, Role.EVALUATOR):
            raise SplitAccessError(f"role {role.value} may not run final acceptance")
        candidate = self._candidate(candidate_id)
        if candidate.state is not CandidateState.EVALUATED:
            raise ContractError(
                "candidate must carry a completed selection evaluation before final acceptance"
            )
        if self._qualifying_selection_run(candidate) is None:
            raise ContractError(
                "candidate must carry a completed selection evaluation before final "
                "acceptance: no durable qualifying selection-validation run found"
            )
        self._require_parent_baseline(candidate, baseline, workflow_id=workflow_id)
        rubric = self._frozen_rubric(rubric_digest)
        cases = pack.cases_in(CaseSplit.FINAL_ACCEPTANCE)
        if not cases:
            raise ContractError("pack has no final-acceptance cases")
        verified_inputs = self._verified_inputs(cases)
        run = EvaluationRun(
            run_id=new_id("eval"),
            workflow_id=workflow_id,
            split=CaseSplit.FINAL_ACCEPTANCE,
            baseline_digest=baseline.digest(),
            candidate_digest=candidate.content_digest(),
            case_set_digest=pack.digest(),
            rubric_digest=rubric_digest,
            mode=mode,
            execution_status="running",
        )
        attempts: list[AttemptRecord] = []
        # See run_paired_evaluation: side digests are the ones the durable run
        # record binds to (baseline version digest, candidate CONTENT digest).
        side_digests = {
            Side.BASELINE: baseline.digest(),
            Side.CANDIDATE: candidate.content_digest(),
        }
        for repeat in range(rubric.repeats):  # the frozen standard governs repeats
            for case in cases:
                for side in (Side.BASELINE, Side.CANDIDATE):
                    attempts.append(
                        self._run_attempt(
                            run=run,
                            side=side,
                            case_id=case.case_id,
                            case_input={
                                "caseId": case.case_id,
                                "repeat": repeat,
                                "side": side.value,
                                "inputDigest": case.input_digest,
                                # A6: the known side-specific version digest
                                # always travels with the attempt.
                                "versionDigest": side_digests[side],
                            },
                            input_bytes=verified_inputs[case.case_id],
                            version_digest=side_digests[side],
                            workflow_id=workflow_id,
                            adapter=adapter,
                            mode=mode,
                            reserve_usd=per_attempt_reserve_usd,
                            timeout_s=None,
                            repeat_index=repeat,
                        )
                    )
        completed = EvaluationRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            split=run.split,
            baseline_digest=run.baseline_digest,
            candidate_digest=run.candidate_digest,
            case_set_digest=run.case_set_digest,
            rubric_digest=run.rubric_digest,
            attempts=tuple(attempts),
            outcomes=(),
            mode=run.mode,
            execution_status="completed",
            uncertainty_note=(
                ""
                if all(a.status is AttemptStatus.OK for a in attempts)
                else "contains non-ok attempts"
            ),
        )
        self._persist(KIND_EVALUATION, completed.run_id, completed)
        for attempt in attempts:
            self._persist(KIND_ATTEMPT, attempt.attempt_id, attempt)
        self._event(EventKind.RUN_COMPLETED, completed.run_id, {"finalAcceptance": True})
        return completed

    # --- decisions & release --------------------------------------------------

    def _accepted_target(self, verdict_value: str) -> CandidateState:
        if verdict_value == "accepted":
            return CandidateState.ACCEPTED
        if verdict_value == "rejected":
            return CandidateState.REJECTED
        return CandidateState.INCONCLUSIVE

    def record_decision(
        self,
        decision: AcceptanceDecision,
        *,
        role: Role,
        final_acceptance_run: EvaluationRun | str,
    ) -> None:
        """Record an acceptance decision against a DURABLE final run.

        The run is resolved by id from trusted storage (the caller's object is
        used for nothing but its id), must be completed on the
        final-acceptance split, and the verdict is recomputed here from the
        frozen rubric — a decision whose verdict contradicts that computation
        is refused, as is evidence not anchored to a persisted export of the
        run. Candidate state and the decision record are written atomically.
        """
        if role not in (Role.ACCEPTANCE_OWNER, Role.EVALUATOR):
            raise SplitAccessError("only the acceptance side may record decisions")
        run_id = (
            final_acceptance_run.run_id
            if isinstance(final_acceptance_run, EvaluationRun)
            else str(final_acceptance_run)
        )
        run = self._load_run(run_id)  # unknown/unpersisted ids fail closed
        if run.split is not CaseSplit.FINAL_ACCEPTANCE:
            raise SplitAccessError(
                "acceptance decisions require an evaluation on the final-acceptance split"
            )
        if run.execution_status != "completed":
            raise ContractError(
                f"final-acceptance run {run_id!r} has execution status "
                f"{run.execution_status!r}; only completed runs can ground a decision"
            )
        if not run.attempts:
            raise ContractError(
                f"final-acceptance run {run_id!r} recorded no attempts; there is no "
                "evidence to accept on"
            )
        if decision.candidate_digest != run.candidate_digest:
            raise ContractError("decision does not bind to this acceptance run's candidate")
        candidate = self._candidate_by_digest(decision.candidate_digest)
        # A7: the acceptance run must have measured the candidate's own parent
        # baseline. A run against a different baseline is not evidence about
        # THIS candidate's deployment lineage.
        if candidate.parent_version.digest() != run.baseline_digest:
            raise ContractError(
                f"acceptance run {run_id!r} measured baseline {run.baseline_digest} but "
                f"candidate {candidate.candidate_id!r} is derived from parent "
                f"{candidate.parent_version.version_id} "
                f"({candidate.parent_version.digest()}); rebase the candidate explicitly "
                "and re-run acceptance under fresh evidence"
            )

        # Owner validation (A5): the configured acceptance owner, never the
        # candidate's own proposer (any identity string, not just "proposer").
        configured_owner = self.project.owners.get(Role.ACCEPTANCE_OWNER, "")
        if configured_owner and decision.owner != configured_owner:
            raise ContractError(
                f"acceptance owner must be the project's configured acceptance owner "
                f"{configured_owner!r}, got {decision.owner!r}"
            )
        if decision.owner == candidate.proposer:
            raise ContractError(
                f"the proposer ({candidate.proposer!r}) cannot be the acceptance owner"
            )

        # Trusted-path verdict (A4): recompute from the durable record.
        _summary, verdict = self._recompute(run)
        if verdict is not decision.verdict:
            raise ContractError(
                f"decision verdict {decision.verdict.value!r} contradicts the "
                f"controller's recomputation ({verdict.value!r}) for run {run_id!r}"
            )
        rubric = self._frozen_rubric(run.rubric_digest)
        if verdict.value == "accepted":
            expected_binding = binding_for_run(run, rubric)
            if decision.binding is None or decision.binding.digest() != expected_binding.digest():
                raise ContractError(
                    "accepted decision's approval binding does not match the "
                    f"final-acceptance run {run_id!r}"
                )

        # Evidence anchor: the digest must match a persisted export of THIS run.
        export = self.store.load(KIND_RUN_EXPORT, run_id)
        if export is None or str(export.get("manifestDigest")) != decision.evidence_digest:
            raise ContractError(
                f"decision evidence digest is not anchored to a persisted export of "
                f"run {run_id!r}; export evidence before recording the decision"
            )

        moved = candidate.with_transition(self._accepted_target(verdict.value))
        decision = replace(decision, final_acceptance_run_id=run_id)
        with self.store.transaction():
            self._save_candidate(moved)
            self._persist(KIND_DECISION, decision.decision_id, decision)
        self._event(
            EventKind.APPROVAL_RECORDED,
            decision.decision_id,
            {"verdict": decision.verdict.value, "candidate": decision.candidate_digest},
        )

    def _latest_accepted_decision(self, candidate: Candidate) -> AcceptanceDecision | None:
        """The durable ACCEPTED decision for a candidate, if any."""
        best: AcceptanceDecision | None = None
        for decision_id in self.store.list_ids(KIND_DECISION):
            data = self.store.load(KIND_DECISION, decision_id)
            if data is None:  # pragma: no cover - id came from list_ids
                continue
            decision = AcceptanceDecision.from_dict(data)
            if decision.candidate_digest != candidate.content_digest():
                continue
            if decision.verdict.value != "accepted" or decision.binding is None:
                continue
            if best is None or decision.decided_at > best.decided_at:
                best = decision
        return best

    def _durable_decision_for_binding(
        self, candidate: Candidate, binding: ApprovalBinding
    ) -> AcceptanceDecision:
        """The durable accepted decision carrying exactly this binding."""
        for decision_id in self.store.list_ids(KIND_DECISION):
            data = self.store.load(KIND_DECISION, decision_id)
            if data is None:  # pragma: no cover - id came from list_ids
                continue
            decision = AcceptanceDecision.from_dict(data)
            if decision.verdict.value != "accepted":
                continue
            if decision.candidate_digest != candidate.content_digest():
                continue
            if decision.binding is not None and decision.binding.digest() == binding.digest():
                return decision
        raise ContractError(
            "no durable accepted decision carries this approval binding; acceptance "
            "must be recorded (and persisted) before approval"
        )

    def _verify_decision_current(self, decision: AcceptanceDecision, candidate: Candidate) -> None:
        """Re-resolve a decision's digests from trusted state (A5).

        The binding is verified against the DURABLE final-acceptance run and
        the CURRENT active rubric/baseline mappings — never against values
        the caller supplied. Any drift invalidates the approval.
        """
        run_id = decision.final_acceptance_run_id
        if not run_id:
            raise ContractError(
                f"decision {decision.decision_id!r} predates durable-run binding and "
                "cannot be re-verified; record a fresh acceptance decision"
            )
        run = self._load_run(run_id)
        if run.split is not CaseSplit.FINAL_ACCEPTANCE or run.execution_status != "completed":
            raise ApprovalInvalidatedError(
                f"backing final-acceptance run {run_id!r} is no longer a completed "
                "final-acceptance record"
            )
        if run.candidate_digest != candidate.content_digest():
            raise ApprovalInvalidatedError("candidate content changed since the acceptance run")
        # A7: candidate lineage must still equal the baseline the acceptance
        # run MEASURED. Comparing only the active baseline against the run
        # let a v0-parented candidate ride on a v1 measurement; the parent
        # itself is the deployment predecessor and rollback target, so a
        # mismatch invalidates the approval until an explicit rebase produces
        # a new candidate digest and fresh evidence.
        if candidate.parent_version.digest() != run.baseline_digest:
            raise ApprovalInvalidatedError(
                f"candidate {candidate.candidate_id!r} is derived from parent "
                f"{candidate.parent_version.version_id} "
                f"({candidate.parent_version.digest()}) while the acceptance run "
                f"{run_id!r} measured {run.baseline_digest}; the approval no longer "
                "applies — rebase explicitly and re-accept under fresh evidence"
            )
        assert decision.binding is not None  # accepted decisions carry bindings
        decision.binding.verify(
            candidate_digest=candidate.content_digest(),
            rubric_digest=run.rubric_digest,
            environment_digest=run.environment_digest or UNRECORDED_ENVIRONMENT_DIGEST,
            acceptance_case_set_digest=run.case_set_digest,
        )
        # Current active rubric: a later `baseline`/re-freeze invalidates.
        rubric_mapping = self.store.load(KIND_WORKFLOW_RUBRIC, run.workflow_id)
        if (
            rubric_mapping is not None
            and str(rubric_mapping.get("rubricDigest", "")) != run.rubric_digest
        ):
            raise ApprovalInvalidatedError(
                f"active rubric for workflow {run.workflow_id!r} changed since the "
                "acceptance run; re-evaluate and re-accept under the new standard"
            )
        # Current active baseline: the run measured the version still installed.
        baseline_mapping = self.store.load(KIND_WORKFLOW_BASELINE, run.workflow_id)
        if baseline_mapping is not None:
            record = self.store.load(
                KIND_BASELINE, str(baseline_mapping.get("baselineRecordId", ""))
            )
            if record is not None:
                current_baseline = AgentVersion.from_dict(record)
                if current_baseline.digest() != run.baseline_digest:
                    raise ApprovalInvalidatedError(
                        f"active baseline for workflow {run.workflow_id!r} changed "
                        "since the acceptance run; the decision no longer applies"
                    )
        # The bound case set must still be durably available.
        if self._pack_by_digest(run.case_set_digest) is None:
            raise ApprovalInvalidatedError(
                "the acceptance case set bound to this decision is no longer stored; "
                "its evidence cannot be re-reviewed"
            )

    def record_approval(
        self, candidate_id: str, *, role: Role, binding: ApprovalBinding
    ) -> Candidate:
        if role not in (Role.RELEASE_OWNER, Role.ACCEPTANCE_OWNER):
            raise ContractError("only the release/acceptance owner may approve")
        candidate = self._candidate(candidate_id)
        decision = self._durable_decision_for_binding(candidate, binding)
        self._verify_decision_current(decision, candidate)
        approved = candidate.with_transition(CandidateState.APPROVED)
        with self.store.transaction():
            self._save_candidate(approved)
        self._event(EventKind.APPROVAL_RECORDED, candidate_id, {"state": "approved"})
        return approved

    def record_release(self, record: ReleaseRecord, *, role: Role) -> None:
        if role is not Role.RELEASE_OWNER:
            raise ContractError("only the release owner may record a release")
        candidate = self._candidate_by_digest(record.candidate_digest)
        decision = self._latest_accepted_decision(candidate)
        if decision is None:
            raise ContractError(
                "no durable accepted decision for this candidate; approval must "
                "come before a release record"
            )
        # Release independently re-verifies: drift after approve breaks here.
        self._verify_decision_current(decision, candidate)
        released = candidate.with_transition(CandidateState.RELEASED)
        with self.store.transaction():
            self._save_candidate(released)
            self._persist(KIND_RELEASE, record.release_id, record)
        self._event(EventKind.RELEASE_RECORDED, record.release_id, {"recorded": True})

    def _candidate_by_digest(self, digest: str) -> Candidate:
        return self.candidate_by_content_digest(digest)

    def candidate_by_content_digest(self, digest: str) -> Candidate:
        """Public lookup of a candidate by its CONTENT digest (state-independent)."""
        for candidate_id in self.store.list_ids(KIND_CANDIDATE):
            candidate = self._candidate(candidate_id)
            if candidate.content_digest() == digest:
                return candidate
        raise ContractError(f"no candidate matches digest {digest!r}")

    # --- recovery ------------------------------------------------------------

    def mark_needs_reconciliation(self, subject: str, reason: str) -> None:
        self._event(EventKind.RUN_RECONCILIATION, subject, {"reason": reason, "state": "needed"})

    def reconcile(self, subject: str, action: str) -> None:
        """Explicit reconciliation action (query or human verification).

        Blind replay after unknown side effects is forbidden; callers must
        describe what was verified. Recording the action itself is the gate
        for resuming deterministic offline work.
        """
        if not action.strip():
            raise ReconciliationRequiredError(
                "reconciliation requires a description of what was verified"
            )
        self._event(EventKind.RUN_RECONCILIATION, subject, {"action": action, "state": "resolved"})

    # --- reporting ---------------------------------------------------------------

    def full_cost_usd(self) -> dict[str, Any]:
        entries = self.journal.cost_entries()
        measured = sum(e.amount_usd or 0.0 for e in entries if e.measurable)
        unmeasurable = [e.entry_id for e in entries if not e.measurable]
        human_minutes = sum(e.human_minutes or 0.0 for e in entries)
        return {
            "measuredUsd": round(measured, 6),
            "unmeasurableEntries": unmeasurable,
            "humanMinutes": human_minutes,
            "outstandingReservedUsd": self.ledger.outstanding_usd(),
            "remainingUsd": self.ledger.remaining_usd(),
        }
