"""TUI Resume continues a run its detached worker paused (M4 A3 review §2).

The TaskScreen Resume handler used to spawn a worker with the DEFAULT
``start`` command while the prior worker had already acknowledged ``start``
with result ``paused`` — so the new worker was refused by the explicit-
command gate, exited 1, and the run stayed paused while the UI claimed it
had dispatched a resume. Correct behavior, asserted here with the REAL
Textual ``VouchApp.run_test`` and REAL detached worker processes: Resume
issues an explicit durable resume command and dispatches THAT exact version;
closing/reopening the UI, repeated Resume against a live worker, and
cancel-versus-resume all preserve one authoritative task execution.
"""

from __future__ import annotations

import asyncio
import importlib.util as _il
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vouch_agent.appservices.execution import (
    KIND_EXECUTION_CONFIG,
    ExecutionService,
)
from vouch_agent.appservices.worker_lifecycle import (
    KIND_WORKER_COMMAND,
    execute_run_detached,
)
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.tasks import TaskStatus
from vouch_agent.tui.app import VouchApp

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
# Step one omits the required "done" key: completion genuinely needs the
# second response, so the resume is a real continuation of the SAME pool.
_SCRIPT_ONE = 'one = {"stage": 1}\nreturn one'
_SCRIPT_TWO = 'two = {"stage": 2, "done": True}\nreturn two'

#: Real worker child that parks at a trusted pre-model barrier while holding
#: the real Supervisor ownership AND the fenced worker lease (live worker).
_BARRIER_CHILD = """
import sys, time
from pathlib import Path
from vouch_agent.orchestrator.supervisor import Supervisor
from vouch_agent.appservices.worker_lifecycle import execute_run_detached
project, run_id, ready, release = sys.argv[1:5]
original = Supervisor._phase_model_call
def barrier(self, *args):
    Path(ready).write_text('ready')
    deadline = time.monotonic() + 60
    while not Path(release).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return original(self, *args)
Supervisor._phase_model_call = barrier
sys.exit(execute_run_detached(Path(project), run_id))
"""


def _scripts(tmp: Path) -> tuple[Path, ...]:
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, text in enumerate((_SCRIPT_ONE, _SCRIPT_TWO)):
        path = tmp / f"step{index}.py"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def _submit(service: ExecutionService) -> str:
    return service.submit(
        goal="Synthetic TUI pause/resume task",
        inputs={"fact": {"value": "synthetic", "source": "test"}},
        budget_usd=0.5,
        max_steps=4,
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )


def _pause_via_detached_worker(
    project: Path, workspace: ProjectWorkspace, scripts: tuple[Path, ...]
) -> str:
    """Pause the run through its REAL detached worker: seal paused in-process
    (so the original provider pool is the sealed one), arm a pause request,
    and let the worker's explicit ``start`` stop at the pause boundary — the
    exact state the TUI's Resume button faces (start acknowledged 'paused')."""
    service = ExecutionService(workspace)
    run_id = _submit(service)
    workspace.store.save("pause-request", run_id, {"requested": True})
    assert service.execute(run_id, script_files=scripts).status is TaskStatus.PAUSED
    workspace.store.save("pause-request", run_id, {"requested": True})
    assert execute_run_detached(project, run_id) == 0
    assert service.status(run_id).status is TaskStatus.PAUSED
    record = workspace.store.load(KIND_WORKER_COMMAND, run_id)
    assert record is not None and record["command"] == "start"
    assert record["result"] == "paused" and record["acknowledgedAt"] is not None
    return run_id


def test_tui_resume_continues_paused_run_with_durable_command(tmp_path: Path) -> None:
    """The review's reproduction, inverted: pause → worker exits → the REAL
    TaskScreen Resume issues the durable resume command, dispatches a worker
    bound to THAT version, and the run completes with one continuation on the
    original sealed provider — even after the UI client is closed."""
    pytest.importorskip("textual")
    asyncio.run(_resume_flow(tmp_path))


async def _resume_flow(tmp_path: Path) -> None:
    from textual.widgets import DataTable, Static

    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    scripts = _scripts(tmp_path / "s")
    run_id = _pause_via_detached_worker(project, workspace, scripts)
    sealed = workspace.store.load(KIND_EXECUTION_CONFIG, run_id)
    assert sealed is not None and sealed["scriptsDigest"]
    start_record = dict(workspace.store.load(KIND_WORKER_COMMAND, run_id))
    workspace.close()  # the pausing client exits; the TUI reconnects fresh

    vouch = VouchApp(project_dir=str(project))
    async with vouch.run_test() as pilot:
        await pilot.press("ctrl+t")
        screen = vouch.screen
        screen.action_refresh()
        screen.query_one("#task-runs", DataTable).move_cursor(row=0)
        screen.action_resume_selected()
        message = str(screen.query_one("#task-result", Static).render())
        assert "resume" in message, message
        worker_pid = int(message.split("pid ")[1].split()[0])

    # the UI client is closed; the spawned worker must finish the run alone
    _, wait_status = await asyncio.to_thread(os.waitpid, worker_pid, 0)
    assert os.waitstatus_to_exitcode(wait_status) == 0, "the resume worker failed"

    fresh = ProjectWorkspace.open(project)
    try:
        service = ExecutionService(fresh)
        run = service.status(run_id)
        assert run is not None and run.status is TaskStatus.COMPLETED, (
            run.error if run else "no run"
        )
        result = service.result(run_id)
        assert result is not None
        payload = json.loads(fresh.artifacts.get(result.artifact_refs[-1]).decode("utf-8"))
        assert payload == {"stage": 2, "done": True}  # the FINAL pool response

        # one continuation: both model steps ran in the single resume dispatch
        model_steps = [s for s in run.steps if s.kind.value == "model-call"]
        assert len(model_steps) == 2, [s.kind.value for s in run.steps]
        cursor = fresh.store.load("run-query-cursor", run_id)
        assert cursor is not None and cursor["cursor"] == 2

        # the ORIGINAL sealed provider executed, unchanged
        config = fresh.store.load(KIND_EXECUTION_CONFIG, run_id)
        assert config is not None and config["scriptsDigest"] == sealed["scriptsDigest"]
        assert config["providerName"] is None  # the operator's script pool

        # the durable command the TUI issued was consumed by that worker
        record = fresh.store.load(KIND_WORKER_COMMAND, run_id)
        assert record is not None
        assert record["command"] == "resume"
        assert int(record["version"]) == int(start_record["version"]) + 1
        assert record["result"] == "completed" and record["acknowledgedAt"] is not None
    finally:
        fresh.close()


