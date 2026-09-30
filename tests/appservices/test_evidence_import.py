"""Durable ingestion of the ACTUAL retained Choose evidence exports (M4 §D).

These tests import the real retained exporter files (browser-records.json with
its 32 screenshots, regression-records.json with its 69 passing suites) — not
hand-built objects that merely satisfy the resolver's schema — and prove:

* validation refuses wrong formats, dirty provenance, missing/tampered
  screenshots, unmapped workflows, unknown case mappings and zero tests;
* persisted records are durable: a FRESH PROCESS re-opens the workspace,
  digest-verifies every screenshot byte, and resolves browser coverage through
  the public promotion path against a run that actually reports the tested
  source revision;
* the real tested source identity binds: a run reporting a different (later)
  revision cannot promote the historical browser evidence;
* W-C9 stays regression-only.

Skips explicitly when the retained evidence directory is absent.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _evidence_dir() -> Path:
    """Locate the retained Choose evidence export directory.

    Checked in order: an explicit override, the workspace sibling of this
    checkout (worktrees live under /tmp, so the MAIN repo's sibling is the
    documented retained location).
    """
    import os

    candidates = [
        Path(os.environ.get("VOUCH_EVIDENCE_DIR", "/nonexistent")),
        _REPO.parent / "vouch-m4-choose-evidence",
        Path.home() / "dev/products/deepintelli/vouch-m4-choose-evidence",
    ]
    for candidate in candidates:
        if (candidate / "browser-records.json").is_file() and (
            candidate / "regression-records.json"
        ).is_file():
            return candidate
    return candidates[1]


_EVIDENCE_DIR = _evidence_dir()
_BROWSER_EXPORT = _EVIDENCE_DIR / "browser-records.json"
_CONTROL_EXPORT = _EVIDENCE_DIR / "regression-records.json"

_ACCEPTANCE = _REPO / "tests" / "regression" / "acceptance"
if str(_ACCEPTANCE) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE))

from support import make_baseline, make_candidate  # noqa: E402

from vouch_agent.appservices.evidence_import import (  # noqa: E402
    import_browser_evidence,
    import_control_evidence,
)
from vouch_agent.appservices.workspace import ProjectWorkspace  # noqa: E402
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack  # noqa: E402
from vouch_agent.contracts.common import (  # noqa: E402
    Role,
    RunMode,
    canonical_json,
    digest_bytes,
    digest_of,
)
from vouch_agent.contracts.project import ProjectSpec  # noqa: E402
from vouch_agent.errors import ContractError  # noqa: E402
from vouch_agent.evaluation.attestation_evidence import (  # noqa: E402
    KIND_BROWSER_EVIDENCE,
    KIND_CONTROL_REGRESSION,
    resolve_attestation_evidence,
)
from vouch_agent.storage import save_task_pack  # noqa: E402

_BROWSER_WORKFLOWS = ("W-C1", "W-C4", "W-C5", "W-C6", "W-C7", "W-C8")
_RETAINED_COMMIT = "e522ca4760979da505b3ce1dd7cab39e72225443"


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "attestation_helpers",
        _REPO / "tests" / "evaluation" / "test_attestation_layer_records.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace(tmp_path: Path) -> ProjectWorkspace:
    spec = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-evidence-import",
            "name": "evidence import regression",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": workflow_id,
                    "name": workflow_id,
                    "mainObjective": "score",
                }
                for workflow_id in (*_BROWSER_WORKFLOWS, "W-C9")
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": 5.0},
        }
    )
    return ProjectWorkspace.create(tmp_path, spec)


def _pack_for_workflow(workspace: ProjectWorkspace, workflow_id: str) -> TaskPack:
    payload = {"schemaVersion": "1", "caseId": f"case-{workflow_id}", "workflowId": workflow_id}
    workspace.artifacts.put(canonical_json(payload).encode("utf-8"))
    pack = TaskPack(
        pack_id=f"pack-{workflow_id}",
        workflow_id=workflow_id,
        cases=(
            TaskCase(
                case_id=f"case-{workflow_id}",
                workflow_id=workflow_id,
                split=CaseSplit.SELECTION_VALIDATION,
                group_id=f"synthetic-{workflow_id}",
                input_digest=digest_of(payload),
                source_refs=(workflow_id,),
                synthetic=True,
            ),
        ),
        mode=RunMode.FIXTURE,
        notes="SYNTHETIC pack binding imported evidence to this project",
    )
    save_task_pack(workspace.store, pack)
    workspace.controller().import_pack(pack, Role.EVALUATOR)
    return pack


def _pack_refs(workspace: ProjectWorkspace) -> dict[str, str]:
    """Create + import each workflow's pack, then return the id mapping."""
    for workflow in _BROWSER_WORKFLOWS:
        _pack_for_workflow(workspace, workflow)
    return {workflow: f"pack-{workflow}" for workflow in _BROWSER_WORKFLOWS}


