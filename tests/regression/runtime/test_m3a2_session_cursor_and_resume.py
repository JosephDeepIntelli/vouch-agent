"""Gate A2 regressions — durable session semantics through the DEFAULT path.

The reviewer proved every isolated step restarted the whole response pool
(draft, draft, draft) and ExecutionService.resume used ("placeholder",).
These regressions pin the corrected behavior: a monotonic query cursor, a
persisted execution configuration, and resume that continues the ORIGINAL
provider — verified through the public service, not a retained Supervisor.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from m3_runtime_helpers import init_native_project
from test_supervisor_jaz import SCRIPT_DRAFT, SCRIPT_FINAL

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.common import RunMode
from vouch_agent.orchestrator.supervisor import KIND_TASK_RUN
from vouch_agent.runtime.ports import WorkerSessionConfig

_SCHEMA = {
    "type": "object",
    "required": ["recommendation", "confidence", "priceUsd"],
    "properties": {
        "recommendation": {"type": "string"},
        "confidence": {"type": "string"},
        "priceUsd": {"type": "number"},
    },
}


def _scripts(tmp: Path) -> tuple[Path, ...]:
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in (("draft.py", SCRIPT_DRAFT), ("final.py", SCRIPT_FINAL)):
        path = tmp / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def test_two_step_task_completes_through_default_isolated_path(tmp_path: Path) -> None:
    """The reviewer's exact proof: draft then final — identical to the
    in-process control — through the DEFAULT (isolated) worker path."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        outcome = service.run(
            goal="Produce a recommendation",
            inputs={"candidates": ["A", "B"]},
            budget_usd=0.5,
            max_steps=3,
            script_files=_scripts(tmp_path / "scripts"),
            completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
        )
        assert outcome.status.value == "completed", outcome.error
        result = outcome.result
        assert result is not None
        final = json.loads(
            workspace.artifacts.get(result.artifact_refs[-1]).decode("utf-8")
        )
        assert final["confidence"] == "high"  # the SECOND script, not draft again
        assert final["priceUsd"] == 12.5
    finally:
        workspace.close()


def test_resume_continues_the_original_provider_not_a_placeholder(tmp_path: Path) -> None:
    """Pause before the first model step; a FRESH service must resume with
    the original scripted provider and complete (reviewer's resume proof)."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _scripts(tmp_path / "scripts")
    try:
        run_id = service.submit(
            goal="Produce a recommendation",
            inputs={"candidates": ["A", "B"]},
            budget_usd=0.5,
            max_steps=3,
            completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
        )
        # queue a pause before execution begins
        workspace.store.save("pause-request", run_id, {"requested": True})
        first = service.execute(run_id, script_files=scripts)
        assert first.status.value == "paused", first.error

        fresh_workspace = ProjectWorkspace.open(project)
        fresh_service = ExecutionService(fresh_workspace)
        resumed = fresh_service.resume(run_id)
        try:
            assert resumed.status.value == "completed", resumed.error
            result = resumed.result
            assert result is not None
            final = json.loads(
                fresh_workspace.artifacts.get(result.artifact_refs[-1]).decode("utf-8")
            )
            assert final["confidence"] == "high"
        finally:
            fresh_workspace.close()
    finally:
        workspace.close()


def test_execution_config_is_content_sealed_and_verified(tmp_path: Path) -> None:
    """The sealed execution configuration snapshots script CONTENT: resume is
    immune to later file changes (it runs the persisted pool), while a
    re-EXECUTE with different script bytes refuses on digest mismatch."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _scripts(tmp_path / "scripts")
    try:
        run_id = service.submit(
            goal="Produce a recommendation",
            inputs={"candidates": ["A", "B"]},
            budget_usd=0.5,
            max_steps=3,
            completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
        )
        workspace.store.save("pause-request", run_id, {"requested": True})
        assert service.execute(run_id, script_files=scripts).status.value == "paused"

        # (a) resume after the files were DELETED still completes: the config
        # holds the sealed content, not filesystem references
        for path in scripts:
            path.unlink()
        resumed = ExecutionService(ProjectWorkspace.open(project)).resume(run_id)
        assert resumed.status.value == "completed", resumed.error

        # (b) re-executing with a DIFFERENT pool refuses before dispatch
        scripts = _scripts(tmp_path / "scripts")  # recreate identical content
        run_id2 = service.submit(
            goal="Produce a recommendation",
            inputs={"candidates": ["A", "B"]},
            budget_usd=0.5,
            max_steps=3,
            completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
        )
        workspace.store.save("pause-request", run_id2, {"requested": True})
        assert service.execute(run_id2, script_files=scripts).status.value == "paused"
        tampered = _scripts(tmp_path / "tampered")
        (tampered[1]).write_text(
            SCRIPT_FINAL.replace("12.5", "999.0"), encoding="utf-8"
        )
        outcome = service.execute(run_id2, script_files=tampered)
        # the dispatch is REFUSED (config mismatch) — the run itself stays
        # paused with its sealed configuration, never switched mid-life
        assert outcome.error and (
            "digest" in outcome.error.lower() or "different provider" in outcome.error.lower()
        ), outcome.error
        assert outcome.status.value in ("paused", "failed")
    finally:
        workspace.close()


