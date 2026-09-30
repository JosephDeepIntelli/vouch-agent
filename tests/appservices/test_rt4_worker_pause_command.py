"""Worker pause semantics and explicit commands (M4 A1 §3, review §3).

The M3 review proved the detached worker unconditionally resumed whenever
``execute()`` returned PAUSED, silently overriding a NEW operator pause that
landed mid-flight. Corrected contract: the worker acts only on an explicit
durable ``start``/``resume`` command (with an expected version when spawned
with one); once started, PAUSED is a STOPPING condition — continuing requires
a separate resume command. Command acknowledgement and the actual durable
status are persisted.
"""

from __future__ import annotations

import importlib.util as _il
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.worker_lifecycle import (
    KIND_WORKER_COMMAND,
    execute_run_detached,
    issue_worker_command,
    spawn_detached_worker,
    worker_lease,
)
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.orchestrator.supervisor import Supervisor

_spec = _il.spec_from_file_location(
    "m3_runtime_helpers",
    Path(__file__).parents[1] / "regression" / "runtime" / "m3_runtime_helpers.py",
)
_m3 = _il.module_from_spec(_spec)
_spec.loader.exec_module(_m3)
init_native_project = _m3.init_native_project

_SCHEMA = {
    "type": "object",
    "required": ["stage", "done"],
    "properties": {"stage": {"type": "number"}, "done": {"type": "boolean"}},
}

# Step one deliberately OMITS the required "done" key (schema `required`
# checks presence), so the run genuinely needs step two.
_SCRIPT_ONE = 'one = {"stage": 1}\nreturn one'
_SCRIPT_TWO = 'two = {"stage": 2, "done": True}\nreturn two'


def _scripts(tmp: Path, pool: tuple[str, ...]) -> tuple[Path, ...]:
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, text in enumerate(pool):
        path = tmp / f"step{index}.py"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def _submit(service: ExecutionService, *, max_steps: int = 4) -> str:
    return service.submit(
        goal="Synthetic pause task",
        inputs={"fact": {"value": "synthetic", "source": "test"}},
        budget_usd=0.5,
        max_steps=max_steps,
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )


def _insert_pause_after(step_ordinal: int, run_id: str) -> Any:
    """Patch seam: save a NEW durable pause request during the worker's
    ``_phase_model_call``, after the given model ordinal completes."""
    original = Supervisor._phase_model_call

    def hook(self: Supervisor, state: Any, *args: Any) -> Any:
        result = original(self, state, *args)
        if state.model_ordinal == step_ordinal + 1:
            self._store.save("pause-request", run_id, {"requestedAt": "synthetic-new-pause"})
        return result

    return patch.object(Supervisor, "_phase_model_call", hook)


