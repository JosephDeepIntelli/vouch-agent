"""Worker command acknowledgement is fenced to the ACCEPTED version (M4 A3
review §3).

The exiting worker used to acknowledge whichever command record was CURRENT
— but PAUSED becomes durably visible before that acknowledgement runs, so a
resume issued in that interval was consumed by the OLD start worker's
``paused`` ack; dispatching that exact resume version afterwards refused as
already consumed and the run stayed paused forever. Correct contract,
asserted here at the review's exact ordering: an acknowledgement may only
consume the command THIS attempt accepted; a newer command issued after the
dispatch stays pending and its own worker completes the run.
"""

from __future__ import annotations

import importlib.util as _il
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.worker_lifecycle import (
    KIND_WORKER_COMMAND,
    _acknowledge_command,
    execute_run_detached,
    issue_worker_command,
)
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.tasks import TaskStatus

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
# Step one omits the required "done" key, so completing genuinely needs the
# second script — the resume worker's continuation is real work.
_SCRIPT_ONE = 'one = {"stage": 1}\nreturn one'
_SCRIPT_TWO = 'two = {"stage": 2, "done": True}\nreturn two'


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
        goal="Synthetic command fencing task",
        inputs={"fact": {"value": "synthetic", "source": "test"}},
        budget_usd=0.5,
        max_steps=4,
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )


def test_exiting_worker_does_not_consume_newer_resume_command(tmp_path: Path) -> None:
    """The review's exact handoff ordering, inverted to the correct outcome:
    a resume issued between the OLD worker's PAUSED dispatch and its
    acknowledgement stays PENDING (not consumed by the ``paused`` ack), and
    dispatching that exact resume version subsequently completes the run."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _scripts(tmp_path / "s")
    run_id = _submit(service)
    workspace.store.save("pause-request", run_id, {"requested": True})
    # seal the configuration paused so the worker starts from a pause
    assert service.execute(run_id, script_files=scripts).status is TaskStatus.PAUSED
    # the seal's pause consumed that request; arm a fresh one so the WORKER's
    # own dispatch also stops at the loop-top pause boundary (PAUSED durable)
    workspace.store.save("pause-request", run_id, {"requested": True})

    newer: dict[str, Any] = {}
    original_dispatch = __import__(
        "vouch_agent.appservices.worker_lifecycle", fromlist=["_dispatch"]
    )._dispatch

    def pause_then_issue_new_resume(*args: object, **kwargs: object) -> Any:
        outcome = original_dispatch(*args, **kwargs)
        assert outcome.status is TaskStatus.PAUSED
        # PAUSED is durably visible to another client here; it issues the
        # next explicit resume BEFORE the old attempt acknowledges.
        newer.update(issue_worker_command(workspace, run_id, "resume"))
        return outcome

    with patch(
        "vouch_agent.appservices.worker_lifecycle._dispatch", pause_then_issue_new_resume
    ):
        assert execute_run_detached(project, run_id) == 0  # a pause is a clean stop

    # the newer resume command was NOT consumed by the old worker's ack
    after = workspace.store.load(KIND_WORKER_COMMAND, run_id)
    assert after is not None
    assert after["command"] == "resume" and after["version"] == newer["version"]
    assert after["acknowledgedAt"] is None, (
        "the exiting start worker consumed a NEWER resume command "
        f"(result={after['result']!r})"
    )
    assert after["result"] is None
    assert service.status(run_id).status is TaskStatus.PAUSED

    # dispatching that exact resume version succeeds and completes the run
    assert (
        execute_run_detached(
            project, run_id, command="resume", command_version=int(after["version"])
        )
        == 0
    )
    final = service.status(run_id)
    assert final is not None and final.status is TaskStatus.COMPLETED
    result = service.result(run_id)
    assert result is not None
    payload = json.loads(workspace.artifacts.get(result.artifact_refs[-1]).decode("utf-8"))
    assert payload == {"stage": 2, "done": True}
    ack = workspace.store.load(KIND_WORKER_COMMAND, run_id)
    assert ack is not None and ack["result"] == "completed" and ack["acknowledgedAt"]
    workspace.close()


def test_acknowledgement_is_fenced_to_command_and_version(tmp_path: Path) -> None:
    """Unit fence: ``_acknowledge_command`` only consumes the record it
    accepted — a different command or a different version is left untouched
    for its own worker, whether or not it is still pending."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    run_id = _submit(ExecutionService(workspace))
    try:
        accepted = issue_worker_command(workspace, run_id, "start")
        # a DIFFERENT command (newer resume) is current: refuse to consume
        newer = issue_worker_command(workspace, run_id, "resume")
        assert _acknowledge_command(workspace, run_id, "paused", accepted=accepted) is False
        record = workspace.store.load(KIND_WORKER_COMMAND, run_id)
        assert record is not None
        assert record["version"] == newer["version"] and record["acknowledgedAt"] is None

        # the accepted version/command still acknowledges normally
        assert _acknowledge_command(workspace, run_id, "paused", accepted=newer) is True
        record = workspace.store.load(KIND_WORKER_COMMAND, run_id)
        assert record is not None and record["result"] == "paused"
        assert record["acknowledgedAt"] is not None
    finally:
        workspace.close()


def test_matching_acknowledgement_unchanged_for_plain_start(tmp_path: Path) -> None:
    """The common path is intact: a worker whose accepted command is still
    the current one acknowledges it with the durable outcome."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    run_id = _submit(ExecutionService(workspace))
    try:
        accepted = issue_worker_command(workspace, run_id, "start")
        assert _acknowledge_command(workspace, run_id, "completed", accepted=accepted) is True
        record = workspace.store.load(KIND_WORKER_COMMAND, run_id)
        assert record is not None
        assert record["command"] == "start" and record["result"] == "completed"
    finally:
        workspace.close()
