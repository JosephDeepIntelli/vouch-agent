"""Self-contained port fakes + fixtures for the acceptance regressions.

These regressions must drive PUBLIC controller/CLI entry points only; the
fakes here exist purely to satisfy the storage/gate ports the way the real
workspace does. Nothing in this file is imported by product code.
"""

from __future__ import annotations

import threading
from typing import Any

from vouch_agent.adapters.base import AdapterDescriptor, AdapterExecution
from vouch_agent.contracts.candidate import AgentVersion, Candidate, ChangeType
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
from vouch_agent.contracts.common import RunMode, canonical_json, digest_of
from vouch_agent.contracts.evaluation import Rubric
from vouch_agent.contracts.journal import (
    BudgetReservation,
    CostEntry,
    EventRecord,
    ReservationStatus,
)
from vouch_agent.contracts.project import ProjectSpec
from vouch_agent.controller import VouchController
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass

#: Kind string the application services use to anchor exported evidence.
RUN_EXPORT_KIND = "run-export"


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}
        self.lock = threading.Lock()

    def save(self, kind: str, record_id: str, data: dict) -> None:
        with self.lock:
            self.data[(kind, record_id)] = dict(data)

    def load(self, kind: str, record_id: str) -> dict | None:
        with self.lock:
            entry = self.data.get((kind, record_id))
            return dict(entry) if entry else None

    def list_ids(self, kind: str) -> list[str]:
        with self.lock:
            return sorted(rid for (k, rid) in self.data if k == kind)

    def transaction(self):
        return threading.Lock()

    def __enter__(self):  # pragma: no cover - parity with the port
        return self

    def __exit__(self, *args: object) -> None:  # pragma: no cover
        return None


class FakeArtifacts:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, payload: bytes) -> str:
        from vouch_agent.contracts.common import digest_bytes

        digest = digest_bytes(payload)
        self.blobs[digest] = payload
        return digest

    def get(self, digest: str) -> bytes:
        from vouch_agent.errors import ContractError, DigestMismatchError

        if digest not in self.blobs:
            raise ContractError(f"artifact {digest} not found")
        payload = self.blobs[digest]
        from vouch_agent.contracts.common import digest_bytes

        if digest_bytes(payload) != digest:
            raise DigestMismatchError(
                f"artifact {digest} content does not match its name — tampering or corruption"
            )
        return payload

    def exists(self, digest: str) -> bool:
        return digest in self.blobs


class FakeLedger:
    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.settled = 0.0
        self.reservations: dict[str, BudgetReservation] = {}
        self.lock = threading.Lock()

    def reserve(self, holder: str, amount_usd: float) -> BudgetReservation:
        with self.lock:
            from vouch_agent.errors import BudgetExhaustedError

            open_sum = sum(
                r.amount_usd for r in self.reservations.values() if r.status.value == "open"
            )
            if self.settled + open_sum + amount_usd > self.cap + 1e-9:
                raise BudgetExhaustedError(
                    f"cap {self.cap} would be exceeded by reserving {amount_usd}"
                )
            reservation = BudgetReservation(
                reservation_id=f"rsv-{len(self.reservations) + 1}",
                holder=holder,
                amount_usd=amount_usd,
            )
            self.reservations[reservation.reservation_id] = reservation
            return reservation

    def settle(self, reservation_id: str, actual_usd: float) -> BudgetReservation:
        with self.lock:
            record = self.reservations[reservation_id]
            settled = BudgetReservation(
                reservation_id=record.reservation_id,
                holder=record.holder,
                amount_usd=record.amount_usd,
                status=ReservationStatus.SETTLED,
                settled_amount_usd=actual_usd,
            )
            self.reservations[reservation_id] = settled
            self.settled += actual_usd
            return settled

    def release(self, reservation_id: str) -> BudgetReservation:
        with self.lock:
            record = self.reservations[reservation_id]
            released = BudgetReservation(
                reservation_id=record.reservation_id,
                holder=record.holder,
                amount_usd=record.amount_usd,
                status=ReservationStatus.RELEASED,
            )
            self.reservations[reservation_id] = released
            return released

    def outstanding_usd(self) -> float:
        return sum(r.amount_usd for r in self.reservations.values() if r.status.value == "open")

    def settled_usd(self) -> float:
        return self.settled

    def remaining_usd(self) -> float:
        return self.cap - self.settled_usd() - self.outstanding_usd()


class FakeJournal:
    def __init__(self) -> None:
        self._events: list[EventRecord] = []
        self._costs: list[CostEntry] = []
        self.lock = threading.Lock()

    def append(self, event: EventRecord) -> None:
        with self.lock:
            self._events.append(event)

    def append_cost(self, entry: CostEntry) -> None:
        with self.lock:
            self._costs.append(entry)

    def events(self, subject: str | None = None) -> list[EventRecord]:
        return [e for e in self._events if subject is None or e.subject == subject]

    def cost_entries(self, subject: str | None = None) -> list[CostEntry]:
        return [c for c in self._costs if subject is None or c.subject == subject]


