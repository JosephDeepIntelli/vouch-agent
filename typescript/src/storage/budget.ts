/**
 * Atomic reservation-based budget ledger on SQLite (design §10, JAZ ADR #2).
 *
 * Invariant, always, including under concurrent reservation attempts from
 * multiple processes:
 *
 *     sum(settled, top-level) + sum(open, top-level) <= total cap
 *
 * Every mutating operation runs inside one BEGIN IMMEDIATE transaction on
 * its own connection, so the check-then-insert sequence is atomic.
 * Honesty rules: reservation before work; settlement books the actual;
 * over-settlement that would breach the cap raises and leaves the
 * reservation OPEN; a crashed process's stale OPEN reservation is NEVER
 * silently released — it keeps counting and is surfaced for reconciliation.
 */

import {
  BudgetExhaustedError,
  ContractError,
  newId,
  ReservationError,
  utcNowIso,
} from "../contracts/common.ts";
import type { BudgetReservationData, ReservationStatusValue } from "../contracts/journal.ts";
import { initWal, openDatabase } from "./sqlite.ts";

const EPS = 1e-9;

const BUDGET_STATE_DDL = `
CREATE TABLE IF NOT EXISTS budget_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    total_usd_cap REAL NOT NULL,
    created_at TEXT NOT NULL
)
`;

const RESERVATIONS_DDL = `
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
`;

function requireAmount(amountUsd: number, allowZero: boolean): number {
  const value = Number(amountUsd);
  if (!Number.isFinite(value) || value < 0 || (value === 0 && !allowZero)) {
    throw new ContractError(
      `amount_usd must be a finite positive number, got ${JSON.stringify(amountUsd)}`,
    );
  }
  return value;
}

export class BudgetLedger {
  readonly path: string;
  private cap: number;

  constructor(path: string, totalUsdCap: number) {
    const cap = Number(totalUsdCap);
    if (!Number.isFinite(cap) || cap < 0) {
      throw new ContractError(
        `total_usd_cap must be a finite number >= 0, got ${JSON.stringify(totalUsdCap)}`,
      );
    }
    this.path = path;
    const db = openDatabase(path);
    try {
      initWal(db);
      db.exec(BUDGET_STATE_DDL);
      db.exec(RESERVATIONS_DDL);
      db.exec("BEGIN IMMEDIATE");
      try {
        const row = db.prepare("SELECT total_usd_cap FROM budget_state WHERE singleton = 1")
          .get() as
            | { total_usd_cap: number }
            | undefined;
        if (row === undefined) {
          db.prepare(
            "INSERT INTO budget_state (singleton, total_usd_cap, created_at) VALUES (1, ?, ?)",
          )
            .run(cap, utcNowIso());
        } else if (Math.abs(Number(row.total_usd_cap) - cap) > EPS) {
          db.exec("ROLLBACK");
          throw new ContractError(
            `budget ledger at ${path} was opened with cap ${row.total_usd_cap} but got ${cap}; ` +
              `the cap is part of the ProjectSpec — change it there`,
          );
        }
        db.exec("COMMIT");
      } catch (exc) {
        try {
          db.exec("ROLLBACK");
        } catch {
          // already rolled back above
        }
        throw exc;
      }
    } finally {
      db.close();
    }
    this.cap = cap;
  }

  totalCapUsd(): number {
    return this.cap;
  }

  private static sums(db: ReturnType<typeof openDatabase>): { settled: number; openSum: number } {
    const settled = Number(
      (db.prepare(
        "SELECT COALESCE(SUM(settled_amount_usd), 0) AS s FROM budget_reservations " +
          "WHERE status = 'settled' AND parent_reservation_id IS NULL",
      ).get() as { s: number }).s,
    );
    const openSum = Number(
      (db.prepare(
        "SELECT COALESCE(SUM(amount_usd), 0) AS s FROM budget_reservations " +
          "WHERE status = 'open' AND parent_reservation_id IS NULL",
      ).get() as { s: number }).s,
    );
    return { settled, openSum };
  }

