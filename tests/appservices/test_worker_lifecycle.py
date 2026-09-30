"""Detached worker lifecycle + native export (M3-B1/B2/B5).

A run started detached completes in an INDEPENDENT process: the client that
spawned it can exit immediately; a fresh client reconnects and reads the
completed result. Export writes the full ResultPackage with digest-verified
artifacts atomically.
"""

from __future__ import annotations

import importlib.util as _il
import json
import time
from pathlib import Path

import pytest

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.worker import export_run, spawn_detached_worker, worker_lease
from vouch_agent.appservices.workspace import ProjectWorkspace

_spec = _il.spec_from_file_location(
    "m3_runtime_helpers",
    Path(__file__).parents[1] / "regression" / "runtime" / "m3_runtime_helpers.py",
)
_m3 = _il.module_from_spec(_spec)
_spec.loader.exec_module(_m3)
init_native_project = _m3.init_native_project

_SCHEMA = {
    "type": "object",
    # "verified" is required and the DRAFT deliberately omits it, so the run
    # GENUINELY needs both model steps (M4 A1 test-gap repair: the old draft
    # already satisfied the schema, so nothing demanded step two).
    "required": ["finding", "source", "verified"],
    "properties": {
        "finding": {"type": "string"},
        "source": {"type": "string"},
        "verified": {"type": "boolean"},
    },
}
# Two-step pool: whichever step boundary the cooperative pause lands on,
# resume has a response left to consume (pause timing is inherently racy).
_SCRIPT_DRAFT = (
    'value = materials["fact"]["value"]\n'
    'return {"finding": value, "source": materials["fact"]["source"]}'
)
_SCRIPT_FINAL = (
    'value = materials["fact"]["value"]\n'
    'return {"finding": value, "source": materials["fact"]["source"], "verified": True}'
)


def _fact(path: Path, value: str) -> Path:
    """Write the FLAT material object the scripts read as
    ``materials["fact"]["value"]`` — passed to submit() verbatim (the old
    helper wrapped it a second time under ``fact``)."""
    payload = json.dumps({"fact": {"value": value, "source": "material-A"}})
    path.write_text(payload, encoding="utf-8")
    return path