class ChannelAdapter:
    """Deterministic adapter emitting explicit checked-pass guardrail channels.

    ``baseline_values``/``candidate_values`` are indexed by the repeat number
    carried in ``case_input``; ``channels`` are emitted on both sides for every
    attempt (0 = checked pass), with a violation (1) on candidate repeats
    listed in ``violate_repeats``. ``cost=None`` + ``ok=False`` reproduces a
    post-dispatch failure with unknown spend.
    """

    def __init__(
        self,
        *,
        metric: str = "score",
        baseline_values: tuple[float, ...] = (1.0,),
        candidate_values: tuple[float, ...] = (2.0,),
        channels: tuple[str, ...] = (),
        violate_repeats: frozenset[int] = frozenset(),
        cost: float | None = 0.01,
        ok: bool = True,
        omit_channels: frozenset[str] = frozenset(),
        adapter_id: str = "channel-fixture@1",
    ) -> None:
        self.metric = metric
        self.baseline_values = baseline_values
        self.candidate_values = candidate_values
        self.channels = channels
        self.violate_repeats = violate_repeats
        self.cost = cost
        self.ok = ok
        self.omit_channels = omit_channels
        self.adapter_id = adapter_id
        self.executed_case_inputs: list[dict[str, Any]] = []

    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(adapter_id=self.adapter_id, workflows=("wf-compare",))

    def prepare(self, run_id: str, mode: RunMode) -> None:
        return None

    def execute(
        self, *, run_id: str, attempt_id: str, workflow_id: str, case_input: dict, mode: RunMode
    ) -> AdapterExecution:
        self.executed_case_inputs.append(dict(case_input))
        repeat = int(case_input.get("repeat", 0))
        baseline_side = case_input.get("side") == "baseline"
        values = self.baseline_values if baseline_side else self.candidate_values
        usage: dict[str, Any] = {self.metric: values[min(repeat, len(values) - 1)]}
        if self.cost is not None:
            usage["costUsd"] = self.cost
        for channel in self.channels:
            if channel in self.omit_channels and not baseline_side:
                continue  # candidate side never measured the channel
            usage[f"guardrail:{channel}"] = (
                1 if (not baseline_side and repeat in self.violate_repeats) else 0
            )
        return AdapterExecution(
            ok=self.ok,
            outputs={"caseId": case_input.get("caseId"), "side": case_input.get("side")},
            # usage carries the metric always; costUsd only when metered (an
            # unmetered post-dispatch failure is exactly repro #6's shape)
            usage=usage,
            error=None if self.ok else "synthetic post-dispatch failure",
            mode=mode,
            runner_version=self.adapter_id,
        )

    def collect(self, run_id: str) -> tuple[str, ...]:
        return ()

    def cleanup(self, run_id: str) -> None:
        return None


def make_controller(
    cap: float = 1.0, *, gate_case_prefixes: tuple[str, ...] = ("case-", "eval_")
) -> tuple[VouchController, FakeStore, FakeLedger, FakeJournal, FakeArtifacts]:
    store, artifacts, ledger, journal = FakeStore(), FakeArtifacts(), FakeLedger(cap), FakeJournal()
    project = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-acc",
            "name": "acceptance regression",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "wf-compare",
                    "name": "Compare",
                    "mainObjective": "score",
                }
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": cap},
        }
    )
    broker = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"run-adapter-attempt"}),
            max_risk_class=RiskClass.R1,
            allowed_resources=frozenset(),
            resource_prefixes=gate_case_prefixes,
            max_reservation_usd=0.5,
        ),
        ledger,
        journal,
    )
    controller = VouchController(project, store, artifacts, ledger, journal, broker)
    return controller, store, ledger, journal, artifacts


def make_pack() -> TaskPack:
    """One selection-validation case + one final-acceptance case."""

    def case(cid: str, split: CaseSplit) -> TaskCase:
        return TaskCase(
            case_id=cid,
            workflow_id="wf-compare",
            split=split,
            group_id="family-acc",
            input_digest=digest_of({"case": cid}),
        )

    return TaskPack(
        pack_id="pack-acc",
        workflow_id="wf-compare",
        cases=(
            case("case-sel-1", CaseSplit.SELECTION_VALIDATION),
            case("case-fin-1", CaseSplit.FINAL_ACCEPTANCE),
        ),
        mode=RunMode.FIXTURE,
        notes="synthetic acceptance regression pack",
    )


def make_candidate(proposer: str = "proposer-agent") -> Candidate:
    return Candidate(
        candidate_id="cand-acc",
        parent_version=AgentVersion(version_id="v0", source_ref="git:abc"),
        change_type=ChangeType.PROMPT_DELTA,
        delta="+ cite every price claim",
        rationale="unsupported price claims",
        expected_impact="score up",
        proposer=proposer,
    )


def make_baseline() -> AgentVersion:
    return AgentVersion(version_id="v0", source_ref="git:abc")


def rubric_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "main_metric": "score",
        "direction": "increase",
        "thresholds": {},
        "hard_guardrails": ("unsafe",),
        "repeats": 1,
    }
    fields.update(overrides)
    return fields


def seed_inputs(artifacts, pack) -> None:
    """Materialize each case's input bytes so digest verification can pass."""
    for case in pack.cases:
        payload = {"case": case.case_id}
        assert artifacts.put(canonical_json(payload).encode("utf-8")) == case.input_digest


def rubric(**overrides: Any) -> Rubric:
    """Unfrozen rubric; the controller's freeze_rubric freezes (and digests) it."""
    return Rubric(**rubric_fields(**overrides))


def frozen_rubric(**overrides: Any) -> Rubric:
    """Already-frozen rubric for the pure evaluation-level regressions."""
    from vouch_agent.evaluation.verdict import freeze_rubric

    return freeze_rubric(Rubric(**rubric_fields(**overrides)), "ana")
