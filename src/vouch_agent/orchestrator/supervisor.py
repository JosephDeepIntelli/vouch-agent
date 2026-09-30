"""The Supervisor — bounded business task execution (design §4.4, §10).

The supervisor is trusted code. It drives the execution loop
``confirm goal/constraints -> gather context -> bounded model steps ->
check completion conditions -> deliver or stop explicitly`` and owns the
honesty rules that make a ``ResultPackage`` mean something:

* **Budget before work** — a run-level reservation is taken on the
  :class:`~vouch_agent.storage.interfaces.BudgetLedger` at submit, before
  any step runs. Every *underlying model query* (nested and multi-turn
  included) carves an atomic child reservation out of the run's parent
  through :class:`_StepQueryBudget` BEFORE the call starts, so a second
  $0.01 turn under a $0.015 task cap is refused instead of executed and
  reconciled afterwards (review A2). Unmeasurable cost settles
  conservatively at the reservation and is recorded as an uncertainty,
  never as zero.
* **Failure keeps its metering** — measured usage is snapshotted around
  every step and carried through error paths (on the exception where the
  runtime can attach it): an exception's class name never certifies zero
  spend. A verified pre-execution rejection books nothing; an operation
  that began settles its measured cost and conservatively assumes the rest.
* **Persist intent, act, persist result** — every step is persisted as
  in-flight *before* its side-effecting work and updated after its outcome
  is known; a checkpoint (snapshot + journal event) follows each persisted
  step (see :mod:`vouch_agent.orchestrator.checkpoints`).
* **Owned runs and fenced writes** (review A3) — a persisted ownership
  lease says which supervisor may checkpoint/finalize a run; every run
  write is revision-fenced. Another supervisor may *request* cancel/pause
  but cannot finalize an actively owned run; a superseded owner's writes
  are rejected instead of overwriting the new decision. Cancellation stays
  authoritative: a terminal status is never replaced by a stale snapshot.
* **Recoverable terminal writes** — a write-ahead
  :class:`~vouch_agent.orchestrator.records.FinalizationIntent` lists each
  terminal write (result package, ledger settlement, metadata/journal
  finalization, ownership release); a crash between them is finished
  exactly once on restart without replaying model/effect work.
* **A model saying "done" never completes a run** — only the spec's
  ``success_criteria`` checked by deterministic trusted code
  (:mod:`vouch_agent.orchestrator.conditions`) can; a ``none`` marker ends
  the run incomplete-but-delivered, recorded honestly. A run that overran
  its budget is never certified as budget-conformant completion.
* **Fail closed** — bounds (``max_steps``, wall clock, budget) stop the run
  explicitly with an honest package; known protocol failures
  (``LiveCallBlockedError``, replay exhaustion) fail the step without
  crashing the supervisor; *unexpected* exceptions during side-effecting
  steps mark the step ``side_effect_unknown`` and move the run to
  ``needs-reconciliation`` — resuming requires a verified note and never
  replays the unknown step.
* **Invocation bookkeeping** — every model step records a child
  :class:`~vouch_agent.contracts.invocation.Invocation` under the run's
  root invocation; capabilities narrow only; each carries its budget slice
  reservation id.

What this supervisor does NOT do (honest gaps): no real model backend or
hardened sandbox (it talks to the ``Runtime`` port only), no external tool
calls or broker-authorized side effects, no concurrency within a run, and
the ownership lease is only as strong as the metadata store's transaction
(an in-memory store cannot fence two processes).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from vouch_agent.contracts.common import (
    RunMode,
    canonical_json,
    digest_of,
    new_id,
    utc_now_iso,
)
from vouch_agent.contracts.invocation import (
    Invocation,
    InvocationStatus,
    new_invocation_id,
)
from vouch_agent.contracts.journal import (
    BudgetReservation,
    CostCategory,
    CostEntry,
    EventKind,
    EventRecord,
    ReservationStatus,
)
from vouch_agent.contracts.tasks import (
    TASK_TRANSITIONS,
    ResultPackage,
    RunStep,
    StepKind,
    TaskRun,
    TaskSpec,
    TaskStatus,
    new_run_id,
    transition_task_status,
)
from vouch_agent.errors import (
    BudgetError,
    BudgetExhaustedError,
    ContractError,
    DigestMismatchError,
    InvalidStateTransitionError,
    ReconciliationRequiredError,
    ReservationError,
    VouchError,
)
from vouch_agent.orchestrator.checkpoints import (
    CHECKPOINT_KIND,
    SIDE_EFFECTING_STEP_KINDS,
    STEP_STATUS_FAILED,
    STEP_STATUS_OK,
    STEP_STATUS_RUNNING,
    STEP_STATUS_SKIPPED,
    STEP_STATUS_UNKNOWN,
    write_checkpoint,
)
from vouch_agent.orchestrator.conditions import (
    CONDITION_ARTIFACT_SCHEMA,
    ConditionContext,
    ConditionEvaluation,
    evaluate_conditions,
    normalize_criteria,
)
from vouch_agent.orchestrator.records import (
    KIND_RUN_CLOCK,
    KIND_RUN_FINALIZATION,
    KIND_RUN_OWNERSHIP,
    FinalizationIntent,
    NativeOperationResult,
    OwnershipRecord,
    RunOwnershipError,
    read_finalization,
    read_ownership,
)
from vouch_agent.runtime.failure_usage import usage_of
from vouch_agent.runtime.ports import ModelCallResult, Runtime, RuntimeSession, WorkerSessionConfig
from vouch_agent.storage.interfaces import (
    ArtifactStore,
    Journal,
    MetadataStore,
)

# --- persisted record kinds (the metadata store is kind-agnostic) -------------

KIND_TASK_SPEC = "task-spec"
KIND_TASK_RUN = "task-run"
KIND_INVOCATION = "invocation"
KIND_RESULT_PACKAGE = "result-package"
KIND_RUN_INDEX = "run-index"
KIND_RUN_RESERVATION = "run-reservation"
KIND_BUDGET_SLICE = "budget-slice"
KIND_CANCEL_REQUEST = "cancel-request"
KIND_PAUSE_REQUEST = "pause-request"
KIND_RECONCILIATION_NOTE = "reconciliation-note"
#: Monotonic run-global scripted-response cursor (M4 A1 review §2): the
#: number of underlying provider responses COMPLETED model steps have
#: consumed, persisted as an ABSOLUTE number together with the execution
#: configuration identity it is valid for. Never reconstructed by summing
#: per-step counters.
KIND_RUN_QUERY_CURSOR = "run-query-cursor"

#: Step statuses that mean a work step was consumed (its script position is
#: taken; it is never re-run). ``unknown`` steps are consumed *and* gated on
#: reconciliation before anything continues.
_CONSUMED_WORK_STATUSES = frozenset({STEP_STATUS_OK, STEP_STATUS_UNKNOWN})

_TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})

#: Float tolerance for budget comparisons (never "pass" a bound by a hair).
_EPS = 1e-9


def _now_epoch() -> float:
    """Wall-clock epoch seconds — persisted deadlines must survive restarts,
    so ``time.monotonic()`` (per-process) cannot be used for them."""
    return time.time()


class _StopReason(StrEnum):
    CONDITIONS_MET = "conditions-met"
    PENDING_ACCEPTANCE = "pending-acceptance"
    MAX_STEPS = "max-steps"
    WALL_CLOCK = "wall-clock"
    BUDGET_EXHAUSTED = "budget-exhausted"
    MODEL_FAILED = "model-failed"
    PREPARE_FAILED = "prepare-failed"
    NO_CRITERIA = "no-success-criteria"
    CANCELLED = "cancelled"
    NEEDS_RECONCILIATION = "needs-reconciliation"
    OPERATION_INCOMPLETE = "operation-incomplete"


@dataclass(frozen=True)
class _Stop:
    reason: _StopReason
    error: str | None = None


@dataclass(frozen=True)
class SupervisorPolicy:
    """Trusted defaults applied when a TaskSpec leaves a bound unspecified.

    Capability rule: each step kind requests a subset of the run's granted
    capabilities — widening is rejected at construction time, and the
    invocation contract enforces narrowing again per child invocation.
    """

    default_budget_usd: float = 1.0
    default_max_steps: int = 8
    default_wall_clock_s: float = 300.0
    step_budget_usd: float = 0.25
    #: How long an ownership lease stays valid without a checkpoint
    #: heartbeat. Another supervisor may take over an expired lease; the
    #: superseded owner's writes are then rejected by revision fencing.
    ownership_lease_s: float = 900.0
    #: A paused run may not sit on its budget reservation forever: past this
    #: expiry a resume is refused (cancel() still closes it honestly).
    paused_run_expiry_s: float = 86400.0
    run_capabilities: tuple[str, ...] = (
        "model.invoke",
        "artifact.read",
        "artifact.write",
    )
    model_step_capabilities: tuple[str, ...] = ("model.invoke",)
    context_step_capabilities: tuple[str, ...] = ("artifact.read",)

    def __post_init__(self) -> None:
        if self.default_budget_usd <= 0:
            raise ContractError("default_budget_usd must be > 0")
        if self.default_max_steps <= 0:
            raise ContractError("default_max_steps must be > 0")
        if self.default_wall_clock_s <= 0:
            raise ContractError("default_wall_clock_s must be > 0")
        if self.step_budget_usd <= 0:
            raise ContractError("step_budget_usd must be > 0")
        if self.ownership_lease_s <= 0:
            raise ContractError("ownership_lease_s must be > 0")
        if self.paused_run_expiry_s <= 0:
            raise ContractError("paused_run_expiry_s must be > 0")
        if len(set(self.run_capabilities)) != len(self.run_capabilities):
            raise ContractError("run_capabilities must not contain duplicates")
        for name, caps in (
            ("model_step_capabilities", self.model_step_capabilities),
            ("context_step_capabilities", self.context_step_capabilities),
        ):
            if not caps:
                raise ContractError(f"{name} must not be empty")
            widened = sorted(set(caps) - set(self.run_capabilities))
            if widened:
                raise ContractError(f"{name} cannot widen run_capabilities: {widened}")

    def digest(self) -> str:
        return digest_of(
            {
                "policyVersion": 2,
                "defaultBudgetUsd": self.default_budget_usd,
                "defaultMaxSteps": self.default_max_steps,
                "defaultWallClockS": self.default_wall_clock_s,
                "stepBudgetUsd": self.step_budget_usd,
                "ownershipLeaseS": self.ownership_lease_s,
                "pausedRunExpiryS": self.paused_run_expiry_s,
                "runCapabilities": list(self.run_capabilities),
                "modelStepCapabilities": list(self.model_step_capabilities),
                "contextStepCapabilities": list(self.context_step_capabilities),
            }
        )


class _HierarchyLedger(Protocol):
    """The budget-ledge surface the supervisor needs (review A2).

    ``reserve_child`` / ``children`` / ``settle_parent_from_children`` are the
    hierarchical primitives the storage ledger provides; they are not yet on
    the storage *port* Protocol, so the supervisor states what it needs here.
    """

    def reserve(self, holder: str, amount_usd: float) -> BudgetReservation: ...

    def reserve_child(
        self, parent_reservation_id: str, holder: str, amount_usd: float
    ) -> BudgetReservation: ...

    def settle(self, reservation_id: str, actual_usd: float) -> BudgetReservation: ...

    def release(self, reservation_id: str) -> BudgetReservation: ...

    def children(self, parent_reservation_id: str) -> list[BudgetReservation]: ...

    def settle_parent_from_children(
        self, parent_reservation_id: str, *, overage_usd: float = 0.0
    ) -> BudgetReservation: ...

    def outstanding_usd(self) -> float: ...

    def settled_usd(self) -> float: ...

    def remaining_usd(self) -> float: ...


@dataclass
class _RunState:
    """Mutable working state for one run, rebuilt from persistence on load."""

    run: TaskRun
    spec: TaskSpec
    conditions: tuple[dict[str, Any], ...]
    reservation: BudgetReservation
    root_invocation_id: str
    policy_digest: str
    steps: list[RunStep]
    budget_usd: float
    max_steps: int
    wall_clock_s: float
    measured_usd: float = 0.0
    assumed_unmeasured_usd: float = 0.0
    #: Spend no child reservation covers (work whose outcome is unknown but
    #: which never reserved a query, or spend a runtime reported beyond its
    #: reservations) — booked as an explicit overage when the run reservation
    #: closes, so the ledger never hides it inside the children's actuals.
    childless_assumed_usd: float = 0.0
    childless_measured_usd: float = 0.0
    #: Spend the runtime reported beyond its per-query reservations: a
    #: port-contract violation, booked honestly but never certified away.
    unreserved_spend_usd: float = 0.0
    #: Query reservations whose real cost the parent refused to absorb: the
    #: overrun stays open for reconciliation instead of being clipped.
    overrun_reservations: dict[str, float] = dataclasses.field(default_factory=dict)
    unmeasured_cost: bool = False
    artifact_keys: dict[str, str] = dataclasses.field(default_factory=dict)
    model_ordinal: int = 1
    sequence: int = 0
    #: Honest caveats a native deterministic operation reported (e.g.
    #: "discrepancies found — review the report"), folded into the package.
    native_not_done: tuple[str, ...] = ()
    #: Revision of the persisted task-run record this state was built from;
    #: every write must be based on the current revision or it is fenced.
    revision: int = 0

    def conservative_spent_usd(self) -> float:
        return self.measured_usd + self.assumed_unmeasured_usd

    def overrun_usd(self) -> float:
        return self.conservative_spent_usd() - self.budget_usd


class _StepQueryBudget:
    """Controller-side :class:`~vouch_agent.runtime.ports.QueryBudget`.

    Every underlying model query of the current step carves an atomic child
    reservation out of the run's parent reservation BEFORE the call starts.
    Because committed children can never exceed the parent, a query that
    does not fit the remaining task budget is refused before it runs — the
    enforcement point the review demanded, instead of accounting for the
    overshoot afterwards.

    Every settlement books a journal cost line, so the journal, the ledger
    children and the slice mirror all tell the same story.

    Thread safety (M3 Gate A3): in the process-isolated runtime these calls
    arrive on the worker channel's reader thread while the supervisor drives
    per-step lifecycle methods on its own thread — every method is therefore
    serialized by one reentrant lock.
    """

    def __init__(
        self,
        ledger: _HierarchyLedger,
        parent_reservation_id: str,
        journal: Journal,
        *,
        run_id: str,
        mode: RunMode,
    ) -> None:
        self._ledger = ledger
        self._parent = parent_reservation_id
        self._journal = journal
        self._run_id = run_id
        self._mode = mode
        self._step_holder = ""
        self._allowance_usd = 0.0
        self._open: dict[str, float] = {}
        self._step_ids: list[str] = []
        self._query_seq = 0
        self._step_measured = 0.0
        self._step_assumed = 0.0
        #: reservation_id -> refused actual: the parent could not absorb the
        #: provider's real cost, so the overrun stays OPEN for reconciliation.
        self._refused: dict[str, float] = {}
        self._lock = threading.RLock()

    # -- per-step lifecycle ------------------------------------------------------

    def begin_step(self, holder: str, allowance_usd: float) -> None:
        with self._lock:
            self._step_holder = holder
            self._allowance_usd = allowance_usd
            self._step_ids = []
            self._step_measured = 0.0
            self._step_assumed = 0.0

    def end_step(self) -> list[str]:
        """Reservation ids created by this step whose query never settled."""
        with self._lock:
            orphans = [rid for rid in self._step_ids if rid in self._open]
            self._step_ids = []
            return orphans

    def step_created_any(self) -> bool:
        with self._lock:
            return bool(self._step_ids)

    def step_measured(self) -> float:
        """Measured dollars this step's query children settled."""
        with self._lock:
            return round(self._step_measured, 6)

    def step_assumed(self) -> float:
        """Dollars this step had to assume conservatively (unmeasured queries
        and reservations whose query never reported)."""
        with self._lock:
            return round(self._step_assumed, 6)

    # -- QueryBudget port ----------------------------------------------------------

    def reserve_query(self, estimate_usd: float | None) -> str:
        with self._lock:
            # An unknown price reserves the whole remaining step allowance: the
            # conservative reading. It self-corrects on settlement (settled
            # children stop counting at their reservation amount), and while it
            # is open NO further query can be dispatched under it.
            amount = float(estimate_usd) if estimate_usd is not None else self._allowance_usd
            if not amount > _EPS:
                raise BudgetExhaustedError(
                    f"no budget left to reserve for a model query in {self._step_holder or 'run'}"
                )
            self._query_seq += 1
            child = self._ledger.reserve_child(
                self._parent, f"{self._step_holder}#q{self._query_seq}", round(amount, 6)
            )
            self._open[child.reservation_id] = child.amount_usd
            self._step_ids.append(child.reservation_id)
            return child.reservation_id

    def settle_query(self, reservation_id: str, actual_usd: float | None) -> None:
        with self._lock:
            amount = self._open.pop(reservation_id, None)
            if amount is None:
                return
            if actual_usd is None:
                # Unmeasurable: book the full reservation conservatively, never zero.
                self._ledger.settle(reservation_id, amount)
                self._step_assumed += amount
                self._book(amount=None, measurable=False, reservation_id=reservation_id)
                return
            actual = float(actual_usd)
            if not math.isfinite(actual):
                # Non-finite metering can never be booked; refuse it (B2) rather
                # than letting NaN/inf reach the ledger or a report.
                self._open[reservation_id] = amount
                raise ContractError(
                    f"non-finite cost {actual_usd!r} reported for query {reservation_id}"
                )
            try:
                self._ledger.settle(reservation_id, round(actual, 6))
            except BudgetExhaustedError:
                # The provider exceeded its declared bound beyond what the parent
                # can absorb: the child stays OPEN, the reported actual is kept on
                # record and the overrun is surfaced for reconciliation — never
                # clipped down to what the reservation allowed.
                self._open[reservation_id] = amount
                self._refused[reservation_id] = actual
                self._journal.append(
                    EventRecord(
                        event_id=new_id("evt"),
                        kind=EventKind.AUDIT_NOTE,
                        subject=self._run_id,
                        actor="supervisor",
                        mode=self._mode,
                        data={
                            "action": "settlement-refused-overrun",
                            "reservationId": reservation_id,
                            "reportedUsd": round(actual, 6),
                            "reservedUsd": round(amount, 6),
                        },
                    )
                )
                raise
            self._step_measured += round(actual, 6)
            self._book(amount=round(actual, 6), measurable=True, reservation_id=reservation_id)

    def release_query(self, reservation_id: str) -> None:
        with self._lock:
            amount = self._open.pop(reservation_id, None)
            if amount is None:
                return
            self._ledger.release(reservation_id)

    def adopt_orphan(self, reservation_id: str) -> float:
        """Conservatively settle a reservation whose query never reported."""
        with self._lock:
            amount = self._open.pop(reservation_id, 0.0)
            if amount > 0:
                self._ledger.settle(reservation_id, amount)
                self._step_assumed += amount
                self._book(amount=None, measurable=False, reservation_id=reservation_id)
            return amount

    def amount_of(self, reservation_id: str) -> float:
        with self._lock:
            return self._open.get(reservation_id, 0.0)

    def refused_settlements(self) -> dict[str, float]:
        """Actual costs the parent reservation refused to absorb (overruns)."""
        with self._lock:
            return dict(self._refused)

    # -- internals -------------------------------------------------------------------

    def _book(self, *, amount: float | None, measurable: bool, reservation_id: str) -> None:
        self._journal.append_cost(
            CostEntry(
                entry_id=new_id("cost"),
                category=CostCategory.MODEL,
                subject=self._run_id,
                amount_usd=amount,
                measurable=measurable,
                mode=self._mode,
                note=f"query reservation {reservation_id}",
            )
        )


