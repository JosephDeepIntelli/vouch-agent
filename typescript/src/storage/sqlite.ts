/**
 * node:sqlite wrapper — verified under `deno run` AND `deno compile`
 * (SQLite 3.53.2, Deno 2.9.7; see scripts/verify-native.sh).
 *
 * One short-lived connection per operation for the journal/ledger (safe
 * across threads AND processes without in-process locking), WAL mode, and
 * BEGIN IMMEDIATE writer transactions so concurrent writers serialize at
 * the database, not in application code.
 */

import { DatabaseSync } from "node:sqlite";

export const BUSY_TIMEOUT_MS = 30_000;

export function openDatabase(path: string): DatabaseSync {
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA busy_timeout=${BUSY_TIMEOUT_MS}`);
  return db;
}

export function initWal(db: DatabaseSync): void {
  db.exec("PRAGMA journal_mode=WAL");
  db.exec("PRAGMA synchronous=NORMAL");
}

/** Run `body` inside one BEGIN IMMEDIATE transaction on its own connection. */
export function withImmediate<T>(path: string, body: (db: DatabaseSync) => T): T {
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

/** Map a node:sqlite constraint error to a marker the caller can classify. */
export function isUniqueViolation(exc: unknown): boolean {
  return exc instanceof Error && exc.message.includes("UNIQUE constraint failed");
}
