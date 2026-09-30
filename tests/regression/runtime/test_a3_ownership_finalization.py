"""A3 regressions (orchestrator): durable run ownership, authoritative
cancellation and exactly-once terminal finalization.

Converted from the coordinator reproductions ``reproduce_ownership.py`` and
``reproduce_finalization.py`` (review-runtime-20260928, defects 4 and 5),
assertions inverted:

* a second supervisor may REQUEST cancel/pause but must never finalize
  another supervisor's actively-owned run — cancellation stays
  authoritative, the journal and the ledger agree, no double settlement;
* a crash at any terminal-write boundary (result save, ledger settlement,
  metadata/journal finalization) recovers exactly once on restart without
  replaying model/effect work — terminal status alone is not proof that the
  bookkeeping completed;
* the wall-clock allowance is persisted across resume (resuming must not
  reset it) and a paused run expires with its accounting kept safe.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fakes import (
    FakeMetadataStore,
    ScriptedCall,
    SimulatedCrash,
    make_harness,
    schema_spec,
    two_step_script,
)

from vouch_agent.contracts.journal import EventKind
from vouch_agent.contracts.tasks import TaskStatus
from vouch_agent.errors import InvalidStateTransitionError
from vouch_agent.orchestrator import (
    KIND_RUN_FINALIZATION,
    RecoveryClassification,
    Supervisor,
    SupervisorPolicy,
    recover,
)
from vouch_agent.orchestrator.records import FinalizationIntent

# --- defect 4: a second supervisor cancels another active run -------------------


def test_second_supervisor_cannot_finalize_an_active_run() -> None:
    """The deterministic interleaving from the reproduction: supervisor B
    calls cancel() while A is inside its model call."""
    state: dict[str, object] = {}

    def cancel_from_second_client(_index: int) -> None:
        second = state["second"]
        assert isinstance(second, Supervisor)
        returned = second.cancel(str(state["run_id"]), "second-client cancellation")
        state["cancellation_status"] = returned.status.value

    harness = make_harness(
        [
            ScriptedCall(
                content='{"recommendation":"A","confidence":"high","priceUsd":12.5}', cost_usd=0.01
            )
        ],
        on_call=cancel_from_second_client,
    )
    state["second"] = Supervisor(
        harness.runtime,
        harness.store,
        harness.artifacts,
        harness.ledger,
        harness.journal,
        harness.policy,
    )
    run_id = harness.supervisor.submit(schema_spec())
    state["run_id"] = run_id

    run = harness.supervisor.execute(run_id)  # A finalizes at its next boundary

    # B did NOT finalize the in-flight run: it only recorded the request
    assert state["cancellation_status"] == TaskStatus.RUNNING.value
    # A's own loop applied the authoritative cancellation
    assert run.status is TaskStatus.CANCELLED
    assert harness.supervisor.get_run(run_id).status is TaskStatus.CANCELLED
    # no stale "completed" snapshot, no ReservationError, and money agrees
    journal_cost = sum(
        e.amount_usd or 0.0 for e in harness.journal.cost_entries(run_id) if e.measurable
    )
    assert journal_cost == pytest.approx(0.01)
    assert harness.ledger.settled_usd() == pytest.approx(0.01)
    assert harness.ledger.outstanding_usd() == pytest.approx(0.0)


def test_second_supervisor_cannot_pause_finalize_an_active_run() -> None:
    state: dict[str, object] = {}

    def pause_from_second_client(_index: int) -> None:
        second = state["second"]
        assert isinstance(second, Supervisor)
        returned = second.pause(str(state["run_id"]))
        state["pause_status"] = returned.status.value

    harness = make_harness(
        [
            ScriptedCall(content='{"recommendation":"A"}', cost_usd=0.01),
            ScriptedCall(
                content='{"recommendation":"A","confidence":"high","priceUsd":1}', cost_usd=0.01
            ),
        ],
        on_call=pause_from_second_client,
    )
    state["second"] = Supervisor(
        harness.runtime,
        harness.store,
        harness.artifacts,
        harness.ledger,
        harness.journal,
        harness.policy,
    )
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    state["run_id"] = run_id
    run = harness.supervisor.execute(run_id)

    assert state["pause_status"] == TaskStatus.RUNNING.value  # request only
    assert run.status is TaskStatus.PAUSED  # the owner paused at its boundary
    assert harness.supervisor.get_result(run_id) is None  # nothing finalized by B


def test_second_supervisor_cannot_execute_an_owned_run() -> None:
    """Two clients may not run the same run at the same time."""
    started = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_inside_first_call(_index: int) -> None:
        started.set()
        release.wait(timeout=10)

    harness = make_harness(
        [
            ScriptedCall(content='{"recommendation":"A"}', cost_usd=0.01),
            ScriptedCall(
                content='{"recommendation":"A","confidence":"high","priceUsd":1}', cost_usd=0.01
            ),
        ],
        on_call=hold_inside_first_call,
    )
    second = Supervisor(
        harness.runtime,
        harness.store,
        harness.artifacts,
        harness.ledger,
        harness.journal,
        harness.policy,
    )
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))

    def run_first() -> None:
        try:
            harness.supervisor.execute(run_id)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run_first)
    first.start()
    assert started.wait(timeout=5)
    with pytest.raises(Exception, match="owned by supervisor"):
        second.execute(run_id)
    release.set()
    first.join(timeout=10)
    assert errors == []


def test_stale_owner_cannot_overwrite_the_new_decision() -> None:
    """After a lease expiry takeover, the old owner's writes are fenced."""
    short_lease = SupervisorPolicy(ownership_lease_s=0.05)
    harness = make_harness(two_step_script(), policy=short_lease)
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))

    # A takes the lease (short), then the lease expires and B takes over and
    # finalizes the cancellation.
    harness.supervisor._acquire_ownership(run_id, purpose="test")  # type: ignore[report-private-usage]
    stale_state = harness.supervisor._load_state(run_id)  # type: ignore[report-private-usage]

    new_owner = Supervisor(
        harness.runtime,
        harness.store,
        harness.artifacts,
        harness.ledger,
        harness.journal,
        harness.policy,
    )
    import time as _time

    _time.sleep(0.1)  # the lease A holds expires
    cancelled = new_owner.cancel(run_id, "operator cancel")
    assert cancelled.status is TaskStatus.CANCELLED

    # A's stale snapshot must not overwrite the persisted cancellation
    from vouch_agent.orchestrator import RunOwnershipError

    with pytest.raises(RunOwnershipError):
        harness.supervisor._persist_run(stale_state)  # type: ignore[report-private-usage]
    assert new_owner.get_run(run_id).status is TaskStatus.CANCELLED


