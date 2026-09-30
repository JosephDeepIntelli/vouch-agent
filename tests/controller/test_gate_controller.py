"""Lead-owned gate + controller tests: the offline vertical improvement flow.

These use tiny in-test fakes for the storage/journal/adapter ports (the
storage specialist ships the real SQLite implementations); the point here is
the *controller's* semantics: budget-before-work, failure booking, split
isolation, digest-bound approvals, state-machine honesty.
"""

from __future__ import annotations

import threading

import pytest

from vouch_agent.adapters.base import AdapterDescriptor, AdapterExecution
from vouch_agent.contracts.candidate import AgentVersion, Candidate, ChangeType
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
from vouch_agent.contracts.common import Role, RunMode, canonical_json, digest_of
from vouch_agent.contracts.decision import ApprovalBinding, Verdict
from vouch_agent.contracts.evaluation import Rubric
from vouch_agent.contracts.journal import (
    BudgetReservation,
    CostEntry,
    EventRecord,
    ReservationStatus,
)
from vouch_agent.contracts.project import ProjectSpec
from vouch_agent.controller import VouchController
from vouch_agent.errors import (
    ApprovalInvalidatedError,
    BudgetExhaustedError,
    ContractError,
    GateDeniedError,
    SplitAccessError,
)
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass
from vouch_agent.gate.proposal import ActionProposal

# --- port fakes ---------------------------------------------------------------


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
            return [rid for (k, rid) in self.data if k == kind]

    def transaction(self):  # pragma: no cover - not exercised in these tests
        return threading.Lock()

    def __enter__(self):  # pragma: no cover
        return self

    def __exit__(self, *args):  # pragma: no cover
        return None


class FakeArtifacts:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, payload: bytes) -> str:
        from vouch_agent.contracts.common import digest_bytes

        d = digest_bytes(payload)
        self.blobs[d] = payload
        return d

    def get(self, digest: str) -> bytes:
        return self.blobs[digest]

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
            if (
                self.settled
                + sum(r.amount_usd for r in self.reservations.values() if r.status.value == "open")
                + amount_usd
                > self.cap + 1e-9
            ):
                from vouch_agent.errors import BudgetExhaustedError

                raise BudgetExhaustedError(
                    f"cap {self.cap} would be exceeded by reserving {amount_usd}"
                )
            r = BudgetReservation(
                reservation_id=f"rsv-{len(self.reservations) + 1}",
                holder=holder,
                amount_usd=amount_usd,
            )
            self.reservations[r.reservation_id] = r
            return r

    def settle(self, reservation_id: str, actual_usd: float) -> BudgetReservation:
        with self.lock:
            r = self.reservations[reservation_id]
            settled = BudgetReservation(
                reservation_id=r.reservation_id,
                holder=r.holder,
                amount_usd=r.amount_usd,
                status=ReservationStatus.SETTLED,
                settled_amount_usd=actual_usd,
            )
            self.reservations[reservation_id] = settled
            self.settled += actual_usd
            return settled

    def release(self, reservation_id: str) -> BudgetReservation:  # pragma: no cover
        r = self.reservations[reservation_id]
        out = BudgetReservation(
            reservation_id=r.reservation_id,
            holder=r.holder,
            amount_usd=r.amount_usd,
            status=ReservationStatus.RELEASED,
        )
        self.reservations[reservation_id] = out
        return out

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