def test_cursor_never_replays_consumed_responses(tmp_path: Path) -> None:
    """A restart BETWEEN steps advances the cursor once: the second step sees
    the second response, never the first again."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _scripts(tmp_path / "scripts")
    try:
        run_id = service.submit(
            goal="Produce a recommendation",
            inputs={"candidates": ["A", "B"]},
            budget_usd=0.5,
            max_steps=4,
            completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
        )
        workspace.store.save("pause-request", run_id, {"requested": True})
        assert service.execute(run_id, script_files=scripts).status.value == "paused"
        # a fresh service resumes mid-task: consumed draft response is gone
        resumed = ExecutionService(ProjectWorkspace.open(project)).resume(run_id)
        assert resumed.status.value == "completed", resumed.error
        run = workspace.store.load(KIND_TASK_RUN, run_id)
        assert run is not None
        model_steps = [s for s in run["steps"] if s["kind"] == "model-call" and s["status"] == "ok"]
        # exactly the two intended model steps ran — no replayed first response
        assert len(model_steps) == 2
    finally:
        workspace.close()


# --- M4 A1 §2: per-step DELTA usage + absolute durable cursor ---------------------
#
# The M3 review proved the isolated per-step usage was the CUMULATIVE child
# snapshot persisted as per-step (llmCalls 1 then 2), and the resume cursor
# summed those counters (1+2=3) so a fresh resume SKIPPED response 3 and
# failed ReplayExhausted. These regressions pin the corrected behavior.

_THREE_STEP_POOL = (
    'one = {"recommendation": "candidate A", "confidence": "low"}\nreturn one',
    'two = {"recommendation": "candidate A", "confidence": "low"}\nreturn two',
    SCRIPT_FINAL,
)


def _write_pool(tmp: Path, pool: tuple[str, ...]) -> tuple[Path, ...]:
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, text in enumerate(pool):
        path = tmp / f"step{index}.py"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def _model_steps(service: ExecutionService, run_id: str) -> list[Any]:
    run = service.status(run_id)
    assert run is not None
    return [s for s in run.steps if s.kind.value == "model-call"]


def _pause_after_two(service: ExecutionService, run_id: str) -> Any:
    """Patch seam: insert a durable pause request after TWO model steps."""
    from unittest.mock import patch

    from vouch_agent.orchestrator.supervisor import Supervisor

    original = Supervisor._phase_model_call

    def hook(self: Any, state: Any, *args: Any) -> Any:
        result = original(self, state, *args)
        if state.model_ordinal == 3:
            self._store.save("pause-request", run_id, {"requested": True})
        return result

    return patch.object(Supervisor, "_phase_model_call", hook)


def test_per_step_usage_is_delta_and_cursor_is_absolute(tmp_path: Path) -> None:
    """Three distinct responses; a deterministic pause after TWO model steps.
    Each step's persisted llmCalls is its own DELTA (1 and 1, not 1 and 2),
    and the durable cursor is the ABSOLUTE 2 — not the sum 3."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _write_pool(tmp_path / "three", _THREE_STEP_POOL)
    run_id = service.submit(
        goal="Produce a recommendation",
        inputs={"candidates": ["A", "B"]},
        budget_usd=0.5,
        max_steps=4,
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )
    try:
        with _pause_after_two(service, run_id):
            first = service.execute(run_id, script_files=scripts)
        assert first.status.value == "paused", first.error
        steps = _model_steps(service, run_id)
        assert len(steps) == 2
        deltas = [s.usage.get("llmCalls") for s in steps]
        absolutes = [s.usage.get("queryCursor") for s in steps]
        assert deltas == [1, 1], f"per-step llmCalls must be DELTAS, got {deltas}"
        assert absolutes == [1, 2], f"absolute queryCursor per step, got {absolutes}"
        assert steps[1].usage.get("measuredUsd") == pytest.approx(0.01)
        assert service._durable_cursor(run_id) == 2, "cursor must be the absolute 2, not 1+2"
    finally:
        workspace.close()