def test_tui_resume_refuses_while_a_worker_is_live(tmp_path: Path) -> None:
    """Repeated Resume (or a stale-UI Resume) against a run a LIVE worker
    already executes must not issue a competing command or spawn a second
    worker — one authoritative execution."""
    pytest.importorskip("textual")
    asyncio.run(_live_worker_flow(tmp_path))


async def _live_worker_flow(tmp_path: Path) -> None:
    from textual.widgets import DataTable, Static

    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    run_id = _submit(service)
    ready, release = tmp_path / "ready", tmp_path / "release"
    workspace.close()

    worker = subprocess.Popen(
        [sys.executable, "-c", _BARRIER_CHILD, str(project), run_id, str(ready), str(release)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    spawns: list[int] = []
    original_spawn = __import__(
        "vouch_agent.appservices.worker", fromlist=["spawn_detached_worker"]
    ).spawn_detached_worker

    def recording_spawn(*args: object, **kwargs: object) -> int:
        pid = original_spawn(*args, **kwargs)
        spawns.append(pid)
        return pid

    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert ready.exists(), "barrier worker never started"
        client = ProjectWorkspace.open(project)
        try:
            before = client.store.load(KIND_WORKER_COMMAND, run_id)
            vouch = VouchApp(project_dir=str(project))
            async with vouch.run_test() as pilot:
                await pilot.press("ctrl+t")
                screen = vouch.screen
                screen.action_refresh()
                screen.query_one("#task-runs", DataTable).move_cursor(row=0)
                with patch(
                    "vouch_agent.appservices.worker.spawn_detached_worker", recording_spawn
                ):
                    screen.action_resume_selected()
                message = str(screen.query_one("#task-result", Static).render())
                assert "refused" in message, message
                assert "already" in message, message
            assert spawns == [], "Resume spawned a worker against a live owner"
            after = client.store.load(KIND_WORKER_COMMAND, run_id)
            assert after == before, "the refused Resume mutated the command record"
        finally:
            client.close()
    finally:
        release.write_text("go", encoding="utf-8")
        worker.wait(timeout=60)


def test_tui_resume_refuses_terminal_run_after_cancel(tmp_path: Path) -> None:
    """Cancel wins over Resume: a cancelled run is terminal, Resume refuses
    honestly without issuing a command or spawning anything."""
    pytest.importorskip("textual")
    asyncio.run(_cancel_flow(tmp_path))


async def _cancel_flow(tmp_path: Path) -> None:
    from textual.widgets import DataTable, Static

    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    scripts = _scripts(tmp_path / "s")
    run_id = _pause_via_detached_worker(project, workspace, scripts)
    assert ExecutionService(workspace).cancel(run_id, "operator cancelled").status is (
        TaskStatus.CANCELLED
    )
    before: dict[str, Any] | None = dict(
        workspace.store.load(KIND_WORKER_COMMAND, run_id) or {}
    )
    workspace.close()

    spawns: list[int] = []
    original_spawn = __import__(
        "vouch_agent.appservices.worker", fromlist=["spawn_detached_worker"]
    ).spawn_detached_worker

    def recording_spawn(*args: object, **kwargs: object) -> int:
        pid = original_spawn(*args, **kwargs)
        spawns.append(pid)
        return pid

    vouch = VouchApp(project_dir=str(project))
    async with vouch.run_test() as pilot:
        await pilot.press("ctrl+t")
        screen = vouch.screen
        screen.action_refresh()
        screen.query_one("#task-runs", DataTable).move_cursor(row=0)
        with patch("vouch_agent.appservices.worker.spawn_detached_worker", recording_spawn):
            screen.action_resume_selected()
        message = str(screen.query_one("#task-result", Static).render())
        assert "refused" in message and "cancelled" in message, message
    assert spawns == [], "Resume spawned a worker for a terminal run"

    fresh = ProjectWorkspace.open(project)
    try:
        run = ExecutionService(fresh).status(run_id)
        assert run is not None and run.status is TaskStatus.CANCELLED
        assert fresh.store.load(KIND_WORKER_COMMAND, run_id) == before
    finally:
        fresh.close()
