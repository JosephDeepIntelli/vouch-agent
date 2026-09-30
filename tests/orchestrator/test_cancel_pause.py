"""Cooperative cancellation and pause between steps: partial steps kept,
a cancelled run is never "completed", a paused run resumes without
re-doing completed phases.
"""

from __future__ import annotations

import pytest
from fakes import make_harness, schema_spec, two_step_script

from vouch_agent.contracts.journal import EventKind, ReservationStatus
from vouch_agent.contracts.tasks import StepKind, TaskStatus
from vouch_agent.errors import InvalidStateTransitionError


def test_cancel_between_steps_keeps_partial_work() -> None:
    # The fake runtime's on_call hook cancels the run from inside model
    # call 1 (i.e. between step boundaries); the loop must finish that step,
    # then stop at the boundary without running model call 2.
    holder: dict[str, object] = {}

    def cancel_after_first(index: int) -> None:
        supervisor = holder["supervisor"]
        run_id = holder["run_id"]
        if index == 1:
            returned = supervisor.cancel(run_id, reason="user asked to stop")
            holder["cancel_return_status"] = returned.status

    h = make_harness(two_step_script(), on_call=cancel_after_first)
    holder["supervisor"] = h.supervisor
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    holder["run_id"] = run_id
    run = h.supervisor.execute(run_id)

    assert holder["cancel_return_status"] is TaskStatus.RUNNING  # cooperative
    assert run.status is TaskStatus.CANCELLED
    assert h.runtime.calls == 1  # the in-flight step finished, the next never ran
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable() is False
    assert package.conclusion.startswith("cancelled")
    assert any("cancelled by request" in i for i in package.not_done_items)
    # partial steps kept, including the completed model step
    model_steps = [s for s in run.steps if s.kind is StepKind.MODEL_CALL]
    assert len(model_steps) == 1 and model_steps[0].status == "ok"
    assert package.artifact_refs  # the partial artifact is handed over
    # budget settled for what ran
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.status is ReservationStatus.SETTLED
    assert reservation.settled_amount_usd == pytest.approx(0.01)
    # a second cancel on a terminal run is refused
    with pytest.raises(InvalidStateTransitionError):
        h.supervisor.cancel(run_id)


def test_cancel_queued_run_never_starts_work() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec())
    run = h.supervisor.cancel(run_id, reason="changed my mind")
    assert run.status is TaskStatus.CANCELLED
    assert h.runtime.calls == 0
    package = h.supervisor.get_result(run_id)
    assert package is not None and package.deliverable() is False
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.settled_amount_usd == pytest.approx(0.0)


def test_execute_terminal_run_raises() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec())
    h.supervisor.cancel(run_id)
    with pytest.raises(InvalidStateTransitionError):
        h.supervisor.execute(run_id)
    with pytest.raises(InvalidStateTransitionError):
        h.supervisor.resume(run_id, "note")


def test_pause_between_steps_then_resume_completes() -> None:
    holder: dict[str, object] = {}

    def pause_after_first(index: int) -> None:
        if index == 1:
            holder["supervisor"].pause(holder["run_id"])

    h = make_harness(two_step_script(), on_call=pause_after_first)
    holder["supervisor"] = h.supervisor
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    holder["run_id"] = run_id
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.PAUSED
    assert h.runtime.calls == 1
    assert h.supervisor.get_result(run_id) is None  # no package while paused
    assert EventKind.RUN_PAUSED.value in h.journal.kinds(run_id)

    # resume: continues from where it stopped, completes the second step
    resumed = h.supervisor.execute(run_id)
    assert resumed.status is TaskStatus.COMPLETED
    # earlier steps preserved verbatim (prefix), not redone
    assert resumed.steps[: len(run.steps)] == run.steps
    model_steps = [s for s in resumed.steps if s.kind is StepKind.MODEL_CALL]
    assert len(model_steps) == 2 and all(s.status == "ok" for s in model_steps)
    package = h.supervisor.get_result(run_id)
    assert package is not None and package.deliverable()


def test_pause_only_allowed_while_running() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec())
    with pytest.raises(InvalidStateTransitionError):
        h.supervisor.pause(run_id)  # QUEUED has no PAUSED edge
    # after completion it is terminal
    h.supervisor.execute(run_id)
    with pytest.raises(InvalidStateTransitionError):
        h.supervisor.pause(run_id)


def test_pause_of_hard_crashed_running_run_pauses_directly() -> None:
    # A crash left the run RUNNING with no active loop in this supervisor;
    # pause() applies immediately at the persisted state machine.
    from fakes import FakeMetadataStore

    from vouch_agent.orchestrator import CHECKPOINT_KIND

    def crash_on_first_checkpoint(save_kind: str, record_id: str, data: dict) -> bool:
        return save_kind == CHECKPOINT_KIND and data.get("sequence") == 2

    store = FakeMetadataStore(raise_when=crash_on_first_checkpoint)
    h = make_harness(two_step_script(), store=store)
    from fakes import SimulatedCrash

    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    with pytest.raises(SimulatedCrash):
        h.supervisor.execute(run_id)
    assert h.supervisor.get_run(run_id).status is TaskStatus.RUNNING

    paused = h.supervisor.pause(run_id)
    assert paused.status is TaskStatus.PAUSED
    assert h.supervisor.get_result(run_id) is None
    resumed = h.supervisor.execute(run_id)
    assert resumed.status is TaskStatus.COMPLETED