class ScriptedAdapter:
    """Deterministic offline adapter speaking the AdapterClient port.

    Emits the workflow's main metric on every attempt plus explicit
    checked-pass guardrail channels (review A4: a usable candidate attempt
    that omits a REQUIRED hard channel is unknown, never an implicit pass).
    """

    def __init__(
        self,
        *,
        ok: bool = True,
        cost: float | None = 0.01,
        fail_on=None,
        metric: str = "score",
        channels: tuple[str, ...] = (),
    ) -> None:
        self.ok = ok
        self.cost = cost
        self.fail_on = set(fail_on or ())
        self.metric = metric
        self.channels = channels
        self.calls: list[str] = []

    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(adapter_id="scripted-fixture@1", workflows=("wf-compare",))

    def prepare(self, run_id: str, mode: RunMode) -> None:
        self.calls.append(f"prepare:{run_id}")

    def execute(self, *, run_id, attempt_id, workflow_id, case_input, mode) -> AdapterExecution:
        self.calls.append(f"execute:{case_input.get('caseId')}")
        if case_input.get("caseId") in self.fail_on:
            return AdapterExecution(ok=False, error="synthetic failure", usage={"costUsd": 0.002})
        usage: dict = {self.metric: 0.95 if case_input.get("side") == "candidate" else 0.70}
        if self.cost is not None:
            usage["costUsd"] = self.cost
        for channel in self.channels:
            usage[f"guardrail:{channel}"] = 0  # explicit checked pass
        return AdapterExecution(
            ok=self.ok,
            outputs={"caseId": case_input.get("caseId")},
            usage=usage,
            mode=mode,
            runner_version="scripted-fixture@1",
        )

    def collect(self, run_id: str) -> tuple[str, ...]:
        return ()

    def cleanup(self, run_id: str) -> None:
        self.calls.append(f"cleanup:{run_id}")


# --- fixtures -------------------------------------------------------------------


def make_controller(
    cap: float = 1.0,
) -> tuple[VouchController, FakeStore, FakeLedger, FakeJournal, FakeArtifacts]:
    store, artifacts, ledger, journal = FakeStore(), FakeArtifacts(), FakeLedger(cap), FakeJournal()
    project = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-1",
            "name": "Choose internal",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "wf-compare",
                    "name": "Compare",
                    "mainObjective": "supported comparisons",
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
            resource_prefixes=("case-", "eval_"),
            max_reservation_usd=0.5,
        ),
        ledger,
        journal,
    )
    controller = VouchController(project, store, artifacts, ledger, journal, broker)
    return controller, store, ledger, journal, artifacts


def seed_pack_inputs(artifacts: FakeArtifacts, pack: TaskPack) -> None:
    """Materialize each case's input bytes (digest-verified before execution)."""
    for case in pack.cases:
        artifacts.put(canonical_json({"case": case.case_id}).encode("utf-8"))


def make_pack() -> TaskPack:
    def case(cid: str, split: CaseSplit) -> TaskCase:
        return TaskCase(
            case_id=cid,
            workflow_id="wf-compare",
            split=split,
            group_id="family-a",
            input_digest=digest_of({"case": cid}),
        )

    return TaskPack(
        pack_id="pack-1",
        workflow_id="wf-compare",
        cases=(
            case("case-dev-1", CaseSplit.DEVELOPMENT),
            case("case-dev-2", CaseSplit.DEVELOPMENT),
            case("case-sel-1", CaseSplit.SELECTION_VALIDATION),
            case("case-acc-1", CaseSplit.FINAL_ACCEPTANCE),
        ),
        mode=RunMode.FIXTURE,
        notes="synthetic",
    )


def make_candidate() -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        parent_version=AgentVersion(version_id="v0", source_ref="git:abc"),
        change_type=ChangeType.PROMPT_DELTA,
        delta="+ require citations for every price claim",
        rationale="unsupported price claims in Compare",
        expected_impact="fewer unsupported claims",
        proposer="proposer-agent",
    )


def make_baseline() -> AgentVersion:
    return AgentVersion(version_id="v0", source_ref="git:abc")


# --- gate tests --------------------------------------------------------------------