def _retained_available() -> bool:
    return _BROWSER_EXPORT.is_file() and _CONTROL_EXPORT.is_file()


@pytest.mark.skipif(not _retained_available(), reason="retained Choose evidence export not present")
class TestBrowserImport:
    def test_imports_the_actual_retained_export_with_verified_screenshots(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        try:
            result = import_browser_evidence(
                workspace,
                export_path=_BROWSER_EXPORT,
                pack_refs=_pack_refs(workspace),
            )
            assert result.tested_commit == _RETAINED_COMMIT
            assert result.journeys == 8
            assert result.screenshots_verified == 32
            assert len(result.records) == 6
            assert all(record.status == "passed" for record in result.records)

            for record in result.records:
                data = workspace.store.load(KIND_BROWSER_EVIDENCE, record.record_id)
                assert data is not None
                assert data["schemaVersion"] == "1"
                assert data["sourceRevision"] == _RETAINED_COMMIT
                assert data["providerMode"] == "fixture"
                pack = TaskPack.from_dict(
                    workspace.store.load("task-pack", f"pack-{record.workflow_id}")
                )
                assert data["casePackDigest"] == pack.digest()
                # Full matrix per journey; every screenshot digest resolves to
                # bytes that still hash to that digest.
                seen: set[str] = set()
                for journey in data["journeys"]:
                    seen.add(journey["journeyId"])
                    assert journey["status"] == "passed"
                    assert len(journey["screenshotDigests"]) == 1
                assert len(seen) > 0
                cells = {(j["journeyId"], j["locale"], j["viewport"]) for j in data["journeys"]}
                for journey_id in seen:
                    for locale in ("en", "zh"):
                        for viewport in ("desktop", "mobile"):
                            assert (journey_id, locale, viewport) in cells
                for digest in data["artifactDigests"]:
                    payload = workspace.artifacts.get(digest)
                    assert digest_bytes(payload) == digest
                for journey in data["journeys"]:
                    for digest in journey["screenshotDigests"]:
                        payload = workspace.artifacts.get(digest)
                        assert digest_bytes(payload) == digest
        finally:
            workspace.close()

    def test_refuses_wrong_format_and_dirty_provenance(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        try:
            raw = json.loads(_BROWSER_EXPORT.read_text())
            raw["recordVersion"] = 99
            wrong = tmp_path / "wrong-version.json"
            wrong.write_text(json.dumps(raw))
            with pytest.raises(ContractError, match="recordVersion"):
                import_browser_evidence(
                    workspace, export_path=wrong, pack_refs=_pack_refs(workspace)
                )

            raw = json.loads(_BROWSER_EXPORT.read_text())
            raw["provenance"]["dirty"] = True
            dirty = tmp_path / "dirty.json"
            dirty.write_text(json.dumps(raw))
            with pytest.raises(ContractError, match="DIRTY"):
                import_browser_evidence(
                    workspace, export_path=dirty, pack_refs=_pack_refs(workspace)
                )
        finally:
            workspace.close()

    def test_refuses_missing_and_tampered_screenshots(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        try:
            with pytest.raises(ContractError, match="not found under"):
                import_browser_evidence(
                    workspace,
                    export_path=_BROWSER_EXPORT,
                    pack_refs=_pack_refs(workspace),
                    screenshots_dir=tmp_path / "empty",
                )

            raw = json.loads(_BROWSER_EXPORT.read_text())
            victim = raw["journeys"][0]["runs"][0]["screenshot"]
            victim["sha256"] = "sha256:" + "0" * 64
            tampered = tmp_path / "tampered.json"
            tampered.write_text(json.dumps(raw))
            with pytest.raises(ContractError, match="hashes to"):
                import_browser_evidence(
                    workspace,
                    export_path=tampered,
                    pack_refs=_pack_refs(workspace),
                    screenshots_dir=_EVIDENCE_DIR / "screenshots",
                )
        finally:
            workspace.close()

    def test_refuses_unmapped_workflows_and_unknown_case_mappings(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        try:
            _pack_for_workflow(workspace, "W-C1")
            with pytest.raises(ContractError, match="no pack mapping"):
                import_browser_evidence(
                    workspace, export_path=_BROWSER_EXPORT, pack_refs={"W-C1": "pack-W-C1"}
                )
            with pytest.raises(ContractError, match="not in the bound pack"):
                import_browser_evidence(
                    workspace,
                    export_path=_BROWSER_EXPORT,
                    pack_refs=_pack_refs(workspace),
                    journey_cases={"browser_restore_draft": ("case-unknown",)},
                )
            with pytest.raises(ContractError, match="does not match the expected"):
                import_browser_evidence(
                    workspace,
                    export_path=_BROWSER_EXPORT,
                    pack_refs=_pack_refs(workspace),
                    expected_commit="0000000000000000000000000000000000000000",
                )
        finally:
            workspace.close()


@pytest.mark.skipif(not _retained_available(), reason="retained Choose evidence export not present")
class TestControlImport:
    def test_imports_the_actual_retained_regression_export(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        try:
            _pack_for_workflow(workspace, "W-C9")
            result = import_control_evidence(
                workspace, export_path=_CONTROL_EXPORT, pack_ref="pack-W-C9"
            )
            assert result.tested_commit == _RETAINED_COMMIT
            assert result.record.workflow_id == "W-C9"
            assert result.record.status == "passed"
            data = workspace.store.load(KIND_CONTROL_REGRESSION, result.record.record_id)
            assert data is not None
            assert data["sourceRevision"] == _RETAINED_COMMIT
            families = {journey["journeyId"] for journey in data["journeys"]}
            assert families, "no family journeys persisted"
            for digest in data["artifactDigests"]:
                payload = workspace.artifacts.get(digest)
                assert digest_bytes(payload) == digest
        finally:
            workspace.close()

    def test_zero_tests_and_failed_suites_never_become_passed_coverage(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        try:
            _pack_for_workflow(workspace, "W-C9")
            raw = json.loads(_CONTROL_EXPORT.read_text())
            raw["overall"]["tests"] = 0
            raw["suites"] = []
            zero = tmp_path / "zero.json"
            zero.write_text(json.dumps(raw))
            with pytest.raises(ContractError, match="zero tests"):
                import_control_evidence(workspace, export_path=zero, pack_ref="pack-W-C9")

            raw = json.loads(_CONTROL_EXPORT.read_text())
            raw["suites"][0]["fail"] = 1
            raw["suites"][0]["pass"] -= 1
            failing = tmp_path / "failing.json"
            failing.write_text(json.dumps(raw))
            result = import_control_evidence(
                workspace, export_path=failing, pack_ref="pack-W-C9"
            )
            assert result.record.status == "failed", "a failing suite must import as failed"
        finally:
            workspace.close()


class _RevisionAdapter:
    """Deterministic adapter reporting an explicit runner revision.

    Proves the RESOLUTION mechanics: the record's tested source identity must
    equal what this run's sealed exchanges actually report.
    """

    def __init__(self, runner_version: str) -> None:
        self._runner_version = runner_version

    def describe(self):
        # The same descriptor identity the attestation helpers' claims bind,
        # so the public resolver path is exercised unchanged.
        from vouch_agent.adapters.base import AdapterDescriptor

        return AdapterDescriptor(
            adapter_id="channel-fixture@1", workflows=("wf-compare",)
        )

    def prepare(self, run_id: str, mode: RunMode) -> None:
        return None

    def execute(self, *, run_id, attempt_id, workflow_id, case_input, mode):
        from vouch_agent.adapters.base import AdapterExecution

        return AdapterExecution(
            ok=True,
            outputs={"caseId": case_input.get("caseId")},
            usage={"costUsd": 0.001, "score": 1.0},
            mode=mode,
            runner_version=self._runner_version,
        )

    def collect(self, run_id: str) -> tuple[str, ...]:
        return ()

    def cleanup(self, run_id: str) -> None:
        return None


def _genuine_revision_run(workspace: ProjectWorkspace, workflow_id: str, revision: str):
    """A real controller-produced run whose attempts sealed ``revision``."""
    controller = workspace.controller()
    pack = TaskPack.from_dict(workspace.store.load("task-pack", f"pack-{workflow_id}"))
    baseline = make_baseline()
    controller.record_baseline(baseline, workflow_id)
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    rubric_digest = controller.freeze_rubric(
        __import__("vouch_agent.contracts.evaluation", fromlist=["Rubric"]).Rubric(
            main_metric="score",
            direction="increase",
            thresholds={"min-main-improvement": 0.0},
        ),
        frozen_by="ana",
    )
    run = controller.run_paired_evaluation(
        workflow_id=workflow_id,
        candidate_id=candidate.candidate_id,
        baseline=baseline,
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=_RevisionAdapter(revision),
    )
    return run, pack


@pytest.mark.skipif(not _retained_available(), reason="retained Choose evidence export not present")
class TestResolutionBridge:
    def test_matching_source_resolves_and_a_later_build_cannot_promote_history(
        self, tmp_path: Path
    ) -> None:
        helpers = _helpers()
        workspace = _workspace(tmp_path)
        try:
            journey_cases = {
                # The W-C6 journeys exercised this workflow's declared case.
                journey_id: ("case-W-C6",)
                for journey_id in ("browser_handoff_find_compare", "browser_handoff_compare_find")
            }
            import_browser_evidence(
                workspace,
                export_path=_BROWSER_EXPORT,
                pack_refs=_pack_refs(workspace),
                journey_cases=journey_cases,
            )
            run, pack = _genuine_revision_run(workspace, "W-C6", _RETAINED_COMMIT)
            claim = helpers._claim(workspace.store, workspace.artifacts, run, layer="browser")
            assert claim.case_pack_digest == pack.digest()
            evidence = resolve_attestation_evidence(
                claim,
                store=workspace.store,
                artifacts=workspace.artifacts,
                observed_descriptor=_RevisionAdapter(_RETAINED_COMMIT).describe(),
            )
            assert evidence.layer_records, "browser evidence resolved with no layer records"
            assert evidence.layer_record_digests

            # A LATER build (different runner revision) never promotes the
            # historical browser source: the record's tested source identity
            # refuses the foreign revision.
            later_run, _ = _genuine_revision_run(
                workspace, "W-C6", "choose-website-vouch-runner/1 (work-budget v2)"
            )
            later_claim = helpers._claim(
                workspace.store, workspace.artifacts, later_run, layer="browser"
            )
            with pytest.raises(ContractError, match="sourceRevision"):
                resolve_attestation_evidence(
                    later_claim,
                    store=workspace.store,
                    artifacts=workspace.artifacts,
                    observed_descriptor=_RevisionAdapter(
                        "choose-website-vouch-runner/1 (work-budget v2)"
                    ).describe(),
                )
        finally:
            workspace.close()

    def test_fresh_process_reverifies_records_and_promotion(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        try:
            journey_cases = {
                journey_id: ("case-W-C6",)
                for journey_id in ("browser_handoff_find_compare", "browser_handoff_compare_find")
            }
            import_browser_evidence(
                workspace,
                export_path=_BROWSER_EXPORT,
                pack_refs=_pack_refs(workspace),
                journey_cases=journey_cases,
            )
            _genuine_revision_run(workspace, "W-C6", _RETAINED_COMMIT)
        finally:
            workspace.close()

        script = r"""
import importlib.util, json, sys
from pathlib import Path
sys.path.insert(0, "__SRC__")
sys.path.insert(0, "__ACCEPTANCE__")
spec = importlib.util.spec_from_file_location(
    "attestation_helpers", "__HELPERS__")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.controller.service import KIND_EVALUATION
from vouch_agent.evaluation.attestation_evidence import (
    KIND_BROWSER_EVIDENCE, resolve_attestation_evidence)
from vouch_agent.contracts.common import digest_bytes
from vouch_agent.workflows.manifest import (
    CoverageLayer, CoverageStatus, RunnerIntegrationRecord,
    WorkflowEntry, WorkflowManifest)

workspace = ProjectWorkspace.open(Path("__PROJECT__"))
records = workspace.store.list_ids(KIND_BROWSER_EVIDENCE)
assert len(records) == 6, f"expected 6 durable records, found {len(records)}"
verified_screenshots = 0
for record_id in records:
    data = workspace.store.load(KIND_BROWSER_EVIDENCE, record_id)
    assert data["sourceRevision"] == "__COMMIT__"
    for journey in data["journeys"]:
        for digest in journey["screenshotDigests"]:
            payload = workspace.artifacts.get(digest)
            assert digest_bytes(payload) == digest
            verified_screenshots += 1

run_id = workspace.store.list_ids(KIND_EVALUATION)[0]
from vouch_agent.contracts.evaluation import EvaluationRun
run = EvaluationRun.from_dict(workspace.store.load(KIND_EVALUATION, run_id))
claim = helpers._claim(workspace.store, workspace.artifacts, run, layer="browser")
from vouch_agent.adapters.base import AdapterDescriptor
descriptor = AdapterDescriptor(adapter_id="channel-fixture@1", workflows=("wf-compare",))
evidence = resolve_attestation_evidence(
    claim, store=workspace.store, artifacts=workspace.artifacts,
    observed_descriptor=descriptor)
manifest = WorkflowManifest(
    entries=(WorkflowEntry(workflow_id="W-C6", name="x", status=CoverageStatus.DECLARED),),
    guardrails=())
record = RunnerIntegrationRecord(
    workflow_id=claim.workflow_id,
    runner_id=claim.runner_id,
    runner_descriptor_digest=claim.runner_descriptor_digest,
    verified_by="reviewer",
    statement="fresh-process promotion",
    runner_revision=claim.runner_revision,
    case_pack_digest=claim.case_pack_digest,
    coverage_layer=CoverageLayer.BROWSER,
    provider_mode=claim.provider_mode,
    verified_run_id=claim.verified_run_id,
    verified_attempt_digests=claim.verified_attempt_digests,
    evidence_digest="sha256:" + "0" * 64,
)
promoted = manifest.with_runner_integration(
    record, store=workspace.store, artifacts=workspace.artifacts,
    observed_descriptor=descriptor)
restored = WorkflowManifest.from_dict(promoted.to_dict())
verified = restored.verify_integrations(workspace.store, workspace.artifacts, descriptor)
status = verified.effective_status("W-C6")
print("FRESH_OK", verified_screenshots, status)
assert status == "runner-integrated", status
"""
        script = (
            script.replace("__SRC__", str(_REPO / "src"))
            .replace("__ACCEPTANCE__", str(_ACCEPTANCE))
            .replace(
                "__HELPERS__",
                str(_REPO / "tests" / "evaluation" / "test_attestation_layer_records.py"),
            )
            .replace("__PROJECT__", str(tmp_path))
            .replace("__COMMIT__", _RETAINED_COMMIT)
        )
        import os
        import subprocess

        env = dict(os.environ)
        env["PYTHONPATH"] = str(_REPO / "src")
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("FRESH_OK 32 runner-integrated"), proc.stdout


@pytest.mark.skipif(not _retained_available(), reason="retained Choose evidence export not present")
def test_cli_import_commands_are_public(tmp_path: Path) -> None:
    """The public CLI path works end to end for both importers."""
    from typer.testing import CliRunner

    from vouch_agent.cli.main import app

    workspace = _workspace(tmp_path)
    try:
        for workflow in _BROWSER_WORKFLOWS:
            _pack_for_workflow(workspace, workflow)
        _pack_for_workflow(workspace, "W-C9")
    finally:
        workspace.close()

    cli = CliRunner()
    browser = cli.invoke(
        app,
        [
            "evidence-import-browser",
            "--project",
            str(tmp_path),
            "--export",
            str(_BROWSER_EXPORT),
            "--pack",
            "W-C1=pack-W-C1",
            "--pack",
            "W-C4=pack-W-C4",
            "--pack",
            "W-C5=pack-W-C5",
            "--pack",
            "W-C6=pack-W-C6",
            "--pack",
            "W-C7=pack-W-C7",
            "--pack",
            "W-C8=pack-W-C8",
        ],
    )
    assert browser.exit_code == 0, browser.output
    assert f"tested source: {_RETAINED_COMMIT}" in browser.output
    assert "screenshots verified: 32" in browser.output
    assert "durable claims" in browser.output

    control = cli.invoke(
        app,
        [
            "evidence-import-control",
            "--project",
            str(tmp_path),
            "--export",
            str(_CONTROL_EXPORT),
            "--pack",
            "pack-W-C9",
        ],
    )
    assert control.exit_code == 0, control.output
    assert "W-C9 stays regression-only" in control.output
