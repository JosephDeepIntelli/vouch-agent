"""In-test fakes for the orchestrator tests.

The supervisor must be exercised against the *ports* only, so these fakes
implement ``vouch_agent.runtime.ports`` and ``vouch_agent.storage.interfaces``
exactly:

* :class:`FakeRuntime` — scripted ``ModelCallResult`` stream consumed in
  order across sessions, with per-call failure injection (known Vouch
  errors vs. unexpected crashes raised *after* the simulated side effect),
  delays for wall-clock tests, a call log and side-effect counter for
  no-replay assertions, and replay exhaustion when the script runs dry.
* :class:`FakeMetadataStore` / :class:`FakeArtifactStore` /
  :class:`FakeBudgetLedger` / :class:`FakeJournal` — port-shaped in-memory
  stores. The metadata store can inject a hard persistence crash via a
  predicate, simulating a process dying between persist phases (the raised
  :class:`SimulatedCrash` must propagate out of the supervisor, leaving
  persisted state exactly as it was).

The real JAZ-backed runtime engine and SQLite stores are wired at lead
merge; nothing here proves them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

from vouch_agent.contracts.common import digest_bytes, new_id
from vouch_agent.contracts.journal import (
    BudgetReservation,
    CostEntry,
    EventKind,
    EventRecord,
    ReservationStatus,
)
from vouch_agent.errors import (
    BudgetExhaustedError,
    ContractError,
    ReservationError,
    VouchError,
)
from vouch_agent.runtime.ports import ModelCallResult, WorkerSessionConfig


class SimulatedCrash(Exception):
    """Stands in for the process dying at a persistence boundary.

    Deliberately NOT a VouchError: the supervisor must let it propagate
    (only its own side-effecting step calls are guarded), leaving whatever
    was last persisted as the recovery state.
    """


# --- fake runtime ---------------------------------------------------------------


@dataclass
class ScriptedCall:
    """One scripted model-call outcome (consumed in order, across sessions)."""

    content: str = '{"ok": true}'
    prompt_tokens: int = 120
    completion_tokens: int = 80
    cost_usd: float | None = 0.01
    model_id: str = "fake-model-1"
    #: known protocol failure raised instead of returning (e.g. LiveCallBlockedError)
    error: Exception | None = None
    #: unexpected failure raised AFTER the simulated side effect happened
    crash: Exception | None = None
    delay_s: float = 0.0
    #: whether this call performs a (simulated) external side effect
    side_effect: bool = True


class FakeSession:
    """RuntimeSession over the shared FakeRuntime script/log.

    Honors the port's query-budget contract (review A2) exactly like the real
    engine must: every underlying call reserves a controller budget BEFORE it
    runs, a dispatch-time protocol failure settles the measured cost (the
    call was made), and an empty script fails before any reservation exists.
    """

    def __init__(self, runtime: FakeRuntime, config: WorkerSessionConfig) -> None:
        self._runtime = runtime
        self._config = config
        self._id = new_id("sess")
        self._calls = 0
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._id

    @property
    def closed(self) -> bool:
        return self._closed

    def step(self, instruction: str, scope: dict[str, Any] | None = None) -> ModelCallResult:
        self._calls += 1
        budget = self._config.query_budget
        if budget is None or not self._runtime._script:
            return self._runtime._perform_call(self, instruction)
        logged_before = len(self._runtime.call_log)
        reservation = budget.reserve_query(None)  # the fake's price is unknown upfront
        try:
            result = self._runtime._perform_call(self, instruction)
        except VouchError:
            # A dispatch-time protocol failure: if the call was actually made,
            # its measured cost stands; a refusal raised before any model work
            # (an empty script, a blocked live call) releases the reservation.
            if len(self._runtime.call_log) > logged_before:
                budget.settle_query(reservation, self._runtime.call_log[-1]["costUsd"])
            else:
                budget.release_query(reservation)
            raise
        except BaseException:
            # A crash mid-call: the reservation stays open for the supervisor
            # to adopt conservatively — never settled to zero here.
            raise
        budget.settle_query(reservation, result.cost_usd)
        return result

    def structured_step(
        self, instruction: str, return_type: type, scope: dict[str, Any] | None = None
    ) -> Any:
        result = self.step(instruction, scope)
        import json

        value = json.loads(result.content)
        if not isinstance(value, return_type):
            raise ContractError(
                f"structured_step return {type(value).__name__} is not {return_type.__name__}"
            )
        return value

    def usage(self) -> dict[str, Any]:
        mine = [c for c in self._runtime.call_log if c["session"] == self._id]
        costs = [c["costUsd"] for c in mine]
        unmeasured = any(c is None for c in costs)
        return {
            "calls": len(mine),
            "promptTokens": sum(c["promptTokens"] for c in mine),
            "completionTokens": sum(c["completionTokens"] for c in mine),
            "costUsd": None if unmeasured else round(sum(c for c in costs if c), 6),
            "backendId": self._runtime.backend_id(),
        }

    def close(self) -> None:
        self._closed = True
        self._runtime.closed_sessions += 1


class FakeRuntime:
    """Runtime port fake: scripted responses, failure injection, call log."""

    def __init__(
        self,
        script: Sequence[ScriptedCall] | None = None,
        on_call: Callable[[int], None] | None = None,
    ) -> None:
        self._script: list[ScriptedCall] = list(script or [])
        self._on_call = on_call
        self.call_log: list[dict[str, Any]] = []
        self.side_effects = 0
        self.sessions: list[FakeSession] = []
        self.closed_sessions = 0

    def open_session(self, config: WorkerSessionConfig) -> FakeSession:
        session = FakeSession(self, config)
        self.sessions.append(session)
        return session

    def backend_id(self) -> str:
        return "fake-scripted@1"

    @property
    def calls(self) -> int:
        return len(self.call_log)

    def _perform_call(self, session: FakeSession, instruction: str) -> ModelCallResult:
        if not self._script:
            from vouch_agent.errors import ReplayExhaustedError

            raise ReplayExhaustedError(
                "fixture script exhausted; live fallback is disabled in fixture mode"
            )
        item = self._script.pop(0)
        if item.error is not None:
            # Pre-dispatch protocol failure (blocked live call, quota refusal):
            # nothing was asked of a model, so nothing is logged or charged.
            raise item.error
        index = len(self.call_log) + 1
        self.call_log.append(
            {
                "index": index,
                "instruction": instruction,
                "session": session.session_id,
                "promptTokens": item.prompt_tokens,
                "completionTokens": item.completion_tokens,
                "costUsd": item.cost_usd,
                "scriptContent": item.content,
            }
        )
        if item.delay_s > 0:
            time.sleep(item.delay_s)
        if item.side_effect:
            self.side_effects += 1
        if self._on_call is not None:
            self._on_call(index)
        if item.crash is not None:
            raise item.crash
        return ModelCallResult(
            content=item.content,
            prompt_tokens=item.prompt_tokens,
            completion_tokens=item.completion_tokens,
            cost_usd=item.cost_usd,
            model_id=item.model_id,
            raw={"scriptIndex": index},
        )


# --- fake storage ports -----------------------------------------------------------


class FakeMetadataStore:
    """MetadataStore port fake with optional hard-crash injection.

    ``raise_when`` predicates a save and fires ONCE by default (a process
    dies a single time; later supervisors run against the surviving disk
    state, i.e. the same store object with injection disarmed).
    """

    def __init__(
        self,
        raise_when: Callable[[str, str, dict], bool] | None = None,
        fire_once: bool = True,
    ) -> None:
        self.data: dict[tuple[str, str], dict] = {}
        self.save_count = 0
        self.crashes = 0
        self._raise_when = raise_when
        self._fire_once = fire_once
        # Makes transaction() a real mutual exclusion for in-process
        # concurrency (ownership check-and-set), like the SQLite store's
        # BEGIN IMMEDIATE does across processes.
        self._lock = threading.RLock()

    def save(self, kind: str, record_id: str, data: dict) -> None:
        with self._lock:
            payload = dict(data)
            self.save_count += 1
            if self._raise_when is not None and self._raise_when(kind, record_id, payload):
                # The process dies mid-write: nothing for this save reaches disk.
                self.crashes += 1
                if self._fire_once:
                    self._raise_when = None
                raise SimulatedCrash(f"injected persistence crash at {kind}/{record_id}")
            self.data[(kind, record_id)] = payload

    def load(self, kind: str, record_id: str) -> dict | None:
        with self._lock:
            record = self.data.get((kind, record_id))
            return dict(record) if record is not None else None

    def list_ids(self, kind: str) -> list[str]:
        with self._lock:
            return [record_id for (k, record_id) in self.data if k == kind]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            yield


class FakeArtifactStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, payload: bytes) -> str:
        digest = digest_bytes(payload)
        self.blobs[digest] = payload
        return digest

    def get(self, digest: str) -> bytes:
        blob = self.blobs.get(digest)
        if blob is None:
            raise KeyError(digest)
        return blob

    def exists(self, digest: str) -> bool:
        return digest in self.blobs


class FakeBudgetLedger:
    """BudgetLedger port fake with the hierarchical primitives (review A2).

    Mirrors ``SqliteBudgetLedger``'s semantics: children carve an OPEN parent
    atomically (committed children never exceed the parent amount), a child
    settles at its actual, and a parent closes at the sum of its children's
    actuals plus an explicit overage. Not process-atomic like SQLite's
    ``BEGIN IMMEDIATE``, but the invariants it enforces are the same.
    """

    def __init__(self, cap_usd: float = 100.0) -> None:
        self.cap_usd = cap_usd
        self.reservations: dict[str, BudgetReservation] = {}
        self._lock = threading.RLock()

    def reserve(self, holder: str, amount_usd: float) -> BudgetReservation:
        if amount_usd < 0:
            raise ContractError("reserve amount must be >= 0")
        with self._lock:
            if self.remaining_usd() + 1e-9 < amount_usd:
                raise BudgetExhaustedError(
                    f"project budget cap ${self.cap_usd} cannot cover ${amount_usd} "
                    f"(remaining ${self.remaining_usd():.6f})"
                )
            reservation = BudgetReservation(
                reservation_id=new_id("rsv"), holder=holder, amount_usd=float(amount_usd)
            )
            self.reservations[reservation.reservation_id] = reservation
            return reservation

    def reserve_child(
        self, parent_reservation_id: str, holder: str, amount_usd: float
    ) -> BudgetReservation:
        with self._lock:
            parent = self.reservations.get(parent_reservation_id)
            if parent is None:
                raise ReservationError(f"unknown reservation {parent_reservation_id}")
            if parent.parent_reservation_id is not None:
                raise ContractError("child reservations are one level deep")
            if parent.status is not ReservationStatus.OPEN:
                raise ReservationError(
                    f"parent reservation {parent_reservation_id} is {parent.status.value}"
                )
            if amount_usd <= 0:
                raise ContractError("reserve_child amount must be > 0")
            if self._committed_children_usd(parent_reservation_id) + amount_usd > (
                parent.amount_usd + 1e-9
            ):
                raise BudgetExhaustedError(
                    f"cannot allocate ${amount_usd:.4f} for {holder!r} from parent "
                    f"{parent_reservation_id!r}: committed children would exceed "
                    f"${parent.amount_usd:.4f}"
                )
            child = BudgetReservation(
                reservation_id=new_id("rsv"),
                holder=holder,
                amount_usd=float(amount_usd),
                parent_reservation_id=parent_reservation_id,
            )
            self.reservations[child.reservation_id] = child
            return child

    def _committed_children_usd(self, parent_reservation_id: str) -> float:
        committed = 0.0
        for record in self.reservations.values():
            if record.parent_reservation_id != parent_reservation_id:
                continue
            if record.status is ReservationStatus.OPEN:
                committed += record.amount_usd
            elif record.status is ReservationStatus.SETTLED:
                committed += record.settled_amount_usd or 0.0
        return committed

    def children(self, parent_reservation_id: str) -> list[BudgetReservation]:
        with self._lock:
            return [
                r
                for r in self.reservations.values()
                if r.parent_reservation_id == parent_reservation_id
            ]

    def settle_parent_from_children(
        self, parent_reservation_id: str, *, overage_usd: float = 0.0
    ) -> BudgetReservation:
        with self._lock:
            kids = self.children(parent_reservation_id)
            if any(k.status is ReservationStatus.OPEN for k in kids):
                raise ReservationError("parent still has open child allocations")
            settled_kids = [k for k in kids if k.status is ReservationStatus.SETTLED]
            actual = round(
                sum(k.settled_amount_usd or 0.0 for k in settled_kids) + overage_usd,
                9,
            )
            return self.settle(parent_reservation_id, actual)

    def settle(self, reservation_id: str, actual_usd: float) -> BudgetReservation:
        with self._lock:
            reservation = self._open(reservation_id)
            if reservation.parent_reservation_id is not None:
                committed = self._committed_children_usd(reservation.parent_reservation_id)
                after = committed - reservation.amount_usd + float(actual_usd)
                parent = self.reservations[reservation.parent_reservation_id]
                if after > parent.amount_usd + 1e-9:
                    raise BudgetExhaustedError(
                        f"settling child {reservation_id} at ${actual_usd:.4f} would exceed "
                        f"its parent (${after:.4f} > ${parent.amount_usd:.4f}); child stays "
                        "OPEN and must be reconciled"
                    )
            settled = replace(
                reservation,
                status=ReservationStatus.SETTLED,
                settled_amount_usd=round(float(actual_usd), 6),
                closed_at="settled",
            )
            self.reservations[reservation_id] = settled
            return settled

    def release(self, reservation_id: str) -> BudgetReservation:
        with self._lock:
            reservation = self._open(reservation_id)
            released = replace(reservation, status=ReservationStatus.RELEASED, closed_at="released")
            self.reservations[reservation_id] = released
            return released

    def _open(self, reservation_id: str) -> BudgetReservation:
        reservation = self.reservations.get(reservation_id)
        if reservation is None:
            raise ReservationError(f"unknown reservation {reservation_id}")
        if reservation.status is not ReservationStatus.OPEN:
            raise ReservationError(
                f"reservation {reservation_id} is already {reservation.status.value}"
            )
        return reservation

    def outstanding_usd(self) -> float:
        with self._lock:
            return sum(
                r.amount_usd
                for r in self.reservations.values()
                if r.status is ReservationStatus.OPEN and r.parent_reservation_id is None
            )

    def settled_usd(self) -> float:
        with self._lock:
            return sum(
                r.settled_amount_usd or 0.0
                for r in self.reservations.values()
                if r.status is ReservationStatus.SETTLED and r.parent_reservation_id is None
            )

    def remaining_usd(self) -> float:
        with self._lock:
            return self.cap_usd - self.settled_usd() - self.outstanding_usd()


class FakeJournal:
    """Journal port fake. Storage lists are private so the port methods win.

    Honors the port's documented append-only invariant — duplicate event /
    cost entry ids are refused (the real ``SqliteJournal`` enforces it with
    UNIQUE constraints, and the gate's cross-process claim CAS builds on it).
    """

    def __init__(self) -> None:
        self._event_log: list[EventRecord] = []
        self._cost_log: list[CostEntry] = []
        self._event_ids: set[str] = set()
        self._cost_ids: set[str] = set()
        self._lock = threading.RLock()

    def append(self, event: EventRecord) -> None:
        with self._lock:
            if event.event_id in self._event_ids:
                raise ContractError(
                    f"duplicate event id {event.event_id!r}: a replayed event is a bug"
                )
            self._event_ids.add(event.event_id)
            self._event_log.append(event)

    def append_cost(self, entry: CostEntry) -> None:
        with self._lock:
            if entry.entry_id in self._cost_ids:
                raise ContractError(
                    f"duplicate cost entry id {entry.entry_id!r}: a replayed entry is a bug"
                )
            self._cost_ids.add(entry.entry_id)
            self._cost_log.append(entry)

    def events(self, subject: str | None = None) -> list[EventRecord]:
        with self._lock:
            return [e for e in self._event_log if subject is None or e.subject == subject]

    def cost_entries(self, subject: str | None = None) -> list[CostEntry]:
        with self._lock:
            return [c for c in self._cost_log if subject is None or c.subject == subject]

    def kinds(self, subject: str | None = None) -> list[str]:
        return [e.kind.value for e in self.events(subject)]

    def event_data(self, kind: EventKind, subject: str | None = None) -> list[dict]:
        return [dict(e.data) for e in self.events(subject) if e.kind is kind]


# --- harness ----------------------------------------------------------------------


@dataclass
class Harness:
    supervisor: Any
    runtime: FakeRuntime
    store: FakeMetadataStore
    artifacts: FakeArtifactStore
    ledger: FakeBudgetLedger
    journal: FakeJournal
    policy: Any


def make_harness(
    script: Sequence[ScriptedCall] | None = None,
    *,
    on_call: Callable[[int], None] | None = None,
    store: FakeMetadataStore | None = None,
    ledger_cap_usd: float = 100.0,
    ledger: FakeBudgetLedger | None = None,
    artifacts: FakeArtifactStore | None = None,
    journal: FakeJournal | None = None,
    policy: Any = None,
) -> Harness:
    from vouch_agent.orchestrator import Supervisor, SupervisorPolicy

    runtime = FakeRuntime(script, on_call=on_call)
    store_ = store if store is not None else FakeMetadataStore()
    ledger_ = ledger if ledger is not None else FakeBudgetLedger(ledger_cap_usd)
    artifacts_ = artifacts if artifacts is not None else FakeArtifactStore()
    journal_ = journal if journal is not None else FakeJournal()
    policy_ = policy if policy is not None else SupervisorPolicy()
    supervisor = Supervisor(
        runtime=runtime,
        store=store_,
        artifacts=artifacts_,
        ledger=ledger_,
        journal=journal_,
        policy=policy_,
    )
    return Harness(
        supervisor=supervisor,
        runtime=runtime,
        store=store_,
        artifacts=artifacts_,
        ledger=ledger_,
        journal=journal_,
        policy=policy_,
    )


_VALID_DRAFT = '{"recommendation": "draft candidate A", "confidence": "low"}'
_VALID_FINAL = (
    '{"recommendation": "candidate A with sources", "confidence": "high", "priceUsd": 12.5}'
)
_INVALID_FINAL = '{"recommendation": "candidate A"}'  # missing confidence/priceUsd

_RECOMMENDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["recommendation", "confidence", "priceUsd"],
    "properties": {
        "recommendation": {"type": "string"},
        "confidence": {"type": "string"},
        "priceUsd": {"type": "number"},
    },
}


def two_step_script(*, first: str = _VALID_DRAFT, second: str = _VALID_FINAL) -> list[ScriptedCall]:
    """Step 1 produces a draft artifact; step 2 refines it."""
    return [
        ScriptedCall(content=first, cost_usd=0.01),
        ScriptedCall(content=second, cost_usd=0.02),
    ]


def schema_spec(
    *,
    mode: Any = None,
    max_cost_usd: float | None = 0.5,
    max_steps: int | None = None,
    max_wall_clock_s: float | None = None,
    conditions: list[dict[str, Any]] | None = None,
    inputs: dict[str, Any] | None = None,
) -> Any:
    """A TaskSpec whose completion needs a valid final JSON artifact."""
    from vouch_agent.contracts import RunMode, TaskSpec
    from vouch_agent.contracts.tasks import new_task_spec_id

    return TaskSpec(
        spec_id=new_task_spec_id(),
        title="Compare two product candidates",
        goal="Produce a sourced comparison of the two candidates",
        mode=mode if mode is not None else RunMode.FIXTURE,
        inputs=inputs if inputs is not None else {"candidates": ["A", "B"]},
        max_cost_usd=max_cost_usd,
        max_steps=max_steps,
        max_wall_clock_s=max_wall_clock_s,
        success_criteria={
            "conditions": conditions
            if conditions is not None
            else [
                {
                    "type": "artifact_schema",
                    "artifact": "final",
                    "schema": _RECOMMENDATION_SCHEMA,
                }
            ]
        },
    )