def test_detached_csv_run_survives_client_exit_and_reconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detach/reconnect through the deterministic native operation, with the
    CSV computation routed AFTER durable submit through the shared worker
    claim (M4 A1 test-gap repair: the old test computed the reconciliation
    in the submitting client and started a worker on an already-completed
    run).

    A deterministic test barrier (gate file) holds the worker's computation
    in flight while the submitting client exits; the run is observably
    ACTIVE mid-work, then completes once the gate is released, and a fresh
    client reconnects and exports by run id."""

    gate = tmp_path / "gate"
    monkeypatch.setenv("VOUCH_TEST_OPERATION_GATE_FILE", str(gate))

    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    # DURABLE SUBMIT only: materials snapshotted by digest, operation config
    # sealed, run queued — nothing computed yet.
    run_id = service.submit_csv_reconciliation(
        goal="Reconcile catalog vs supplier feed",
        left_csv=b"sku,price\nA-1,10\nA-2,20\n",
        right_csv=b"sku,price\nA-1,10\nA-2,25\n",
        join_key="sku",
    )
    assert service.status(run_id).status.value == "queued"
    # the submitting client exits BEFORE the work runs
    workspace.close()
    pid = spawn_detached_worker(project, run_id)
    assert pid > 0

    fresh = ProjectWorkspace.open(project)
    try:
        # the worker claims the run and reaches the barrier: the operation
        # step is in flight (running) while the gate holds it — an ACTIVE
        # job, not an already-completed record.
        deadline = time.monotonic() + 60
        active = False
        while time.monotonic() < deadline:
            run = ExecutionService(fresh).status(run_id)
            if run is not None and run.status.value == "running":
                active = True
                break
            time.sleep(0.2)
        assert active, "worker never claimed the queued csv run"
        time.sleep(0.3)  # the claim is durable while the gate still holds
        assert gate.exists() is False
        assert ExecutionService(fresh).status(run_id).status.value == "running"

        # the (reconnected) client now exits MID-FLIGHT too: the active job
        # must survive the departure of every client that ever touched it
        fresh.close()
        time.sleep(0.3)
        fresh = ProjectWorkspace.open(project)
        assert ExecutionService(fresh).status(run_id).status.value == "running"

        gate.write_text("go", encoding="utf-8")  # release the barrier
        deadline = time.monotonic() + 60
        final = None
        while time.monotonic() < deadline:
            run = ExecutionService(fresh).status(run_id)
            if run is not None and run.status.value in ("completed", "failed", "cancelled"):
                final = run
                break
            time.sleep(0.2)
        assert final is not None and final.status.value == "completed"
        result = ExecutionService(fresh).result(run_id)
        assert result is not None and result.deliverable()
        # the worker genuinely computed the reconciliation (A-2 changed)
        report = json.loads(fresh.artifacts.get(result.artifact_refs[-1]).decode("utf-8"))
        assert report["rowCounts"] == {"left": 2, "right": 2, "matched": 2}
        assert report["changed"] and report["changed"][0]["key"] == "A-2"
        manifest = export_run(fresh, run_id, tmp_path / "export")
        assert manifest.exists()
    finally:
        gate.write_text("go", encoding="utf-8")  # never leave a worker held
        fresh.close()


def test_detached_run_survives_client_exit_and_reconnects(tmp_path: Path) -> None:
    """A MODEL run detached across a pause boundary: the client seals the
    configuration paused, exits; the worker (explicit start) continues the
    paused run through both model steps — the draft does NOT satisfy the
    schema, so step two is genuinely required — and a fresh client reads the
    verified result. The lease is fenced and cleared when the worker exits."""
    project = init_native_project(tmp_path)
    fact = _fact(tmp_path / "fact.json", "迪普智选 detached fact")
    scripts = []
    for name, text in (("draft.py", _SCRIPT_DRAFT), ("final.py", _SCRIPT_FINAL)):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        scripts.append(path)

    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    run_id = service.submit(
        goal="Extract the fact",
        inputs=json.loads(fact.read_text()),
        budget_usd=0.5,
        max_steps=4,  # context step + two genuine model steps, inside the bound
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )
    # seal the configuration by executing once paused, then the client exits.
    workspace.store.save("pause-request", run_id, {"requested": True})
    first = service.execute(run_id, script_files=tuple(scripts))
    assert first.status.value == "paused"
    workspace.close()

    # the client exits; the worker runs independently
    pid = spawn_detached_worker(project, run_id)
    assert pid > 0

    fresh = ProjectWorkspace.open(project)
    try:
        deadline = time.monotonic() + 90
        final = None
        while time.monotonic() < deadline:
            run = ExecutionService(fresh).status(run_id)
            if run is not None and run.status.value in ("completed", "failed", "cancelled"):
                final = run
                break
            time.sleep(0.5)
        if final is None or final.status.value != "completed":
            run2 = ExecutionService(fresh).status(run_id)
            raise AssertionError(
                f"detached run ended {final.status.value if final else '??'}: "
                f"{(run2.error if run2 else '') or 'unknown'}"
            )
        result = ExecutionService(fresh).result(run_id)
        assert result is not None and result.deliverable()
        payload = json.loads(fresh.artifacts.get(result.artifact_refs[-1]).decode("utf-8"))
        assert payload["finding"] == "迪普智选 detached fact"
        assert payload["verified"] is True  # resume consumed the FINAL step
        # the fenced lease is cleared after the worker exits
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and worker_lease(fresh, run_id) is not None:
            time.sleep(0.5)
        assert worker_lease(fresh, run_id) is None
    finally:
        fresh.close()


def test_export_run_writes_full_result_package(tmp_path: Path) -> None:
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        run_id, _report, _report_digest = service.run_csv_reconciliation(
            goal="reconcile",
            left_csv=b"sku,price\nA-1,1\n",
            right_csv=b"sku,price\nA-1,2\n",
            join_key="sku",
        )
        out = tmp_path / "export"
        manifest_path = export_run(workspace, run_id, out)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["kind"] == "vouch-native-run-export"
        assert manifest["runId"] == run_id
        assert manifest["deliverable"] is True
        assert manifest["notDoneItems"]  # discrepancies stated, not hidden
        artifacts = manifest["artifacts"]
        assert artifacts and all(Path(out, a["file"]).exists() for a in artifacts)
        # every exported artifact byte-verify against its digest
        import hashlib

        for artifact in artifacts:
            payload = Path(out, artifact["file"]).read_bytes()
            assert "sha256:" + hashlib.sha256(payload).hexdigest() == artifact["digest"]
        # no path traversal in filenames
        assert all("/" not in a["file"] and ".." not in a["file"] for a in artifacts)
    finally:
        workspace.close()