# --- defect 5: crash during finalization -----------------------------------------


def test_crash_before_run_settlement_recovers_exactly_once() -> None:
    """The reproduction: a storage failure at the run settlement call."""
    harness = make_harness(two_step_script())
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    original_settle = harness.ledger.settle_parent_from_children

    def storage_failure(*args, **kwargs):
        raise RuntimeError("synthetic process/storage failure before run settlement")

    harness.ledger.settle_parent_from_children = storage_failure  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        harness.supervisor.execute(run_id)
    harness.ledger.settle_parent_from_children = original_settle  # type: ignore[method-assign]

    # recovery: the unfinished finalization is visible and is finished once
    report = recover(harness.store, run_id)
    assert report.classification is RecoveryClassification.FINALIZATION_PENDING
    resumed = harness.supervisor.resume(run_id)
    assert resumed.status is TaskStatus.COMPLETED
    # the work was NOT replayed: still two model calls
    assert harness.runtime.calls == 2
    assert harness.ledger.outstanding_usd() == pytest.approx(0.0)
    assert harness.ledger.settled_usd() == pytest.approx(0.03)
    # finishing again is a no-op, and a terminal run refuses further resume
    assert recover(harness.store, run_id).classification is RecoveryClassification.TERMINAL
    with pytest.raises(InvalidStateTransitionError):
        harness.supervisor.resume(run_id)
    assert harness.runtime.calls == 2


def test_crash_after_result_save_before_ledger_settlement() -> None:
    def dies_on_package_save(kind: str, record_id: str, data: dict) -> bool:
        return kind == "result-package" and record_id == run_id

    store = FakeMetadataStore(raise_when=dies_on_package_save)
    harness = make_harness(two_step_script(), store=store)
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    with pytest.raises(SimulatedCrash):
        harness.supervisor.execute(run_id)

    # the intent was written before the package save: recovery completes all
    # terminal writes exactly once
    intent = store.load(KIND_RUN_FINALIZATION, run_id)
    assert intent is not None
    assert FinalizationIntent.from_dict(intent).package_saved is False

    resumed = harness.supervisor.resume(run_id)
    assert resumed.status is TaskStatus.COMPLETED
    assert harness.runtime.calls == 2
    assert harness.ledger.settled_usd() == pytest.approx(0.03)
    assert harness.supervisor.get_result(run_id) is not None