  private static committedChildrenUsd(
    db: ReturnType<typeof openDatabase>,
    parentId: string,
  ): number {
    const row = db.prepare(
      "SELECT COALESCE(SUM(committed), 0) AS s FROM (" +
        "  SELECT amount_usd AS committed FROM budget_reservations" +
        "   WHERE parent_reservation_id = ?1 AND status = 'open'" +
        "  UNION ALL" +
        "  SELECT COALESCE(settled_amount_usd, 0) FROM budget_reservations" +
        "   WHERE parent_reservation_id = ?1 AND status = 'settled'" +
        ")",
    ).get(parentId) as { s: number };
    return Number(row.s);
  }

  reserve(holder: string, amountUsd: number): BudgetReservationData {
    if (typeof holder !== "string" || holder.length === 0) {
      throw new ContractError(`holder must be a non-empty string, got ${JSON.stringify(holder)}`);
    }
    const amount = requireAmount(amountUsd, false);
    const reservationId = newId("rsv");
    const createdAt = utcNowIso();
    const path = this.path;
    const cap = this.cap;
    withTx(path, (db) => {
      const { settled, openSum } = BudgetLedger.sums(db);
      if (settled + openSum + amount > cap + EPS) {
        throw new BudgetExhaustedError(
          `cannot reserve $${amount.toFixed(4)} for ${JSON.stringify(holder)}: ` +
            `settled $${settled.toFixed(4)} + open $${openSum.toFixed(4)} leaves ` +
            `$${(cap - settled - openSum).toFixed(4)} of $${cap.toFixed(4)}`,
        );
      }
      db.prepare(
        "INSERT INTO budget_reservations (reservation_id, holder, amount_usd, status, " +
          "settled_amount_usd, created_at, closed_at, parent_reservation_id) " +
          "VALUES (?, ?, ?, 'open', NULL, ?, NULL, NULL)",
      ).run(reservationId, holder, amount, createdAt);
    });
    return {
      schemaVersion: "1",
      reservationId,
      holder,
      amountUsd: amount,
      status: "open",
      settledAmountUsd: null,
      createdAt,
      closedAt: null,
      parentReservationId: null,
    };
  }

  reserveChild(
    parentReservationId: string,
    holder: string,
    amountUsd: number,
  ): BudgetReservationData {
    if (typeof holder !== "string" || holder.length === 0) {
      throw new ContractError(`holder must be a non-empty string, got ${JSON.stringify(holder)}`);
    }
    const amount = requireAmount(amountUsd, false);
    const reservationId = newId("rsv");
    const createdAt = utcNowIso();
    const path = this.path;
    withTx(path, (db) => {
      const parent = BudgetLedger.loadRow(db, parentReservationId);
      if (parent.parent_reservation_id !== null) {
        throw new ContractError(
          "child reservations are one level deep; nest by carving a new top-level reservation for the sub-task instead",
        );
      }
      if (parent.status !== "open") {
        throw new ReservationError(
          `parent reservation ${
            JSON.stringify(parentReservationId)
          } is ${parent.status}; cannot allocate from it`,
        );
      }
      const committed = BudgetLedger.committedChildrenUsd(db, parentReservationId);
      if (committed + amount > Number(parent.amount_usd) + EPS) {
        throw new BudgetExhaustedError(
          `cannot allocate $${amount.toFixed(4)} for ${JSON.stringify(holder)} from parent ` +
            `${JSON.stringify(parentReservationId)}: $${committed.toFixed(4)} of ` +
            `$${Number(parent.amount_usd).toFixed(4)} already committed`,
        );
      }
      db.prepare(
        "INSERT INTO budget_reservations (reservation_id, holder, amount_usd, status, " +
          "settled_amount_usd, created_at, closed_at, parent_reservation_id) " +
          "VALUES (?, ?, ?, 'open', NULL, ?, NULL, ?)",
      ).run(reservationId, holder, amount, createdAt, parentReservationId);
    });
    return {
      schemaVersion: "1",
      reservationId,
      holder,
      amountUsd: amount,
      status: "open",
      settledAmountUsd: null,
      createdAt,
      closedAt: null,
      parentReservationId,
    };
  }