def test_gate_denies_r3_and_unlisted_actions() -> None:
    _, _, ledger, journal, _artifacts = make_controller()
    broker = CapabilityBroker(
        GatePolicy(allowed_actions=frozenset({"read-case"}), max_risk_class=RiskClass.R2),
        ledger,
        journal,
    )
    r3 = ActionProposal(action="deploy", arguments={}, risk_class=RiskClass.R3)
    with pytest.raises(GateDeniedError, match="R3"):
        broker.authorize(r3, mode=RunMode.AUTHORIZED_LIVE)
    unknown = ActionProposal(action="steal", arguments={}, risk_class=RiskClass.R0)
    with pytest.raises(GateDeniedError, match="allowlist"):
        broker.authorize(unknown, mode=RunMode.FIXTURE)


def test_gate_blocks_side_effects_outside_authorized_live() -> None:
    _, _, ledger, journal, _artifacts = make_controller()
    broker = CapabilityBroker(
        GatePolicy(allowed_actions=frozenset({"send-data"}), max_risk_class=RiskClass.R2),
        ledger,
        journal,
    )
    proposal = ActionProposal(action="send-data", arguments={"to": "x"}, risk_class=RiskClass.R2)
    with pytest.raises(GateDeniedError, match="forbids side-effecting"):
        broker.authorize(proposal, mode=RunMode.FIXTURE)


def test_gate_blocks_post_approval_parameter_swap() -> None:
    _, _, ledger, journal, _artifacts = make_controller()
    broker = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"run-adapter-attempt"}),
            max_risk_class=RiskClass.R1,
            max_reservation_usd=0.0,
        ),
        ledger,
        journal,
    )
    original = ActionProposal(
        action="run-adapter-attempt",
        arguments={"caseId": "case-1"},
        risk_class=RiskClass.R1,
    )
    swapped = ActionProposal(
        action="run-adapter-attempt",
        arguments={"caseId": "case-final-acceptance-secret"},
        risk_class=RiskClass.R1,
    )
    auth = broker.authorize(original, mode=RunMode.FIXTURE, reservation_id=None)
    with pytest.raises(GateDeniedError, match="no longer match"):
        broker.execute(auth, lambda: "ran", proposal=swapped, mode=RunMode.FIXTURE)


def test_gate_consumed_authorization_cannot_replay() -> None:
    _, _, ledger, journal, _artifacts = make_controller()
    broker = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"run-adapter-attempt"}),
            max_risk_class=RiskClass.R1,
            max_reservation_usd=0.0,
        ),
        ledger,
        journal,
    )
    proposal = ActionProposal(
        action="run-adapter-attempt", arguments={"caseId": "case-1"}, risk_class=RiskClass.R1
    )
    auth = broker.authorize(proposal, mode=RunMode.FIXTURE)
    assert broker.execute(auth, lambda: 1, proposal=proposal) == 1
    with pytest.raises(GateDeniedError, match="already consumed"):
        broker.execute(auth, lambda: 2, proposal=proposal)


# --- vertical flow tests --------------------------------------------------------------


