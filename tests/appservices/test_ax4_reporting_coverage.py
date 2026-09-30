"""ReportService coverage consumers use EFFECTIVE verified status (M4 A2).

Inverted from the M3 reporting defect: a deserialized (reloaded) unverified
``runner-integrated`` claim used to appear under ``runner-integrated`` in
``appservices/reporting.py`` because the summary read the raw persisted
status. The public review report must show it as unverified until resolution
succeeds, with the layer/mode dimensions reported separately.
"""

from __future__ import annotations

import importlib.util as _il
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_TESTS = _HERE.parent
_ACCEPTANCE = _TESTS / "regression" / "acceptance"
if str(_ACCEPTANCE) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE))

_spec = _il.spec_from_file_location(
    "ax4_m3_runtime_helpers", _TESTS / "regression" / "runtime" / "m3_runtime_helpers.py"
)
_m3 = _il.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_m3)
init_native_project = _m3.init_native_project

from support import (  # noqa: E402
    ChannelAdapter,
    FakeArtifacts,
    FakeJournal,
    FakeLedger,
    FakeStore,
    make_baseline,
    make_candidate,
    rubric,
    seed_inputs,
)

from vouch_agent.adapters.fixture_adapter import FixturePack  # noqa: E402
from vouch_agent.appservices.packs import MANIFEST_KIND  # noqa: E402
from vouch_agent.appservices.reporting import ReportService  # noqa: E402
from vouch_agent.appservices.workspace import ProjectWorkspace  # noqa: E402
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack  # noqa: E402
from vouch_agent.contracts.common import Role, RunMode, digest_of  # noqa: E402
from vouch_agent.contracts.project import ProjectSpec  # noqa: E402
from vouch_agent.controller import VouchController  # noqa: E402
from vouch_agent.evaluation.attestation_evidence import descriptor_digest  # noqa: E402
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass  # noqa: E402
from vouch_agent.workflows.manifest import (  # noqa: E402
    DECLARED_CHOOSE_WORKFLOWS,
    CoverageLayer,
    CoverageStatus,
    RunnerIntegrationRecord,
    WorkflowManifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "choose"


def _genuine_w_c3_run():
    """A real controller-produced W-C3 selection run in port-fake stores."""
    store, artifacts, ledger, journal = FakeStore(), FakeArtifacts(), FakeLedger(1.0), FakeJournal()
    project = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-ax4-reporting",
            "name": "ax4 reporting regression",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "W-C3",
                    "name": "Compare",
                    "mainObjective": "score",
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
            allowed_resources=frozenset(),
            resource_prefixes=("case-", "eval_"),
            max_reservation_usd=0.5,
        ),
        ledger,
        journal,
    )
    controller = VouchController(project, store, artifacts, ledger, journal, broker)
    controller.init()
    selection_case, final_case = "case-w-c3-sel-1", "case-w-c3-fin-1"
    pack = TaskPack(
        pack_id="pack-ax4-reporting",
        workflow_id="W-C3",
        cases=(
            TaskCase(
                case_id=selection_case,
                workflow_id="W-C3",
                split=CaseSplit.SELECTION_VALIDATION,
                group_id="family-ax4",
                input_digest=digest_of({"case": selection_case}),
            ),
            TaskCase(
                case_id=final_case,
                workflow_id="W-C3",
                split=CaseSplit.FINAL_ACCEPTANCE,
                group_id="family-ax4",
                input_digest=digest_of({"case": final_case}),
            ),
        ),
        mode=RunMode.FIXTURE,
        notes="synthetic reporting regression pack",
    )
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), "W-C3")
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = ChannelAdapter()
    run = controller.run_paired_evaluation(
        workflow_id="W-C3",
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )
    return store, artifacts, run, adapter


def _promoted_manifest_record() -> dict[str, Any]:
    """A manifest genuinely promoted against durable controller evidence."""
    from vouch_agent.contracts.evaluation import AttemptRecord

    manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
    store, artifacts, run, adapter = _genuine_w_c3_run()
    attempt_digests = []
    revision = ""
    for attempt in run.attempts:
        stored = AttemptRecord.from_dict(store.load("attempt", attempt.attempt_id))
        attempt_digests.append(stored.digest())
        sealed = json.loads(artifacts.get(stored.output_digest).decode("utf-8"))
        revision = sealed["runnerVersion"]
    record = RunnerIntegrationRecord(
        workflow_id="W-C3",
        runner_id=adapter.adapter_id,
        runner_descriptor_digest=descriptor_digest(adapter.describe()),
        verified_by="acceptance-owner",
        statement="controller-observed describe() + verified execution evidence",
        runner_revision=revision,
        case_pack_digest=run.case_set_digest,
        coverage_layer=CoverageLayer.LOGIC,
        provider_mode="fixture",
        verified_run_id=run.run_id,
        verified_attempt_digests=tuple(attempt_digests),
        evidence_digest="sha256:" + "0" * 64,
    )
    promoted = manifest.with_runner_integration(
        record, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
    )
    assert promoted.effective_status("W-C3") == "runner-integrated"
    return promoted.to_dict()


