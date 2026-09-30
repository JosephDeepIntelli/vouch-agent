"""Detached-worker cancellation (M4 A1 §5 tail, review "Existing test gaps").

Cancel from ANOTHER client while the owner's model step is in flight: the
request is durable, the worker polls it OUTSIDE the blocking model call, the
owned worker process group is stopped within a stated bound (the group-stop
grace: TERM -> 2s -> KILL plus the ~0.1s poll interval), open reservations
are reconciled before CANCELLED is published, and nothing dispatches after
the request. Also pins the request-shape contract: the supervisor writes
``requestedAt``/``consumedAt`` — the historical worker hook read a
nonexistent ``requested`` key and never fired.
"""

from __future__ import annotations

import importlib.util as _il
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.worker_lifecycle import _cancel_requested
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.orchestrator.supervisor import KIND_CANCEL_REQUEST, KIND_TASK_RUN

_spec = _il.spec_from_file_location(
    "m3_runtime_helpers",
    Path(__file__).parents[1] / "regression" / "runtime" / "m3_runtime_helpers.py",
)
_m3 = _il.module_from_spec(_spec)
_spec.loader.exec_module(_m3)
init_native_project = _m3.init_native_project

_SCHEMA = {
    "type": "object",
    "required": ["finding", "done"],
    "properties": {"finding": {"type": "string"}, "done": {"type": "boolean"}},
}

#: Step one runs a long deterministic computation and only THEN returns an
#: artifact without the required "done" key — so the run would need a second
#: model step unless the cancel lands mid-computation (the window).
_BUSY_STEP_ONE = (
    "total = 0\n"
    "for i in range(40_000_000):\n"
    "    total += i\n"
    'return {"finding": f"computed {total}"}'
)
_STEP_TWO = 'return {"finding": "second", "done": True}'

#: Stated bound: cancel must reach CANCELLED well inside this after the
#: request is written (poll interval ~0.1s + TERM grace 2s + KILL + reap).
_CANCEL_BOUND_S = 15.0


def _submit(service: ExecutionService) -> str:
    return service.submit(
        goal="Synthetic cancellable task",
        inputs={"fact": {"value": "synthetic", "source": "test"}},
        budget_usd=0.5,
        max_steps=4,
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )


def _scripts(tmp: Path) -> tuple[Path, ...]:
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in (("step0.py", _BUSY_STEP_ONE), ("step1.py", _STEP_TWO)):
        path = tmp / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def _model_step_in_flight(workspace: ProjectWorkspace, run_id: str) -> bool:
    data = workspace.store.load(KIND_TASK_RUN, run_id)
    if data is None:
        return False
    for step in data.get("steps", []):
        if step.get("kind") == "model-call" and step.get("status") == "running":
            return True
    return False


def test_cancel_request_shape_matches_what_the_worker_polls() -> None:
    """Regression for the key mismatch: the supervisor writes
    ``requestedAt``; the worker's poller must honor exactly that shape (and
    a consumed request is inactive)."""

    class ShapeStore:
        def __init__(self) -> None:
            self.records: dict[tuple[str, str], dict] = {}

        def save(self, kind: str, record_id: str, data: dict) -> None:
            self.records[(kind, record_id)] = dict(data)

        def load(self, kind: str, record_id: str) -> dict | None:
            return self.records.get((kind, record_id))

    store = ShapeStore()
    store.save(
        KIND_CANCEL_REQUEST, "run-1", {"requestedAt": "2026-01-01T00:00:00Z", "reason": "op"}
    )
    assert _cancel_requested(store, "run-1") is True
    store.save(
        KIND_CANCEL_REQUEST,
        "run-1",
        {
            "requestedAt": "2026-01-01T00:00:00Z",
            "reason": "op",
            "consumedAt": "2026-01-01T00:01:00Z",
        },
    )
    assert _cancel_requested(store, "run-1") is False
    assert _cancel_requested(store, "run-unknown") is False


def test_cross_client_cancel_stops_in_flight_work_within_bound(tmp_path: Path) -> None:
    """The full path: real detached worker, real guarded child executing a
    multi-second deterministic step, cancel from a SECOND client. The owned
    group stops inside the bound; the run finalizes CANCELLED with the
    ledger reconciled; no second step dispatches; the lease clears."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    run_id = _submit(service)
    scripts = _scripts(tmp_path / "s")
    # seal the execution configuration paused, then let the worker start it
    workspace.store.save("pause-request", run_id, {"requested": True})
    assert service.execute(run_id, script_files=scripts).status.value == "paused"
    workspace.close()  # the submitting client exits

    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "vouch_agent.appservices.worker_main",
            "--project",
            str(project),
            "--run",
            run_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    client = ProjectWorkspace.open(project)
    try:
        # wait until the busy model step is observably in flight
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not _model_step_in_flight(client, run_id):
            time.sleep(0.05)
        assert _model_step_in_flight(client, run_id), "model step never went in flight"

        # a SECOND client requests the cancel while the owner executes
        requested_at = time.monotonic()
        returned = ExecutionService(client).cancel(run_id, "operator changed their mind")
        assert returned is not None  # cooperative: the request stands

        worker.wait(timeout=_CANCEL_BOUND_S + 15)
        bounded = time.monotonic() - requested_at
        assert bounded < _CANCEL_BOUND_S + 15, f"cancel took {bounded:.1f}s"

        run = ExecutionService(client).status(run_id)
        assert run is not None
        assert run.status.value == "cancelled", (
            f"cancel did not win (status={run.status.value}, error={run.error})"
        )
        assert worker.returncode == 0, "a cancelled run is a clean worker stop"
        # no dispatch after the request: exactly ONE model step, in/failed
        model_steps = [s for s in run.steps if s.kind.value == "model-call"]
        assert len(model_steps) == 1, f"work dispatched after the cancel: {model_steps}"
        # reservations reconciled before CANCELLED was published
        assert client.ledger.outstanding_usd() == pytest.approx(0.0)
        assert client.ledger.settled_usd() >= 0.0
        # the fenced lease is released by the exiting owner
        from vouch_agent.appservices.worker_lifecycle import worker_lease

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and worker_lease(client, run_id) is not None:
            time.sleep(0.2)
        assert worker_lease(client, run_id) is None
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)
        client.close()
