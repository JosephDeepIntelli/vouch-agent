"""Independent verification matrix — design §13 behavioral requirements.

One file, one test per mandated behavior, driven only through public
packages (no test-private seams). Deep variants live in their owning
suites; this is the cross-cutting confirmation layer proving each §13
requirement against the INTEGRATED surface:

1. tampering with candidate/label/policy is blocked
2. existing approvals invalidate when digests change
3. concurrent budget cannot over-schedule
4. the acceptance split is unreadable by the proposer
5. systems outside the broker may not be reported as fully protected
6. unknown side effects after a crash are not blindly retried
7. skills degrade in a new environment
8. cross-project unauthorized assets are unreadable
"""

from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

import pytest

from vouch_agent.contracts.candidate import AgentVersion, Candidate, ChangeType
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
from vouch_agent.contracts.common import Role, RunMode, digest_of
from vouch_agent.contracts.decision import ApprovalBinding
from vouch_agent.contracts.project import ProjectSpec
from vouch_agent.contracts.skill import EnvironmentSignature, ReuseRight, SkillState
from vouch_agent.controller import VouchController
from vouch_agent.errors import (
    ApprovalInvalidatedError,
    BudgetExhaustedError,
    DigestMismatchError,
    GateDeniedError,
    SplitAccessError,
    UnauthorizedReuseError,
)
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass
from vouch_agent.gate.proposal import ActionProposal
from vouch_agent.ledger import SkillLedger
from vouch_agent.storage import (
    FileArtifactStore,
    SqliteBudgetLedger,
    SqliteJournal,
    SqliteMetadataStore,
    load_case,
)
from vouch_agent.storage.splits import save_task_pack


def _workspace(tmp_path: Path):
    store = SqliteMetadataStore(tmp_path / "meta.sqlite")
    artifacts = FileArtifactStore(tmp_path)
    journal = SqliteJournal(tmp_path / "journal.sqlite")
    ledger = SqliteBudgetLedger(tmp_path / "budget.sqlite", total_usd_cap=1.0)
    project = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-v",
            "name": "verification",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "wf-compare",
                    "name": "Compare",
                    "mainObjective": "supported_claim_rate",
                }
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": 1.0},
        }
    )
    broker = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"run-adapter-attempt"}),
            max_risk_class=RiskClass.R1,
            resource_prefixes=("case-", "eval_"),
            max_reservation_usd=0.5,
        ),
        ledger,
        journal,
        reservation_status=lambda rid: (
            next(r for r in ledger.reservations() if r.reservation_id == rid).status
        ),
    )
    controller = VouchController(project, store, artifacts, ledger, journal, broker)
    return controller, store, artifacts, ledger, journal


def _pack() -> TaskPack:
    def case(cid: str, split: CaseSplit) -> TaskCase:
        return TaskCase(
            case_id=cid,
            workflow_id="wf-compare",
            split=split,
            group_id="f",
            input_digest=digest_of({"case": cid}),
        )

    return TaskPack(
        pack_id="pack-v",
        workflow_id="wf-compare",
        cases=(
            case("case-dev-1", CaseSplit.DEVELOPMENT),
            case("case-acc-1", CaseSplit.FINAL_ACCEPTANCE),
        ),
        mode=RunMode.FIXTURE,
        notes="synthetic",
    )


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="cand-v",
        parent_version=AgentVersion(version_id="v0", source_ref="git:abc"),
        change_type=ChangeType.PROMPT_DELTA,
        delta="+ cite everything",
        rationale="unsupported claims",
        expected_impact="better support",
        proposer="proposer-agent",
    )


# 1 — tampering ---------------------------------------------------------------