  settle(reservationId: string, actualUsd: number): BudgetReservationData {
    const actual = requireAmount(actualUsd, true);
    const path = this.path;
    const cap = this.cap;
    return withTx(path, (db) => {
      const record = BudgetLedger.toRecord(BudgetLedger.loadRow(db, reservationId));
      if (record.status !== "open") {
        throw new ReservationError(
          `reservation ${JSON.stringify(reservationId)} is ${record.status}, ` +
            `only open reservations can be settled`,
        );
      }
      if (record.parentReservationId !== null) {
        return BudgetLedger.settleChild(db, record, record.parentReservationId, actual);
      }
      const { settled, openSum } = BudgetLedger.sums(db);
      const after = settled + actual + (openSum - record.amountUsd);
      if (after > cap + EPS) {
        throw new BudgetExhaustedError(
          `settling ${JSON.stringify(reservationId)} at $${actual.toFixed(4)} would breach the ` +
            `cap ($${after.toFixed(4)} > $${
              cap.toFixed(4)
            }); reservation stays OPEN and must be reconciled`,
        );
      }
      const closedAt = utcNowIso();
      db.prepare(
        "UPDATE budget_reservations SET status = 'settled', settled_amount_usd = ?, closed_at = ? " +
          "WHERE reservation_id = ?",
      ).run(actual, closedAt, reservationId);
      return { ...record, status: "settled", settledAmountUsd: actual, closedAt };
    });
  }

  private static settleChild(
    db: ReturnType<typeof openDatabase>,
    record: BudgetReservationData,
    parentReservationId: string,
    actual: number,
  ): BudgetReservationData {
    const committed = BudgetLedger.committedChildrenUsd(db, parentReservationId);
    const after = committed - record.amountUsd + actual;
    const parentAmount = Number(
      (BudgetLedger.loadRow(db, parentReservationId)).amount_usd,
    );
    if (after > parentAmount + EPS) {
      throw new BudgetExhaustedError(
        `settling child ${JSON.stringify(record.reservationId)} at $${actual.toFixed(4)} would ` +
          `exceed its parent ($${after.toFixed(4)} > $${parentAmount.toFixed(4)}); child stays ` +
          `OPEN and must be reconciled`,
      );
    }
    const closedAt = utcNowIso();
    db.prepare(
      "UPDATE budget_reservations SET status = 'settled', settled_amount_usd = ?, closed_at = ? " +
        "WHERE reservation_id = ?",
    ).run(actual, closedAt, record.reservationId);
    return {
      ...record,
      status: "settled",
      settledAmountUsd: actual,
      closedAt,
      parentReservationId,
    };
  }

  children(parentReservationId: string): BudgetReservationData[] {
    const db = openDatabase(this.path);
    try {
      const rows = db.prepare(
        "SELECT reservation_id, holder, amount_usd, status, settled_amount_usd, created_at, " +
          "closed_at, parent_reservation_id FROM budget_reservations " +
          "WHERE parent_reservation_id = ? ORDER BY created_at, reservation_id",
      ).all(parentReservationId) as Record<string, unknown>[];
      return rows.map(BudgetLedger.toRecord);
    } finally {
      db.close();
    }
  }

  /** Close a parent at the SUM OF ITS CHILDREN'S ACTUALS plus honest overage. */
  settleParentFromChildren(parentReservationId: string, overageUsd = 0): BudgetReservationData {
    const overage = requireAmount(overageUsd, true);
    const db = openDatabase(this.path);
    try {
      const parent = BudgetLedger.toRecord(BudgetLedger.loadRow(db, parentReservationId));
      if (parent.parentReservationId !== null) {
        throw new ContractError("not a top-level reservation");
      }
      const kids = this.children(parentReservationId);
      if (kids.some((k) => k.status === "open")) {
        throw new ReservationError(
          `parent ${JSON.stringify(parentReservationId)} still has open child allocation(s); ` +
            `settle or release them first`,
        );
      }
      const actual = kids.filter((k) =>
        k.status === "settled"
      ).reduce((sum, k) => sum + (k.settledAmountUsd ?? 0), 0) +
        overage;
      return this.settle(parentReservationId, round(actual, 9));
    } finally {
      db.close();
    }
  }

