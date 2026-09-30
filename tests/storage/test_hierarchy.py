"""Hierarchical (parent/child) reservations — review A2 primitive (lead-owned).

Children carve space atomically out of an OPEN parent and never draw from the
project cap directly; the flat cap invariant from the original suite is
unchanged. These tests are the primitive the runtime specialist builds
per-call reservations on.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from vouch_agent.contracts.journal import ReservationStatus
from vouch_agent.errors import BudgetExhaustedError, ContractError, ReservationError
from vouch_agent.storage import SqliteBudgetLedger


def test_children_cannot_overallocate_parent() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ledger = SqliteBudgetLedger(Path(d) / "b.sqlite", total_usd_cap=1.0)
        parent = ledger.reserve("run-1", 0.10)
        c1 = ledger.reserve_child(parent.reservation_id, "query-1", 0.06)
        assert c1.parent_reservation_id == parent.reservation_id
        with pytest.raises(BudgetExhaustedError, match="already committed"):
            ledger.reserve_child(parent.reservation_id, "query-2", 0.06)
        # cap untouched by children: only the parent draws from it
        assert ledger.outstanding_usd() == pytest.approx(0.10)


def test_child_release_returns_allocation_to_parent() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ledger = SqliteBudgetLedger(Path(d) / "b.sqlite", total_usd_cap=1.0)
        parent = ledger.reserve("run-1", 0.10)
        c1 = ledger.reserve_child(parent.reservation_id, "query-1", 0.06)
        ledger.release(c1.reservation_id)
        # the 0.06 is available again inside the parent
        c2 = ledger.reserve_child(parent.reservation_id, "query-2", 0.06)
        assert c2.status is ReservationStatus.OPEN


def test_parent_settles_at_sum_of_children() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ledger = SqliteBudgetLedger(Path(d) / "b.sqlite", total_usd_cap=1.0)
        parent = ledger.reserve("run-1", 0.10)
        c1 = ledger.reserve_child(parent.reservation_id, "query-1", 0.06)
        c2 = ledger.reserve_child(parent.reservation_id, "query-2", 0.04)
        ledger.settle(c1.reservation_id, 0.05)
        ledger.settle(c2.reservation_id, 0.03)
        settled_parent = ledger.settle_parent_from_children(parent.reservation_id)
        assert settled_parent.settled_amount_usd == pytest.approx(0.08)
        assert ledger.settled_usd() == pytest.approx(0.08)
        assert ledger.outstanding_usd() == pytest.approx(0.0)


def test_parent_overage_visible_not_clipped() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ledger = SqliteBudgetLedger(Path(d) / "b.sqlite", total_usd_cap=1.0)
        parent = ledger.reserve("run-1", 0.10)
        c1 = ledger.reserve_child(parent.reservation_id, "query-1", 0.10)
        # provider exceeded its declared bound: settle the child honestly
        with pytest.raises(BudgetExhaustedError, match="exceed its parent"):
            ledger.settle(c1.reservation_id, 0.20)
        # child stays open; reconcile by closing parent with explicit overage
        ledger.settle(c1.reservation_id, 0.10)
        settled = ledger.settle_parent_from_children(parent.reservation_id, overage_usd=0.02)
        assert settled.settled_amount_usd == pytest.approx(0.12)  # overrun visible


def test_open_children_block_parent_close() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ledger = SqliteBudgetLedger(Path(d) / "b.sqlite", total_usd_cap=1.0)
        parent = ledger.reserve("run-1", 0.10)
        ledger.reserve_child(parent.reservation_id, "query-1", 0.06)
        with pytest.raises(ReservationError, match="open child"):
            ledger.settle_parent_from_children(parent.reservation_id)


def test_cannot_allocate_from_closed_or_nested_parents() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ledger = SqliteBudgetLedger(Path(d) / "b.sqlite", total_usd_cap=1.0)
        parent = ledger.reserve("run-1", 0.10)
        child = ledger.reserve_child(parent.reservation_id, "query-1", 0.04)
        with pytest.raises(ContractError, match="one level deep"):
            ledger.reserve_child(child.reservation_id, "query-1a", 0.01)
        settled_parent = ledger.settle(parent.reservation_id, 0.04)
        with pytest.raises(ReservationError):
            ledger.reserve_child(settled_parent.reservation_id, "late", 0.01)


def _hammer(path: str, parent_id: str, n: int, amount: float, out) -> None:
    ledger = SqliteBudgetLedger(path, total_usd_cap=10.0)
    won = 0
    for _ in range(n):
        try:
            ledger.reserve_child(parent_id, "hammer", amount)
            won += 1
        except (BudgetExhaustedError, ReservationError):
            pass
    out.put(won)


def test_concurrent_children_never_overallocate_parent(tmp_path: Path) -> None:
    cap = 10.0
    path = str(tmp_path / "hammer.sqlite")
    ledger = SqliteBudgetLedger(path, total_usd_cap=cap)
    parent = ledger.reserve("run-1", 0.50)
    out: multiprocessing.Queue = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(target=_hammer, args=(path, parent.reservation_id, 20, 0.05, out))
        for _ in range(4)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
    wins = [out.get() for _ in procs]
    assert sum(wins) * 0.05 <= 0.50 + 1e-9, f"children over-allocated parent: {wins}"


def test_reservation_record_roundtrips_parent_id() -> None:
    import json
    import tempfile

    from vouch_agent.contracts.journal import BudgetReservation

    with tempfile.TemporaryDirectory() as d:
        ledger = SqliteBudgetLedger(Path(d) / "b.sqlite", total_usd_cap=1.0)
        parent = ledger.reserve("run-1", 0.10)
        child = ledger.reserve_child(parent.reservation_id, "query-1", 0.05)
        restored = BudgetReservation.from_dict(json.loads(child.to_canonical_json()))
        assert restored.parent_reservation_id == parent.reservation_id
        assert (
            BudgetReservation.from_dict(
                json.loads(parent.to_canonical_json())
            ).parent_reservation_id
            is None
        )
        # and the ledger reader exposes the linkage
        assert ledger.children(parent.reservation_id)[0].reservation_id == child.reservation_id
