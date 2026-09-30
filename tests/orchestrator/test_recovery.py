"""Restart recovery: hard crashes between persist phases (injected at the
metadata store, so the exception propagates out of the supervisor exactly
like a dead process), ``recover()`` classification without execution, and
the fixture auto-continue rule.
"""

from __future__ import annotations

import pytest
from fakes import (
    FakeMetadataStore,
    ScriptedCall,
    SimulatedCrash,
    make_harness,
    schema_spec,
    two_step_script,
)

from vouch_agent.contracts import RunMode
from vouch_agent.contracts.tasks import StepKind, TaskStatus
from vouch_agent.errors import ContractError, ReconciliationRequiredError
from vouch_agent.orchestrator import (
    CHECKPOINT_KIND,
    KIND_TASK_RUN,
    RecoveryClassification,
    recover,
)


def _crash_after_step_intent(running_kind: str = "model-call") -> FakeMetadataStore:
    """Store that dies on the checkpoint write FOLLOWING a step-intent save.

    The intent (task-run with the step ``running``) is already on disk, the
    side-effecting call has not happened: exactly the "crashed between
    persist phases" state of design §10.
    """

    def predicate(save_kind: str, record_id: str, data: dict) -> bool:
        if save_kind != CHECKPOINT_KIND:
            return False
        steps = (data.get("run") or {}).get("steps") or []
        if not steps:
            return False
        last = steps[-1]
        return last["kind"] == running_kind and last["status"] == "running"

    return FakeMetadataStore(raise_when=predicate)


def test_hard_crash_mid_model_step_yields_persisted_in_flight_state() -> None:
    store = _crash_after_step_intent("model-call")
    h = make_harness(two_step_script(), store=store)
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    with pytest.raises(SimulatedCrash):
        h.supervisor.execute(run_id)

    # persisted truth: run RUNNING with the model step in flight (intent only)
    run = h.supervisor.get_run(run_id)
    assert run is not None and run.status is TaskStatus.RUNNING
    in_flight = [s for s in run.steps if s.status == "running"]
    assert len(in_flight) == 1 and in_flight[0].kind is StepKind.MODEL_CALL
    assert h.runtime.calls == 0  # the call itself never happened

    report = recover(h.store, run_id)
    assert report.classification is RecoveryClassification.NEEDS_RECONCILIATION
    assert "unknown outcome" in report.detail

    # recovery is read-only: nothing executed while classifying
    calls_before = h.runtime.calls
    recover(h.store, run_id)
    assert h.runtime.calls == calls_before


def test_hard_crash_mid_model_step_resume_never_replays() -> None:
    # The crash happened BEFORE the runtime call (intent persisted, no call),
    # but persisted state cannot prove that — so the step is marked unknown
    # and consumed; the resumed run only performs NEW steps.
    store = _crash_after_step_intent("model-call")
    h = make_harness(two_step_script(), store=store)
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    with pytest.raises(SimulatedCrash):
        h.supervisor.execute(run_id)
    assert h.runtime.calls == 0  # died before the first call

    report = recover(h.store, run_id)
    assert report.classification is RecoveryClassification.NEEDS_RECONCILIATION

    run = h.supervisor.resume(run_id, "checked broker log: no model call was dispatched")
    # the unknown step is consumed: two NEW model steps (draft fails schema,
    # final passes), never a re-attempt recorded as the same step
    assert h.runtime.calls == 2
    assert run.status is TaskStatus.COMPLETED
    persisted = h.supervisor.get_run(run_id)
    assert persisted is not None
    statuses = [(s.kind.value, s.status) for s in persisted.steps]
    assert ("model-call", "unknown") in statuses  # honestly on record
    assert statuses.count(("model-call", "ok")) == 2
    # every persisted model step has a distinct step id (no overwrite/replay)
    model_ids = [s.step_id for s in persisted.steps if s.kind is StepKind.MODEL_CALL]
    assert len(set(model_ids)) == 3
    package = h.supervisor.get_result(run_id)
    assert package is not None and package.deliverable()

    # and the classification is terminal now
    assert recover(h.store, run_id).classification is RecoveryClassification.TERMINAL