def test_crash_between_ledger_settlement_and_terminal_status() -> None:
    """The ledger settled but the run record never reached its terminal
    status: recovery finishes the transition without replaying work."""
    holder: dict[str, str] = {}

    def dies_on_terminal_status(kind: str, record_id: str, data: dict) -> bool:
        if kind != "task-run" or record_id != holder.get("run_id"):
            return False
        return data.get("status") in ("completed", "failed", "cancelled")

    store = FakeMetadataStore(raise_when=dies_on_terminal_status)
    harness = make_harness(two_step_script(), store=store)
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    holder["run_id"] = run_id
    with pytest.raises(SimulatedCrash):
        harness.supervisor.execute(run_id)

    # money already settled; the run record is still RUNNING
    assert harness.ledger.settled_usd() == pytest.approx(0.03)
    assert harness.supervisor.get_run(run_id).status is TaskStatus.RUNNING

    report = recover(harness.store, run_id)
    assert report.classification is RecoveryClassification.FINALIZATION_PENDING
    resumed = harness.supervisor.resume(run_id)
    assert resumed.status is TaskStatus.COMPLETED
    assert harness.runtime.calls == 2  # no replay
    assert harness.ledger.settled_usd() == pytest.approx(0.03)  # settled once


def test_recovered_finalization_never_double_settles() -> None:
    """Crash AFTER the ledger committed but BEFORE the flag was written:
    recovery must accept the committed settlement, not raise on it."""
    harness = make_harness(two_step_script())
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    calls = {"n": 0}
    original = harness.ledger.settle_parent_from_children

    def settle_once_then_die(*args, **kwargs):
        calls["n"] += 1
        original(*args, **kwargs)
        raise RuntimeError("crash right after the ledger committed")

    harness.ledger.settle_parent_from_children = settle_once_then_die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        harness.supervisor.execute(run_id)
    harness.ledger.settle_parent_from_children = original  # type: ignore[method-assign]
    assert calls["n"] == 1

    resumed = harness.supervisor.resume(run_id)
    assert resumed.status is TaskStatus.COMPLETED
    assert harness.ledger.settled_usd() == pytest.approx(0.03)
    assert harness.runtime.calls == 2
    kinds = [e.kind for e in harness.journal.events(run_id)]
    assert EventKind.AUDIT_NOTE in kinds  # the already-committed settlement is on record


def test_crash_after_terminal_status_before_checkpoint_recovers() -> None:
    """The run reached its terminal status but the recovery-point/journal
    finalization and the ownership release never ran."""

    holder: dict[str, str] = {}

    def dies_on_final_checkpoint(kind: str, record_id: str, data: dict) -> bool:
        if kind != "checkpoint" or record_id != holder.get("run_id"):
            return False
        run = data.get("run") or {}
        return run.get("status") in ("completed", "failed", "cancelled")

    store = FakeMetadataStore(raise_when=dies_on_final_checkpoint)
    harness = make_harness(two_step_script(), store=store)
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    holder["run_id"] = run_id
    with pytest.raises(SimulatedCrash):
        harness.supervisor.execute(run_id)

    assert harness.supervisor.get_run(run_id).status is TaskStatus.COMPLETED
    report = recover(harness.store, run_id)
    assert report.classification is RecoveryClassification.FINALIZATION_PENDING

    resumed = harness.supervisor.resume(run_id)
    assert resumed.status is TaskStatus.COMPLETED
    assert harness.runtime.calls == 2  # no replay
    assert harness.ledger.settled_usd() == pytest.approx(0.03)
    assert recover(harness.store, run_id).classification is RecoveryClassification.TERMINAL


# --- persisted wall-clock + paused-run expiry ------------------------------------