def test_worker_stops_on_new_operator_pause_mid_flight(tmp_path: Path) -> None:
    """A pause inserted after the first model step DURING active detached
    work stops the worker: no further dispatch, durable status paused,
    exit 0 — exactly the review's inverted reproduction."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _scripts(tmp_path / "s", (_SCRIPT_ONE, _SCRIPT_TWO))
    run_id = _submit(service)
    # pre-seal the configuration paused so the worker starts from a pause
    workspace.store.save("pause-request", run_id, {"requested": True})
    sealed = service.execute(run_id, script_files=scripts)
    assert sealed.status.value == "paused"

    dispatched: list[int] = []
    original = Supervisor._phase_model_call

    def recording_pause(self: Supervisor, state: Any, *args: Any) -> Any:
        result = original(self, state, *args)
        dispatched.append(state.model_ordinal - 1)
        if state.model_ordinal == 2:
            self._store.save("pause-request", run_id, {"requestedAt": "new-operator-pause"})
        return result

    with patch.object(Supervisor, "_phase_model_call", recording_pause):
        exit_code = execute_run_detached(project, run_id)
    current = service.status(run_id)
    assert current is not None
    assert exit_code == 0, "a deliberate pause is a clean stop, not a failure"
    assert current.status.value == "paused", (
        f"worker overrode the new operator pause (status={current.status.value})"
    )
    assert dispatched == [1], f"dispatch continued past the pause: {dispatched}"
    # the command acknowledgement records the actual durable outcome
    ack = workspace.store.load(KIND_WORKER_COMMAND, run_id)
    assert ack is not None and ack["result"] == "paused"
    # no further dispatch until an explicit resume: assert the durable pause
    # request is CONSUMED by the pause itself and the run is parked
    time.sleep(0.3)
    assert service.status(run_id).status.value == "paused"
    workspace.close()


def test_explicit_resume_command_completes_the_paused_run(tmp_path: Path) -> None:
    """A paused detached run continues ONLY via an explicit resume command:
    a versioned worker spawn consumes response two exactly once and
    completes; the command is acknowledged with the durable result."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _scripts(tmp_path / "s", (_SCRIPT_ONE, _SCRIPT_TWO))
    run_id = _submit(service)
    workspace.store.save("pause-request", run_id, {"requested": True})
    assert service.execute(run_id, script_files=scripts).status.value == "paused"

    # a worker start CONTINUES the paused run once (explicit start command)
    with _insert_pause_after(1, run_id):
        exit_code = execute_run_detached(project, run_id)
    assert exit_code == 0
    assert service.status(run_id).status.value == "paused"

    # resume requires an explicit durable command with an expected version
    command = issue_worker_command(workspace, run_id, "resume")
    assert command["version"] >= 1
    pid = spawn_detached_worker(
        project, run_id, command="resume", command_version=command["version"]
    )
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
            time.sleep(0.3)
        assert final is not None and final.status.value == "completed", (
            final.error if final else "worker never finished"
        )
        result = ExecutionService(fresh).result(run_id)
        assert result is not None
        payload = json.loads(fresh.artifacts.get(result.artifact_refs[-1]).decode("utf-8"))
        assert payload == {"stage": 2, "done": True}  # response two, exactly once
        ack = fresh.store.load(KIND_WORKER_COMMAND, run_id)
        assert ack is not None and ack["command"] == "resume"
        assert ack["result"] == "completed" and ack["acknowledgedAt"]
    finally:
        fresh.close()


def test_stale_command_spawn_refuses_without_touching_the_run(tmp_path: Path) -> None:
    """A duplicate/stale spawn for an already-acknowledged command refuses
    (exit failure) and does not re-execute the run; a resume without any
    durable command refuses too."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _scripts(tmp_path / "s", (_SCRIPT_ONE, _SCRIPT_TWO))
    run_id = _submit(service)
    workspace.store.save("pause-request", run_id, {"requested": True})
    assert service.execute(run_id, script_files=scripts).status.value == "paused"

    command = issue_worker_command(workspace, run_id, "resume")
    # the FIRST resume worker consumes the command
    exit_code = execute_run_detached(
        project, run_id, command="resume", command_version=command["version"]
    )
    assert exit_code == 0
    assert service.status(run_id).status.value == "completed"

    # a second spawn with the SAME (already acknowledged) version refuses
    stale = execute_run_detached(
        project, run_id, command="resume", command_version=command["version"]
    )
    assert stale != 0, "a consumed command must not execute twice"
    # and the completed run was not reopened
    assert service.status(run_id).status.value == "completed"
    workspace.close()

    # resume without any durable command record at all: refused
    project2 = init_native_project(tmp_path / "p2")
    workspace2 = ProjectWorkspace.open(project2)
    service2 = ExecutionService(workspace2)
    run_id2 = _submit(service2)
    try:
        assert execute_run_detached(project2, run_id2, command="resume") != 0
        assert service2.status(run_id2).status.value == "queued"
        assert worker_lease(workspace2, run_id2) is None
    finally:
        workspace2.close()