def test_1_tampered_candidate_and_policy_are_blocked(tmp_path: Path) -> None:
    controller, store, *_ = _workspace(tmp_path)
    controller.init()
    save_task_pack(store, _pack())
    controller.import_pack(_pack(), Role.EVALUATOR)
    sealed = controller.propose(_candidate())
    sealed = controller.seal(sealed)

    # candidate content tampered after sealing -> content digest no longer
    # matches any recorded candidate; approvals bound to the old digest die
    tampered = Candidate(
        candidate_id="cand-v",
        parent_version=sealed.parent_version,
        change_type=sealed.change_type,
        delta=sealed.delta + " TAMPERED",
        rationale=sealed.rationale,
        expected_impact=sealed.expected_impact,
        proposer=sealed.proposer,
    )
    assert tampered.content_digest() != sealed.content_digest()
    binding = ApprovalBinding(
        candidate_digest=sealed.content_digest(),
        rubric_digest=digest_of({"r": 1}),
        environment_digest=digest_of({"e": 1}),
        acceptance_case_set_digest=digest_of({"a": 1}),
    )
    with pytest.raises(ApprovalInvalidatedError):
        binding.verify(
            candidate_digest=tampered.content_digest(),
            rubric_digest=binding.rubric_digest,
            environment_digest=binding.environment_digest,
            acceptance_case_set_digest=binding.acceptance_case_set_digest,
        )

    # policy tampering: an authorization issued under one policy cannot be
    # executed after the policy object changed
    _, _, _, ledger_v, journal_v = _workspace(tmp_path / "p2")
    broker = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"run-adapter-attempt"}),
            max_risk_class=RiskClass.R1,
            max_reservation_usd=0.0,
        ),
        ledger_v,
        journal_v,
    )
    proposal = ActionProposal(
        action="run-adapter-attempt", arguments={"caseId": "case-dev-1"}, risk_class=RiskClass.R1
    )
    auth = broker.authorize(proposal, mode=RunMode.FIXTURE)
    changed = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"run-adapter-attempt", "extra-action"}),
            max_risk_class=RiskClass.R1,
            max_reservation_usd=0.0,
        ),
        ledger_v,
        journal_v,
    )
    with pytest.raises(GateDeniedError, match=r"unknown authorization|policy changed"):
        changed.execute(auth, lambda: "ran", proposal=proposal)


def test_1b_tampered_artifact_bytes_fail_digest_verification(tmp_path: Path) -> None:
    _, _, artifacts, _, _ = _workspace(tmp_path)
    digest = artifacts.put(b"original payload")
    (tmp_path / "artifacts" / digest.removeprefix("sha256:")).write_bytes(b"tampered payload")
    with pytest.raises(DigestMismatchError):
        artifacts.get(digest)


# 2 — approval invalidation -----------------------------------------------------


def test_2_rubric_or_caseset_change_invalidates_decision_binding(tmp_path: Path) -> None:
    binding = ApprovalBinding(
        candidate_digest=digest_of({"c": 1}),
        rubric_digest=digest_of({"r": 1}),
        environment_digest=digest_of({"e": 1}),
        acceptance_case_set_digest=digest_of({"a": 1}),
    )
    for mutate in (
        {"candidate_digest": digest_of({"c": 2})},
        {"rubric_digest": digest_of({"r": 2})},
        {"environment_digest": digest_of({"e": 2})},
        {"acceptance_case_set_digest": digest_of({"a": 2})},
    ):
        kwargs = {
            "candidate_digest": binding.candidate_digest,
            "rubric_digest": binding.rubric_digest,
            "environment_digest": binding.environment_digest,
            "acceptance_case_set_digest": binding.acceptance_case_set_digest,
        }
        kwargs.update(mutate)
        with pytest.raises(ApprovalInvalidatedError):
            binding.verify(**kwargs)


# 3 — concurrent budget -----------------------------------------------------------


def _hammer_reservations(path: str, cap: float, n: int, amount: float, out) -> None:
    ledger = SqliteBudgetLedger(path, total_usd_cap=cap)
    won = 0
    for _ in range(n):
        try:
            ledger.reserve(f"proc-{threading.current_thread().name}", amount)
            won += 1
        except BudgetExhaustedError:
            pass
    out.put(won)


def test_3_multiprocess_reservation_never_exceeds_cap(tmp_path: Path) -> None:
    cap, amount, n_procs, per = 0.50, 0.05, 4, 30
    path = str(tmp_path / "hammer.sqlite")
    SqliteBudgetLedger(path, total_usd_cap=cap).total_cap_usd()  # create
    out: multiprocessing.Queue = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(target=_hammer_reservations, args=(path, cap, per, amount, out))
        for _ in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    wins = [out.get() for _ in procs]
    assert sum(wins) * amount <= cap + 1e-9, f"oversubscribed: {wins}"


# 4 — split isolation ---------------------------------------------------------------


