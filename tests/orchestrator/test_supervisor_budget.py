"""Budget honesty: reservation before work, slices per model call,
exhaustion mid-run fails closed, unmeasurable costs are never zero.
"""

from __future__ import annotations

import pytest
from fakes import ScriptedCall, make_harness, schema_spec, two_step_script

from vouch_agent.contracts.journal import EventKind, ReservationStatus
from vouch_agent.contracts.tasks import TaskStatus
from vouch_agent.errors import BudgetExhaustedError
from vouch_agent.orchestrator import (
    KIND_BUDGET_SLICE,
    SupervisorPolicy,
)


def test_budget_exhausted_mid_run_fails_closed() -> None:
    # Budget 0.40; slices of 0.25 then 0.15; two calls cost 0.20 each.
    # After the second call the conservative spend equals the budget, so the
    # third call must never be attempted.
    h = make_harness(
        [
            ScriptedCall(content='{"a": 1}', cost_usd=0.20),
            ScriptedCall(content='{"a": 2}', cost_usd=0.20),
            ScriptedCall(content='{"a": 3}', cost_usd=0.20),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.40))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.FAILED
    assert h.runtime.calls == 2  # fails closed: no third step
    entries = h.journal.cost_entries(run_id)
    assert [e.amount_usd for e in entries] == [pytest.approx(0.20), pytest.approx(0.20)]
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable() is False
    assert any("budget exhausted" in item for item in package.not_done_items)
    assert EventKind.BUDGET_EXHAUSTED.value in h.journal.kinds(run_id)
    # the run-level reservation is settled with what actually ran
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.settled_amount_usd == pytest.approx(0.40)
    # slices bound single-call spend: 0.25, then budget-minus-measured (0.20)
    slice_1 = h.store.load(KIND_BUDGET_SLICE, f"{run_id}#model-1")
    slice_2 = h.store.load(KIND_BUDGET_SLICE, f"{run_id}#model-2")
    assert slice_1["amountUsd"] == pytest.approx(0.25)
    assert slice_1["settledAmountUsd"] == pytest.approx(0.20)
    assert slice_2["amountUsd"] == pytest.approx(0.20)
    assert slice_2["settledAmountUsd"] == pytest.approx(0.20)


def test_submit_fails_closed_when_project_cap_below_budget() -> None:
    h = make_harness(two_step_script(), ledger_cap_usd=0.10)
    with pytest.raises(BudgetExhaustedError):
        h.supervisor.submit(schema_spec(max_cost_usd=0.50))
    # nothing was persisted and no work described as scheduled
    assert h.store.data == {}
    assert h.runtime.calls == 0
    assert h.ledger.outstanding_usd() == 0.0


def test_unmeasurable_cost_recorded_not_as_zero() -> None:
    h = make_harness(
        [
            ScriptedCall(content='{"a": 1}', cost_usd=None),  # unmeasurable
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.02,
            ),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.COMPLETED  # schema condition is met
    package = h.supervisor.get_result(run_id)
    assert package is not None
    # total cost is unknown, not zero and not the measured-only sum
    assert package.total_cost_usd is None
    assert any("unmeasured" in u for u in package.uncertainties)
    entries = h.journal.cost_entries(run_id)
    unmeasured = [e for e in entries if not e.measurable]
    assert len(unmeasured) == 1 and unmeasured[0].amount_usd is None
    measured = [e for e in entries if e.measurable]
    assert [e.amount_usd for e in measured] == [pytest.approx(0.02)]
    # conservative settlement: full slice (0.25) + measured 0.02
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.settled_amount_usd == pytest.approx(0.27)
    slice_1 = h.store.load(KIND_BUDGET_SLICE, f"{run_id}#model-1")
    assert slice_1["unmeasurable"] is True
    assert slice_1["settledAmountUsd"] == pytest.approx(0.25)


def test_actual_cost_over_slice_books_actual_and_stops() -> None:
    # A call that costs more than its slice is booked at actual; the next
    # pre-step check sees the budget gone and stops explicitly.
    h = make_harness(
        [
            ScriptedCall(content='{"a": 1}', cost_usd=0.30),  # slice was 0.25
            ScriptedCall(content='{"a": 2}', cost_usd=0.01),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.30))
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.FAILED
    assert h.runtime.calls == 1
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.settled_amount_usd == pytest.approx(0.30)


def test_step_budget_slice_policy_is_applied() -> None:
    h = make_harness(
        two_step_script(),
        policy=SupervisorPolicy(step_budget_usd=0.10, default_budget_usd=1.0),
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=None))
    h.supervisor.execute(run_id)
    slice_1 = h.store.load(KIND_BUDGET_SLICE, f"{run_id}#model-1")
    slice_2 = h.store.load(KIND_BUDGET_SLICE, f"{run_id}#model-2")
    assert slice_1["amountUsd"] == pytest.approx(0.10)
    assert slice_2["amountUsd"] == pytest.approx(0.10)


def test_budget_events_journaled_per_slice_and_run() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    h.supervisor.execute(run_id)
    reserved = h.journal.event_data(EventKind.BUDGET_RESERVED, run_id)
    settled = h.journal.event_data(EventKind.BUDGET_SETTLED, run_id)
    assert [r["level"] for r in reserved] == ["run", "step", "step"]
    assert [s["level"] for s in settled] == ["step", "step", "run"]
    assert reserved[0]["amountUsd"] == pytest.approx(0.5)
    assert settled[-1]["actualUsd"] == pytest.approx(0.03)


def test_reservation_not_open_fails_prepare() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    # simulate an externally settled/closed reservation on the ledger
    reservation_id = next(iter(h.ledger.reservations))
    h.ledger.settle(reservation_id, 0.0)
    from vouch_agent.orchestrator import KIND_RUN_RESERVATION

    record = h.store.load(KIND_RUN_RESERVATION, run_id)
    assert record is not None
    record["status"] = ReservationStatus.SETTLED.value
    h.store.save(KIND_RUN_RESERVATION, run_id, record)

    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.FAILED
    assert h.runtime.calls == 0
    assert "reservation" in (run.error or "")