def test_full_offline_vertical_flow() -> None:
    controller, store, _ledger, journal, artifacts = make_controller(cap=1.0)
    controller.init()
    pack = make_pack()
    seed_pack_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    baseline = make_baseline()
    controller.record_baseline(baseline, "wf-compare")
    rubric_digest = controller.freeze_rubric(Rubric(main_metric="score"), frozen_by="product-owner")

    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal("cand-1")

    adapter = ScriptedAdapter(metric="score")
    # Development split: measurable, but it must NOT promote the candidate.
    dev_run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-1",
        baseline=baseline,
        pack=pack,
        split=CaseSplit.DEVELOPMENT,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )
    assert dev_run.execution_status == "completed"
    assert len(dev_run.attempts) == 4  # 2 cases x 2 sides
    assert all(a.status.value == "ok" for a in dev_run.attempts)
    with pytest.raises(ContractError, match="selection"):
        controller.mark_evaluated("cand-1")

    # The qualifying selection-validation run is what promotes the candidate.
    selection_run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-1",
        baseline=baseline,
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )
    assert len(selection_run.attempts) == 2  # 1 selection case x 2 sides
    moved = controller.mark_evaluated("cand-1")
    assert moved.state.value == "evaluated"

    acc_run = controller.run_final_acceptance(
        workflow_id="wf-compare",
        candidate_id="cand-1",
        baseline=baseline,
        pack=pack,
        rubric_digest=rubric_digest,
        adapter=adapter,
        role=Role.ACCEPTANCE_OWNER,
    )
    assert acc_run.split is CaseSplit.FINAL_ACCEPTANCE
    assert len(acc_run.attempts) == 2  # 1 final case x 2 sides x 1 repeat

    # Decide over the durable record, anchor the evidence export, then record.
    from vouch_agent.contracts.evaluation import EvaluationRun
    from vouch_agent.evaluation import compare_run, decide
    from vouch_agent.evaluation.verdict import verdict_from_summary

    rubric = controller.rubric(rubric_digest)
    stored = EvaluationRun.from_dict(store.load("evaluation", acc_run.run_id) or {})
    summary = compare_run(stored, controller.project.workflow("wf-compare"), rubric)
    assert verdict_from_summary(summary, rubric) is Verdict.ACCEPTED
    evidence_digest = digest_of({"export": acc_run.run_id})
    store.save(
        "run-export",
        acc_run.run_id,
        {"manifestDigest": evidence_digest, "path": f"/synthetic/{acc_run.run_id}"},
    )
    decision = decide(summary, stored, rubric, owner="ana", evidence_digest=evidence_digest)
    controller.record_decision(
        decision, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=acc_run.run_id
    )
    assert store.load("candidate", "cand-1")["state"] == "accepted"

    controller.record_approval("cand-1", role=Role.RELEASE_OWNER, binding=decision.binding)
    assert store.load("candidate", "cand-1")["state"] == "approved"

    from vouch_agent.contracts.decision import ReleaseRecord

    controller.record_release(
        ReleaseRecord(
            release_id="rel-1",
            candidate_digest=decision.candidate_digest,
            deployed_version="choose@v9",
            deployed_by="roger",
        ),
        role=Role.RELEASE_OWNER,
    )
    assert store.load("candidate", "cand-1")["state"] == "released"
    # every attempt booked a cost line; totals include failures and acceptance
    costs = journal.cost_entries()
    assert len(costs) == 8  # 4 dev + 2 selection + 2 acceptance
    report = controller.full_cost_usd()
    assert report["measuredUsd"] > 0
    assert report["unmeasurableEntries"] == []


def test_budget_exhaustion_fails_closed_mid_evaluation() -> None:
    controller, _, _, journal, artifacts = make_controller(cap=0.03)  # only ~2 attempts affordable
    controller.init()
    pack = make_pack()
    seed_pack_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    rubric_digest = controller.freeze_rubric(Rubric(main_metric="x"), frozen_by="po")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal("cand-1")
    seed_pack_inputs(artifacts, pack)
    with pytest.raises(BudgetExhaustedError):
        controller.run_paired_evaluation(
            workflow_id="wf-compare",
            candidate_id="cand-1",
            baseline=make_baseline(),
            pack=pack,
            split=CaseSplit.DEVELOPMENT,
            rubric_digest=rubric_digest,
            adapter=ScriptedAdapter(cost=0.01),
            per_attempt_reserve_usd=0.02,
        )
    assert any(e.kind.value == "budget-exhausted" for e in journal.events())


