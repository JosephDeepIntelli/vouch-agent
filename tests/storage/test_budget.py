"""Budget ledger: invariant under attack (threads, processes, crashes).

The invariant under test everywhere: sum(settled) + sum(open) <= cap.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
from pathlib import Path

import pytest

from vouch_agent.errors import (
    BudgetExhaustedError,
    ContractError,
    ReconciliationRequiredError,
    ReservationError,
)
from vouch_agent.storage.budget import SqliteBudgetLedger


def test_reserve_settle_release_basic_flow(tmp_path):
    ledger = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=10.0)
    r = ledger.reserve("attempt_1", 4.0)
    assert r.status.value == "open"
    assert ledger.outstanding_usd() == pytest.approx(4.0)
    assert ledger.settled_usd() == pytest.approx(0.0)
    assert ledger.remaining_usd() == pytest.approx(6.0)

    settled = ledger.settle(r.reservation_id, 3.0)
    assert settled.status.value == "settled"
    assert settled.settled_amount_usd == pytest.approx(3.0)
    # under-settlement returns the difference to the pool
    assert ledger.remaining_usd() == pytest.approx(7.0)
    assert ledger.outstanding_usd() == pytest.approx(0.0)


def test_release_returns_full_reservation(tmp_path):
    ledger = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=5.0)
    r = ledger.reserve("never_runs", 2.0)
    released = ledger.release(r.reservation_id)
    assert released.status.value == "released"
    assert ledger.remaining_usd() == pytest.approx(5.0)


def test_reserve_beyond_cap_refused(tmp_path):
    ledger = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=3.0)
    ledger.reserve("a", 2.0)
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve("b", 2.0)
    # the failed reservation left no trace
    assert ledger.outstanding_usd() == pytest.approx(2.0)
    assert len(ledger.reservations()) == 1


def test_settled_amounts_count_against_cap(tmp_path):
    ledger = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=10.0)
    r1 = ledger.reserve("a", 4.0)
    ledger.settle(r1.reservation_id, 4.0)
    ledger.reserve("b", 6.0)
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve("c", 0.01)


def test_double_settle_and_unknown_ids_refused(tmp_path):
    ledger = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=5.0)
    r = ledger.reserve("a", 1.0)
    ledger.settle(r.reservation_id, 0.5)
    with pytest.raises(ReservationError):
        ledger.settle(r.reservation_id, 0.5)
    with pytest.raises(ReservationError):
        ledger.settle("rsv_does_not_exist", 0.5)
    with pytest.raises(ReservationError):
        ledger.release("rsv_does_not_exist")
    with pytest.raises(ReservationError):
        ledger.release(r.reservation_id)  # settled, not open

    r2 = ledger.reserve("b", 1.0)
    released_once = ledger.release(r2.reservation_id)
    assert released_once.status.value == "released"
    with pytest.raises(ReservationError):
        ledger.release(r2.reservation_id)  # already closed
    with pytest.raises(ReservationError):
        ledger.settle(r2.reservation_id, 0.1)  # cannot settle a released one


def test_over_settlement_within_cap_allowed_and_breaching_refused(tmp_path):
    ledger = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=10.0)
    r = ledger.reserve("a", 4.0)
    # actual 6 <= cap 10 -> books the real amount
    settled = ledger.settle(r.reservation_id, 6.0)
    assert settled.settled_amount_usd == pytest.approx(6.0)

    r2 = ledger.reserve("b", 2.0)
    # actual 12 would put settled+open at 12 > 10 -> refused, stays OPEN
    with pytest.raises(BudgetExhaustedError):
        ledger.settle(r2.reservation_id, 12.0)
    still_open = [x for x in ledger.reservations() if x.reservation_id == r2.reservation_id]
    assert still_open[0].status.value == "open"
    assert ledger.outstanding_usd() == pytest.approx(2.0)
    # reconciliation can still settle it honestly at the true amount if slack exists
    ledger.settle(r2.reservation_id, 2.0)
    assert ledger.settled_usd() == pytest.approx(8.0)


def test_reserve_rejects_nonpositive_and_nonfinite(tmp_path):
    ledger = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=5.0)
    with pytest.raises(ContractError):
        ledger.reserve("a", 0.0)
    with pytest.raises(ContractError):
        ledger.reserve("a", -1.0)
    with pytest.raises(ContractError):
        ledger.reserve("a", float("nan"))
    with pytest.raises(ContractError):
        ledger.reserve("", 1.0)


def test_cap_mismatch_on_reopen_refused(tmp_path):
    db = tmp_path / "budget.db"
    SqliteBudgetLedger(db, total_usd_cap=10.0)
    with pytest.raises(ContractError, match="cap"):
        SqliteBudgetLedger(db, total_usd_cap=20.0)
    # agreeing reopen is fine
    SqliteBudgetLedger(db, total_usd_cap=10.0)


def test_ledger_state_survives_reopen(tmp_path):
    db = tmp_path / "budget.db"
    ledger = SqliteBudgetLedger(db, total_usd_cap=8.0)
    r = ledger.reserve("a", 3.0)
    ledger.settle(r.reservation_id, 2.5)
    r2 = ledger.reserve("b", 1.0)
    reopened = SqliteBudgetLedger(db, total_usd_cap=8.0)
    assert reopened.settled_usd() == pytest.approx(2.5)
    assert reopened.outstanding_usd() == pytest.approx(1.0)
    assert reopened.remaining_usd() == pytest.approx(4.5)
    reopened.release(r2.reservation_id)
    assert reopened.remaining_usd() == pytest.approx(5.5)


# -- concurrency: threads ------------------------------------------------------


def test_multithreaded_reservations_never_over_reserve(tmp_path):
    cap, amount, threads_n, per_thread = 0.25, 0.01, 8, 50
    ledger = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=cap)
    successes: list[int] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        ok = 0
        local = SqliteBudgetLedger(tmp_path / "budget.db", total_usd_cap=cap)
        for _ in range(per_thread):
            try:
                local.reserve(f"thread-{index}", amount)
                ok += 1
            except BudgetExhaustedError:
                continue
        with lock:
            successes.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(successes) == 25  # exactly cap/amount, never one more
    assert ledger.outstanding_usd() == pytest.approx(cap)
    assert ledger.remaining_usd() == pytest.approx(0.0)
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve("late", 0.001)


def test_threaded_mixed_reserve_settle_keeps_invariant(tmp_path):
    cap = 5.0
    db = tmp_path / "budget.db"
    ledger = SqliteBudgetLedger(db, total_usd_cap=cap)

    def worker(index: int) -> None:
        local = SqliteBudgetLedger(db, total_usd_cap=cap)
        for k in range(40):
            try:
                r = local.reserve(f"w{index}-{k}", 0.5)
                if k % 2 == 0:
                    local.settle(r.reservation_id, 0.4)
            except BudgetExhaustedError:
                continue

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    settled = ledger.settled_usd()
    outstanding = ledger.outstanding_usd()
    assert settled + outstanding <= cap + 1e-9
    assert ledger.remaining_usd() == pytest.approx(cap - settled - outstanding)


# -- concurrency + crash: separate processes -------------------------------------


def _hammer_reservations(
    db_path: str, cap: float, amount: float, count: int, worker: int, out: str
) -> None:
    from vouch_agent.storage.budget import SqliteBudgetLedger

    ledger = SqliteBudgetLedger(db_path, total_usd_cap=cap)
    ok = 0
    for _ in range(count):
        try:
            ledger.reserve(f"worker-{worker}", amount)
            ok += 1
        except BudgetExhaustedError:
            continue
    Path(out).write_text(str(ok))


def _crash_after_reserve(db_path: str, cap: float, holder: str) -> None:
    from vouch_agent.storage.budget import SqliteBudgetLedger

    ledger = SqliteBudgetLedger(db_path, total_usd_cap=cap)
    ledger.reserve(holder, 5.0)
    # Simulate a hard crash: no settle, no cleanup, no flush of anything else.
    os._exit(1)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.skipif(sys.platform != "linux", reason="fork context required")
def test_multiprocess_reservations_never_over_reserve(tmp_path):
    cap, amount, workers, per_worker = 50.0, 1.0, 4, 30
    db = str(tmp_path / "budget.db")
    ctx = multiprocessing.get_context("fork")
    procs = []
    for i in range(workers):
        out = str(tmp_path / f"out-{i}.txt")
        p = ctx.Process(target=_hammer_reservations, args=(db, cap, amount, per_worker, i, out))
        p.start()
        procs.append((p, out))
    successes = 0
    for p, out in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
        successes += int(Path(out).read_text())

    # 4 processes x 30 attempts against a 50-unit cap: exactly 50 may win.
    assert successes == 50
    ledger = SqliteBudgetLedger(db, total_usd_cap=cap)
    assert len(ledger.reservations()) == 50
    assert ledger.outstanding_usd() == pytest.approx(50.0)
    assert ledger.remaining_usd() == pytest.approx(0.0)
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve("post", 0.01)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.skipif(sys.platform != "linux", reason="fork context required")
def test_crashed_process_reservation_is_surfaced_not_released(tmp_path):
    cap = 20.0
    db = str(tmp_path / "budget.db")
    ctx = multiprocessing.get_context("fork")
    crasher = ctx.Process(target=_crash_after_reserve, args=(db, cap, "crashed-holder"))
    crasher.start()
    crasher.join(timeout=60)
    assert crasher.exitcode == 1  # it really crashed

    # A fresh process opens the ledger.
    ledger = SqliteBudgetLedger(db, total_usd_cap=cap)
    opens = ledger.open_reservations()
    assert len(opens) == 1
    assert opens[0].holder == "crashed-holder"
    assert opens[0].status.value == "open"

    # NOT silently released: still counts against the cap...
    assert ledger.outstanding_usd() == pytest.approx(5.0)
    assert ledger.remaining_usd() == pytest.approx(cap - 5.0)

    # ...and recovery is told to reconcile, never to blindly replay/release.
    with pytest.raises(ReconciliationRequiredError, match=r"crashed-holder|reconciliation"):
        ledger.require_reconciled()

    # Reopening again does not time-out or GC the reservation either.
    again = SqliteBudgetLedger(db, total_usd_cap=cap)
    assert len(again.open_reservations()) == 1

    # Only an explicit decision closes it.
    settled = again.settle(opens[0].reservation_id, 4.5)
    assert settled.status.value == "settled"
    assert again.require_reconciled() is None
    assert again.remaining_usd() == pytest.approx(cap - 4.5)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.skipif(sys.platform != "linux", reason="fork context required")
def test_two_processes_settle_and_reserve_concurrently(tmp_path):
    cap = 8.0
    db = str(tmp_path / "budget.db")
    seed = SqliteBudgetLedger(db, total_usd_cap=cap)
    seeded = [seed.reserve(f"seed-{i}", 1.0) for i in range(8)]
    assert seed.remaining_usd() == pytest.approx(0.0)

    def settle_half() -> None:
        ledger = SqliteBudgetLedger(db, total_usd_cap=cap)
        for r in seeded[:4]:
            ledger.settle(r.reservation_id, 0.9)

    def try_reserve(out: str) -> None:
        ledger = SqliteBudgetLedger(db, total_usd_cap=cap)
        ok = 0
        for i in range(20):
            try:
                ledger.reserve(f"race-{i}", 0.5)
                ok += 1
            except BudgetExhaustedError:
                continue
        Path(out).write_text(str(ok))

    ctx = multiprocessing.get_context("fork")
    out = str(tmp_path / "race.txt")
    settler = ctx.Process(target=settle_half)
    racer = ctx.Process(target=try_reserve, args=(out,))
    settler.start()
    racer.start()
    settler.join(timeout=60)
    racer.join(timeout=60)
    assert settler.exitcode == 0 and racer.exitcode == 0

    final = SqliteBudgetLedger(db, total_usd_cap=cap)
    # settled(4x0.9) + open(4x1.0 + races) <= 8 always; races could only use
    # the 0.4 actually freed by under-settlement... plus whatever timing
    # allowed. The invariant is the assertion.
    assert final.settled_usd() + final.outstanding_usd() <= cap + 1e-9
    won = int(Path(out).read_text())
    assert won * 0.5 <= cap - final.settled_usd() + 1e-9
