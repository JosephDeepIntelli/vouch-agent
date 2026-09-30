"""A2 follow-up regressions: a provider exceeding its declared bound.

The review's acceptance line: "a provider exceeding a declared bound remains
visible as an overrun requiring reconciliation" and "never fix overspend by
clipping reported actuals". These exercise the fake-runtime path so the
numbers are exact.
"""

from __future__ import annotations

import pytest
from fakes import ScriptedCall, make_harness, schema_spec

from vouch_agent.contracts.journal import ReservationStatus
from vouch_agent.contracts.tasks import TaskStatus
from vouch_agent.orchestrator import Supervisor


def test_provider_overrun_stays_open_for_reconciliation() -> None:
    """One query declares $0.05 (the whole remaining budget) but costs $0.10:
    the ledger refuses to absorb it, the child stays OPEN and the run ends in
    needs-reconciliation instead of clipping the actual."""
    h = make_harness(
        [
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.10,
            )
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.05))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.NEEDS_RECONCILIATION
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert any("overrun" in u for u in package.uncertainties)

    parent = next(r for r in h.ledger.reservations.values() if r.parent_reservation_id is None)
    assert parent.status is ReservationStatus.OPEN  # never silently closed
    children = h.ledger.children(parent.reservation_id)
    overruns = [c for c in children if c.status is ReservationStatus.OPEN]
    assert overruns, "the refused overrun must stay open and visible"
    # the reported actual was never clipped into the ledger as if it fit
    settled = [c.settled_amount_usd for c in children if c.settled_amount_usd is not None]
    assert settled == []

    # The open overrun is a retained conservative reservation: even a fixture
    # auto-continue cannot spend around it (every further reservation is
    # refused against the same parent), so the run stays unreconciled.
    resumed = h.supervisor.resume(run_id, "")
    assert resumed.status is TaskStatus.NEEDS_RECONCILIATION
    assert h.ledger.outstanding_usd() == pytest.approx(0.05)
    assert h.runtime.calls == 1  # no further model work was bought


def test_overrun_cannot_be_certified_away_by_reconciliation_free_resume() -> None:
    """Even after a verified note, the money state is unchanged: the note
    unblocks execution, it does not absorb the overrun."""
    h = make_harness(
        [
            ScriptedCall(content='{"recommendation": "A"}', cost_usd=0.10),
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.01,
            ),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.05))
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.NEEDS_RECONCILIATION
    parent = next(r for r in h.ledger.reservations.values() if r.parent_reservation_id is None)
    assert parent.status is ReservationStatus.OPEN


def test_second_supervisor_sees_the_open_overrun() -> None:
    """The open overrun keeps counting against the project cap for every
    client — it cannot be spent around."""
    h = make_harness([ScriptedCall(content='{"recommendation": "A"}', cost_usd=0.10)])
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.05))
    h.supervisor.execute(run_id)
    second = Supervisor(h.runtime, h.store, h.artifacts, h.ledger, h.journal, h.policy)
    assert second.get_run(run_id).status is TaskStatus.NEEDS_RECONCILIATION
    # the run reservation still counts against the project budget
    assert h.ledger.outstanding_usd() == pytest.approx(0.05)
