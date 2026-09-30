"""Atomic reservation-based budget ledger on SQLite (design §10, JAZ ADR #2).

The invariant this module must uphold, always, including under concurrent
reservation attempts from multiple threads and multiple processes:

    sum(settled) + sum(open reservations) <= total cap

Mechanism: every mutating operation runs inside a single ``BEGIN IMMEDIATE``
transaction on its own SQLite connection. ``BEGIN IMMEDIATE`` takes the
database write lock *before* reading the sums, so the check-then-insert
sequence is atomic with respect to every other process and thread; SQLite's
busy timeout turns transient contention into waiting rather than failure.
WAL mode lets readers proceed while a writer holds the lock.

Honesty rules encoded here:

* Reservation happens **before** work starts; the amount is charged against
  the cap until settlement or release.
* Settlement books the *actual* amount. Over-settlement is permitted only
  while the invariant holds; an over-settlement that would breach the cap
  raises :class:`BudgetExhaustedError` and leaves the reservation OPEN so the
  breach is surfaced for reconciliation instead of absorbed.
* A crashed process's stale OPEN reservation is **never** silently released.
  It keeps counting against the cap and is surfaced via
  :meth:`SqliteBudgetLedger.open_reservations` /
  :meth:`SqliteBudgetLedger.require_reconciled`; only an explicit human (or
  controller recovery) decision settles or releases it.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vouch_agent.contracts.common import new_id, utc_now_iso
from vouch_agent.contracts.journal import BudgetReservation, ReservationStatus
from vouch_agent.errors import (
    BudgetExhaustedError,
    ContractError,
    ReconciliationRequiredError,
    ReservationError,
)
from vouch_agent.storage.store import BUSY_TIMEOUT_MS

#: Float comparison tolerance for cap checks (cents-scale money, not symbols).
_EPS = 1e-9

_BUDGET_STATE_DDL = """
CREATE TABLE IF NOT EXISTS budget_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    total_usd_cap REAL NOT NULL,
    created_at TEXT NOT NULL
)
"""

_RESERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    status TEXT NOT NULL,
    settled_amount_usd REAL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    parent_reservation_id TEXT
)
"""


def _require_amount(amount_usd: float, *, allow_zero: bool) -> float:
    value = float(amount_usd)
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ContractError(f"amount_usd must be a finite positive number, got {amount_usd!r}")
    return value