def test_proposer_cannot_import_or_run_final_acceptance() -> None:
    controller, _, _, _, _artifacts = make_controller()
    controller.init()
    with pytest.raises(SplitAccessError):
        controller.import_pack(make_pack(), Role.PROPOSER)
    pack = make_pack()
    controller.import_pack(pack, Role.EVALUATOR)
    rubric_digest = controller.freeze_rubric(Rubric(main_metric="x"), frozen_by="po")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal("cand-1")
    # proposer role is refused at the acceptance door regardless of arguments
    with pytest.raises(SplitAccessError):
        controller.run_final_acceptance(
            workflow_id="wf-compare",
            candidate_id="cand-1",
            baseline=make_baseline(),
            pack=pack,
            rubric_digest=rubric_digest,
            adapter=ScriptedAdapter(),
            role=Role.PROPOSER,
        )
    # and even the acceptance owner needs a completed selection evaluation first
    with pytest.raises(ContractError, match="selection evaluation"):
        controller.run_final_acceptance(
            workflow_id="wf-compare",
            candidate_id="cand-1",
            baseline=make_baseline(),
            pack=pack,
            rubric_digest=rubric_digest,
            adapter=ScriptedAdapter(),
            role=Role.ACCEPTANCE_OWNER,
        )


def test_unfrozen_rubric_blocks_evaluation() -> None:
    controller, _, _, _, _artifacts = make_controller()
    controller.init()
    pack = make_pack()
    controller.import_pack(pack, Role.EVALUATOR)
    controller.propose(make_candidate())
    controller.seal("cand-1")
    with pytest.raises(ContractError, match="rubric"):
        controller.run_paired_evaluation(
            workflow_id="wf-compare",
            candidate_id="cand-1",
            baseline=make_baseline(),
            pack=pack,
            split=CaseSplit.DEVELOPMENT,
            rubric_digest="sha256:" + "0" * 64,
            adapter=ScriptedAdapter(),
        )


def test_failed_attempts_are_booked_not_hidden() -> None:
    controller, _, _, journal, artifacts = make_controller(cap=1.0)
    controller.init()
    pack = make_pack()
    seed_pack_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    rubric_digest = controller.freeze_rubric(Rubric(main_metric="x"), frozen_by="po")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal("cand-1")
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-1",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.DEVELOPMENT,
        rubric_digest=rubric_digest,
        adapter=ScriptedAdapter(fail_on={"case-dev-2"}),
    )
    statuses = [a.status.value for a in run.attempts]
    assert "failed" in statuses
    assert run.uncertainty_note  # surfaced, not swallowed
    booked = journal.cost_entries()
    assert len(booked) == 4  # failures are cost lines too


def test_approval_invalidates_when_candidate_digest_changes() -> None:
    controller, _, _, _, _artifacts = make_controller()
    controller.init()
    candidate = make_candidate()
    controller.propose(candidate)
    sealed = controller.seal("cand-1")
    binding = ApprovalBinding(
        candidate_digest=sealed.digest(),
        rubric_digest=digest_of({"r": 1}),
        environment_digest=digest_of({"e": 1}),
        acceptance_case_set_digest=digest_of({"a": 1}),
    )
    # someone edits the delta after the fact -> digest no longer matches
    tampered = Candidate(
        candidate_id="cand-1",
        parent_version=candidate.parent_version,
        change_type=candidate.change_type,
        delta=candidate.delta + " TAMPER",
        rationale=candidate.rationale,
        expected_impact=candidate.expected_impact,
        proposer=candidate.proposer,
    )
    with pytest.raises(ApprovalInvalidatedError):
        binding.verify(
            candidate_digest=tampered.digest(),
            rubric_digest=binding.rubric_digest,
            environment_digest=binding.environment_digest,
            acceptance_case_set_digest=binding.acceptance_case_set_digest,
        )


def test_reconciliation_requires_explicit_action() -> None:
    controller, _, _, journal, _artifacts = make_controller()
    controller.mark_needs_reconciliation("run-1", "crash between persist phases")
    with pytest.raises(Exception, match="reconciliation requires"):
        controller.reconcile("run-1", "")
    controller.reconcile("run-1", "queried adapter journal; no side effects had started")
    kinds = [e.kind.value for e in journal.events()]
    assert "run-reconciliation" in kinds