def test_4_proposer_cannot_read_acceptance_case_by_direct_id(tmp_path: Path) -> None:
    controller, store, *_ = _workspace(tmp_path)
    controller.init()
    save_task_pack(store, _pack())
    controller.import_pack(_pack(), Role.EVALUATOR)
    with pytest.raises(SplitAccessError):
        load_case(store, "case-acc-1", Role.PROPOSER)
    with pytest.raises(SplitAccessError):
        load_case(store, "case-acc-1", Role.ENGINEER)
    assert load_case(store, "case-acc-1", Role.ACCEPTANCE_OWNER).case_id == "case-acc-1"
    # and the proposer cannot even import a pack containing acceptance cases
    with pytest.raises(SplitAccessError):
        controller.import_pack(_pack(), Role.PROPOSER)


# 5 — no unbrokered protection claims ------------------------------------------------


def test_5_manifest_refuses_unattested_runner_integration() -> None:
    from vouch_agent.adapters.fixture_adapter import FixturePack
    from vouch_agent.workflows.manifest import WorkflowManifest

    fixture_pack = FixturePack.load(Path("fixtures/choose"))
    manifest = WorkflowManifest.from_fixture_pack(fixture_pack)
    integrated = [e for e in manifest.entries if e.status.value == "runner-integrated"]
    assert integrated == [], "no workflow may claim runner integration without attestation"


# 6 — crash unknown side effects ------------------------------------------------------


def test_6_unknown_side_effect_requires_reconciliation_not_replay() -> None:
    from vouch_agent.contracts.tasks import RunStep, StepKind, TaskRun, TaskStatus

    run = TaskRun(run_id="run-x", task_digest=digest_of({"t": 1}))
    run = run.with_transition(TaskStatus.RUNNING)
    # record the in-flight intent exactly as the supervisor does
    in_flight = RunStep(
        step_id="s1",
        kind=StepKind.TOOL_CALL,
        status="running",
        started_at="2026-09-28T00:00:00+00:00",
    )
    run = run.with_step(in_flight)
    crashed = run.with_transition(TaskStatus.NEEDS_RECONCILIATION)
    # blind completion is refused; only an explicit resume transition exists
    from vouch_agent.errors import InvalidStateTransitionError

    with pytest.raises(InvalidStateTransitionError):
        crashed.with_transition(TaskStatus.COMPLETED)


# 7 — skill drift ----------------------------------------------------------------------


def _env(model: str = "m1") -> EnvironmentSignature:
    return EnvironmentSignature(
        model_id=model,
        tool_schema_version="1",
        workflow_id="wf",
        domain="d",
        locale="en",
        data_policy="internal",
        acceptance_version="1",
    )


def test_7_new_environment_degrades_trusted_skill(tmp_path: Path) -> None:
    store = SqliteMetadataStore(tmp_path / "l.sqlite")
    journal = SqliteJournal(tmp_path / "lj.sqlite")
    ledger = SkillLedger(store, journal)
    entry = ledger.distill(content="s", environment=_env())
    ledger.start_probation(
        entry.skill_id, verification_refs=("sha256:" + "a" * 64,), reuse_right=ReuseRight.PROJECT
    )
    ledger.promote(entry.skill_id, verification_ref="sha256:" + "b" * 64)
    degraded = ledger.record_outcome(
        entry.skill_id, outcome="unknown", environment=_env(model="other")
    )
    assert degraded.state is SkillState.PROBATION


# 8 — cross-project assets ---------------------------------------------------------------


def test_8_cross_project_asset_refused(tmp_path: Path) -> None:
    store = SqliteMetadataStore(tmp_path / "l2.sqlite")
    journal = SqliteJournal(tmp_path / "lj2.sqlite")
    ledger = SkillLedger(store, journal)
    entry = ledger.distill(content="s", environment=_env())
    ledger.start_probation(
        entry.skill_id, verification_refs=("sha256:" + "a" * 64,), reuse_right=ReuseRight.PROJECT
    )
    with pytest.raises(UnauthorizedReuseError):
        ledger.acquire(
            entry.skill_id,
            _env(),
            project_id="proj-other",
            authorized_projects=frozenset({"proj-home"}),
            role="engineer",
        )


# honesty guard: fixture-mode reports never claim live execution --------------------------


def test_fixture_mode_blocks_live_calls_by_contract() -> None:
    assert RunMode.FIXTURE.allows_live_calls() is False
    assert RunMode.OFFLINE_EVALUATION.allows_live_calls() is False
    assert RunMode.AUTHORIZED_LIVE.allows_live_calls() is True
    from vouch_agent.errors import LiveCallBlockedError

    with pytest.raises(LiveCallBlockedError):
        RunMode.FIXTURE.fail_closed_live()