def test_hard_crash_mid_deterministic_step_classifies_clean_resume() -> None:
    store = _crash_after_step_intent("plan")
    h = make_harness(two_step_script(), store=store)
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    with pytest.raises(SimulatedCrash):
        h.supervisor.execute(run_id)

    report = recover(h.store, run_id)
    assert report.classification is RecoveryClassification.CLEAN_RESUME
    assert "deterministic" in report.detail

    # fixture run: a fresh supervisor executes (auto-continue), no note needed
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.COMPLETED
    statuses = [(s.kind.value, s.status) for s in run.steps]
    assert ("plan", "skipped") in statuses and ("plan", "ok") in statuses


def test_queued_run_classifies_clean_resume() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec())
    report = recover(h.store, run_id)
    assert report.classification is RecoveryClassification.CLEAN_RESUME
    assert h.runtime.calls == 0


def test_completed_run_classifies_terminal() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    h.supervisor.execute(run_id)
    report = recover(h.store, run_id)
    assert report.classification is RecoveryClassification.TERMINAL
    assert report.run.status is TaskStatus.COMPLETED


def test_recover_unknown_run_raises() -> None:
    h = make_harness()
    with pytest.raises(ContractError):
        recover(h.store, "run_does_not_exist")


def test_fixture_auto_continue_without_unknown_steps() -> None:
    # A fixture run parked in needs-reconciliation with NO unknown steps
    # (e.g. an operator/recovery tool marked it during investigation) may
    # auto-continue: deterministic offline work is safely resumable.
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    data = h.store.load(KIND_TASK_RUN, run_id)
    assert data is not None
    data["status"] = TaskStatus.NEEDS_RECONCILIATION.value
    h.store.save(KIND_TASK_RUN, run_id, data)

    run = h.supervisor.resume(run_id, "")  # no note: fixture + all steps known
    assert run.status is TaskStatus.COMPLETED
    assert h.runtime.calls == 2


def test_non_fixture_needs_reconciliation_always_requires_note() -> None:
    h = make_harness(
        two_step_script(),
    )
    spec = schema_spec(mode=RunMode.OFFLINE_EVALUATION, max_cost_usd=0.5)
    run_id = h.supervisor.submit(spec)
    data = h.store.load(KIND_TASK_RUN, run_id)
    assert data is not None
    data["status"] = TaskStatus.NEEDS_RECONCILIATION.value
    h.store.save(KIND_TASK_RUN, run_id, data)

    with pytest.raises(ReconciliationRequiredError):
        h.supervisor.resume(run_id, "")
    run = h.supervisor.resume(run_id, "offline materials re-verified, no side effects")
    assert run.status is TaskStatus.COMPLETED


def test_resume_after_crash_settles_reservation_exactly_once() -> None:
    store = _crash_after_step_intent("model-call")
    h = make_harness(
        [
            ScriptedCall(content='{"recommendation": "A"}', cost_usd=0.01),
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.01,
            ),
            ScriptedCall(content="unused", cost_usd=0.01),
        ],
        store=store,
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    with pytest.raises(SimulatedCrash):
        h.supervisor.execute(run_id)
    run = h.supervisor.resume(run_id, "verified no call dispatched")
    assert run.status is TaskStatus.COMPLETED
    reservation = next(iter(h.ledger.reservations.values()))
    # conservative total: measured 0.02 + the unknown step's full slice 0.25
    assert reservation.settled_amount_usd == pytest.approx(0.27)


def test_execute_on_hard_crashed_running_run_demands_reconciliation() -> None:
    store = _crash_after_step_intent("model-call")
    h = make_harness(two_step_script(), store=store)
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    with pytest.raises(SimulatedCrash):
        h.supervisor.execute(run_id)

    # execute() must NOT auto-resume unknown side-effect state
    with pytest.raises(ReconciliationRequiredError):
        h.supervisor.execute(run_id)
    assert h.runtime.calls == 0
