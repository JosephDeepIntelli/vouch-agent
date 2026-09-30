"""CSV reconciliation accepts real user filenames (M4 A3 review §1).

`vouch reconcile` used to forward the operator's file basenames straight
into the internal ``TaskAttachment.name`` (contract-safe
``[A-Za-z0-9._-]{1,64}`` only), so `产品 目录.csv` and `supplier feed.csv`
were rejected before any run was submitted. Correct behavior, asserted here
through the REAL Typer CLI and the REAL detached worker: per-material
snapshots with separate safe internal attachment ids, UNCHANGED user-visible
display names, each material's bytes under their OWN digest, and readable
names in the exported manifest / reopened workspace.
"""

from __future__ import annotations

import importlib.util as _il
import json
import re
import time
from pathlib import Path

from helpers import init_project, invoke, out

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.native_export import verify_native_export
from vouch_agent.appservices.worker import spawn_detached_worker
from vouch_agent.appservices.workspace import ProjectWorkspace

_spec = _il.spec_from_file_location(
    "m3_runtime_helpers",
    Path(__file__).parents[1] / "regression" / "runtime" / "m3_runtime_helpers.py",
)
_m3 = _il.module_from_spec(_spec)
_spec.loader.exec_module(_m3)
init_native_project = _m3.init_native_project

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _run_id_of(result_text: str) -> str:
    return result_text.split("run: ")[1].split()[0]


def _spec_materials(project: Path, run_id: str) -> list[dict]:
    """The run's material records, read back from the REOPENED workspace."""
    workspace = ProjectWorkspace.open(project)
    try:
        index = workspace.store.load("run-index", run_id)
        assert index is not None, "run-index record missing"
        spec = workspace.store.load("task-spec", str(index["specId"]))
        assert spec is not None, "task-spec record missing"
        materials = spec["inputs"]["materials"]
        assert isinstance(materials, list) and len(materials) == 2
        return materials
    finally:
        workspace.close()


def test_reconcile_accepts_spaces_and_chinese_filenames(tmp_path: Path) -> None:
    """The public `vouch reconcile` command succeeds on the review's exact
    inputs and exports readable display names beside safe internal ids."""
    project = init_project(tmp_path)
    left = tmp_path / "产品 目录.csv"
    right = tmp_path / "supplier feed.csv"
    left.write_text("sku,price\nA-1,10\nA-2,20\n", encoding="utf-8")
    right.write_text("sku,price\nA-1,10\nA-2,25\n", encoding="utf-8")
    export = tmp_path / "export"

    result = invoke(
        "reconcile",
        "--project",
        str(project),
        "--left",
        str(left),
        "--right",
        str(right),
        "--join-key",
        "sku",
        "--out",
        str(export),
    )
    assert result.exit_code == 0, out(result)
    assert "1 changed" in result.stdout and "matched" in result.stdout

    # the exported manifest verifies and shows the UNCHANGED display names
    manifest = verify_native_export(export)
    entries = manifest["materials"]
    assert [entry["displayName"] for entry in entries] == ["产品 目录.csv", "supplier feed.csv"]
    for entry in entries:
        assert _SAFE_ID.fullmatch(str(entry["attachmentId"]))
        assert entry["digest"] and entry["sizeBytes"]

    # reopening the workspace shows the same: safe internal ids, real names
    run_id = _run_id_of(result.stdout)
    materials = _spec_materials(project, run_id)
    assert [record["displayName"] for record in materials] == [
        "产品 目录.csv",
        "supplier feed.csv",
    ]
    for record in materials:
        assert _SAFE_ID.fullmatch(record["attachmentId"])
        assert record["attachment"]["name"] == record["attachmentId"]
    assert materials[0]["attachmentId"] != materials[1]["attachmentId"]