_RESUME_CHILD = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from vouch_agent.appservices.execution import ExecutionService\n"
    "from vouch_agent.appservices.workspace import ProjectWorkspace\n"
    "project, run_id = sys.argv[1], sys.argv[2]\n"
    "ws = ProjectWorkspace.open(Path(project))\n"
    "outcome = ExecutionService(ws).resume(run_id)\n"
    "run = ExecutionService(ws).status(run_id)\n"
    "steps = [s for s in run.steps if s.kind.value == 'model-call' and s.status == 'ok']\n"
    "print(json.dumps({'status': outcome.status.value, 'error': outcome.error,\n"
    "  'model_steps': len(steps),\n"
    "  'cost': outcome.result.total_cost_usd if outcome.result else None,\n"
    "  'settled': ws.ledger.settled_usd(), 'outstanding': ws.ledger.outstanding_usd()}))\n"
)


def test_fresh_process_resume_consumes_third_response_exactly_once(tmp_path: Path) -> None:
    """Close the service after the pause; a FRESH PROCESS resume consumes
    response 3 exactly once: completed, three model steps total, the final
    artifact IS the third response, cost $0.03 everywhere, cursor 3."""
    import subprocess
    import sys

    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _write_pool(tmp_path / "three", _THREE_STEP_POOL)
    run_id = service.submit(
        goal="Produce a recommendation",
        inputs={"candidates": ["A", "B"]},
        budget_usd=0.5,
        max_steps=4,
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )
    try:
        with _pause_after_two(service, run_id):
            assert service.execute(run_id, script_files=scripts).status.value == "paused"
    finally:
        workspace.close()  # the submitting client is GONE

    proc = subprocess.run(
        [sys.executable, "-c", _RESUME_CHILD, str(project), run_id],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["status"] == "completed", report
    assert report["model_steps"] == 3, report  # 2 before the pause + response 3 ONCE
    assert report["cost"] == pytest.approx(0.03), report
    assert report["settled"] == pytest.approx(0.03), report
    assert report["outstanding"] == pytest.approx(0.0), report

    fresh = ProjectWorkspace.open(project)
    try:
        result = ExecutionService(fresh).result(run_id)
        assert result is not None
        final = json.loads(fresh.artifacts.get(result.artifact_refs[-1]).decode("utf-8"))
        assert final["confidence"] == "high" and final["priceUsd"] == 12.5
        assert ExecutionService(fresh)._durable_cursor(run_id) == 3
    finally:
        fresh.close()


def test_cursor_config_identity_refuses_a_switched_pool(tmp_path: Path) -> None:
    """The durable cursor is bound to the sealed config: a cursor record
    recorded under a different config identity refuses (fail closed) instead
    of positioning a run against a pool it never consumed from."""
    from vouch_agent.orchestrator.supervisor import KIND_RUN_QUERY_CURSOR

    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    scripts = _write_pool(tmp_path / "three", _THREE_STEP_POOL)
    run_id = service.submit(
        goal="Produce a recommendation",
        inputs={"candidates": ["A", "B"]},
        budget_usd=0.5,
        max_steps=4,
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )
    try:
        with _pause_after_two(service, run_id):
            assert service.execute(run_id, script_files=scripts).status.value == "paused"
        record = workspace.store.load(KIND_RUN_QUERY_CURSOR, run_id)
        assert record is not None and record["cursor"] == 2
        record["configIdentity"] = "sha256:some-other-pool"
        workspace.store.save(KIND_RUN_QUERY_CURSOR, run_id, record)
        with pytest.raises(Exception, match=r"cursor|config"):
            service._durable_cursor(run_id)
    finally:
        workspace.close()


def test_child_restart_between_steps_advances_cursor_once(tmp_path: Path) -> None:
    """A worker child that dies BETWEEN steps is respawned at the monotonic
    cursor: the next step sees the NEXT response, never a replayed one, and
    the session's per-step delta stays correct across the restart."""
    import os
    import signal

    from vouch_agent.runtime.isolated_runtime import IsolatedStepRuntime

    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    scripts = _write_pool(tmp_path / "three", _THREE_STEP_POOL)
    texts = tuple(path.read_text(encoding="utf-8") for path in scripts)
    runtime = IsolatedStepRuntime()
    session = runtime.open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=4,
            wall_clock_s=30.0,
            max_cost_usd=0.5,
            scripted_responses=texts,
        )
    )
    try:
        first = session.step("step one", scope={})
        assert first.raw.get("return_value") == {
            "recommendation": "candidate A",
            "confidence": "low",
        }
        # kill the guarded child BETWEEN steps; the next step must respawn at
        # the monotonic cursor (the first worker pid no longer exists)
        channel = session._channel
        assert channel is not None and channel.worker_pid is not None
        os.kill(channel.worker_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5.0
        while channel.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not channel.alive()
        second = session.step("step two", scope={})
        assert second.raw.get("return_value") == {
            "recommendation": "candidate A",
            "confidence": "low",
        }
        third = session.step("step three", scope={})
        assert third.raw.get("return_value")["priceUsd"] == 12.5  # response 3, not 1 or 2
        usage = session.usage()
        assert usage["llm_calls"] == 3
        assert usage["scripted_cursor"] == 3
        # per-step DELTAS even across the child restart
        assert second.raw.get("llm_calls") == 1
        assert third.raw.get("llm_calls") == 1
    finally:
        session.close()
        workspace.close()


def test_nested_step_counts_both_queries_in_delta_and_cursor(tmp_path: Path) -> None:
    """A step whose code performs a NESTED invoke consumes TWO underlying
    queries: the persisted delta is 2 and the absolute cursor advances to 2
    after that single step."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    nested = (
        'sub = invoke(task="Verify the primary candidate")\nreturn sub',
        'nested = {"recommendation": "candidate A (verified)", '
        '"confidence": "high", "priceUsd": 12.5}\nreturn nested',
    )
    scripts = _write_pool(tmp_path / "nested", nested)
    try:
        outcome = service.run(
            goal="Produce a recommendation",
            inputs={"candidates": ["A", "B"]},
            budget_usd=0.5,
            max_steps=3,
            script_files=scripts,
            completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
        )
        assert outcome.status.value == "completed", outcome.error
        steps = _model_steps(service, outcome.run_id)
        assert len(steps) == 1
        assert steps[0].usage.get("llmCalls") == 2
        assert steps[0].usage.get("queryCursor") == 2
        assert steps[0].usage.get("sessionLlmCalls") == 2
        assert service._durable_cursor(outcome.run_id) == 2
    finally:
        workspace.close()