  release(reservationId: string): BudgetReservationData {
    const path = this.path;
    return withTx(path, (db) => {
      const record = BudgetLedger.toRecord(BudgetLedger.loadRow(db, reservationId));
      if (record.status !== "open") {
        throw new ReservationError(
          `reservation ${JSON.stringify(reservationId)} is ${record.status}, ` +
            `only open reservations can be released`,
        );
      }
      const closedAt = utcNowIso();
      db.prepare(
        "UPDATE budget_reservations SET status = 'released', closed_at = ? WHERE reservation_id = ?",
      ).run(closedAt, reservationId);
      return { ...record, status: "released", closedAt };
    });
  }

  outstandingUsd(): number {
    return this.readSums().openSum;
  }

  settledUsd(): number {
    return this.readSums().settled;
  }

  remainingUsd(): number {
    const { settled, openSum } = this.readSums();
    return this.cap - settled - openSum;
  }

  reservations(): BudgetReservationData[] {
    const db = openDatabase(this.path);
    try {
      const rows = db.prepare(
        "SELECT reservation_id, holder, amount_usd, status, settled_amount_usd, created_at, " +
          "closed_at, parent_reservation_id FROM budget_reservations " +
          "ORDER BY created_at, reservation_id",
      ).all() as Record<string, unknown>[];
      return rows.map(BudgetLedger.toRecord);
    } finally {
      db.close();
    }
  }

  openReservations(olderThan?: string): BudgetReservationData[] {
    const opens = this.reservations().filter((r) => r.status === "open");
    if (olderThan === undefined) return opens;
    return opens.filter((r) => r.createdAt < olderThan);
  }

  private readSums(): { settled: number; openSum: number } {
    const db = openDatabase(this.path);
    try {
      return BudgetLedger.sums(db);
    } finally {
      db.close();
    }
  }

  private static loadRow(
    db: ReturnType<typeof openDatabase>,
    reservationId: string,
  ): Record<string, unknown> {
    if (typeof reservationId !== "string" || reservationId.length === 0) {
      throw new ContractError("reservation_id must be a non-empty string");
    }
    const row = db.prepare(
      "SELECT reservation_id, holder, amount_usd, status, settled_amount_usd, created_at, " +
        "closed_at, parent_reservation_id FROM budget_reservations WHERE reservation_id = ?",
    ).get(reservationId) as Record<string, unknown> | undefined;
    if (row === undefined) {
      throw new ReservationError(`unknown reservation ${JSON.stringify(reservationId)}`);
    }
    return row;
  }

  private static toRecord(row: Record<string, unknown>): BudgetReservationData {
    return {
      schemaVersion: "1",
      reservationId: String(row["reservation_id"]),
      holder: String(row["holder"]),
      amountUsd: Number(row["amount_usd"]),
      status: String(row["status"]) as ReservationStatusValue,
      settledAmountUsd: row["settled_amount_usd"] === null
        ? null
        : Number(row["settled_amount_usd"]),
      createdAt: String(row["created_at"]),
      closedAt: row["closed_at"] === null ? null : String(row["closed_at"]),
      parentReservationId: row["parent_reservation_id"] === null
        ? null
        : String(row["parent_reservation_id"]),
    };
  }
}

function withTx<T>(path: string, body: (db: ReturnType<typeof openDatabase>) => T): T {
  const db = openDatabase(path);
  try {
    db.exec("BEGIN IMMEDIATE");
    let result: T;
    try {
      result = body(db);
    } catch (exc) {
      db.exec("ROLLBACK");
      throw exc;
    }
    db.exec("COMMIT");
    return result;
  } finally {
    db.close();
  }
}

export function round(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round((value + Number.EPSILON * Math.sign(value)) * factor) / factor;
}
