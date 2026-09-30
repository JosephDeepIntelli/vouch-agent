"""Lead-owned integration: the vertical improvement flow on real persistence.

Controller (lead) + SQLite store/journal/budget ledger + role-gated splits +
comparator/verdict + evidence export (specialist S implementations), driven
end to end offline: pack import -> baseline -> candidate -> sealed evaluation
-> outcomes/verdict -> acceptance on the final split -> decision anchored to
an exported evidence package -> approval -> release record -> rollback export.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch_agent.adapters.base import AdapterDescriptor, AdapterExecution
from vouch_agent.contracts.candidate import AgentVersion, Candidate, CandidateState, ChangeType
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack
from vouch_agent.contracts.common import Role, RunMode, digest_of
from vouch_agent.contracts.decision import ReleaseRecord
from vouch_agent.contracts.evaluation import Rubric
from vouch_agent.contracts.project import ProjectSpec
from vouch_agent.controller import VouchController
from vouch_agent.errors import SplitAccessError
from vouch_agent.evaluation import compare_run, decide, run_with_outcomes, verdict_from_summary
from vouch_agent.export import export_evidence_package, export_rollback_package, verify_package
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass
from vouch_agent.storage import (
    FileArtifactStore,
    SqliteBudgetLedger,
    SqliteJournal,
    SqliteMetadataStore,
    load_task_pack,
    save_task_pack,
)


class OfflineCompareAdapter:
    """Deterministic adapter: candidate strictly better on the dev metric,
    with guardrail channels surfaced on one dev case for the baseline only."""

    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(adapter_id="offline-compare-fixture@1", workflows=("wf-compare",))

    def prepare(self, run_id: str, mode: RunMode) -> None:
        return None

    def execute(self, *, run_id, attempt_id, workflow_id, case_input, mode) -> AdapterExecution:
        case_id = case_input["caseId"]
        is_candidate = case_input.get("side") == "candidate"
        usage: dict = {
            "costUsd": 0.01,
            "supported_claim_rate": 0.95 if is_candidate else 0.70,
            # Explicit checked-pass hard channel on the candidate (review A4:
            # synthetic adapters must measure the REQUIRED channels; an
            # unmeasured hard channel is unknown, never an implicit pass).
            "guardrail:fabricated-citation": 0,
        }
        if not is_candidate and case_id == "case-dev-1":
            usage["guardrail:fabricated-citation"] = 1
        return AdapterExecution(
            ok=True,
            outputs={"caseId": case_id, "side": case_input.get("side")},
            usage=usage,
            mode=mode,
            runner_version="offline-compare-fixture@1",
        )

    def collect(self, run_id: str) -> tuple[str, ...]:
        return ()

    def cleanup(self, run_id: str) -> None:
        return None


def workflow():
    return _project().workflow("wf-compare")


def _project() -> ProjectSpec:
    return ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-int",
            "name": "Choose internal integration",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "wf-compare",
                    "name": "Compare",
                    "mainObjective": "supported_claim_rate",
                    "guardrails": ["fabricated-citation"],
                }
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": 5.0},
        }
    )


def _pack() -> TaskPack:
    def case(cid: str, split: CaseSplit) -> TaskCase:
        return TaskCase(
            case_id=cid,
            workflow_id="wf-compare",
            split=split,
            group_id="family-a",
            input_digest=digest_of({"case": cid}),
        )

    # Selection-validation (not development) is the promotion split (A5).
    return TaskPack(
        pack_id="pack-int",
        workflow_id="wf-compare",
        cases=(
            case("case-dev-1", CaseSplit.SELECTION_VALIDATION),
            case("case-dev-2", CaseSplit.SELECTION_VALIDATION),
            case("case-acc-1", CaseSplit.FINAL_ACCEPTANCE),
            case("case-acc-2", CaseSplit.FINAL_ACCEPTANCE),
        ),
        mode=RunMode.FIXTURE,
        notes="synthetic integration pack",
    )


@pytest.fixture()
def controller(tmp_path: Path) -> VouchController:
    store = SqliteMetadataStore(tmp_path / "meta.sqlite")
    artifacts = FileArtifactStore(tmp_path)
    journal = SqliteJournal(tmp_path / "journal.sqlite")
    ledger = SqliteBudgetLedger(tmp_path / "budget.sqlite", total_usd_cap=5.0)
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
            ledger.reservations()
            and next(r for r in ledger.reservations() if r.reservation_id == rid).status
        ),
    )
    return VouchController(_project(), store, artifacts, ledger, journal, broker)


def test_vertical_flow_on_sqlite(controller: VouchController, tmp_path: Path) -> None:
    controller.init()
    pack = _pack()
    # Materialize the synthetic case inputs: the controller digest-verifies
    # the BYTES behind every input digest before execution (review A6).
    from vouch_agent.contracts.common import canonical_json

    for case in pack.cases:
        controller.artifacts.put(canonical_json({"case": case.case_id}).encode("utf-8"))
    save_task_pack(controller.store, pack)
    controller.import_pack(
        load_task_pack(controller.store, pack.pack_id, Role.EVALUATOR), Role.EVALUATOR
    )

    # proposer never sees acceptance cases through the same store
    proposer_view = load_task_pack(controller.store, pack.pack_id, Role.PROPOSER)
    assert not proposer_view.cases_in(CaseSplit.FINAL_ACCEPTANCE)
    from vouch_agent.storage import load_case

    with pytest.raises(SplitAccessError):
        load_case(controller.store, "case-acc-1", Role.PROPOSER)

    baseline = AgentVersion(version_id="v0", source_ref="git:abc")
    controller.record_baseline(baseline, "wf-compare")
    frozen = Rubric(
        main_metric="supported_claim_rate",
        direction="increase",
        thresholds={"min-main-improvement": 0.1, "min-complete-pairs": 2},
        hard_guardrails=("fabricated-citation",),
    )
    rubric_digest = controller.freeze_rubric(frozen, frozen_by="product-owner")
    frozen_record = controller.rubric(rubric_digest)

    candidate = Candidate(
        candidate_id="cand-int",
        parent_version=baseline,
        change_type=ChangeType.PROMPT_DELTA,
        delta="+ require citations for every claim",
        rationale="unsupported claims clustered in Compare",
        expected_impact="supported_claim_rate up, no fabricated citations",
        proposer="proposer-agent",
    )
    controller.propose(candidate)
    controller.seal("cand-int")

    adapter = OfflineCompareAdapter()
    run = controller.run_paired_evaluation(
        workflow_id="wf-compare",
        candidate_id="cand-int",
        baseline=baseline,
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )

    # comparison attaches its annotated pairs onto the run record
    summary = compare_run(run, workflow(), frozen_record)
    run = run_with_outcomes(run, summary)
    # baseline-only guardrail hits are CONTEXT, never candidate clearance:
    # the candidate carries no hard violations and the baseline hit is
    # surfaced in the uncertainty note instead of being averaged away.
    assert not summary.hard_violations
    assert "baseline violates guardrails" in summary.uncertainty_note
    verdict = verdict_from_summary(summary, frozen_record)
    assert verdict.value == "accepted"
    controller.mark_evaluated("cand-int")

    acc_run = controller.run_final_acceptance(
        workflow_id="wf-compare",
        candidate_id="cand-int",
        baseline=baseline,
        pack=pack,
        rubric_digest=rubric_digest,
        adapter=adapter,
        role=Role.ACCEPTANCE_OWNER,
    )
    acc_summary = compare_run(acc_run, workflow(), frozen_record)
    acc_run = run_with_outcomes(acc_run, acc_summary)
    assert verdict_from_summary(acc_summary, frozen_record).value == "accepted"

    # export evidence FIRST, then decide anchored to the manifest digest;
    # the controller re-loads the durable run and re-computes the verdict, so
    # the export record must be persisted for the decision to be anchored.
    exported = export_evidence_package(
        destination=tmp_path / "evidence",
        run=acc_run,
        summary=acc_summary,
        rubric=frozen_record,
        candidate=controller.candidate("cand-int"),
        events=controller.journal.events(),
        cost_entries=controller.journal.cost_entries(),
    )
    controller.store.save(
        "run-export",
        acc_run.run_id,
        {"manifestDigest": exported.manifest_digest, "path": str(exported.path)},
    )
    decision = decide(
        acc_summary,
        acc_run,
        frozen_record,
        owner="ana",
        evidence_digest=exported.manifest_digest,
    )
    controller.record_decision(
        decision, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=acc_run.run_id
    )
    assert controller.candidate("cand-int").state is CandidateState.ACCEPTED

    # tamper detection on the exported package
    verify_package(exported.path)
    attempts_file = exported.path / "attempts.json"
    attempts_file.write_text(attempts_file.read_text() + " ")
    from vouch_agent.errors import DigestMismatchError

    with pytest.raises(DigestMismatchError):
        verify_package(exported.path)

    controller.record_approval("cand-int", role=Role.RELEASE_OWNER, binding=decision.binding)
    controller.record_release(
        ReleaseRecord(
            release_id="rel-int",
            candidate_digest=decision.candidate_digest,
            deployed_version="choose@v9",
            deployed_by="roger",
        ),
        role=Role.RELEASE_OWNER,
    )
    assert controller.candidate("cand-int").state is CandidateState.RELEASED

    rollback = export_rollback_package(
        destination=tmp_path / "rollback",
        candidate=controller.candidate("cand-int"),
        mode=RunMode.FIXTURE,
        decision=decision,
    )
    verify_package(rollback.path)

    # full-burden accounting: every attempt priced, nothing hidden
    report = controller.full_cost_usd()
    assert report["unmeasurableEntries"] == []
    assert report["measuredUsd"] > 0
    assert len(controller.journal.cost_entries()) >= len(run.attempts) + len(acc_run.attempts)