def test_resume_does_not_reset_the_wall_clock_allowance() -> None:
    """A paused run keeps its original deadline: resuming must not hand it a
    fresh allowance."""
    from vouch_agent.orchestrator import KIND_RUN_CLOCK

    holder: dict[str, object] = {}

    def pause_after_first(index: int) -> None:
        if index == 1:
            holder["supervisor"].pause(holder["run_id"])  # type: ignore[union-attr]

    harness = make_harness(
        [
            ScriptedCall(content='{"recommendation":"A"}', cost_usd=0.01),
            ScriptedCall(
                content='{"recommendation":"A","confidence":"high","priceUsd":1}', cost_usd=0.01
            ),
        ],
        on_call=pause_after_first,
    )
    holder["supervisor"] = harness.supervisor
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5, max_wall_clock_s=60))
    holder["run_id"] = run_id
    run = harness.supervisor.execute(run_id)
    assert run.status is TaskStatus.PAUSED

    clock = harness.store.load(KIND_RUN_CLOCK, run_id)
    assert clock is not None
    deadline = float(clock["deadlineEpochS"])

    # Forge an already-spent allowance — the run was paused until after its
    # deadline — then resume: the ORIGINAL deadline governs, so no further
    # model work may start. A resume that reset the allowance would run the
    # second step and complete.
    import time as _time

    clock["deadlineEpochS"] = _time.time() - 1
    harness.store.save(KIND_RUN_CLOCK, run_id, clock)
    resumed = harness.supervisor.execute(run_id)
    assert resumed.status is TaskStatus.FAILED
    assert "wall-clock" in (resumed.error or "")
    assert harness.runtime.calls == 1  # the second model step never ran
    assert deadline > 0


def test_expired_paused_run_refuses_resume_but_keeps_safe_accounting() -> None:
    import time as _time

    from vouch_agent.orchestrator import KIND_RUN_CLOCK

    holder: dict[str, object] = {}

    def pause_after_first(index: int) -> None:
        if index == 1:
            holder["supervisor"].pause(holder["run_id"])  # type: ignore[union-attr]

    harness = make_harness(
        [
            ScriptedCall(content='{"recommendation":"A"}', cost_usd=0.01),
            ScriptedCall(
                content='{"recommendation":"A","confidence":"high","priceUsd":1}', cost_usd=0.01
            ),
        ],
        on_call=pause_after_first,
    )
    holder["supervisor"] = harness.supervisor
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    holder["run_id"] = run_id
    run = harness.supervisor.execute(run_id)
    assert run.status is TaskStatus.PAUSED

    clock = harness.store.load(KIND_RUN_CLOCK, run_id)
    assert clock is not None
    clock["pauseExpiresAtEpochS"] = _time.time() - 1  # already expired
    harness.store.save(KIND_RUN_CLOCK, run_id, clock)

    with pytest.raises(InvalidStateTransitionError, match="paused past its expiry"):
        harness.supervisor.execute(run_id)

    # safe accounting: the reservation is still open (counting against the
    # project) until the run is explicitly cancelled
    assert harness.ledger.outstanding_usd() == pytest.approx(0.5)
    cancelled = harness.supervisor.cancel(run_id, "paused too long")
    assert cancelled.status is TaskStatus.CANCELLED
    assert harness.ledger.outstanding_usd() == pytest.approx(0.0)
    assert harness.ledger.settled_usd() == pytest.approx(0.01)
    assert harness.supervisor.get_result(run_id) is not None


# --- supporting checks --------------------------------------------------------------


def test_finalization_intent_lists_every_terminal_write(tmp_path: Path) -> None:
    """The write-ahead intent names each terminal write in order (so recovery
    and reviewers can see exactly what may be missing)."""
    harness = make_harness(two_step_script())
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    harness.supervisor.execute(run_id)
    record = harness.store.load(KIND_RUN_FINALIZATION, run_id)
    assert record is not None
    intent = FinalizationIntent.from_dict(record)
    assert intent.complete()
    assert intent.pending_writes() == ()
    assert intent.target_status == "completed"
    assert intent.reservation_id in harness.ledger.reservations


def test_two_supervisors_share_stores_without_double_settlement(tmp_path: Path) -> None:
    """End-to-end ownership smoke: one run, two supervisors, one settlement."""
    harness = make_harness(two_step_script())
    second = Supervisor(
        harness.runtime,
        harness.store,
        harness.artifacts,
        harness.ledger,
        harness.journal,
        harness.policy,
    )
    run_id = harness.supervisor.submit(schema_spec(max_cost_usd=0.5))
    run = harness.supervisor.execute(run_id)
    assert run.status is TaskStatus.COMPLETED
    # the second client reads the same durable truth
    assert second.get_run(run_id).status is TaskStatus.COMPLETED
    assert second.get_result(run_id) is not None
    assert harness.ledger.settled_usd() == pytest.approx(0.03)
    with pytest.raises(InvalidStateTransitionError):
        second.cancel(run_id)
