/** Append-only event + cost journal on SQLite. Records are never updated;
 * corrections are new events. Duplicate ids are refused — a replayed event
 * is a bug, not a no-op.
 *
 * Each operation opens its own short-lived connection: one instance is
 * safely usable from multiple workers and processes without an in-process
 * locking protocol. */

import { canonicalJson, isPlainObject, parseCanonical } from "../contracts/canonical.ts";
import { ContractError } from "../contracts/common.ts";
import {
  type CostEntryData,
  costFromDict,
  eventFromDict,
  type EventRecordData,
} from "../contracts/journal.ts";
import { initWal, isUniqueViolation, openDatabase } from "./sqlite.ts";

const EVENTS_DDL = `
CREATE TABLE IF NOT EXISTS journal_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject TEXT NOT NULL,
    data TEXT NOT NULL,
    mode TEXT NOT NULL
)
`;

const COSTS_DDL = `
CREATE TABLE IF NOT EXISTS journal_costs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    subject TEXT NOT NULL,
    amount_usd REAL,
    human_minutes REAL,
    measurable INTEGER NOT NULL,
    mode TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    note TEXT NOT NULL
)
`;

export class Journal {
  constructor(readonly path: string) {
    const db = openDatabase(path);
    try {
      initWal(db);
      db.exec(EVENTS_DDL);
      db.exec(COSTS_DDL);
    } finally {
      db.close();
    }
  }

  append(event: EventRecordData): void {
    const db = openDatabase(this.path);
    try {
      db.prepare(
        "INSERT INTO journal_events (event_id, kind, occurred_at, actor, subject, data, mode) " +
          "VALUES (?, ?, ?, ?, ?, ?, ?)",
      ).run(
        event.eventId,
        event.kind,
        event.occurredAt,
        event.actor,
        event.subject,
        canonicalJson(event.data),
        event.mode,
      );
    } catch (exc) {
      if (isUniqueViolation(exc)) {
        throw new ContractError(`duplicate event id ${JSON.stringify(event.eventId)}`);
      }
      throw exc;
    } finally {
      db.close();
    }
  }

  appendCost(entry: CostEntryData): void {
    const db = openDatabase(this.path);
    try {
      db.prepare(
        "INSERT INTO journal_costs (entry_id, category, subject, amount_usd, human_minutes, " +
          "measurable, mode, recorded_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
      ).run(
        entry.entryId,
        entry.category,
        entry.subject,
        entry.amountUsd,
        entry.humanMinutes,
        entry.measurable ? 1 : 0,
        entry.mode,
        entry.recordedAt,
        entry.note,
      );
    } catch (exc) {
      if (isUniqueViolation(exc)) {
        throw new ContractError(`duplicate cost entry id ${JSON.stringify(entry.entryId)}`);
      }
      throw exc;
    } finally {
      db.close();
    }
  }

  events(subject?: string): EventRecordData[] {
    const db = openDatabase(this.path);
    try {
      const sql = subject === undefined
        ? "SELECT event_id, kind, occurred_at, actor, subject, data, mode FROM journal_events ORDER BY seq"
        : "SELECT event_id, kind, occurred_at, actor, subject, data, mode FROM journal_events WHERE subject = ? ORDER BY seq";
      const rows =
        (subject === undefined ? db.prepare(sql).all() : db.prepare(sql).all(subject)) as Record<
          string,
          unknown
        >[];
      return rows.map((row) =>
        eventFromDict({
          schemaVersion: "1",
          eventId: String(row["event_id"]),
          kind: String(row["kind"]),
          occurredAt: String(row["occurred_at"]),
          actor: String(row["actor"]),
          subject: String(row["subject"]),
          data: loadsObject(String(row["data"]), String(row["event_id"])),
          mode: String(row["mode"]),
        })
      );
    } finally {
      db.close();
    }
  }

  costEntries(subject?: string): CostEntryData[] {
    const db = openDatabase(this.path);
    try {
      const base =
        "SELECT entry_id, category, subject, amount_usd, human_minutes, measurable, mode, recorded_at, note FROM journal_costs";
      const rows = (subject === undefined
        ? db.prepare(`${base} ORDER BY seq`).all()
        : db.prepare(`${base} WHERE subject = ? ORDER BY seq`).all(subject)) as Record<
          string,
          unknown
        >[];
      return rows.map((row) =>
        costFromDict({
          schemaVersion: "1",
          entryId: String(row["entry_id"]),
          category: String(row["category"]),
          subject: String(row["subject"]),
          amountUsd: row["amount_usd"] === null ? null : Number(row["amount_usd"]),
          humanMinutes: row["human_minutes"] === null ? null : Number(row["human_minutes"]),
          measurable: Number(row["measurable"]) !== 0,
          mode: String(row["mode"]),
          recordedAt: String(row["recorded_at"]),
          note: String(row["note"]),
        })
      );
    } finally {
      db.close();
    }
  }

  /** A journal writer pinned to one subject — what a worker session gets. */
  subjectWriter(subject: string): SubjectJournal {
    if (typeof subject !== "string" || subject.length === 0) {
      throw new ContractError("subject must be a non-empty string");
    }
    return new SubjectJournal(this, subject);
  }
}

export class SubjectJournal {
  constructor(private journal: Journal, readonly subject: string) {}

  append(event: EventRecordData): void {
    if (event.subject && event.subject !== this.subject) {
      throw new ContractError(
        `subject-scoped writer for ${JSON.stringify(this.subject)} cannot append an event about ${
          JSON.stringify(event.subject)
        }`,
      );
    }
    this.journal.append({ ...event, subject: this.subject });
  }

  appendCost(entry: CostEntryData): void {
    if (entry.subject && entry.subject !== this.subject) {
      throw new ContractError(
        `subject-scoped writer for ${
          JSON.stringify(this.subject)
        } cannot append a cost entry about ${JSON.stringify(entry.subject)}`,
      );
    }
    this.journal.appendCost({ ...entry, subject: this.subject });
  }
}

function loadsObject(payload: string, ref: string): Record<string, unknown> {
  const parsed = parseCanonical(payload);
  if (!isPlainObject(parsed)) {
    throw new ContractError(`journal event ${JSON.stringify(ref)} carries non-object data`);
  }
  return parsed;
}
