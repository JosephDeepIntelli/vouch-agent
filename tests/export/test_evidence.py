"""Evidence/rollback export: determinism, digest verification, mode honesty."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from vouch_agent.contracts.candidate import AgentVersion, Candidate, ChangeType
from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import RunMode
from vouch_agent.contracts.evaluation import (
    AttemptRecord,
    AttemptStatus,
    EvaluationRun,
    Rubric,
    Side,
)
from vouch_agent.contracts.journal import CostCategory, CostEntry, EventKind, EventRecord
from vouch_agent.contracts.project import WorkflowDeclaration
from vouch_agent.errors import ContractError, DigestMismatchError
from vouch_agent.evaluation.comparator import compare_run
from vouch_agent.evaluation.verdict import decide, freeze_rubric
from vouch_agent.export.evidence import (
    MANIFEST_NAME,
    export_evidence_package,
    export_rollback_package,
    read_package,
    verify_package,
)

D = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
D3 = "sha256:" + "c" * 64
ENV = "sha256:" + "d" * 64
EVIDENCE = "sha256:" + "e" * 64

# Singletons: export determinism is "same records in -> same bytes out", so
# every test below reuses the exact same record objects.

WORKFLOW = WorkflowDeclaration(
    workflow_id="wf-compare",
    name="Compare",
    main_objective="submission quality",
    guardrails=("severe-errors",),
)

RUBRIC = freeze_rubric(
    Rubric(main_metric="score", hard_guardrails=("severe-errors",), thresholds={}),
    "owner",
)

ATTEMPTS = [
    AttemptRecord(
        attempt_id="att_b1",
        run_id="run_1",
        side=Side.BASELINE,
        case_id="case_a",
        status=AttemptStatus.OK,
        started_at="2026-09-28T10:00:00.000+00:00",
        cost_usd=0.01,
        usage={"score": 0.5},
    ),
    AttemptRecord(
        attempt_id="att_c1",
        run_id="run_1",
        side=Side.CANDIDATE,
        case_id="case_a",
        status=AttemptStatus.OK,
        started_at="2026-09-28T10:00:01.000+00:00",
        cost_usd=0.02,
        usage={"score": 0.7},
    ),
    # a failed attempt: kept, billed, visible in the bundle
    AttemptRecord(
        attempt_id="att_c2",
        run_id="run_1",
        side=Side.CANDIDATE,
        case_id="case_b",
        status=AttemptStatus.FAILED,
        started_at="2026-09-28T10:00:02.000+00:00",
        cost_usd=0.005,
        usage={},
        error="adapter crashed",
    ),
    AttemptRecord(
        attempt_id="att_b2",
        run_id="run_1",
        side=Side.BASELINE,
        case_id="case_b",
        status=AttemptStatus.OK,
        started_at="2026-09-28T10:00:03.000+00:00",
        cost_usd=0.01,
        usage={"score": 0.4},
    ),
]

RUN = EvaluationRun(
    run_id="run_1",
    workflow_id="wf-compare",
    split=CaseSplit.SELECTION_VALIDATION,
    baseline_digest=D,
    candidate_digest=D2,
    case_set_digest=D3,
    rubric_digest=RUBRIC.digest(),
    environment_digest=ENV,
    attempts=tuple(ATTEMPTS),
    mode=RunMode.FIXTURE,
    execution_status="completed",
    created_at="2026-09-28T10:05:00.000+00:00",
)

SUMMARY = compare_run(RUN, WORKFLOW, RUBRIC)

EVENTS = [
    EventRecord(
        event_id="evt_1",
        kind=EventKind.RUN_STARTED,
        subject="run_1",
        mode=RunMode.FIXTURE,
        occurred_at="2026-09-28T10:00:00.000+00:00",
    ),
    EventRecord(
        event_id="evt_2",
        kind=EventKind.ATTEMPT_ENDED,
        subject="att_c2",
        data={"status": "failed"},
        mode=RunMode.FIXTURE,
        occurred_at="2026-09-28T10:00:02.000+00:00",
    ),
]

COSTS = [
    CostEntry(
        entry_id="cost_1",
        category=CostCategory.MODEL,
        subject="run_1",
        amount_usd=0.035,
        mode=RunMode.FIXTURE,
        recorded_at="2026-09-28T10:06:00.000+00:00",
    ),
    CostEntry(
        entry_id="cost_2",
        category=CostCategory.SEARCH,
        subject="att_c2",
        amount_usd=None,
        measurable=False,
        mode=RunMode.FIXTURE,
        recorded_at="2026-09-28T10:06:01.000+00:00",
        note="search pricing unknown",
    ),
]

CANDIDATE = Candidate(
    candidate_id="cand_1",
    parent_version=AgentVersion(
        version_id="ver_1", source_ref="git:abc123", model_id="fixture-model"
    ),
    change_type=ChangeType.PROMPT_DELTA,
    delta="Ask for bullet-list submissions.",
    rationale="fewer rework rounds",
    expected_impact="score up",
    proposer="model-worker",
    created_at="2026-09-28T09:00:00.000+00:00",
)

DECISION = decide(
    SUMMARY,
    RUN,
    RUBRIC,
    owner="yu-pei",
    evidence_digest=EVIDENCE,
    allowed_release_scope="none",
    note="fixture evidence only",
)


def _export(tmp_path, name="pkg", decision=DECISION):
    return export_evidence_package(
        destination=tmp_path / name,
        run=RUN,
        summary=SUMMARY,
        rubric=RUBRIC,
        decision=decision,
        candidate=CANDIDATE,
        events=EVENTS,
        cost_entries=COSTS,
    )


# -- determinism ---------------------------------------------------------------


def test_same_inputs_produce_identical_packages(tmp_path):
    first = _export(tmp_path, "pkg-a")
    second = _export(tmp_path, "pkg-b")
    assert first.manifest_digest == second.manifest_digest
    assert first.file_digests == second.file_digests
    assert (first.path / MANIFEST_NAME).read_bytes() == (second.path / MANIFEST_NAME).read_bytes()


def test_manifest_records_mode_run_and_verdict(tmp_path):
    package = _export(tmp_path)
    manifest = json.loads((package.path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["mode"] == "fixture"
    assert manifest["runId"] == "run_1"
    assert manifest["workflowId"] == "wf-compare"
    # the failed candidate attempt is a measured-bad outcome: rejected, not
    # quietly averaged into an acceptance
    assert manifest["verdict"] == "rejected"
    assert manifest["syntheticOnly"] is True
    assert MANIFEST_NAME not in manifest["files"]  # no self-reference


def test_package_contains_all_attempts_including_failures(tmp_path):
    package = _export(tmp_path)
    attempts = json.loads((package.path / "attempts.json").read_text(encoding="utf-8"))
    ids = {a["attemptId"] for a in attempts["attempts"]}
    assert ids == {"att_b1", "att_c1", "att_b2", "att_c2"}
    failed = next(a for a in attempts["attempts"] if a["attemptId"] == "att_c2")
    assert failed["status"] == "failed" and failed["error"] == "adapter crashed"


def test_cost_journal_keeps_unmeasurable_entries_explicit(tmp_path):
    package = _export(tmp_path)
    costs = json.loads((package.path / "cost-journal.json").read_text(encoding="utf-8"))
    unmeasurable = [c for c in costs["entries"] if c["entryId"] == "cost_2"]
    assert unmeasurable[0]["amountUsd"] is None
    assert unmeasurable[0]["measurable"] is False


def test_candidate_delta_text_is_the_diff(tmp_path):
    package = _export(tmp_path)
    delta = (package.path / "candidate-delta.txt").read_text(encoding="utf-8")
    assert delta == "Ask for bullet-list submissions."


def test_export_without_decision(tmp_path):
    package = _export(tmp_path, "no-decision", decision=None)
    assert not (package.path / "decision.json").exists()
    manifest = json.loads((package.path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["verdict"] is None


def test_export_refuses_nonempty_destination(tmp_path):
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "junk").write_text("x")
    with pytest.raises(ContractError, match="not empty"):
        export_evidence_package(destination=dest, run=RUN, summary=SUMMARY, rubric=RUBRIC)


# -- verification on read -----------------------------------------------------------


def test_verify_package_passes_and_read_package_roundtrips(tmp_path):
    package = _export(tmp_path)
    verification = verify_package(package.path, expected_manifest_digest=package.manifest_digest)
    assert verification.manifest_digest == package.manifest_digest
    assert verification.mode is RunMode.FIXTURE
    assert verification.run_id == "run_1"

    bundle = read_package(package.path)
    assert bundle["records"]["run.json"]["runId"] == "run_1"
    assert bundle["records"]["comparison.json"]["completePairs"] == 1


def test_tampered_file_fails_verification(tmp_path):
    package = _export(tmp_path)
    target = package.path / "attempts.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    data["attempts"][0]["status"] = "ok-but-better"  # rewrite history
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(DigestMismatchError, match=r"attempts\.json"):
        verify_package(package.path)


def test_deleted_file_fails_verification(tmp_path):
    package = _export(tmp_path)
    (package.path / "decision.json").unlink()
    with pytest.raises(ContractError, match="does not match manifest"):
        verify_package(package.path)


def test_injected_extra_file_fails_verification(tmp_path):
    package = _export(tmp_path)
    (package.path / "extra-evidence.json").write_text("{}")
    with pytest.raises(ContractError, match="does not match manifest"):
        verify_package(package.path)


def test_forged_manifest_fails_against_external_anchor(tmp_path):
    """Files may all be intact while the manifest headline itself is forged.

    The manifest cannot vouch for its own bytes; the decision's
    evidence_digest anchors it from outside (§5).
    """
    package = _export(tmp_path)
    anchored = package.manifest_digest
    verify_package(package.path, expected_manifest_digest=anchored)  # clean pass

    manifest_path = package.path / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verdict"] = "accepted"  # forge the headline
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    with pytest.raises(DigestMismatchError, match="expected"):
        verify_package(package.path, expected_manifest_digest=anchored)


def test_missing_manifest_fails_verification(tmp_path):
    empty = tmp_path / "empty-pkg"
    empty.mkdir()
    with pytest.raises(ContractError, match="manifest"):
        verify_package(empty)


# -- mode honesty (§13.2) --------------------------------------------------------------


def test_mixed_mode_cost_entries_refused(tmp_path):
    live_cost = CostEntry(
        entry_id="cost_live",
        category=CostCategory.MODEL,
        subject="run_1",
        amount_usd=0.5,
        mode=RunMode.AUTHORIZED_LIVE,
        recorded_at="2026-09-28T11:00:00.000+00:00",
    )
    with pytest.raises(ContractError, match="mix run modes"):
        export_evidence_package(
            destination=tmp_path / "mixed",
            run=RUN,
            summary=SUMMARY,
            rubric=RUBRIC,
            cost_entries=[*COSTS, live_cost],
        )


def test_mixed_mode_events_refused(tmp_path):
    offline_event = EventRecord(
        event_id="evt_off",
        kind=EventKind.RUN_STARTED,
        subject="run_1",
        mode=RunMode.OFFLINE_EVALUATION,
        occurred_at="2026-09-28T11:00:00.000+00:00",
    )
    with pytest.raises(ContractError, match="mix run modes"):
        export_evidence_package(
            destination=tmp_path / "mixed",
            run=RUN,
            summary=SUMMARY,
            rubric=RUBRIC,
            events=[*EVENTS, offline_event],
        )


def test_rubric_digest_mismatch_refused(tmp_path):
    other = freeze_rubric(
        Rubric(main_metric="score", thresholds={"min-main-improvement": 0.4}), "owner"
    )
    with pytest.raises(DigestMismatchError):
        export_evidence_package(
            destination=tmp_path / "mismatch", run=RUN, summary=SUMMARY, rubric=other
        )


def test_summary_run_mismatch_refused(tmp_path):
    shifted = replace(SUMMARY, run_id="run_other")
    with pytest.raises(ContractError, match="summary"):
        export_evidence_package(
            destination=tmp_path / "mismatch", run=RUN, summary=shifted, rubric=RUBRIC
        )


# -- rollback packages -------------------------------------------------------------------


def test_rollback_package_describes_revert_with_no_deploy_action(tmp_path):
    package = export_rollback_package(
        destination=tmp_path / "rollback", candidate=CANDIDATE, mode=RunMode.FIXTURE
    )
    rollback = json.loads((package.path / "rollback.json").read_text(encoding="utf-8"))
    assert rollback["revert"]["deployAction"] == "none"
    assert rollback["revert"]["deployActionPerformed"] is False
    assert rollback["revert"]["sourceRef"] == "git:abc123"
    assert rollback["parentVersion"]["versionId"] == "ver_1"
    assert rollback["candidateDeltaDigest"] == CANDIDATE.delta_digest()
    assert "Vouch performs no deployment" in rollback["warning"]
    assert package.mode is RunMode.FIXTURE
    verify_package(package.path)


def test_rollback_package_is_deterministic(tmp_path):
    a = export_rollback_package(
        destination=tmp_path / "rb-a", candidate=CANDIDATE, mode=RunMode.FIXTURE
    )
    b = export_rollback_package(
        destination=tmp_path / "rb-b", candidate=CANDIDATE, mode=RunMode.FIXTURE
    )
    assert a.manifest_digest == b.manifest_digest
    assert (a.path / "rollback.json").read_bytes() == (b.path / "rollback.json").read_bytes()


# -- A7: rollback restores the MEASURED baseline, refusing to guess -----------------------


def test_rollback_refuses_when_lineage_and_measured_baseline_disagree(tmp_path):
    """The coordinator counterexample: a candidate parented to v0 while v1 is
    the measured baseline. The export refuses rather than restoring v0."""
    measured = AgentVersion(version_id="ver_2", source_ref="git:new")
    with pytest.raises(ContractError, match="refuses to guess"):
        export_rollback_package(
            destination=tmp_path / "rollback",
            candidate=CANDIDATE,
            mode=RunMode.FIXTURE,
            measured_baseline=measured,
        )
    assert not (tmp_path / "rollback").exists() or not any((tmp_path / "rollback").iterdir())


def test_rollback_restores_the_measured_baseline_when_they_agree(tmp_path):
    measured = replace(CANDIDATE.parent_version)  # same digest as the parent
    package = export_rollback_package(
        destination=tmp_path / "rollback",
        candidate=CANDIDATE,
        mode=RunMode.FIXTURE,
        measured_baseline=measured,
    )
    rollback = json.loads((package.path / "rollback.json").read_text(encoding="utf-8"))
    assert rollback["revert"]["sourceRef"] == "git:abc123"
    assert rollback["measuredBaseline"]["versionId"] == "ver_1"
    assert rollback["parentVersion"]["versionId"] == "ver_1"
    verify_package(package.path)


def test_rollback_without_measured_baseline_keeps_legacy_shape(tmp_path):
    """Callers that cannot state the measured baseline (lead wiring pending)
    still get a package; the lineage binding is enforced upstream at
    evaluation/decision/approval, so the parent is the measured baseline."""
    package = export_rollback_package(
        destination=tmp_path / "rollback", candidate=CANDIDATE, mode=RunMode.FIXTURE
    )
    rollback = json.loads((package.path / "rollback.json").read_text(encoding="utf-8"))
    assert rollback["measuredBaseline"] is None
    assert rollback["revert"]["sourceRef"] == "git:abc123"
    verify_package(package.path)
