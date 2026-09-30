/**
 * Metadata store + content-addressed artifact store (design §5, §10).
 *
 * TS storage is NAMESPACED: this implementation writes `.vowdo/workspace.json`
 * with `tool: "vowdo-agent-ts"` and its own schema version (see
 * appservices/workspace.ts). A Python workspace is never opened for writing
 * by this code; the only cross-implementation path is the explicit,
 * backed-up `vowdo import-python` migration.
 */

import {
  canonicalJson,
  CanonicalJsonError,
  isPlainObject,
  parseCanonical,
} from "../contracts/canonical.ts";
import { ContractError } from "../contracts/common.ts";
import { initWal, openDatabase } from "./sqlite.ts";
import type { DatabaseSync } from "node:sqlite";

const RECORDS_DDL = `
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, record_id)
)
`;

export function requireNonEmptyStr(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ContractError(`${field} must be a non-empty string, got ${JSON.stringify(value)}`);
  }
  return value;
}

export class MetadataStore {
  private db: DatabaseSync;
  private txDepth = 0;

  constructor(readonly path: string) {
    this.db = openDatabase(path);
    initWal(this.db);
    this.db.exec(RECORDS_DDL);
  }

  save(kind: string, recordId: string, data: object): void {
    kind = requireNonEmptyStr(kind, "kind");
    recordId = requireNonEmptyStr(recordId, "record_id");
    if (!isPlainObject(data)) {
      throw new ContractError(`record data must be an object`);
    }
    let payload: string;
    try {
      payload = canonicalJson(data);
    } catch (exc) {
      if (exc instanceof CanonicalJsonError) {
        throw new ContractError(`record ${kind}/${recordId} is not serializable: ${exc.message}`);
      }
      throw exc;
    }
    this.db
      .prepare(
        "INSERT INTO records (kind, record_id, data) VALUES (?, ?, ?) " +
          "ON CONFLICT (kind, record_id) DO UPDATE SET data = excluded.data",
      )
      .run(kind, recordId, payload);
  }

  /** Load a record; the canonical text is re-validated through the shared
   * numeric domain (foreign records with e.g. `5.0` spellings are refused
   * with an explicit error rather than silently re-spelled). */
  load(kind: string, recordId: string): Record<string, unknown> | null {
    kind = requireNonEmptyStr(kind, "kind");
    recordId = requireNonEmptyStr(recordId, "record_id");
    const row = this.db
      .prepare("SELECT data FROM records WHERE kind = ? AND record_id = ?")
      .get(kind, recordId) as { data: string } | undefined;
    if (row === undefined) return null;
    let parsed: unknown;
    try {
      parsed = parseCanonical(row.data);
    } catch (exc) {
      if (exc instanceof CanonicalJsonError) {
        throw new ContractError(
          `stored record ${kind}/${recordId} leaves the shared numeric domain: ${exc.message}`,
        );
      }
      throw exc;
    }
    if (!isPlainObject(parsed)) {
      throw new ContractError(`stored record ${kind}/${recordId} is not an object`);
    }
    return parsed;
  }

  listIds(kind: string): string[] {
    kind = requireNonEmptyStr(kind, "kind");
    const rows = this.db
      .prepare("SELECT record_id FROM records WHERE kind = ? ORDER BY record_id")
      .all(kind) as { record_id: string }[];
    return rows.map((r) => r.record_id);
  }

  /**
   * Group saves into one atomic transaction. The outermost call takes
   * BEGIN IMMEDIATE; nested uses map to savepoints.
   */
  transaction<T>(body: () => T): T {
    if (this.txDepth > 0) {
      const savepoint = `vowdo_sp_${this.txDepth}_${crypto.randomUUID().slice(0, 8)}`;
      this.db.exec(`SAVEPOINT ${savepoint}`);
      this.txDepth += 1;
      try {
        const result = body();
        this.txDepth -= 1;
        this.db.exec(`RELEASE ${savepoint}`);
        return result;
      } catch (exc) {
        this.txDepth -= 1;
        this.db.exec(`ROLLBACK TO ${savepoint}`);
        this.db.exec(`RELEASE ${savepoint}`);
        throw exc;
      }
    }
    this.db.exec("BEGIN IMMEDIATE");
    this.txDepth += 1;
    try {
      const result = body();
      this.txDepth -= 1;
      this.db.exec("COMMIT");
      return result;
    } catch (exc) {
      this.txDepth -= 1;
      this.db.exec("ROLLBACK");
      throw exc;
    }
  }

  close(): void {
    if (this.txDepth > 0) {
      this.db.exec("ROLLBACK");
      this.txDepth = 0;
    }
    this.db.close();
  }
}