class SqliteBudgetLedger:
    """Reservation ledger against one project cap.

    The cap is persisted on first open; reopening with a different cap is a
    :class:`ContractError` — the cap belongs to the (versioned) ProjectSpec,
    and two processes disagreeing about it would be an accounting split-brain.
    """

    def __init__(self, path: str | Path, total_usd_cap: float) -> None:
        cap = float(total_usd_cap)
        if not math.isfinite(cap) or cap < 0:
            raise ContractError(f"total_usd_cap must be a finite number >= 0, got {cap!r}")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(_BUDGET_STATE_DDL)
            conn.execute(_RESERVATIONS_DDL)
            self._migrate(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT total_usd_cap FROM budget_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO budget_state (singleton, total_usd_cap, created_at) "
                    "VALUES (1, ?, ?)",
                    (cap, utc_now_iso()),
                )
            elif abs(float(row[0]) - cap) > _EPS:
                conn.execute("ROLLBACK")
                raise ContractError(
                    f"budget ledger at {self._path} was opened with cap {row[0]} "
                    f"but got {cap}; the cap is part of the ProjectSpec — change it there"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        self._cap = cap

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Idempotent in-place migration (older ledgers lack the parent column)."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(budget_reservations)")}
        if "parent_reservation_id" not in columns:
            conn.execute("ALTER TABLE budget_reservations ADD COLUMN parent_reservation_id TEXT")

    # -- connections -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction on a dedicated connection."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _sums(conn: sqlite3.Connection) -> tuple[float, float]:
        """Cap-level sums count TOP-LEVEL reservations only; children are
        allocations inside a parent and never draw from the cap directly."""
        settled = conn.execute(
            "SELECT COALESCE(SUM(settled_amount_usd), 0) FROM budget_reservations "
            "WHERE status = 'settled' AND parent_reservation_id IS NULL"
        ).fetchone()[0]
        open_sum = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM budget_reservations "
            "WHERE status = 'open' AND parent_reservation_id IS NULL"
        ).fetchone()[0]
        return float(settled), float(open_sum)

    @staticmethod
    def _committed_children_usd(conn: sqlite3.Connection, parent_id: str) -> float:
        """Amount of the parent already committed: open children at their
        reservation amount, settled children at their actual."""
        row = conn.execute(
            "SELECT COALESCE(SUM(committed), 0) FROM ("
            "  SELECT amount_usd AS committed FROM budget_reservations "
            "   WHERE parent_reservation_id = ? AND status = 'open'"
            "  UNION ALL"
            "  SELECT COALESCE(settled_amount_usd, 0) FROM budget_reservations "
            "   WHERE parent_reservation_id = ? AND status = 'settled'"
            ")",
            (parent_id, parent_id),
        ).fetchone()
        return float(row[0])

    # -- BudgetLedger port ---------------------------------------------------------

    def reserve(self, holder: str, amount_usd: float) -> BudgetReservation:
        if not isinstance(holder, str) or not holder:
            raise ContractError(f"holder must be a non-empty string, got {holder!r}")
        amount = _require_amount(amount_usd, allow_zero=False)
        reservation_id = new_id("rsv")
        created_at = utc_now_iso()
        with self._immediate() as conn:
            settled, open_sum = self._sums(conn)
            if settled + open_sum + amount > self._cap + _EPS:
                raise BudgetExhaustedError(
                    f"cannot reserve ${amount:.4f} for {holder!r}: "
                    f"settled ${settled:.4f} + open ${open_sum:.4f} leaves "
                    f"${self._cap - settled - open_sum:.4f} of ${self._cap:.4f}"
                )
            conn.execute(
                "INSERT INTO budget_reservations "
                "(reservation_id, holder, amount_usd, status, settled_amount_usd, "
                " created_at, closed_at, parent_reservation_id) "
                "VALUES (?, ?, ?, 'open', NULL, ?, NULL, NULL)",
                (reservation_id, holder, amount, created_at),
            )
        return BudgetReservation(
            reservation_id=reservation_id,
            holder=holder,
            amount_usd=amount,
            status=ReservationStatus.OPEN,
            created_at=created_at,
        )

    def settle(self, reservation_id: str, actual_usd: float) -> BudgetReservation:
        actual = _require_amount(actual_usd, allow_zero=True)
        with self._immediate() as conn:
            record = self._load(conn, reservation_id)
            if record.status is not ReservationStatus.OPEN:
                raise ReservationError(
                    f"reservation {reservation_id!r} is {record.status.value}, "
                    "only open reservations can be settled"
                )
            if record.parent_reservation_id is not None:
                return self._settle_child(conn, record, record.parent_reservation_id, actual)
            settled, open_sum = self._sums(conn)
            # This reservation stops counting as `open` and starts counting
            # as `settled` at the *actual* amount.
            after = settled + actual + (open_sum - record.amount_usd)
            if after > self._cap + _EPS:
                raise BudgetExhaustedError(
                    f"settling {reservation_id!r} at ${actual:.4f} would breach the "
                    f"cap (${after:.4f} > ${self._cap:.4f}); reservation stays OPEN "
                    "and must be reconciled"
                )
            closed_at = utc_now_iso()
            conn.execute(
                "UPDATE budget_reservations SET status = 'settled', settled_amount_usd = ?, "
                "closed_at = ? WHERE reservation_id = ?",
                (actual, closed_at, reservation_id),
            )
        return BudgetReservation(
            reservation_id=record.reservation_id,
            holder=record.holder,
            amount_usd=record.amount_usd,
            status=ReservationStatus.SETTLED,
            settled_amount_usd=actual,
            created_at=record.created_at,
            closed_at=closed_at,
        )

    # -- hierarchical allocations (design §10 / review A2) --------------------------

    def reserve_child(
        self, parent_reservation_id: str, holder: str, amount_usd: float
    ) -> BudgetReservation:
        """Atomically carve a child allocation out of an OPEN parent.

        Children never touch the cap: the invariant is that committed children
        (open at their amount, settled at their actual) cannot exceed the
        parent's amount. The check-then-insert runs inside the same
        ``BEGIN IMMEDIATE`` as every other mutation, so concurrent children
        cannot over-allocate the parent.
        """
        if not isinstance(holder, str) or not holder:
            raise ContractError(f"holder must be a non-empty string, got {holder!r}")
        amount = _require_amount(amount_usd, allow_zero=False)
        reservation_id = new_id("rsv")
        created_at = utc_now_iso()
        with self._immediate() as conn:
            parent = self._load(conn, parent_reservation_id)
            if parent.parent_reservation_id is not None:
                raise ContractError(
                    "child reservations are one level deep; nest by carving a new "
                    "top-level reservation for the sub-task instead"
                )
            if parent.status is not ReservationStatus.OPEN:
                raise ReservationError(
                    f"parent reservation {parent_reservation_id!r} is "
                    f"{parent.status.value}; cannot allocate from it"
                )
            committed = self._committed_children_usd(conn, parent_reservation_id)
            if committed + amount > parent.amount_usd + _EPS:
                raise BudgetExhaustedError(
                    f"cannot allocate ${amount:.4f} for {holder!r} from parent "
                    f"{parent_reservation_id!r}: ${committed:.4f} of "
                    f"${parent.amount_usd:.4f} already committed"
                )
            conn.execute(
                "INSERT INTO budget_reservations "
                "(reservation_id, holder, amount_usd, status, settled_amount_usd, "
                " created_at, closed_at, parent_reservation_id) "
                "VALUES (?, ?, ?, 'open', NULL, ?, NULL, ?)",
                (reservation_id, holder, amount, created_at, parent_reservation_id),
            )
        return BudgetReservation(
            reservation_id=reservation_id,
            holder=holder,
            amount_usd=amount,
            status=ReservationStatus.OPEN,
            created_at=created_at,
            parent_reservation_id=parent_reservation_id,
        )

    def _settle_child(
        self,
        conn: sqlite3.Connection,
        record: BudgetReservation,
        parent_reservation_id: str,
        actual: float,
    ) -> BudgetReservation:
        committed = self._committed_children_usd(conn, parent_reservation_id)
        # settling this child at `actual` replaces its open amount in committed
        after = committed - record.amount_usd + actual
        parent_amount = self._load(conn, parent_reservation_id).amount_usd
        if after > parent_amount + _EPS:
            raise BudgetExhaustedError(
                f"settling child {record.reservation_id!r} at ${actual:.4f} would "
                f"exceed its parent (${after:.4f} > ${parent_amount:.4f}); child stays "
                "OPEN and must be reconciled"
            )
        closed_at = utc_now_iso()
        conn.execute(
            "UPDATE budget_reservations SET status = 'settled', settled_amount_usd = ?, "
            "closed_at = ? WHERE reservation_id = ?",
            (actual, closed_at, record.reservation_id),
        )
        return BudgetReservation(
            reservation_id=record.reservation_id,
            holder=record.holder,
            amount_usd=record.amount_usd,
            status=ReservationStatus.SETTLED,
            settled_amount_usd=actual,
            created_at=record.created_at,
            closed_at=closed_at,
            parent_reservation_id=parent_reservation_id,
        )

    def children(self, parent_reservation_id: str) -> list[BudgetReservation]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT reservation_id, holder, amount_usd, status, settled_amount_usd, "
                "created_at, closed_at, parent_reservation_id FROM budget_reservations "
                "WHERE parent_reservation_id = ? ORDER BY created_at, reservation_id",
                (parent_reservation_id,),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_record(row) for row in rows]

    def settle_parent_from_children(
        self, parent_reservation_id: str, *, overage_usd: float = 0.0
    ) -> BudgetReservation:
        """Close a parent at the SUM OF ITS CHILDREN'S ACTUALS plus any honest
        overage (spend outside child allocations). Booking an overage beyond
        the cap raises and leaves the parent OPEN for reconciliation — an
        overrun stays visible instead of being clipped away."""
        overage = _require_amount(overage_usd, allow_zero=True)
        with self._immediate() as conn:
            parent = self._load(conn, parent_reservation_id)
            if parent.parent_reservation_id is not None:
                raise ContractError("not a top-level reservation")
            kids = self.children(parent_reservation_id)
            open_kids = [k for k in kids if k.status is ReservationStatus.OPEN]
            if open_kids:
                raise ReservationError(
                    f"parent {parent_reservation_id!r} still has {len(open_kids)} open "
                    "child allocation(s); settle or release them first"
                )
            actual = (
                sum(
                    k.settled_amount_usd or 0.0
                    for k in kids
                    if k.status is ReservationStatus.SETTLED
                )
                + overage
            )
        return self.settle(parent_reservation_id, round(actual, 9))

    def release(self, reservation_id: str) -> BudgetReservation:
        with self._immediate() as conn:
            record = self._load(conn, reservation_id)
            if record.status is not ReservationStatus.OPEN:
                raise ReservationError(
                    f"reservation {reservation_id!r} is {record.status.value}, "
                    "only open reservations can be released"
                )
            closed_at = utc_now_iso()
            conn.execute(
                "UPDATE budget_reservations SET status = 'released', closed_at = ? "
                "WHERE reservation_id = ?",
                (closed_at, reservation_id),
            )
        return BudgetReservation(
            reservation_id=record.reservation_id,
            holder=record.holder,
            amount_usd=record.amount_usd,
            status=ReservationStatus.RELEASED,
            created_at=record.created_at,
            closed_at=closed_at,
        )

    def outstanding_usd(self) -> float:
        conn = self._connect()
        try:
            return self._sums(conn)[1]
        finally:
            conn.close()

    def settled_usd(self) -> float:
        conn = self._connect()
        try:
            return self._sums(conn)[0]
        finally:
            conn.close()

    def remaining_usd(self) -> float:
        conn = self._connect()
        try:
            settled, open_sum = self._sums(conn)
        finally:
            conn.close()
        return self._cap - settled - open_sum

    # -- reconciliation --------------------------------------------------------------

    def total_cap_usd(self) -> float:
        return self._cap

    def reservations(self) -> list[BudgetReservation]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT reservation_id, holder, amount_usd, status, settled_amount_usd, "
                "created_at, closed_at, parent_reservation_id FROM budget_reservations "
                "ORDER BY created_at, reservation_id"
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_record(row) for row in rows]

    def open_reservations(self, *, older_than: str | None = None) -> list[BudgetReservation]:
        """Open reservations, optionally only those created before a timestamp.

        This is the crash-recovery surface: stale open reservations are *not*
        released here or anywhere else automatically — they are reported so a
        human/controller can explicitly settle (work actually happened) or
        release (work never happened).
        """
        opens = [r for r in self.reservations() if r.status is ReservationStatus.OPEN]
        if older_than is None:
            return opens
        # ISO-8601 UTC strings with identical formatting compare correctly
        # as plain strings.
        return [r for r in opens if r.created_at < older_than]

    def require_reconciled(self, *, older_than: str | None = None) -> None:
        """Raise :class:`ReconciliationRequiredError` if stale opens exist.

        Recovery flows call this before resuming work: a crashed process's
        reservation may or may not correspond to spend that actually happened,
        and only reconciliation can decide — never a blind release or replay.
        """
        stale = self.open_reservations(older_than=older_than)
        if stale:
            ids = ", ".join(r.reservation_id for r in stale)
            raise ReconciliationRequiredError(
                f"{len(stale)} open budget reservation(s) require reconciliation: {ids}"
            )

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    def _load(conn: sqlite3.Connection, reservation_id: str) -> BudgetReservation:
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ContractError("reservation_id must be a non-empty string")
        row = conn.execute(
            "SELECT reservation_id, holder, amount_usd, status, settled_amount_usd, "
            "created_at, closed_at, parent_reservation_id "
            "FROM budget_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ReservationError(f"unknown reservation {reservation_id!r}")
        return SqliteBudgetLedger._row_to_record(row)

    @staticmethod
    def _row_to_record(row: tuple) -> BudgetReservation:
        parent = row[7] if len(row) > 7 else None
        return BudgetReservation(
            reservation_id=row[0],
            holder=row[1],
            amount_usd=float(row[2]),
            status=ReservationStatus(row[3]),
            settled_amount_usd=float(row[4]) if row[4] is not None else None,
            created_at=row[5],
            closed_at=row[6],
            parent_reservation_id=parent,
        )