def _usage_cost(usage: dict[str, Any] | None) -> tuple[float | None, bool]:
    """(measured cost, unmeasured flag) from a session usage snapshot.

    Returns ``(None, True)`` when the shape is unrecognized or the session
    reports unmeasured calls: spend that cannot be measured is never zero.
    """
    if not isinstance(usage, dict):
        return None, True
    if "cost_usd" in usage:  # JazSession shape
        cost = usage.get("cost_usd")
        unmeasured = int(usage.get("unmeasured_calls", 0) or 0) > 0 or cost is None
        return (None if cost is None else float(cost)), unmeasured
    if "costUsd" in usage:  # scripted-test runtime shape
        cost = usage.get("costUsd")
        return (None if cost is None else float(cost)), cost is None
    return None, True


class Supervisor:
    """Business task execution over the runtime/storage ports."""

    def __init__(
        self,
        runtime: Runtime,
        store: MetadataStore,
        artifacts: ArtifactStore,
        ledger: _HierarchyLedger,
        journal: Journal,
        policy: SupervisorPolicy | None = None,
        *,
        config_identity: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._artifacts = artifacts
        self._ledger = ledger
        self._journal = journal
        self._policy = policy if policy is not None else SupervisorPolicy()
        self._active: set[str] = set()
        #: Sessions currently executing a run, for prompt external stops
        #: (cancel from another client): maps run id -> the session whose
        #: owned worker group must be stopped. Guarded by its own lock —
        #: ``stop_active_session`` is called from a NON-supervisor thread
        #: (the worker's cancel poller), unlike everything else here.
        self._active_sessions: dict[str, RuntimeSession] = {}
        self._session_lock = threading.Lock()
        #: Execution-configuration identity (e.g. the sealed provider/script
        #: digest) stamped into the durable query-cursor record: a cursor is
        #: only meaningful against the pool it was consumed from, so a later
        #: reader can refuse a cursor recorded under a different config.
        self._config_identity = config_identity
        #: Identity of this supervisor instance in the persisted ownership
        #: lease: distinct Supervisor objects (same process or not) are
        #: distinct owners.
        self._owner_id = new_id("sup")

    # --- public API ------------------------------------------------------------

    def submit(self, spec: TaskSpec) -> str:
        """Persist spec + queued run; reserve budget BEFORE any work starts."""
        conditions = normalize_criteria(spec.success_criteria)  # fail closed early
        self._validate_spec_bounds(spec)
        budget_usd = (
            float(spec.max_cost_usd)
            if spec.max_cost_usd is not None
            else self._policy.default_budget_usd
        )
        run_id = new_run_id()
        # Atomic ledger reservation first: if the project cap refuses, nothing
        # is persisted and nothing is described as schedulable.
        reservation = self._ledger.reserve(holder=run_id, amount_usd=budget_usd)
        policy_digest = self._policy.digest()

        self._store.save(KIND_TASK_SPEC, spec.spec_id, spec.to_dict())
        root_invocation = Invocation(
            invocation_id=new_invocation_id(),
            task_ref=spec.spec_id,
            input_refs=(),
            output_schema={},
            policy_digest=policy_digest,
            budget_reservation_id=reservation.reservation_id,
            granted_capabilities=self._policy.run_capabilities,
        )
        self._store.save(KIND_INVOCATION, root_invocation.invocation_id, root_invocation.to_dict())
        run = TaskRun(
            run_id=run_id,
            task_digest=spec.digest(),
            status=TaskStatus.QUEUED,
            mode=spec.mode,
        )
        self._store.save(KIND_TASK_RUN, run_id, {**run.to_dict(), "revision": 1})
        self._store.save(
            KIND_RUN_INDEX,
            run_id,
            {
                "specId": spec.spec_id,
                "taskDigest": run.task_digest,
                "reservationId": reservation.reservation_id,
                "rootInvocationId": root_invocation.invocation_id,
                "policyDigest": policy_digest,
                "budgetUsd": budget_usd,
            },
        )
        self._store.save(KIND_RUN_RESERVATION, run_id, reservation.to_dict())
        self._journal.append(
            EventRecord(
                event_id=new_id("evt"),
                kind=EventKind.BUDGET_RESERVED,
                subject=run_id,
                actor="supervisor",
                mode=spec.mode,
                data={
                    "level": "run",
                    "reservationId": reservation.reservation_id,
                    "amountUsd": budget_usd,
                },
            )
        )
        self._journal.append(
            EventRecord(
                event_id=new_id("evt"),
                kind=EventKind.RUN_STARTED,
                subject=run_id,
                actor="supervisor",
                mode=spec.mode,
                data={
                    "taskDigest": run.task_digest,
                    "specId": spec.spec_id,
                    "budgetReservationId": reservation.reservation_id,
                    "budgetUsd": budget_usd,
                    "conditionCount": len(conditions),
                },
            )
        )
        state = self._load_state(run_id)
        self._checkpoint(state)
        return run_id

    def execute(self, run_id: str) -> TaskRun:
        """Run the §4.4 loop to an explicit stop. Returns the final TaskRun."""
        self._guard_not_active(run_id)
        pending = self._pending_finalization(run_id)
        if pending is not None:
            return self._finish_finalization(pending)
        self._acquire_ownership(run_id, purpose="execute")
        state = self._load_state(run_id)
        self._prepare_start(state)
        return self._run_loop(state)

    def resume(self, run_id: str, reconciliation_note: str = "") -> TaskRun:
        """Resume after reconciliation (or any non-terminal stop).

        A run with unknown side-effect state requires a non-empty, verified
        note — :class:`ReconciliationRequiredError` otherwise. The unknown
        step is never replayed: execution continues from the next step.
        Deterministic fixture runs with no unknown steps auto-continue.

        A run whose terminal writes are incomplete (a crash during
        finalization) is *finished*, not resumed: the missing bookkeeping
        runs exactly once and no model/effect work is replayed.
        """
        self._guard_not_active(run_id)
        pending = self._pending_finalization(run_id)
        if pending is not None:
            return self._finish_finalization(pending)
        self._acquire_ownership(run_id, purpose="resume")
        state = self._load_state(run_id)
        if state.run.status in _TERMINAL_STATUSES:
            raise InvalidStateTransitionError(
                f"run {run_id} is terminal ({state.run.status.value}); nothing to resume"
            )
        if state.run.status is TaskStatus.RUNNING and any(
            s.status == STEP_STATUS_RUNNING and s.kind in SIDE_EFFECTING_STEP_KINDS
            for s in state.steps
        ):
            # A hard crash left a side-effecting step in flight with the run
            # still marked RUNNING. resume() is the reconciliation entry: it
            # normalizes the unknown step and moves the run to
            # NEEDS_RECONCILIATION before the note gate applies.
            self._enter_reconciliation(state, "crash left side-effecting step in flight")
        if state.run.status is TaskStatus.NEEDS_RECONCILIATION:
            self._reconcile(state, reconciliation_note)
        self._prepare_start(state)
        return self._run_loop(state)

    def cancel(self, run_id: str, reason: str = "") -> TaskRun:
        """Cooperative cancellation, applied at the next step boundary.

        Any client may record a cancellation request. If the run is executing
        in this supervisor, the loop finalizes at its next checkpoint; if
        another supervisor actively owns the run, the request stands and the
        owner finalizes it — this client does not finalize someone else's
        in-flight run. Only an unowned (or expired-lease) run is finalized
        here. Partial steps are kept; a cancelled run is never "completed".
        """
        state = self._load_state(run_id)
        if state.run.status in _TERMINAL_STATUSES:
            raise InvalidStateTransitionError(
                f"run {run_id} is terminal ({state.run.status.value}); cannot cancel"
            )
        self._store.save(
            KIND_CANCEL_REQUEST,
            run_id,
            {"requestedAt": utc_now_iso(), "reason": reason},
        )
        if run_id in self._active:
            return state.run  # the loop finalizes at its next boundary
        if not self._may_finalize(run_id):
            # Actively owned elsewhere: the request is authoritative for the
            # owner's next checkpoint; this client must not settle or
            # finalize another supervisor's in-flight run.
            self._journal_event(
                state,
                EventKind.AUDIT_NOTE,
                {"action": "cancel-requested", "owner": "another-supervisor", "reason": reason},
            )
            return state.run
        self._acquire_ownership(run_id, purpose="cancel")
        return self._finalize(state, _Stop(_StopReason.CANCELLED, error=reason or None), None)

    def pause(self, run_id: str) -> TaskRun:
        """Cooperative pause at the next step boundary (no result package)."""
        state = self._load_state(run_id)
        if state.run.status in _TERMINAL_STATUSES:
            raise InvalidStateTransitionError(
                f"run {run_id} is terminal ({state.run.status.value}); cannot pause"
            )
        if state.run.status is not TaskStatus.RUNNING:
            # QUEUED/PAUSED/NEEDS_RECONCILIATION have no PAUSED edge in v1.
            raise InvalidStateTransitionError(
                f"cannot pause a run in status {state.run.status.value}"
            )
        self._store.save(KIND_PAUSE_REQUEST, run_id, {"requestedAt": utc_now_iso()})
        if run_id in self._active:
            return state.run  # the loop pauses at its next boundary
        if not self._may_finalize(run_id):
            self._journal_event(
                state,
                EventKind.AUDIT_NOTE,
                {"action": "pause-requested", "owner": "another-supervisor"},
            )
            return state.run
        self._acquire_ownership(run_id, purpose="pause")
        return self._do_pause(state)

    def get_run(self, run_id: str) -> TaskRun | None:
        data = self._store.load(KIND_TASK_RUN, run_id)
        return TaskRun.from_dict(data) if data is not None else None

    def get_result(self, run_id: str) -> ResultPackage | None:
        data = self._store.load(KIND_RESULT_PACKAGE, run_id)
        return ResultPackage.from_dict(data) if data is not None else None

    # --- run ownership (review A3) ------------------------------------------------

    def _may_finalize(self, run_id: str) -> bool:
        """May THIS supervisor finalize the run right now?

        True when nobody holds a live lease. Our own stale lease (a crashed
        attempt by this instance) and an expired foreign lease are both
        takeable — recovery must be possible.
        """
        record = read_ownership(self._store, run_id)
        if record is None or not record.lease_live(_now_epoch()):
            return True
        return record.owner_id == self._owner_id

    def _acquire_ownership(self, run_id: str, *, purpose: str) -> OwnershipRecord:
        """Take the run's ownership lease (atomic check-and-set)."""
        now = _now_epoch()
        with self._store.transaction():
            record = read_ownership(self._store, run_id)
            if record is not None and record.lease_live(now) and record.owner_id != self._owner_id:
                raise RunOwnershipError(
                    f"run {run_id} is owned by supervisor {record.owner_id} "
                    f"(lease live for another {record.lease_expires_at_epoch_s - now:.0f}s); "
                    f"a second client may request cancel/pause but cannot {purpose} it"
                )
            acquired = OwnershipRecord(
                run_id=run_id,
                owner_id=self._owner_id,
                acquired_at=utc_now_iso(),
                lease_expires_at_epoch_s=now + self._policy.ownership_lease_s,
                heartbeat_at=utc_now_iso(),
                revision=((record.revision if record is not None else 0) + 1),
            )
            self._store.save(KIND_RUN_OWNERSHIP, run_id, acquired.to_dict())
        return acquired

    def _heartbeat_ownership(self, run_id: str) -> None:
        """Refresh the lease at each checkpoint (best effort; never fatal)."""
        record = read_ownership(self._store, run_id)
        if record is None or record.released_at is not None:
            return
        if record.owner_id != self._owner_id:
            raise RunOwnershipError(
                f"run {run_id} ownership was taken over by {record.owner_id}; "
                "this supervisor's snapshot is stale and must stop writing"
            )
        refreshed = OwnershipRecord(
            run_id=run_id,
            owner_id=record.owner_id,
            acquired_at=record.acquired_at,
            lease_expires_at_epoch_s=_now_epoch() + self._policy.ownership_lease_s,
            heartbeat_at=utc_now_iso(),
            revision=record.revision,
            released_at=None,
        )
        self._store.save(KIND_RUN_OWNERSHIP, run_id, refreshed.to_dict())

    def _release_ownership(self, run_id: str) -> None:
        record = read_ownership(self._store, run_id)
        if record is None or record.owner_id != self._owner_id:
            return  # already released, or taken over by the new owner
        released = OwnershipRecord(
            run_id=run_id,
            owner_id=record.owner_id,
            acquired_at=record.acquired_at,
            lease_expires_at_epoch_s=record.lease_expires_at_epoch_s,
            heartbeat_at=record.heartbeat_at,
            revision=record.revision,
            released_at=utc_now_iso(),
        )
        self._store.save(KIND_RUN_OWNERSHIP, run_id, released.to_dict())

    # --- run clock (persisted wall-clock allowance) ---------------------------------

    def _ensure_clock(self, state: _RunState) -> dict[str, Any]:
        """Load or create the run's clock: the deadline NEVER resets."""
        clock = self._store.load(KIND_RUN_CLOCK, state.run.run_id)
        if clock is not None:
            return clock
        now = _now_epoch()
        clock = {
            "schemaVersion": "1",
            "runId": state.run.run_id,
            "wallClockS": state.wall_clock_s,
            "startedAtEpochS": now,
            "deadlineEpochS": now + state.wall_clock_s,
            "accumulatedElapsedS": 0.0,
            "lastResumedAtEpochS": now,
        }
        self._store.save(KIND_RUN_CLOCK, state.run.run_id, clock)
        return clock

    def _save_clock(self, clock: dict[str, Any]) -> None:
        self._store.save(KIND_RUN_CLOCK, str(clock["runId"]), clock)

    def _pause_expiry_epoch(self, state: _RunState) -> float | None:
        clock = self._store.load(KIND_RUN_CLOCK, state.run.run_id)
        if clock is None:
            return None
        expiry = clock.get("pauseExpiresAtEpochS")
        return float(expiry) if expiry is not None else None

    # --- start preparation -------------------------------------------------------

    def _guard_not_active(self, run_id: str) -> None:
        if run_id in self._active:
            raise ContractError(f"run {run_id} is already executing in this supervisor")

    @staticmethod
    def _validate_spec_bounds(spec: TaskSpec) -> None:
        if spec.max_cost_usd is not None and spec.max_cost_usd < 0:
            raise ContractError("TaskSpec.max_cost_usd must be >= 0")
        if spec.max_wall_clock_s is not None and spec.max_wall_clock_s <= 0:
            raise ContractError("TaskSpec.max_wall_clock_s must be > 0")
        if spec.max_steps is not None and spec.max_steps < 0:
            raise ContractError("TaskSpec.max_steps must be >= 0")

    def _prepare_start(self, state: _RunState) -> None:
        run = state.run
        if run.status in _TERMINAL_STATUSES:
            raise InvalidStateTransitionError(f"run {run.run_id} is terminal ({run.status.value})")
        if run.status is TaskStatus.PAUSED:
            expiry = self._pause_expiry_epoch(state)
            if expiry is not None and _now_epoch() > expiry:
                raise InvalidStateTransitionError(
                    f"run {run.run_id} has been paused past its expiry "
                    f"({self._policy.paused_run_expiry_s:.0f}s); its reservation stays open "
                    "until it is cancelled — cancel() closes it with honest accounting"
                )
        if run.status is TaskStatus.NEEDS_RECONCILIATION:
            raise ReconciliationRequiredError(
                f"run {run.run_id} needs reconciliation; call "
                "resume(run_id, reconciliation_note=...) with a verified note"
            )
        in_flight = [i for i, s in enumerate(state.steps) if s.status == STEP_STATUS_RUNNING]
        side_effecting = [
            state.steps[i] for i in in_flight if state.steps[i].kind in SIDE_EFFECTING_STEP_KINDS
        ]
        if side_effecting:
            ids = [s.step_id for s in side_effecting]
            raise ReconciliationRequiredError(
                f"crash left side-effecting step(s) {ids} with unknown outcome; "
                "recover() then resume(run_id, reconciliation_note=...) is required"
            )
        if in_flight:
            self._skip_in_flight_steps(state, in_flight)
        if run.status in (TaskStatus.QUEUED, TaskStatus.PAUSED):
            self._transition(state, TaskStatus.RUNNING)
        self._save_root_invocation(state, status=InvocationStatus.RUNNING)

    def _skip_in_flight_steps(self, state: _RunState, indexes: list[int]) -> None:
        """Deterministic in-flight steps are safe to skip and redo."""
        for i in indexes:
            old = state.steps[i]
            state.steps[i] = replace(old, status=STEP_STATUS_SKIPPED, ended_at=utc_now_iso())
        state.run = replace(state.run, steps=tuple(state.steps), updated_at=utc_now_iso())
        self._persist_run(state)
        self._journal_event(
            state,
            EventKind.AUDIT_NOTE,
            {
                "action": "skip-deterministic-in-flight-steps",
                "stepIds": [state.steps[i].step_id for i in indexes],
            },
        )
        self._checkpoint(state)

    def _enter_reconciliation(self, state: _RunState, reason: str) -> None:
        """Normalize in-flight steps and move a crashed RUNNING run to
        NEEDS_RECONCILIATION (the state whose only exits are an explicitly
        noted resume, an explicit failure, or cancellation)."""
        in_flight = [i for i, s in enumerate(state.steps) if s.status == STEP_STATUS_RUNNING]
        for i in in_flight:
            step = state.steps[i]
            if step.kind in SIDE_EFFECTING_STEP_KINDS:
                # Outcome unknowable from persisted state: consume the step,
                # mark it unknown, settle its slice conservatively.
                state.steps[i] = replace(
                    step,
                    status=STEP_STATUS_UNKNOWN,
                    ended_at=utc_now_iso(),
                    side_effect_unknown=True,
                    error=step.error or "in flight at crash; outcome unknown",
                )
                self._settle_slice_for_unknown_step(state, state.steps[i])
                state.unmeasured_cost = True
            else:
                state.steps[i] = replace(step, status=STEP_STATUS_SKIPPED, ended_at=utc_now_iso())
        state.run = replace(state.run, steps=tuple(state.steps), updated_at=utc_now_iso())
        self._persist_run(state)
        if state.run.status is TaskStatus.RUNNING:
            self._transition(state, TaskStatus.NEEDS_RECONCILIATION)
        self._journal_event(
            state,
            EventKind.RUN_RECONCILIATION,
            {"action": "marked-unknown", "reason": reason},
        )
        self._checkpoint(state)

    def _reconcile(self, state: _RunState, note: str) -> None:
        """Gate the NEEDS_RECONCILIATION -> RUNNING resume on a verified note."""
        in_flight = [i for i, s in enumerate(state.steps) if s.status == STEP_STATUS_RUNNING]
        if in_flight:  # e.g. an external tool parked the run mid-step
            self._enter_reconciliation(state, "in-flight steps found at resume")
        requires_note = any(
            s.side_effect_unknown or s.status == STEP_STATUS_UNKNOWN for s in state.steps
        )
        if state.run.mode is not RunMode.FIXTURE:
            requires_note = True  # only deterministic fixture runs auto-continue
        if requires_note and not note.strip():
            raise ReconciliationRequiredError(
                f"run {state.run.run_id} has unknown side-effect state; a "
                "non-empty verified reconciliation note is required before resume"
            )
        state.sequence += 1
        self._store.save(
            KIND_RECONCILIATION_NOTE,
            f"{state.run.run_id}:{state.sequence}",
            {"note": note, "recordedAt": utc_now_iso(), "noteRequired": requires_note},
        )
        self._journal_event(
            state,
            EventKind.RUN_RECONCILIATION,
            {"action": "resume", "note": note, "noteRequired": requires_note},
        )
        self._transition(state, TaskStatus.RUNNING)

    def _settle_slice_for_unknown_step(self, state: _RunState, step: RunStep) -> None:
        """Conservatively settle the budget slice of an unknown-outcome step."""
        ordinal = 0
        holder: str | None = None
        for candidate in state.steps:
            if candidate.kind is StepKind.MODEL_CALL:
                ordinal += 1
            if candidate.step_id == step.step_id:
                if candidate.kind is StepKind.MODEL_CALL:
                    holder = f"{state.run.run_id}#model-{ordinal}"
                break
        if holder is None:
            return
        slice_data = self._store.load(KIND_BUDGET_SLICE, holder)
        if slice_data is None or slice_data.get("status") != ReservationStatus.OPEN.value:
            return
        amount = float(slice_data["amountUsd"])
        self._settle_step_slice(
            state,
            slice_data,
            amount,
            unmeasurable=True,
            detail=f"step {step.step_id}: outcome unknown; cost unmeasured",
            assumed_usd=amount,
            childless_assumed=amount,
        )
        state.unmeasured_cost = True
        state.assumed_unmeasured_usd += amount
        state.childless_assumed_usd += amount

    # --- the execution loop (design §4.4) ----------------------------------------

    def _run_loop(self, state: _RunState) -> TaskRun:
        run_id = state.run.run_id
        self._active.add(run_id)
        session: RuntimeSession | None = None
        budget: _StepQueryBudget | None = None
        clock = self._ensure_clock(state)
        clock["lastResumedAtEpochS"] = _now_epoch()
        self._save_clock(clock)
        deadline = float(clock["deadlineEpochS"])
        try:
            stop: _Stop | None = None

            if not self._has_ok_step(state, StepKind.PLAN):
                stop = self._phase_confirm_goal(state)
            if stop is None and not self._has_ok_step(state, StepKind.TOOL_CALL):
                stop = self._phase_gather_context(state)
            if stop is None and not self._has_ok_step(state, StepKind.GATE_CHECK):
                stop = self._phase_gate_check(state)

            while stop is None:
                if self._pause_requested(run_id):
                    return self._do_pause(state)
                if self._cancel_requested(run_id):
                    stop = _Stop(_StopReason.CANCELLED)
                    break
                if self._consumed_work_steps(state) >= state.max_steps:
                    stop = _Stop(_StopReason.MAX_STEPS)
                    break
                if _now_epoch() >= deadline:
                    stop = _Stop(_StopReason.WALL_CLOCK)
                    break
                if state.conservative_spent_usd() >= state.budget_usd - _EPS:
                    self._journal_event(
                        state,
                        EventKind.BUDGET_EXHAUSTED,
                        {
                            "budgetUsd": state.budget_usd,
                            "measuredUsd": state.measured_usd,
                            "assumedUnmeasuredUsd": state.assumed_unmeasured_usd,
                        },
                    )
                    stop = _Stop(_StopReason.BUDGET_EXHAUSTED)
                    break
                if session is None:
                    budget = self._query_budget(state)
                    session = self._runtime.open_session(
                        dataclasses.replace(
                            self._session_config(state, deadline), query_budget=budget
                        )
                    )
                    self._register_session(run_id, session)
                assert budget is not None  # created with the session, never before
                stop = self._phase_model_call(state, session, budget)
                if stop is not None:
                    break
                if self._cancel_requested(run_id):
                    # Cancellation is authoritative: a request recorded while
                    # the step was in flight wins over "conditions met".
                    stop = _Stop(_StopReason.CANCELLED)
                    break
                evaluation = self._evaluate(state)
                if evaluation.all_passed():
                    stop = _Stop(_StopReason.CONDITIONS_MET)
                elif evaluation.machine_checks_passed():
                    stop = _Stop(_StopReason.PENDING_ACCEPTANCE)
            assert stop is not None  # the loop only exits with a stop or a pause
            if stop.reason is not _StopReason.CANCELLED and self._cancel_requested(run_id):
                stop = _Stop(_StopReason.CANCELLED)  # authoritative at finalize too
            return self._finalize(state, stop, session)
        finally:
            self._unregister_session(run_id)
            if session is not None:
                with contextlib.suppress(Exception):  # close must not mask results
                    session.close()
            self._active.discard(run_id)
            with contextlib.suppress(VouchError):
                self._accumulate_elapsed(state)

    # --- prompt external stop (cancel from another client, M4 A1 §5) ----------

    def _register_session(self, run_id: str, session: RuntimeSession) -> None:
        with self._session_lock:
            self._active_sessions[run_id] = session

    def _unregister_session(self, run_id: str) -> None:
        with self._session_lock:
            self._active_sessions.pop(run_id, None)

    def stop_active_session(self, run_id: str, reason: str = "") -> bool:
        """Stop the run's in-flight worker session (owned process group).

        Called from OUTSIDE the supervisor's thread (a worker's cancel
        poller): the durable cancel request stands, and this accelerates it
        past the blocking model call by cancelling and closing the session —
        which terminates its guarded worker's whole process group within the
        bounded group-stop grace (TERM -> 2s -> KILL). The loop then
        finalizes CANCELLED at its boundary with reservations reconciled.
        Returns whether an in-flight session was found.
        """
        with self._session_lock:
            session = self._active_sessions.get(run_id)
        if session is None:
            return False
        cancel = getattr(session, "cancel", None)
        if callable(cancel):
            with contextlib.suppress(Exception):
                cancel(reason or "cancelled by operator request")
        with contextlib.suppress(Exception):
            session.close()
        return True

    # --- native deterministic operations (M4 A1 test-gap repair) ---------------

    def execute_native_operation(
        self, run_id: str, compute: Callable[[], NativeOperationResult]
    ) -> TaskRun:
        """Claim a submitted run and execute a trusted DETERMINISTIC operation.

        The same ownership lease, persisted steps, checkpoints, pause/cancel
        request semantics and recoverable finalization protocol as the model
        path — but the work is ``compute`` (trusted controller code returning
        a :class:`NativeOperationResult`), never model output. Used by the
        shared worker to run sealed operations (e.g. CSV reconciliation)
        AFTER durable submission, so a detached worker genuinely executes the
        operation instead of reading an already-completed result.
        """
        self._guard_not_active(run_id)
        pending = self._pending_finalization(run_id)
        if pending is not None:
            return self._finish_finalization(pending)
        self._acquire_ownership(run_id, purpose="execute-native-operation")
        state = self._load_state(run_id)
        self._prepare_start(state)
        self._ensure_clock(state)
        run_id = state.run.run_id
        self._active.add(run_id)
        try:
            if self._pause_requested(run_id):
                return self._do_pause(state)
            if self._cancel_requested(run_id):
                return self._finalize(state, _Stop(_StopReason.CANCELLED), None)
            step = self._begin_step(
                state, StepKind.TOOL_CALL, input_digest=state.run.task_digest
            )
            try:
                outcome = compute()
            except Exception as exc:
                self._end_step(
                    state, step, status=STEP_STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"
                )
                return self._finalize(
                    state, _Stop(_StopReason.PREPARE_FAILED, error=str(exc)), None
                )
            digest = self._artifacts.put(outcome.payload)
            for key, artifact_digest in (outcome.artifacts or {}).items():
                state.artifact_keys[key] = artifact_digest
            state.artifact_keys["final"] = digest
            state.native_not_done = tuple(outcome.not_done or ())
            self._end_step(
                state,
                step,
                status=STEP_STATUS_OK,
                output_digest=digest,
                usage=dict(outcome.usage or {}),
            )
            if self._cancel_requested(run_id):
                # Cancellation is authoritative even over a produced artifact.
                return self._finalize(state, _Stop(_StopReason.CANCELLED), None)
            evaluation = self._evaluate(state)
            stop = (
                _Stop(_StopReason.CONDITIONS_MET)
                if evaluation.all_passed()
                else _Stop(_StopReason.OPERATION_INCOMPLETE)
            )
            return self._finalize(state, stop, None)
        finally:
            self._active.discard(run_id)
            with contextlib.suppress(VouchError):
                self._accumulate_elapsed(state)

    def _accumulate_elapsed(self, state: _RunState) -> None:
        clock = self._store.load(KIND_RUN_CLOCK, state.run.run_id)
        if clock is None:
            return
        resumed = float(clock.get("lastResumedAtEpochS", 0.0) or 0.0)
        if resumed <= 0:
            return
        clock["accumulatedElapsedS"] = round(
            float(clock.get("accumulatedElapsedS", 0.0)) + max(0.0, _now_epoch() - resumed), 3
        )
        clock["lastResumedAtEpochS"] = 0.0
        self._save_clock(clock)

    def _pause_requested(self, run_id: str) -> bool:
        return self._request_active(KIND_PAUSE_REQUEST, run_id)

    def _cancel_requested(self, run_id: str) -> bool:
        return self._request_active(KIND_CANCEL_REQUEST, run_id)

    def _request_active(self, kind: str, run_id: str) -> bool:
        record = self._store.load(kind, run_id)
        return record is not None and "consumedAt" not in record

    def _consume_request(self, kind: str, run_id: str) -> None:
        record = self._store.load(kind, run_id)
        if record is not None:
            record["consumedAt"] = utc_now_iso()
            self._store.save(kind, run_id, record)

    def _phase_confirm_goal(self, state: _RunState) -> _Stop | None:
        """Deterministic: re-confirm goal, constraints and the open reservation."""
        step = self._begin_step(state, StepKind.PLAN, input_digest=state.run.task_digest)
        if state.reservation.status is not ReservationStatus.OPEN:
            error = f"run budget reservation is {state.reservation.status.value}"
            self._end_step(state, step, status=STEP_STATUS_FAILED, error=error)
            return _Stop(_StopReason.PREPARE_FAILED, error=error)
        summary = {
            "goal": state.spec.goal,
            "mode": state.spec.mode.value,
            "budgetUsd": state.budget_usd,
            "maxSteps": state.max_steps,
            "wallClockS": state.wall_clock_s,
            "conditionCount": len(state.conditions),
            "locale": state.spec.locale,
        }
        self._end_step(
            state,
            step,
            status=STEP_STATUS_OK,
            output_digest=digest_of(summary),
            usage={"conditionCount": len(state.conditions)},
        )
        if not state.conditions:
            return _Stop(_StopReason.NO_CRITERIA)
        return None

    def _phase_gather_context(self, state: _RunState) -> _Stop | None:
        """Materialize TaskSpec.inputs into a content-addressed artifact.

        Deterministic and idempotent (content addressing), so a crash here is
        safely redoable — but it is still persisted as a TOOL_CALL step with
        intent-before-work, and recovery treats an in-flight occurrence
        conservatively.
        """
        step = self._begin_step(
            state,
            StepKind.TOOL_CALL,
            input_digest=digest_of({"inputs": state.spec.inputs}),
        )
        payload = canonical_json(state.spec.inputs).encode("utf-8")
        digest = self._artifacts.put(payload)
        state.artifact_keys["inputs"] = digest
        self._end_step(
            state,
            step,
            status=STEP_STATUS_OK,
            output_digest=digest,
            usage={"inputKeys": sorted(state.spec.inputs)},
        )
        return None

    def _phase_gate_check(self, state: _RunState) -> _Stop | None:
        """Trusted pre-work gate: mode policy, backend identity, budget left."""
        backend_id = self._runtime.backend_id()
        step = self._begin_step(
            state, StepKind.GATE_CHECK, input_digest=digest_of({"backend": backend_id})
        )
        if not backend_id:
            error = "runtime backend id is empty"
            self._end_step(state, step, status=STEP_STATUS_FAILED, error=error)
            return _Stop(_StopReason.PREPARE_FAILED, error=error)
        usage = {
            "mode": state.spec.mode.value,
            "allowsLiveCalls": state.spec.mode.allows_live_calls(),
            "allowsSideEffects": state.spec.mode.allows_side_effects(),
            "backendId": backend_id,
            "remainingBudgetUsd": round(state.budget_usd - state.conservative_spent_usd(), 6),
        }
        self._end_step(
            state,
            step,
            status=STEP_STATUS_OK,
            output_digest=digest_of(usage),
            usage=usage,
        )
        return None

    # --- model steps (review A2: reserve before every underlying query) -----------

    def _query_budget(self, state: _RunState) -> _StepQueryBudget:
        return _StepQueryBudget(
            self._ledger,
            state.reservation.reservation_id,
            self._journal,
            run_id=state.run.run_id,
            mode=state.run.mode,
        )

    def _session_usage(self, session: RuntimeSession | None) -> dict[str, Any] | None:
        if session is None:
            return None
        try:
            return dict(session.usage())
        except Exception:  # pragma: no cover - metering must never crash accounting
            return None

    @staticmethod
    def _step_query_delta(
        result: ModelCallResult,
        usage_before: dict[str, Any] | None,
        usage_after: dict[str, Any] | None,
    ) -> int | None:
        """Underlying queries THIS step consumed (nested invokes included).

        Prefers the runtime-reported per-step delta (``raw["llm_calls"]`` — a
        port contract both real runtimes honor); falls back to the difference
        of the two session snapshots. ``None`` when no reliable count exists.
        """
        raw_calls = (result.raw or {}).get("llm_calls") if getattr(result, "raw", None) else None
        if isinstance(raw_calls, int) and not isinstance(raw_calls, bool) and raw_calls >= 0:
            return raw_calls
        before = (usage_before or {}).get("llm_calls")
        after = (usage_after or {}).get("llm_calls")
        if (
            isinstance(before, int)
            and not isinstance(before, bool)
            and isinstance(after, int)
            and not isinstance(after, bool)
        ):
            return max(0, after - before)
        return None

    def _advance_query_cursor(self, state: _RunState, calls_delta: int | None) -> int | None:
        """Advance the durable MONOTONIC run-global query cursor (M4 A1 §2).

        The cursor is an ABSOLUTE number maintained incrementally: each
        completed model step adds exactly its own delta once, so worker
        restarts between steps and nested-query steps all advance it
        correctly, and nothing downstream ever reconstructs it by summing
        per-step counters. The record carries the execution-config identity
        it is valid for. Returns the new cursor (None when the step carried
        no reliable count and no record exists yet).
        """
        run_id = state.run.run_id
        record = self._store.load(KIND_RUN_QUERY_CURSOR, run_id)
        current = int(record["cursor"]) if record and record.get("cursor") is not None else 0
        if calls_delta is None:
            if record is None:
                return None
            return current
        new_cursor = current + int(calls_delta)
        if record is None or new_cursor > current:
            self._store.save(
                KIND_RUN_QUERY_CURSOR,
                run_id,
                {
                    "runId": run_id,
                    "cursor": new_cursor,
                    "configIdentity": self._config_identity,
                    "modelOrdinal": state.model_ordinal,
                    "updatedAt": utc_now_iso(),
                },
            )
        return new_cursor

    def _phase_model_call(
        self, state: _RunState, session: RuntimeSession, budget: _StepQueryBudget
    ) -> _Stop | None:
        """One bounded model step: slice budget, persist intent, call, settle."""
        n = state.model_ordinal
        remaining = state.budget_usd - state.conservative_spent_usd()
        slice_amount = round(min(self._policy.step_budget_usd, remaining), 6)
        if slice_amount <= _EPS:
            self._journal_event(
                state,
                EventKind.BUDGET_EXHAUSTED,
                {"budgetUsd": state.budget_usd, "atModelStep": n},
            )
            return _Stop(_StopReason.BUDGET_EXHAUSTED)

        # Budget slice reservation, persisted before the call (record id is
        # the holder string, so recovery can find it without extra indexes).
        slice_holder = f"{state.run.run_id}#model-{n}"
        slice_reservation = BudgetReservation(
            reservation_id=new_id("rsv"), holder=slice_holder, amount_usd=slice_amount
        )
        self._store.save(KIND_BUDGET_SLICE, slice_holder, slice_reservation.to_dict())
        self._journal_event(
            state,
            EventKind.BUDGET_RESERVED,
            {
                "level": "step",
                "holder": slice_holder,
                "reservationId": slice_reservation.reservation_id,
                "amountUsd": slice_amount,
                "modelStep": n,
            },
        )

        invocation = self._root_invocation(state).child(
            new_invocation_id(), capabilities=self._policy.model_step_capabilities
        )
        invocation = replace(
            invocation,
            input_refs=tuple(d for d in (state.artifact_keys.get("inputs"),) if d is not None),
            output_schema=self._model_output_schema(state),
            budget_reservation_id=slice_reservation.reservation_id,
            status=InvocationStatus.RUNNING,
            started_at=utc_now_iso(),
        )
        self._store.save(KIND_INVOCATION, invocation.invocation_id, invocation.to_dict())

        instruction = self._instruction(state, n)
        step = self._begin_step(
            state,
            StepKind.MODEL_CALL,
            input_digest=digest_of(
                {
                    "modelStep": n,
                    "instruction": instruction,
                    "context": state.artifact_keys.get("inputs"),
                    "priorArtifacts": list(state.artifact_keys.values()),
                }
            ),
            slice_reservation_id=slice_reservation.reservation_id,
        )

        # Per-query reservation authority: every underlying query of this
        # step reserves a child of the run reservation BEFORE it runs.
        budget.begin_step(slice_holder, slice_amount)
        usage_before = self._session_usage(session)
        # Scoped input path (review Stage C): the model step receives the
        # VERIFIED input bytes (digest-checked on read) as in-scope data —
        # not opaque digests. Canned outputs cannot fake data dependence.
        # Later steps additionally receive earlier steps' VERIFIED artifacts
        # as scoped context (M3 Gate A2): prior work is consumable as real
        # content across steps AND across worker/session restarts, where the
        # child's heap/namespace is deliberately NOT reconstructible.
        materials = self._materials(state)
        prior_artifacts = self._prior_artifacts(state)
        try:
            result = session.step(
                instruction, scope={"materials": materials, "prior_artifacts": prior_artifacts}
            )
        except VouchError as exc:
            # Known protocol failure — but never an excuse to book zero: the
            # measured usage that survived the failure is recovered from the
            # error itself (worker processes attach it) or the session's
            # metering snapshots.
            carried = usage_of(exc)
            usage_after = carried if carried is not None else self._session_usage(session)
            measured, assumed, unmeasurable = self._close_step_budget(
                state,
                budget,
                slice_reservation,
                slice_amount,
                reported_cost=None,
                usage_before=usage_before,
                usage_after=usage_after,
                detail=f"model step {n}: failed with {exc.code}",
            )
            if isinstance(exc, BudgetError):
                # The controller's per-query allocation refused a call: the
                # refusal is a budget stop, journalled as one.
                self._journal_event(
                    state,
                    EventKind.BUDGET_EXHAUSTED,
                    {
                        "budgetUsd": state.budget_usd,
                        "measuredUsd": round(state.measured_usd, 6),
                        "assumedUnmeasuredUsd": round(state.assumed_unmeasured_usd, 6),
                        "reason": f"query refused before it ran: {exc.code}",
                    },
                )
            self._end_step(
                state,
                step,
                status=STEP_STATUS_FAILED,
                error=f"{exc.code}: {exc}",
                usage={
                    "failedCostUsd": (None if unmeasurable else measured),
                    "assumedUnmeasuredUsd": assumed,
                },
                invocation=replace(
                    invocation,
                    status=InvocationStatus.FAILED,
                    ended_at=utc_now_iso(),
                    error=str(exc),
                ),
            )
            return _Stop(_StopReason.MODEL_FAILED, error=f"{exc.code}: {exc}")
        except Exception as exc:
            # Unexpected failure during the side-effecting call: the outcome is
            # UNKNOWN. Mark the step, settle everything it reserved
            # conservatively, and require reconciliation — never replayed.
            self._close_step_budget(
                state,
                budget,
                slice_reservation,
                slice_amount,
                reported_cost=None,
                usage_before=usage_before,
                usage_after=None,  # metering is unusable after a crash
                detail=f"model step {n}: outcome unknown after crash",
                force_conservative=True,
            )
            state.model_ordinal += 1  # consumed: never replayed
            self._end_step(
                state,
                step,
                status=STEP_STATUS_UNKNOWN,
                error=f"outcome unknown: {type(exc).__name__}: {exc}",
                side_effect_unknown=True,
                invocation=replace(
                    invocation,
                    status=InvocationStatus.FAILED,
                    ended_at=utc_now_iso(),
                    error=f"side-effect outcome unknown: {exc}",
                ),
            )
            self._journal_event(
                state,
                EventKind.RUN_RECONCILIATION,
                {
                    "action": "marked-unknown",
                    "stepId": step.step_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return _Stop(
                _StopReason.NEEDS_RECONCILIATION,
                error=f"{type(exc).__name__}: {exc}",
            )

        usage_after = self._session_usage(session)
        measured, assumed, unmeasurable = self._close_step_budget(
            state,
            budget,
            slice_reservation,
            slice_amount,
            reported_cost=result.cost_usd,
            usage_before=usage_before,
            usage_after=usage_after,
            detail=f"model step {n} ({step.step_id})",
        )
        # The step artifact is the EVALUATED result when the runtime provides
        # one (real engines return model-authored code as `content`; the
        # JSON-safe evaluated value rides in raw["return_value"]). Trusted
        # serialization here — the model never certifies its own artifact.
        return_value = result.raw.get("return_value") if result.raw else None
        if return_value is not None:
            import json as _json

            payload = _json.dumps(return_value, ensure_ascii=False, sort_keys=True)
        else:
            payload = result.content
        digest = self._artifacts.put(payload.encode("utf-8"))
        state.artifact_keys[f"step:{n}"] = digest
        state.artifact_keys["final"] = digest
        if n == 1:
            state.artifact_keys["draft"] = digest
        calls_delta = self._step_query_delta(result, usage_before, usage_after)
        query_cursor = self._advance_query_cursor(state, calls_delta)
        usage = {
            "promptTokens": result.prompt_tokens,
            "completionTokens": result.completion_tokens,
            "costUsd": result.cost_usd,
            "measuredUsd": measured,
            "assumedUnmeasuredUsd": assumed,
            "modelId": result.model_id,
            "backendId": self._runtime.backend_id(),
            # PER-STEP underlying query count (Gate A2 / M4 A1 §2): the delta
            # this one step consumed (nested invokes included) — NOT a
            # cumulative session counter mislabeled as a step value.
            "llmCalls": calls_delta,
            # Session-cumulative count for evidence, kept separate from the
            # delta; resume must never re-derive the cursor by summing it.
            "sessionLlmCalls": (
                int(usage_after["llm_calls"])
                if isinstance(usage_after, dict) and isinstance(usage_after.get("llm_calls"), int)
                else None
            ),
            # Run-global MONOTONIC cursor AFTER this step (absolute).
            "queryCursor": query_cursor,
        }
        self._end_step(
            state,
            step,
            status=STEP_STATUS_OK,
            output_digest=digest,
            usage=usage,
            invocation=replace(
                invocation,
                status=InvocationStatus.COMPLETED,
                ended_at=utc_now_iso(),
                result_digest=digest,
            ),
        )
        state.model_ordinal += 1
        return None

    def _close_step_budget(
        self,
        state: _RunState,
        budget: _StepQueryBudget,
        slice_reservation: BudgetReservation,
        slice_amount: float,
        *,
        reported_cost: float | None,
        usage_before: dict[str, Any] | None,
        usage_after: dict[str, Any] | None,
        detail: str,
        force_conservative: bool = False,
    ) -> tuple[float, float, bool]:
        """Settle one step's slice from what its query children booked.

        The ledger children are the source of truth for money; the measured
        usage delta is a cross-check that can only *add* honesty: spend a
        runtime reports beyond its reservations is booked (as an explicit
        overage at run close, never clipped), and a step that reserved
        nothing but cannot show a measured zero settles conservatively at the
        full slice. Returns ``(measured, assumed, unmeasurable)``.
        """
        refused = budget.refused_settlements()
        for reservation_id in budget.end_step():
            if reservation_id in refused:
                # Keep it OPEN with its real cost on record: an overrun the
                # parent cannot absorb is reconciled, never clipped.
                continue
            budget.adopt_orphan(reservation_id)
        if refused:
            state.overrun_reservations.update(refused)
        measured = budget.step_measured()
        assumed = budget.step_assumed()
        cost, unmeasured = self._step_cost(reported_cost, usage_before, usage_after)
        childless_assumed = 0.0
        childless_measured = 0.0
        if cost is not None and not unmeasured and float(cost) > measured + _EPS:
            # Measured spend no child reservation covers (a runtime reporting
            # beyond what it reserved). Booked in full — never clipped — and
            # flagged: spend the supervisor cannot reserve is a port-contract
            # violation the run must not certify away.
            gap = round(float(cost) - measured, 6)
            measured += gap
            childless_measured = gap
            state.unreserved_spend_usd += gap
            self._journal_event(
                state,
                EventKind.AUDIT_NOTE,
                {
                    "action": "spend-without-query-reservation",
                    "usd": gap,
                    "backendId": self._runtime.backend_id(),
                },
            )
            # Book it as a cost line too, or a restart would forget money the
            # runtime already reported (the journal is the rebuild source).
            self._append_cost(
                state,
                amount_usd=gap,
                measurable=True,
                note=f"measured spend with no query reservation (${gap:.6f})",
            )
        if measured <= _EPS and assumed <= _EPS and (unmeasured or cost is None):
            # Nothing reserved and nothing measurable: either the runtime ran
            # no query at all, or it spent money its reservations never saw.
            # The conservative reading is the full slice.
            assumed = slice_amount
            childless_assumed = slice_amount
        if force_conservative and assumed <= _EPS and measured <= _EPS:
            assumed = slice_amount
            childless_assumed = slice_amount
        state.measured_usd += measured
        state.assumed_unmeasured_usd += assumed
        state.childless_assumed_usd += childless_assumed
        state.childless_measured_usd += childless_measured
        unmeasurable = assumed > _EPS
        if unmeasurable:
            state.unmeasured_cost = True
        self._settle_step_slice(
            state,
            slice_reservation.to_dict(),
            round(measured + assumed, 6),
            unmeasurable=unmeasurable,
            detail=detail,
            assumed_usd=assumed,
            childless_assumed=childless_assumed,
            childless_measured=childless_measured,
        )
        return measured, assumed, unmeasurable

    def _step_cost(
        self,
        reported: float | None,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> tuple[float | None, bool]:
        """(measured step cost, unmeasured?) from the result and/or snapshots.

        Prefers the measured delta between two session snapshots — it is the
        only number that stays correct when a step aggregates several
        underlying queries and it is available on failure paths too, where
        there is no ``ModelCallResult`` at all.
        """
        before_cost, before_unknown = _usage_cost(before)
        after_cost, after_unknown = _usage_cost(after)
        if before is not None and after is not None and not before_unknown:
            if after_unknown or after_cost is None or before_cost is None:
                return None, True
            return round(after_cost - before_cost, 6), False
        if reported is None:
            return None, True
        return float(reported), False

    # --- finalize (review A3: recoverable terminal writes) -------------------------

    def _finalize(self, state: _RunState, stop: _Stop, session: RuntimeSession | None) -> TaskRun:
        """Check conditions with trusted code, build + persist the package.

        Terminal writes run as an ordered, idempotent protocol behind a
        write-ahead intent record: result package -> ledger settlement ->
        metadata/journal finalization -> ownership release. A crash between
        them is finished exactly once by the next client (see
        ``_finish_finalization``); terminal status alone never proves the
        bookkeeping completed.
        """
        session_usage: dict[str, Any] = {}
        if session is not None:
            try:
                session_usage = dict(session.usage())
            except Exception:  # pragma: no cover - metering must not crash finalize
                session_usage = {}

        # Conservative bookkeeping first: a query reservation a crashed step
        # left open (from an earlier process) is settled at its full amount.
        self._adopt_open_children(state)
        # ... and anything still open after that is a refused overrun.
        for child in self._ledger.children(state.reservation.reservation_id):
            if child.status is ReservationStatus.OPEN:
                state.overrun_reservations.setdefault(
                    child.reservation_id, child.settled_amount_usd or child.amount_usd
                )

        overrun = state.overrun_usd()
        step = self._begin_step(
            state,
            StepKind.FINALIZE,
            input_digest=digest_of({"stopReason": stop.reason.value, "error": stop.error}),
        )
        evaluation = self._evaluate(state)
        package = self._build_package(state, stop, evaluation)
        if state.overrun_reservations:
            # A provider cost the run's reservation could not absorb stays
            # open for reconciliation: never clipped, never certified.
            package = replace(
                package,
                uncertainties=(
                    *package.uncertainties,
                    "at least one model query cost more than its reservation could "
                    f"absorb ({sorted(state.overrun_reservations)}); the overrun is "
                    "open pending reconciliation",
                ),
            )
            stop = _Stop(
                _StopReason.NEEDS_RECONCILIATION,
                error="budget overrun: a query exceeded its declared bound",
            )
        if stop.reason is _StopReason.NEEDS_RECONCILIATION:
            target = TaskStatus.NEEDS_RECONCILIATION
        elif stop.reason is _StopReason.CANCELLED:
            target = TaskStatus.CANCELLED
        elif stop.reason is _StopReason.CONDITIONS_MET and package.deliverable():
            target = TaskStatus.COMPLETED
        else:
            target = TaskStatus.FAILED
        if state.unreserved_spend_usd > _EPS and target is TaskStatus.COMPLETED:
            # Spend the runtime reported outside its reservations cannot be
            # certified as a budget-conformant completion either.
            target = TaskStatus.FAILED
            note = (
                f"runtime reported ${state.unreserved_spend_usd:.6f} of spend no "
                "per-query reservation covers; the budget port contract was violated"
            )
            package = replace(
                package,
                not_done_items=(*package.not_done_items, note),
                uncertainties=(*package.uncertainties, note),
            )
        if overrun > _EPS and target is TaskStatus.COMPLETED:
            # A run that overran its budget is never certified as a
            # budget-conformant completion: the overrun stays visible in the
            # package, the run error and the ledger.
            target = TaskStatus.FAILED
            overrun_note = (
                f"budget overrun: conservative spend ${state.conservative_spent_usd():.6f} "
                f"exceeds the ${state.budget_usd:.6f} task budget by ${overrun:.6f}"
            )
            package = replace(
                package,
                not_done_items=(*package.not_done_items, overrun_note),
                uncertainties=(
                    *package.uncertainties,
                    "budget overrun: completion cannot be certified as budget-conformant",
                ),
            )
        self._end_step(
            state,
            step,
            status=STEP_STATUS_OK,
            output_digest=package.digest(),
            usage={
                "stopReason": stop.reason.value,
                "session": session_usage,
                "measuredUsd": round(state.measured_usd, 6),
                "assumedUnmeasuredUsd": round(state.assumed_unmeasured_usd, 6),
                "overrunUsd": round(max(0.0, overrun), 6),
            },
        )

        conservative = round(state.conservative_spent_usd(), 6)
        intent = FinalizationIntent(
            run_id=state.run.run_id,
            target_status=target.value,
            result_digest=package.digest(),
            package=package.to_dict(),
            conservative_usd=conservative,
            childless_overage_usd=round(
                state.childless_assumed_usd + state.childless_measured_usd, 6
            ),
            reservation_id=state.reservation.reservation_id,
            ledger_action=("keep-open" if target is TaskStatus.NEEDS_RECONCILIATION else "close"),
            unmeasured=state.unmeasured_cost,
        )
        return self._run_finalization_protocol(state, intent)

    def _run_finalization_protocol(self, state: _RunState, intent: FinalizationIntent) -> TaskRun:
        """Durably record the intent, then perform each terminal write once."""
        self._write_intent(intent)
        return self._apply_finalization_writes(state, intent)

    def _write_intent(self, intent: FinalizationIntent) -> None:
        self._store.save(KIND_RUN_FINALIZATION, intent.run_id, intent.to_dict())

    def _apply_finalization_writes(self, state: _RunState, intent: FinalizationIntent) -> TaskRun:
        current = intent
        if not current.package_saved:
            self._store.save(KIND_RESULT_PACKAGE, current.run_id, current.package)
            current = current.with_flag("packageSaved")
            self._write_intent(current)
        if not current.ledger_settled:
            self._settle_run_ledger(state, current)
            current = current.with_flag("ledgerSettled")
            self._write_intent(current)
        if not current.metadata_finalized:
            self._finalize_metadata(state, current)
            current = current.with_flag("metadataFinalized")
            self._write_intent(current)
        if not current.ownership_released:
            self._release_ownership(current.run_id)
            current = current.with_flag("ownershipReleased")
            self._write_intent(current)
        self._consume_request(KIND_PAUSE_REQUEST, current.run_id)
        self._consume_request(KIND_CANCEL_REQUEST, current.run_id)
        return self.get_run(current.run_id) or state.run

    def _settle_run_ledger(self, state: _RunState, intent: FinalizationIntent) -> None:
        """Close (or deliberately keep open) the run's budget reservation."""
        if intent.ledger_action == "keep-open":
            # Reconciliation may resume: the reservation keeps counting.
            self._store.save(KIND_RUN_RESERVATION, intent.run_id, state.reservation.to_dict())
            return
        self._adopt_open_children(state)
        if state.reservation.status is not ReservationStatus.OPEN:
            self._journal_event(
                state,
                EventKind.AUDIT_NOTE,
                {
                    "action": "run-reservation-already-closed",
                    "status": state.reservation.status.value,
                    "intendedUsd": intent.conservative_usd,
                },
            )
        else:
            try:
                settled = self._ledger.settle_parent_from_children(
                    intent.reservation_id or state.reservation.reservation_id,
                    overage_usd=intent.childless_overage_usd,
                )
                self._store.save(KIND_RUN_RESERVATION, intent.run_id, settled.to_dict())
            except ReservationError:
                # Our own earlier attempt committed before the crash (the
                # write-ahead intent proves one was in progress and we hold
                # the ownership lease): exactly-once, not double settlement.
                self._journal_event(
                    state,
                    EventKind.AUDIT_NOTE,
                    {
                        "action": "run-settlement-already-committed",
                        "intendedUsd": intent.conservative_usd,
                    },
                )
        self._journal_event(
            state,
            EventKind.BUDGET_SETTLED,
            {
                "level": "run",
                "reservationId": intent.reservation_id,
                "actualUsd": intent.conservative_usd,
                "overageUsd": intent.childless_overage_usd,
                "unmeasured": intent.unmeasured,
            },
        )

    def _finalize_metadata(self, state: _RunState, intent: FinalizationIntent) -> None:
        """Terminal status transition + journal event + checkpoint.

        A persisted terminal status is never overwritten: if another owner
        already finalized (cancellation is authoritative), the run keeps the
        persisted decision and the discrepancy is journalled instead of the
        stale snapshot winning.
        """
        run_id = intent.run_id
        target = TaskStatus(intent.target_status)
        persisted = self._store.load(KIND_TASK_RUN, run_id)
        persisted_status = TaskStatus(str(persisted["status"])) if persisted is not None else None
        if persisted_status in _TERMINAL_STATUSES and persisted_status is not target:
            self._journal_event(
                state,
                EventKind.AUDIT_NOTE,
                {
                    "action": "terminal-status-not-overwritten",
                    "persistedStatus": persisted_status.value,
                    "intendedStatus": target.value,
                },
            )
        elif persisted_status is not target:
            current = state.run if persisted_status == state.run.status else self.get_run(run_id)
            if current is None:  # pragma: no cover - record existed a moment ago
                raise ContractError(f"run {run_id} vanished during finalization")
            if target in TASK_TRANSITIONS[current.status]:
                self._transition(
                    state,
                    target,
                    error=intent_error(intent),
                    result_digest=intent.result_digest,
                )
            else:
                self._journal_event(
                    state,
                    EventKind.AUDIT_NOTE,
                    {
                        "action": "terminal-transition-unavailable",
                        "fromStatus": current.status.value,
                        "intendedStatus": target.value,
                    },
                )
        if target is TaskStatus.COMPLETED:
            self._journal_event(
                state,
                EventKind.RUN_COMPLETED,
                {"resultDigest": intent.result_digest, "stopReason": "finalization"},
            )
        elif target is TaskStatus.CANCELLED:
            self._journal_event(
                state,
                EventKind.RUN_CANCELLED,
                {"resultDigest": intent.result_digest},
            )
        else:
            self._journal_event(
                state,
                EventKind.RUN_FAILED,
                {
                    "resultDigest": intent.result_digest,
                    "stopReason": "finalization",
                    "error": intent_error(intent),
                    "needsReconciliation": target is TaskStatus.NEEDS_RECONCILIATION,
                },
            )
        root_status = {
            TaskStatus.COMPLETED: InvocationStatus.COMPLETED,
            TaskStatus.CANCELLED: InvocationStatus.CANCELLED,
        }.get(target, InvocationStatus.FAILED)
        self._save_root_invocation(state, status=root_status, result_digest=intent.result_digest)
        self._checkpoint(state)

    def _adopt_open_children(self, state: _RunState) -> None:
        """Settle any still-open child reservation of the run conservatively.

        Covers a query whose process died between reserve and settle (also
        across a restart): the work may have happened, so the full
        reservation is assumed spent and recorded as unmeasurable.
        """
        try:
            children = self._ledger.children(state.reservation.reservation_id)
        except Exception:  # pragma: no cover - ledgers without children() keep working
            return
        for child in children:
            if child.status is not ReservationStatus.OPEN:
                continue
            if child.reservation_id in state.overrun_reservations:
                # A refused overrun is NOT absorbed: it stays open and visible
                # until a human reconciles it.
                continue
            self._ledger.settle(child.reservation_id, child.amount_usd)
            self._append_cost(
                state,
                amount_usd=None,
                measurable=False,
                note=(
                    f"query reservation {child.reservation_id} never settled "
                    "(owner died); assumed spent conservatively"
                ),
            )
            state.unmeasured_cost = True
            state.assumed_unmeasured_usd += child.amount_usd

    # --- crash recovery of terminal writes -------------------------------------------

    def _pending_finalization(self, run_id: str) -> FinalizationIntent | None:
        intent = read_finalization(self._store, run_id)
        if intent is None or intent.complete():
            return None
        return intent

    def _finish_finalization(self, intent: FinalizationIntent) -> TaskRun:
        """Finish a crashed run's terminal writes exactly once (no replay)."""
        run_id = intent.run_id
        # The previous owner is gone (its lease expired or it released); take
        # the lease so no third writer interleaves with the recovery.
        self._acquire_ownership(run_id, purpose="finish-finalization")
        state = self._load_state(run_id)
        self._journal_event(
            state,
            EventKind.AUDIT_NOTE,
            {
                "action": "finalization-recovered",
                "pendingWrites": list(intent.pending_writes()),
            },
        )
        return self._apply_finalization_writes(state, intent)

    def _do_pause(self, state: _RunState) -> TaskRun:
        """Pause at a step boundary: keep steps and reservation, no package."""
        self._consume_request(KIND_PAUSE_REQUEST, state.run.run_id)
        clock = self._store.load(KIND_RUN_CLOCK, state.run.run_id)
        if clock is not None:
            now = _now_epoch()
            clock["pausedAtEpochS"] = now
            clock["pauseExpiresAtEpochS"] = now + self._policy.paused_run_expiry_s
            self._save_clock(clock)
        self._transition(state, TaskStatus.PAUSED)
        self._journal_event(
            state,
            EventKind.RUN_PAUSED,
            {"action": "run-paused", "pauseExpiresAfterS": self._policy.paused_run_expiry_s},
        )
        self._checkpoint(state)
        self._release_ownership(state.run.run_id)
        return state.run

    def _build_package(
        self, state: _RunState, stop: _Stop, evaluation: ConditionEvaluation
    ) -> ResultPackage:
        checks = evaluation.checks
        done_items = [
            f"condition {r.key} passed ({r.detail})" for r in evaluation.results if r.passed
        ]
        done_items += [
            f"{s.kind.value} step {s.step_id} completed"
            for s in state.steps
            if s.status == STEP_STATUS_OK and s.kind is not StepKind.FINALIZE
        ]
        not_done: list[str] = [
            f"condition {r.key} NOT met: {r.detail}" for r in evaluation.results if not r.passed
        ]
        uncertainties: list[str] = []
        external_actions: dict[str, str] = {}
        for marker in evaluation.human_markers():
            external_actions[f"human-acceptance:{marker.key}"] = "pending"
            not_done.append(f"condition {marker.key}: delivered pending human acceptance")
        if state.unmeasured_cost:
            uncertainties.append(
                "at least one model call has unmeasured cost; total_cost_usd is "
                "unknown and the budget was settled conservatively"
            )
        unknown_steps = [s.step_id for s in state.steps if s.side_effect_unknown]
        if unknown_steps:
            uncertainties.append(f"step outcome(s) unknown pending reconciliation: {unknown_steps}")
        reason_notes: dict[_StopReason, str] = {
            _StopReason.MAX_STEPS: (
                f"stopped at the step bound ({state.max_steps} work steps) before "
                "completion conditions were met"
            ),
            _StopReason.WALL_CLOCK: (
                f"stopped at the wall-clock bound ({state.wall_clock_s}s) before "
                "completion conditions were met"
            ),
            _StopReason.BUDGET_EXHAUSTED: (
                f"stopped: budget exhausted (${state.budget_usd:.6f} reserved) "
                "before completion conditions were met"
            ),
            _StopReason.MODEL_FAILED: f"model step failed: {stop.error}",
            _StopReason.PREPARE_FAILED: f"preparation failed: {stop.error}",
            _StopReason.NO_CRITERIA: (
                "spec declares no success criteria; completion is not "
                "machine-checkable, so the run is not completed"
            ),
            _StopReason.PENDING_ACCEPTANCE: (
                "machine-checkable conditions met; delivery is incomplete until a "
                "human accepts the result"
            ),
            _StopReason.OPERATION_INCOMPLETE: (
                "the deterministic operation completed but its result did not satisfy "
                "the run's completion conditions"
            ),
            _StopReason.CANCELLED: "cancelled by request; partial steps kept",
            _StopReason.NEEDS_RECONCILIATION: (
                f"stopped with unknown side-effect outcome ({stop.error}); "
                "reconciliation required and the unknown step will not be replayed"
            ),
        }
        note = reason_notes.get(stop.reason)
        if note:
            not_done.append(note)
        not_done.extend(state.native_not_done)
        conclusions: dict[_StopReason, str] = {
            _StopReason.CONDITIONS_MET: (
                f"completed: {len(checks)}/{len(checks)} conditions checked true by trusted code"
            ),
            _StopReason.PENDING_ACCEPTANCE: (
                "work complete pending human acceptance; delivery is "
                "incomplete-but-delivered until accepted"
            ),
            _StopReason.CANCELLED: "cancelled before completion; partial steps kept",
            _StopReason.NEEDS_RECONCILIATION: (
                "stopped: side-effect outcome unknown; a reconciliation note is "
                "required before any replay"
            ),
        }
        conclusion = conclusions.get(stop.reason) or (
            f"stopped without meeting completion conditions: {stop.reason.value}"
        )
        return ResultPackage(
            run_id=state.run.run_id,
            conclusion=conclusion,
            artifact_refs=tuple(state.artifact_keys.values()),
            done_items=tuple(done_items),
            not_done_items=tuple(not_done),
            uncertainties=tuple(uncertainties),
            external_actions=external_actions,
            total_cost_usd=(None if state.unmeasured_cost else round(state.measured_usd, 6)),
            completed_conditions_check=checks,
        )

    # --- step persistence helpers ---------------------------------------------------

    def _begin_step(
        self,
        state: _RunState,
        kind: StepKind,
        *,
        input_digest: str | None,
        slice_reservation_id: str | None = None,
    ) -> RunStep:
        """Persist a step as in-flight BEFORE any side-effecting work."""
        step = RunStep(
            step_id=new_id("step"),
            kind=kind,
            status=STEP_STATUS_RUNNING,
            started_at=utc_now_iso(),
            input_digest=input_digest,
            usage=({"sliceReservationId": slice_reservation_id} if slice_reservation_id else {}),
        )
        state.steps.append(step)
        state.run = replace(state.run, steps=tuple(state.steps), updated_at=utc_now_iso())
        self._persist_run(state)
        self._checkpoint(state)
        return step

    def _end_step(
        self,
        state: _RunState,
        step: RunStep,
        *,
        status: str,
        error: str | None = None,
        output_digest: str | None = None,
        usage: dict[str, Any] | None = None,
        side_effect_unknown: bool = False,
        invocation: Invocation | None = None,
    ) -> None:
        """Persist the step's known outcome (and the invocation's, if given)."""
        index = next(i for i, s in enumerate(state.steps) if s.step_id == step.step_id)
        merged_usage: dict[str, Any] = dict(step.usage)
        merged_usage.update(usage or {})
        state.steps[index] = replace(
            step,
            status=status,
            ended_at=utc_now_iso(),
            output_digest=output_digest,
            usage=merged_usage,
            error=error,
            side_effect_unknown=side_effect_unknown,
        )
        state.run = replace(state.run, steps=tuple(state.steps), updated_at=utc_now_iso())
        self._persist_run(state)
        if invocation is not None:
            self._store.save(KIND_INVOCATION, invocation.invocation_id, invocation.to_dict())
        self._checkpoint(state)

    def _settle_step_slice(
        self,
        state: _RunState,
        slice_data: dict[str, Any],
        actual_usd: float,
        *,
        unmeasurable: bool,
        detail: str,
        assumed_usd: float | None = None,
        childless_assumed: float = 0.0,
        childless_measured: float = 0.0,
    ) -> None:
        """Settle one step budget slice (store record + journal event).

        The slice record mirrors what the step's query children already
        booked in the ledger; cost lines come from those children, so the
        journal, the ledger and this record cannot disagree. The ``childless``
        amounts are the parts of the step's cost no child reservation covers —
        they are carried as an explicit overage when the run reservation
        closes, never absorbed into the children's actuals.
        """
        reservation = BudgetReservation.from_dict(slice_data)
        settled = replace(
            reservation,
            status=ReservationStatus.SETTLED,
            settled_amount_usd=round(float(actual_usd), 6),
            closed_at=utc_now_iso(),
        )
        record = settled.to_dict()
        record["unmeasurable"] = unmeasurable
        if assumed_usd is None:
            assumed_usd = float(actual_usd) if unmeasurable else 0.0
        record["assumedUsd"] = round(float(assumed_usd), 6)
        record["childlessAssumedUsd"] = round(float(childless_assumed), 6)
        record["childlessMeasuredUsd"] = round(float(childless_measured), 6)
        # The record id is the holder string (see _phase_model_call).
        self._store.save(KIND_BUDGET_SLICE, settled.holder, record)
        self._journal_event(
            state,
            EventKind.BUDGET_SETTLED,
            {
                "level": "step",
                "holder": settled.holder,
                "reservationId": settled.reservation_id,
                "reservedUsd": settled.amount_usd,
                "actualUsd": settled.settled_amount_usd,
                "unmeasurable": unmeasurable,
            },
        )

    def _append_cost(
        self,
        state: _RunState,
        *,
        amount_usd: float | None,
        measurable: bool,
        note: str,
    ) -> None:
        self._journal.append_cost(
            CostEntry(
                entry_id=new_id("cost"),
                category=CostCategory.MODEL,
                subject=state.run.run_id,
                amount_usd=amount_usd,
                measurable=measurable,
                mode=state.run.mode,
                note=note,
            )
        )

    def _persist_run(self, state: _RunState) -> None:
        """Write the run snapshot behind revision fencing (review A3).

        A run record is only writable by the owner that read its current
        revision: a supervisor whose snapshot was superseded (lease expiry,
        takeover) is rejected here instead of overwriting the new owner's
        decision with a stale one.
        """
        with self._store.transaction():
            current = self._store.load(KIND_TASK_RUN, state.run.run_id)
            if current is not None and int(current.get("revision", 0) or 0) != state.revision:
                raise RunOwnershipError(
                    f"run {state.run.run_id} was written by another supervisor "
                    f"(persisted revision {current.get('revision')}, this snapshot "
                    f"{state.revision}); refusing to overwrite it with a stale snapshot"
                )
            state.revision += 1
            self._store.save(
                KIND_TASK_RUN,
                state.run.run_id,
                {**state.run.to_dict(), "revision": state.revision},
            )

    def _transition(
        self,
        state: _RunState,
        target: TaskStatus,
        *,
        error: str | None = None,
        result_digest: str | None = None,
    ) -> None:
        transition_task_status(state.run.status, target)
        state.run = replace(
            state.run,
            status=target,
            error=error if error is not None else state.run.error,
            result_digest=(result_digest if result_digest is not None else state.run.result_digest),
            updated_at=utc_now_iso(),
        )
        self._persist_run(state)

    def _journal_event(self, state: _RunState, kind: EventKind, data: dict[str, Any]) -> None:
        self._journal.append(
            EventRecord(
                event_id=new_id("evt"),
                kind=kind,
                subject=state.run.run_id,
                actor="supervisor",
                mode=state.run.mode,
                data=data,
            )
        )

    def _checkpoint(self, state: _RunState) -> None:
        state.sequence += 1
        write_checkpoint(
            self._store,
            self._journal,
            state.run,
            sequence=state.sequence,
            budget=self._budget_snapshot(state),
        )
        with contextlib.suppress(VouchError):
            self._heartbeat_ownership(state.run.run_id)

    def _budget_snapshot(self, state: _RunState) -> dict[str, Any]:
        return {
            "reservationId": state.reservation.reservation_id,
            "reservedUsd": state.reservation.amount_usd,
            "measuredUsd": round(state.measured_usd, 6),
            "assumedUnmeasuredUsd": round(state.assumed_unmeasured_usd, 6),
            "unmeasurable": state.unmeasured_cost,
        }

    def _save_root_invocation(
        self,
        state: _RunState,
        *,
        status: InvocationStatus,
        result_digest: str | None = None,
    ) -> None:
        invocation = self._root_invocation(state)
        finished = status in (
            InvocationStatus.COMPLETED,
            InvocationStatus.FAILED,
            InvocationStatus.CANCELLED,
        )
        updated = replace(
            invocation,
            status=status,
            started_at=invocation.started_at or utc_now_iso(),
            ended_at=utc_now_iso() if finished else invocation.ended_at,
            result_digest=result_digest or invocation.result_digest,
        )
        self._store.save(KIND_INVOCATION, updated.invocation_id, updated.to_dict())

    def _root_invocation(self, state: _RunState) -> Invocation:
        data = self._store.load(KIND_INVOCATION, state.root_invocation_id)
        if data is None:
            raise ContractError(
                f"root invocation {state.root_invocation_id} missing for run {state.run.run_id}"
            )
        return Invocation.from_dict(data)

    # --- evaluation + small helpers --------------------------------------------

    def _evaluate(self, state: _RunState) -> ConditionEvaluation:
        context = ConditionContext(
            artifacts=dict(state.artifact_keys),
            load_artifact=self._artifacts.get,
            measured_cost_usd=state.measured_usd,
            has_unmeasured_cost=state.unmeasured_cost,
            cost_bound_usd=state.spec.max_cost_usd,
        )
        return evaluate_conditions(state.conditions, context)

    def _model_output_schema(self, state: _RunState) -> dict[str, Any]:
        for condition in state.conditions:
            if (
                condition.get("type") == CONDITION_ARTIFACT_SCHEMA
                and condition.get("artifact") == "final"
            ):
                return dict(condition["schema"])
        return {}

    def _instruction(self, state: _RunState, model_step: int) -> str:
        return canonical_json(
            {
                "modelStep": model_step,
                "goal": state.spec.goal,
                "mode": state.spec.mode.value,
                "locale": state.spec.locale,
                "contextArtifact": state.artifact_keys.get("inputs"),
                "materialsInScope": sorted(self._materials(state).keys()),
                "priorArtifacts": list(state.artifact_keys.values()),
                "priorArtifactsInScope": sorted(self._prior_artifacts(state).keys()),
                "remainingBudgetUsd": round(state.budget_usd - state.conservative_spent_usd(), 6),
                "note": (
                    "produce the next output artifact; completion is judged only "
                    "by the trusted success_criteria, not by any claim of being done"
                ),
            }
        )

    def _materials(self, state: _RunState) -> dict[str, Any]:
        """Verified task materials for the model step's scope.

        The inputs artifact was sealed at submit; reading it back through the
        artifact store re-verifies its digest (tampered bytes raise here,
        before the step runs). JSON materials parse into objects; anything
        else is delivered as text so the step sees exactly what was supplied.
        """
        digest = state.artifact_keys.get("inputs")
        if digest is None:
            return {}
        payload = self._artifacts.get(digest)  # digest re-verified on read
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"text": payload.decode("utf-8", errors="replace")}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _prior_artifacts(self, state: _RunState) -> dict[str, Any]:
        """Earlier model steps' VERIFIED artifacts as scoped context (A2).

        Each earlier step's output artifact is read back through the digest-
        verifying artifact store, so a later step consumes real verified
        content — not an opaque digest. This is the part of session state
        that IS reconstructible after a worker/session restart; the child's
        heap and REPL namespace are not (and are never promised).
        """
        prior: dict[str, Any] = {}
        for ordinal in range(1, state.model_ordinal):
            digest = state.artifact_keys.get(f"step:{ordinal}")
            if digest is None:
                continue
            payload = self._artifacts.get(digest)  # digest re-verified on read
            try:
                prior[f"step-{ordinal}"] = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                prior[f"step-{ordinal}"] = payload.decode("utf-8", errors="replace")
        return prior

    def _session_config(self, state: _RunState, deadline: float) -> WorkerSessionConfig:
        remaining_steps = max(1, state.max_steps - self._consumed_work_steps(state))
        return WorkerSessionConfig(
            mode=state.spec.mode,
            max_steps=remaining_steps,
            wall_clock_s=max(0.1, deadline - _now_epoch()),
            max_cost_usd=round(max(0.0, state.budget_usd - state.conservative_spent_usd()), 6),
            allow_timeout_pragma=False,
        )

    @staticmethod
    def _has_ok_step(state: _RunState, kind: StepKind) -> bool:
        return any(s.kind is kind and s.status == STEP_STATUS_OK for s in state.steps)

    @staticmethod
    def _consumed_work_steps(state: _RunState) -> int:
        return sum(
            1
            for s in state.steps
            if s.kind in SIDE_EFFECTING_STEP_KINDS and s.status in _CONSUMED_WORK_STATUSES
        )

    # --- state load/rebuild -----------------------------------------------------

    def _load_state(self, run_id: str) -> _RunState:
        run_data = self._store.load(KIND_TASK_RUN, run_id)
        if run_data is None:
            raise ContractError(f"unknown run {run_id!r}")
        run = TaskRun.from_dict(run_data)
        index = self._store.load(KIND_RUN_INDEX, run_id)
        if index is None:
            raise ContractError(f"run {run_id} has no run-index record")
        spec_data = self._store.load(KIND_TASK_SPEC, index["specId"])
        if spec_data is None:
            raise ContractError(f"task spec {index['specId']!r} missing for run {run_id}")
        spec = TaskSpec.from_dict(spec_data)
        if spec.digest() != run.task_digest:
            raise DigestMismatchError(
                f"task spec digest {spec.digest()} does not match run digest {run.task_digest}"
            )
        reservation_data = self._store.load(KIND_RUN_RESERVATION, run_id)
        if reservation_data is None:
            raise ContractError(f"run {run_id} has no run-level budget reservation")
        reservation = BudgetReservation.from_dict(reservation_data)
        if reservation.reservation_id != index["reservationId"]:
            raise ContractError(f"run {run_id} reservation id mismatch")

        state = _RunState(
            run=run,
            spec=spec,
            conditions=normalize_criteria(spec.success_criteria),
            reservation=reservation,
            root_invocation_id=index["rootInvocationId"],
            policy_digest=index["policyDigest"],
            steps=list(run.steps),
            budget_usd=reservation.amount_usd,
            max_steps=(
                int(spec.max_steps)
                if spec.max_steps is not None
                else self._policy.default_max_steps
            ),
            wall_clock_s=(
                float(spec.max_wall_clock_s)
                if spec.max_wall_clock_s is not None
                else self._policy.default_wall_clock_s
            ),
            revision=int(run_data.get("revision", 0) or 0),
        )
        self._rebuild_accounting(state)
        self._rebuild_overruns(state)
        record = self._store.load(CHECKPOINT_KIND, run_id)
        state.sequence = int(record.get("sequence", 0)) if record else 0
        return state

    def _rebuild_overruns(self, state: _RunState) -> None:
        """Refused overruns survive restarts through their journal record.

        A query whose real cost its reservation could not absorb stays OPEN in
        the ledger; the durable audit note written at refusal time lets a
        restarted supervisor recognize it as an overrun to reconcile rather
        than an orphan to conservatively adopt (which would hide it).
        """
        for event in self._journal.events(state.run.run_id):
            if event.kind is not EventKind.AUDIT_NOTE:
                continue
            data = event.data or {}
            if data.get("action") != "settlement-refused-overrun":
                continue
            reservation_id = data.get("reservationId")
            if isinstance(reservation_id, str):
                state.overrun_reservations[reservation_id] = float(
                    data.get("reportedUsd", 0.0) or 0.0
                )

    def _rebuild_accounting(self, state: _RunState) -> None:
        """Rebuild cost accounting + artifact registry from persisted state."""
        entries = self._journal.cost_entries(state.run.run_id)
        state.measured_usd = sum(float(e.amount_usd) for e in entries if e.amount_usd is not None)
        state.unmeasured_cost = any(not e.measurable for e in entries)
        prefix = f"{state.run.run_id}#"
        assumed = 0.0
        childless_assumed = 0.0
        childless_measured = 0.0
        for record_id in self._store.list_ids(KIND_BUDGET_SLICE):
            if not record_id.startswith(prefix):
                continue
            slice_record = self._store.load(KIND_BUDGET_SLICE, record_id)
            if slice_record is None:
                continue
            slice_assumed = float(
                slice_record.get(
                    "assumedUsd",
                    slice_record.get("amountUsd", 0.0) if slice_record.get("unmeasurable") else 0.0,
                )
            )
            assumed += slice_assumed
            childless_assumed += float(slice_record.get("childlessAssumedUsd", slice_assumed))
            childless_measured += float(slice_record.get("childlessMeasuredUsd", 0.0))
        state.assumed_unmeasured_usd = assumed
        state.childless_assumed_usd = childless_assumed
        state.childless_measured_usd = childless_measured
        ordinal = 0
        for step in state.steps:
            if step.kind is StepKind.TOOL_CALL and step.status == STEP_STATUS_OK:
                if "operation" in step.usage:
                    # A native deterministic operation's step: its output is
                    # the run's FINAL artifact, not the inputs snapshot.
                    if step.output_digest:
                        state.artifact_keys["final"] = step.output_digest
                        state.artifact_keys.setdefault(
                            f"operation:{step.usage.get('operation')}", step.output_digest
                        )
                elif step.output_digest:
                    state.artifact_keys["inputs"] = step.output_digest
            elif step.kind is StepKind.MODEL_CALL:
                ordinal += 1
                if step.status == STEP_STATUS_OK and step.output_digest:
                    state.artifact_keys[f"step:{ordinal}"] = step.output_digest
                    state.artifact_keys["final"] = step.output_digest
                    if ordinal == 1:
                        state.artifact_keys["draft"] = step.output_digest
        state.model_ordinal = ordinal + 1


def intent_error(intent: FinalizationIntent) -> str | None:
    """The run error recorded in the finalization intent (idempotent W3)."""
    package = ResultPackage.from_dict(intent.package) if intent.package else None
    if package is not None and package.not_done_items:
        return package.not_done_items[-1]
    return None


__all__ = [
    "KIND_BUDGET_SLICE",
    "KIND_CANCEL_REQUEST",
    "KIND_INVOCATION",
    "KIND_PAUSE_REQUEST",
    "KIND_RECONCILIATION_NOTE",
    "KIND_RESULT_PACKAGE",
    "KIND_RUN_CLOCK",
    "KIND_RUN_FINALIZATION",
    "KIND_RUN_INDEX",
    "KIND_RUN_OWNERSHIP",
    "KIND_RUN_QUERY_CURSOR",
    "KIND_RUN_RESERVATION",
    "KIND_TASK_RUN",
    "KIND_TASK_SPEC",
    "Supervisor",
    "SupervisorPolicy",
]