def test_reload_and_public_review_show_unverified_not_integrated(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        # the promoted manifest is PERSISTED the way a fixture-pack import does
        workspace.store.save(MANIFEST_KIND, "choose", _promoted_manifest_record())
        service = ReportService(workspace)

        # raw persisted claim vs effective consumer view
        coverage = service.workflow_coverage()
        assert coverage["runner-integrated"] == []
        assert coverage["unverified-integration"] == ["W-C3"]
        assert "W-C3" not in coverage["fixture-covered"]  # raw claim is runner-integrated
        assert manifest_entry_status(workspace) == "runner-integrated"

        detail = service.coverage_detail()
        assert detail["W-C3"] == {
            "status": "unverified-integration",
            "layer": "logic",
            "mode": "fixture",
        }

        # the PUBLIC review report carries the effective view (M3 defect:
        # reporting.py used to list the deserialized claim as integrated)
        report = service.review()
        assert report.coverage["runner-integrated"] == []
        assert report.coverage["unverified-integration"] == ["W-C3"]
        assert report.coverage_dimensions["W-C3"]["status"] == "unverified-integration"
        assert report.coverage_dimensions["W-C3"]["layer"] == "logic"
        assert report.coverage_dimensions["W-C2"]["layer"] is None
    finally:
        workspace.close()


def test_review_without_manifest_reports_declared_only(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        service = ReportService(workspace)
        coverage = service.workflow_coverage()
        assert coverage["runner-integrated"] == []
        assert coverage["unverified-integration"] == []
        assert coverage[CoverageStatus.DECLARED.value] == [
            spec.workflow_id for spec in DECLARED_CHOOSE_WORKFLOWS
        ]
        detail = service.coverage_detail()
        assert set(detail) == {spec.workflow_id for spec in DECLARED_CHOOSE_WORKFLOWS}
        assert all(
            entry == {"status": "declared", "layer": None, "mode": None}
            for entry in detail.values()
        )
    finally:
        workspace.close()


def test_summary_shape_stays_cli_formattable(tmp_path: Path) -> None:
    """The CLI/TUI format ``f"{len(ids)} {status}"`` over ``sorted(items())``
    — the summary stays a plain dict[str, list[str]]."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        coverage = ReportService(workspace).workflow_coverage()
        assert all(isinstance(ids, list) for ids in coverage.values())
        rendered = ", ".join(f"{len(ids)} {status}" for status, ids in sorted(coverage.items()))
        assert "9 declared" in rendered
    finally:
        workspace.close()


def manifest_entry_status(workspace: ProjectWorkspace) -> str:
    """The RAW persisted entry status (for contrast with the effective view)."""
    manifest = ReportService(workspace).manifest()
    assert manifest is not None
    return manifest.entry("W-C3").status.value


def test_promoted_record_fails_reverification_after_proof_replacement() -> None:
    """M4 review, digest-binding fix: a promoted browser record's cited proof
    could be replaced with different valid bytes under the same record id and
    the OLD evidence digest still re-verified after a fresh reload (the digest
    bound only record ids/status). Binding the complete validated layer-record
    content — journeys, matrix, case ids and every cited artifact digest —
    must invalidate the claim instead."""
    import importlib.util as _il
    from pathlib import Path as _Path

    import pytest as _pytest

    from vouch_agent.errors import ContractError

    # Reuse the attestation helpers for the claim/record shapes.
    helper_path = (
        _Path(__file__).parents[1] / "evaluation" / "test_attestation_layer_records.py"
    )
    spec = _il.spec_from_file_location("attestation_helpers", helper_path)
    helpers = _il.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    from vouch_agent.contracts.cases import TaskPack
    from vouch_agent.evaluation.attestation_evidence import KIND_BROWSER_EVIDENCE
    from vouch_agent.workflows.manifest import (
        CoverageStatus,
        RunnerIntegrationRecord,
        WorkflowEntry,
        WorkflowManifest,
    )

    store, artifacts, run, adapter = _genuine_w_c3_run()
    pack = TaskPack.from_dict(store.load("task-pack", store.list_ids("task-pack")[0]))
    claim = helpers._claim(store, artifacts, run, layer="browser")
    helpers._browser_record(
        store,
        artifacts,
        "be-public",
        workflow_id="W-C3",
        pack=pack,
        case_ids=[a.case_id for a in run.attempts],
        revision=claim.runner_revision,
    )
    manifest = WorkflowManifest(
        entries=(
            WorkflowEntry(workflow_id="W-C3", name="Compare", status=CoverageStatus.DECLARED),
        ),
        guardrails=(),
    )
    record = RunnerIntegrationRecord(
        workflow_id=claim.workflow_id,
        runner_id=claim.runner_id,
        runner_descriptor_digest=claim.runner_descriptor_digest,
        verified_by="reviewer",
        statement="digest-binding regression",
        runner_revision=claim.runner_revision,
        case_pack_digest=pack.digest(),
        coverage_layer=CoverageLayer.BROWSER,
        provider_mode=claim.provider_mode,
        verified_run_id=claim.verified_run_id,
        verified_attempt_digests=claim.verified_attempt_digests,
        evidence_digest="sha256:" + "0" * 64,
    )
    descriptor = adapter.describe()
    promoted = manifest.with_runner_integration(
        record, store=store, artifacts=artifacts, observed_descriptor=descriptor
    )
    restored = WorkflowManifest.from_dict(promoted.to_dict())
    assert (
        restored.verify_integrations(store, artifacts, descriptor).effective_status("W-C3")
        == "runner-integrated"
    )

    changed = store.load(KIND_BROWSER_EVIDENCE, "be-public")
    replacement = artifacts.put(b"new screenshot after promotion")
    changed["artifactDigests"] = [replacement]
    for journey in changed["journeys"]:
        journey["screenshotDigests"] = [replacement]
    store.save(KIND_BROWSER_EVIDENCE, "be-public", changed)

    with _pytest.raises(ContractError, match="evidence digest"):
        restored.verify_integrations(store, artifacts, descriptor)