def test_reconcile_same_basename_distinct_files_stay_distinct(tmp_path: Path) -> None:
    """Two DIFFERENT files sharing one basename both reconcile through the
    CLI and stay distinct: different internal ids, different own digests,
    each verifying against exactly its own bytes."""
    project = init_project(tmp_path)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    left = dir_a / "catalog.csv"  # same basename, different bytes
    right = dir_b / "catalog.csv"
    left_bytes = b"sku,price\nA-1,10\nA-2,20\n"
    right_bytes = b"sku,price\nA-1,10\nA-2,99\n"
    left.write_bytes(left_bytes)
    right.write_bytes(right_bytes)

    result = invoke(
        "reconcile",
        "--project",
        str(project),
        "--left",
        str(left),
        "--right",
        str(right),
        "--join-key",
        "sku",
    )
    assert result.exit_code == 0, out(result)

    run_id = _run_id_of(result.stdout)
    materials = _spec_materials(project, run_id)
    # same display name twice, but never collapsed into one material
    assert [record["displayName"] for record in materials] == ["catalog.csv", "catalog.csv"]
    ids = [record["attachmentId"] for record in materials]
    digests = [record["attachment"]["contentDigest"] for record in materials]
    assert ids[0] != ids[1]
    assert digests[0] != digests[1]

    # each material's bytes verify against its OWN digest (no shared blob)
    workspace = ProjectWorkspace.open(project)
    try:
        assert workspace.artifacts.get(digests[0]) == left_bytes
        assert workspace.artifacts.get(digests[1]) == right_bytes
    finally:
        workspace.close()


def test_reconcile_materials_survive_the_detached_worker_path(tmp_path: Path) -> None:
    """A CSV run submitted with user filenames is executed by the REAL
    detached worker: the source-linked result cites each material's own
    digest, the report's findings reference both sides, and the export
    shows the readable names (the client that submitted is gone)."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    run_id = service.submit_csv_reconciliation(
        goal="Reconcile catalog against supplier feed",
        left_csv="sku,名称\nA-1,电水壶\nA-2,磨豆机\n".encode(),
        right_csv="sku,名称\nA-1,电水壶\nA-2,咖啡磨\n".encode(),
        join_key="sku",
        left_name="产品 目录.csv",
        right_name="supplier feed.csv",
    )
    workspace.close()  # the submitting client exits before the work runs
    assert spawn_detached_worker(project, run_id) > 0

    fresh = ProjectWorkspace.open(project)
    try:
        deadline = time.monotonic() + 90
        final = None
        while time.monotonic() < deadline:
            run = ExecutionService(fresh).status(run_id)
            if run is not None and run.status.value in ("completed", "failed", "cancelled"):
                final = run
                break
            time.sleep(0.3)
        assert final is not None and final.status.value == "completed", (
            final.error if final else "worker never finished"
        )
        result = ExecutionService(fresh).result(run_id)
        assert result is not None
        # source-linked result: the package cites BOTH materials' own digests
        index = fresh.store.load("run-index", run_id)
        spec = fresh.store.load("task-spec", str(index["specId"]))
        digests = [m["attachment"]["contentDigest"] for m in spec["inputs"]["materials"]]
        for digest in digests:
            assert digest in result.artifact_refs
            fresh.artifacts.get(digest)  # verifies against its own bytes

        # the report references both sides by name and position
        report = json.loads(fresh.artifacts.get(result.artifact_refs[-1]).decode("utf-8"))
        assert report["changed"] and report["changed"][0]["key"] == "A-2"
        assert report["changed"][0]["leftRef"]["source"] == "left"
        assert report["changed"][0]["rightRef"]["source"] == "right"

        from vouch_agent.appservices.worker import export_run

        manifest_path = export_run(fresh, run_id, tmp_path / "export")
        assert manifest_path.exists()
        manifest = verify_native_export(tmp_path / "export")
        assert [entry["displayName"] for entry in manifest["materials"]] == [
            "产品 目录.csv",
            "supplier feed.csv",
        ]
    finally:
        fresh.close()
