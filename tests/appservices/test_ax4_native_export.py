"""Native run export trustworthiness (M4 A3): actual identity, staged
publication, import-side verification, unrelated-package rejection."""

from __future__ import annotations

import importlib.util as _il
import json
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_ACCEPTANCE = _TESTS.parent / "regression" / "acceptance"
if str(_ACCEPTANCE) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE))

_spec_ = _il.spec_from_file_location(
    "ax4_native_m3_helpers", _TESTS.parent / "regression" / "runtime" / "m3_runtime_helpers.py"
)
_m3 = _il.module_from_spec(_spec_)
assert _spec_.loader is not None
_spec_.loader.exec_module(_m3)
init_native_project = _m3.init_native_project

import pytest  # noqa: E402

from vouch_agent.appservices.execution import ExecutionService  # noqa: E402
from vouch_agent.appservices.native_export import (  # noqa: E402
    INCOMPLETE_MARKER,
    MANIFEST_NAME,
    export_run,
    verify_native_export,
)
from vouch_agent.appservices.workspace import ProjectWorkspace  # noqa: E402
from vouch_agent.contracts.tasks import ResultPackage  # noqa: E402
from vouch_agent.errors import ContractError, DigestMismatchError  # noqa: E402

LEFT = "sku,name,price\nA-1,Kettle,95.00\nA-2,Grinder,129.00\n"
RIGHT = "sku,name,price\nA-1,Kettle,95.00\nA-2,Grinder,139.00\n"


def _csv_run(workspace: ProjectWorkspace) -> tuple[str, str]:
    service = ExecutionService(workspace)
    run_id, _report, digest = service.run_csv_reconciliation(
        goal="Reconcile the catalog export against the supplier feed",
        left_csv=LEFT.encode(),
        right_csv=RIGHT.encode(),
        join_key="sku",
        # NOTE: the current execution call site still requires contract-safe
        # attachment names; user-visible display names (spaces/Chinese) flow
        # through the materials helpers once that site is rewired (M4 A3).
        left_name="catalog.csv",
        right_name="feed.csv",
    )
    return run_id, digest


def test_manifest_carries_actual_run_identity_and_verified_artifacts(
    tmp_path: Path,
) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        run_id, report_digest = _csv_run(workspace)
        destination = tmp_path / "export"
        manifest_path = export_run(workspace, run_id, destination)
        assert manifest_path.name == MANIFEST_NAME

        manifest = verify_native_export(destination)
        assert manifest["runId"] == run_id
        assert manifest["taskSpecId"], "task spec identity must be carried"
        assert manifest["taskDigest"]
        assert manifest["runStatus"] == "completed"
        assert manifest["terminal"] is True
        assert manifest["mode"] == "fixture"
        assert manifest["operation"] == "vouch-csv-reconcile/1"
        # material refs/digests: both inputs cited with their own digests
        materials = manifest["materials"]
        assert len(materials) == 2
        assert {entry["digest"] for entry in materials} != {report_digest}
        assert all(entry["digest"] and entry["sizeBytes"] for entry in materials)
        # completion checks, costs, uncertainties, deliverable state
        assert manifest["completedConditionsCheck"]
        assert manifest["totalCostUsd"] == 0.0
        assert "uncertainties" in manifest and "notDoneItems" in manifest
        assert manifest["deliverable"] is True
        # artifact bytes verified: the discrepancy report is one of them
        digests = {entry["digest"] for entry in manifest["artifacts"]}
        assert report_digest in digests
        for entry in manifest["artifacts"]:
            payload = (destination / entry["file"]).read_bytes()
            assert len(payload) == entry["bytes"]
    finally:
        workspace.close()


def test_injected_result_package_for_another_run_is_rejected(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        run_id, _digest = _csv_run(workspace)
        foreign = ResultPackage(
            run_id="run_somewhere_else",
            conclusion="not this run's package",
        )
        with pytest.raises(ContractError, match="unrelated result package"):
            export_run(workspace, run_id, tmp_path / "export", result=foreign)
        # and nothing was published: no complete export exists
        assert not (tmp_path / "export" / MANIFEST_NAME).exists()
    finally:
        workspace.close()


def test_unknown_run_id_refused(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        with pytest.raises(ContractError, match="unknown task run"):
            export_run(workspace, "run_does_not_exist", tmp_path / "export")
    finally:
        workspace.close()


def test_partial_run_exports_as_not_terminal(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        service = ExecutionService(workspace)
        run_id = service.submit(
            goal="long work", inputs={}, completion_conditions=[{"type": "none"}]
        )
        workspace.store.save(
            "result-package",
            run_id,
            ResultPackage(
                run_id=run_id,
                conclusion="partial: work still queued",
                not_done_items=("the operation has not executed yet",),
                completed_conditions_check={"none[0]": False},
            ).to_dict(),
        )
        manifest_path = export_run(workspace, run_id, tmp_path / "export")
        assert manifest_path.is_file()
        manifest = verify_native_export(tmp_path / "export")
        assert manifest["terminal"] is False
        assert manifest["runStatus"] == "queued"
        assert manifest["deliverable"] is False
        assert manifest["notDoneItems"]
    finally:
        workspace.close()


def test_interrupted_export_never_appears_complete(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        run_id, _digest = _csv_run(workspace)
        destination = tmp_path / "export"
        export_run(workspace, run_id, destination)
        # simulate an interrupted RE-export: staging marker left behind
        (destination / INCOMPLETE_MARKER).write_bytes(b"interrupted")
        with pytest.raises(ContractError, match="INCOMPLETE"):
            verify_native_export(destination)
        (destination / INCOMPLETE_MARKER).unlink()
        assert verify_native_export(destination)["runId"] == run_id
    finally:
        workspace.close()


def test_tampered_artifact_detected_on_import(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        run_id, _digest = _csv_run(workspace)
        destination = tmp_path / "export"
        export_run(workspace, run_id, destination)
        manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
        victim = manifest["artifacts"][0]["file"]
        (destination / victim).write_bytes(b"rewritten after export")
        with pytest.raises(DigestMismatchError):
            verify_native_export(destination)
    finally:
        workspace.close()


def test_missing_or_unlisted_files_detected_on_import(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        run_id, _digest = _csv_run(workspace)
        destination = tmp_path / "export"
        export_run(workspace, run_id, destination)
        manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
        victim = destination / manifest["artifacts"][0]["file"]
        removed = victim.read_bytes()
        victim.unlink()
        with pytest.raises(ContractError, match="missing artifact"):
            verify_native_export(destination)
        victim.write_bytes(removed)
        (destination / "smuggled.txt").write_bytes(b"not in the manifest")
        with pytest.raises(ContractError, match="does not match manifest"):
            verify_native_export(destination)
    finally:
        workspace.close()


def test_reexport_over_a_prior_complete_export(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    try:
        run_id, _digest = _csv_run(workspace)
        destination = tmp_path / "export"
        first = export_run(workspace, run_id, destination)
        second = export_run(workspace, run_id, destination)
        assert first == second
        assert verify_native_export(destination)["runId"] == run_id
    finally:
        workspace.close()
