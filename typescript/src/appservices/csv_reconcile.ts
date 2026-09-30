/**
 * CSV reconciliation — the shipped deterministic business utility.
 *
 * Ported from `vouch_agent/appservices/csv_reconcile.py` with its exact
 * observable behavior: duplicate/empty header rejection, empty-key rows that
 * never join, ambiguous duplicate keys (never silently authoritative),
 * physical source-line references (multiline quoted records included),
 * bounds enforced WHILE parsing, and byte-identical report artifacts.
 *
 * The CSV reader reimplements Python's `csv.reader` semantics for the
 * default dialect (doublequote, no escapechar, lenient quoting): a quote is
 * special only at field start, characters after the closing quote are
 * appended, and `\r`, `\n`, `\r\n` each terminate a record (counting one
 * physical line each).
 */

import { digestBytes, pythonPrettyJson } from "../contracts/canonical.ts";
import { ContractError } from "../contracts/common.ts";

export const MAX_ROWS = 200_000;
export const MAX_COLUMNS = 256;
/** Per-field allocation bound while parsing. */
export const MAX_FIELD_BYTES = 8 * 1024 * 1024;
/** Occurrence refs listed per duplicate-key entry (total always reported). */
export const MAX_DUPLICATE_OCCURRENCES_LISTED = 16;

export interface RowRef {
  source: "left" | "right";
  rowNumber: number;
  line: number;
}

interface Row {
  number: number; // 1-based data-record ordinal
  line: number; // physical start line
  values: string[];
}

export interface ReconcileReport {
  schemaVersion: "2";
  kind: "csv-reconciliation";
  operation: "vouch-csv-reconcile/1";
  deterministic: true;
  joinKey: string;
  rowCounts: { left: number; right: number; matched: number };
  comparedColumns: string[];
  schemaDivergence: { leftOnlyColumns: string[]; rightOnlyColumns: string[] };
  comparisonAdequacy: { comparableColumns: number; sufficient: boolean; reason: string | null };
  changed: Array<
    {
      key: string;
      leftRef: RowRef;
      rightRef: RowRef;
      differences: Array<{ column: string; left: string; right: string }>;
    }
  >;
  missingInLeft: Array<{ key: string; rightRef: RowRef }>;
  missingInRight: Array<{ key: string; leftRef: RowRef }>;
  duplicateKeys: Array<{
    key: string;
    source: "left" | "right";
    occurrenceCount: number;
    occurrences: RowRef[];
    occurrencesTruncated: boolean;
    comparison: "ambiguous";
  }>;
  emptyKeyRows: Array<{ source: "left" | "right"; key: ""; ref: RowRef }>;
  blankRecords: Array<{ source: "left" | "right"; line: number }>;
  clean: boolean;
}

// --- Python-compatible CSV reader ---------------------------------------------

interface ParsedCsv {
  header: string[];
  rows: Row[];
  blankRecordLines: number[];
  /** Every yielded record in order (blanks included), csv.reader-style. */
  allRecords: string[][];
  /** Physical line AFTER each record — Python reader.line_num post-yield. */
  allEndLines: number[];
}

/**
 * Parse with bounds enforced WHILE reading. Line semantics match Python's
 * `csv.reader`: every `\r`, `\n` or `\r\n` (one line) advances the physical
 * line counter, including newlines embedded in quoted fields.
 */
export function parseCsv(payload: Uint8Array, name: string, delimiter: string): ParsedCsv {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(payload);
  } catch (exc) {
    throw new ContractError(`CSV material ${pyRepr(name)} is not valid UTF-8: ${exc}`);
  }
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1); // utf-8-sig

  const rows: Row[] = [];
  const blankRecordLines: number[] = [];
  const allRecords: string[][] = [];
  const allEndLines: number[] = [];
  let header: string[] | null = null;
  let line = 1; // current physical line
  let recordStartLine = 1;
  let index = 0;
  let field = "";
  let fieldChars = 0;
  let record: string[] = [];
  let inQuotes = false;
  let sawAnyQuote = false;

  const appendChar = (ch: string) => {
    field += ch;
    fieldChars += 1;
    if (fieldChars > MAX_FIELD_BYTES) {
      throw new ContractError(
        `CSV material ${pyRepr(name)} could not be parsed: field exceeds ` +
          `${MAX_FIELD_BYTES} characters at line ${recordStartLine}`,
      );
    }
  };
  const pushField = () => {
    record.push(field);
    field = "";
    fieldChars = 0;
  };
  const finishRecord = (endLine: number) => {
    // Python yields [] for a completely empty line: no field ever started.
    // A quoted empty string ("") DID start a field and yields [''].
    if (record.length !== 0 || field.length !== 0 || sawAnyQuote) {
      pushField();
    }
    allRecords.push(record);
    allEndLines.push(endLine);
    if (header === null) {
      header = record;
      validateHeader(header, name);
    } else if (record.length === 0 || (record.length === 1 && record[0] === "")) {
      blankRecordLines.push(recordStartLine);
    } else {
      if (record.length !== header.length) {
        throw new ContractError(
          `CSV material ${pyRepr(name)} has a ragged row (${record.length} fields, ` +
            `expected ${header.length}) at line ${recordStartLine}`,
        );
      }
      if (rows.length >= MAX_ROWS) {
        throw new ContractError(
          `CSV material ${pyRepr(name)} exceeds ${MAX_ROWS} data rows (first exceeded at ` +
            `line ${recordStartLine})`,
        );
      }
      rows.push({ number: rows.length + 1, line: recordStartLine, values: record });
    }
    record = [];
    recordStartLine = endLine + 1;
  };

  while (index < text.length) {
    const ch = text[index];
    if (inQuotes) {
      if (ch === '"') {
        if (text[index + 1] === '"') {
          appendChar('"');
          index += 2;
          continue;
        }
        inQuotes = false;
        index += 1;
        continue;
      }
      if (ch === "\n") {
        line += 1;
        appendChar(ch);
        index += 1;
        continue;
      }
      if (ch === "\r") {
        line += 1;
        appendChar(ch);
        index += 1;
        if (text[index] === "\n") {
          appendChar("\n");
          index += 1;
        }
        continue;
      }
      appendChar(ch);
      index += 1;
      continue;
    }
    if (ch === '"') {
      if (field.length === 0 && !sawAnyQuote) {
        inQuotes = true;
        sawAnyQuote = true;
        index += 1;
        continue;
      }
      // lenient (strict=False): quotes outside an opening position are literal
      appendChar(ch);
      index += 1;
      continue;
    }
    if (ch === delimiter) {
      pushField();
      sawAnyQuote = false;
      index += 1;
      continue;
    }
    if (ch === "\n" || ch === "\r") {
      // The terminator closes the record ON this physical line (Python's
      // reader.line_num after yield = lines consumed including this one).
      finishRecord(line);
      line += 1;
      if (ch === "\r" && text[index + 1] === "\n") index += 1;
      sawAnyQuote = false;
      index += 1;
      continue;
    }
    appendChar(ch);
    index += 1;
  }
  // EOF: an open quoted field is accepted leniently; any pending field ends
  // the final record (Python returns a final record when one exists).
  if (field.length > 0 || record.length > 0) {
    finishRecord(line);
  }
  if (header === null) {
    throw new ContractError(`CSV material ${pyRepr(name)} is empty`);
  }
  return { header, rows, blankRecordLines, allRecords, allEndLines };
}

function validateHeader(header: string[], name: string): void {
  if (header.length === 0 || header.length > MAX_COLUMNS) {
    throw new ContractError(
      `CSV material ${pyRepr(name)} header must be 1..${MAX_COLUMNS} columns, ` +
        `got ${header.length}`,
    );
  }
  const seen = new Set<string>();
  const duplicated: string[] = [];
  header.forEach((column, i) => {
    const position = i + 1;
    if (column.trim().length === 0) {
      throw new ContractError(
        `CSV material ${pyRepr(name)} has an empty header name at column ${position}; ` +
          `unnamed columns cannot be compared`,
      );
    }
    if (seen.has(column) && !duplicated.includes(column)) duplicated.push(column);
    seen.add(column);
  });
  if (duplicated.length > 0) {
    throw new ContractError(
      `CSV material ${pyRepr(name)} has duplicate header names ${pyListRepr(duplicated)}; ` +
        `duplicate columns silently overwrite values — fix the header before reconciling`,
    );
  }
}

// --- reconciliation ------------------------------------------------------------

interface SideIndex {
  rows: Row[];
  byKey: Map<string, Row[]>;
  emptyKeyRows: Row[];
  blankRecords: number[];
}

export function reconcileCsvs(
  left: Uint8Array,
  right: Uint8Array,
  options: {
    joinKey: string;
    leftName?: string;
    rightName?: string;
    delimiter?: string;
    ignoreColumns?: string[];
  },
): ReconcileReport {
  const joinKey = options.joinKey;
  const leftName = options.leftName ?? "left.csv";
  const rightName = options.rightName ?? "right.csv";
  const delimiter = options.delimiter ?? ",";
  const ignoreColumns = options.ignoreColumns ?? [];
  if (delimiter !== "," && delimiter !== ";" && delimiter !== "\t") {
    throw new ContractError(`unsupported CSV delimiter ${pyRepr(delimiter)}`);
  }
  const leftParsed = parseCsv(left, leftName, delimiter);
  const rightParsed = parseCsv(right, rightName, delimiter);
  const leftHeader = leftParsed.header;
  const rightHeader = rightParsed.header;

  for (
    const [side, header, fileName] of [
      ["left", leftHeader, leftName],
      ["right", rightHeader, rightName],
    ] as const
  ) {
    if (!header.includes(joinKey)) {
      throw new ContractError(
        `join key ${pyRepr(joinKey)} is not a column of the ${side} CSV ` +
          `(${fileName}); columns: ${pyListRepr(header)}`,
      );
    }
  }

  const ignoreSet = new Set(ignoreColumns);
  const compared = leftHeader.filter(
    (c) => rightHeader.includes(c) && c !== joinKey && !ignoreSet.has(c),
  );
  const leftOnly = leftHeader.filter((c) => !rightHeader.includes(c));
  const rightOnly = rightHeader.filter((c) => !leftHeader.includes(c));
  let sufficient = true;
  let reason = "";
  if (compared.length > 0) {
    // sufficient
  } else if (ignoreColumns.length > 0) {
    sufficient = false;
    reason = `no comparable column remains: every shared non-key column is ignored ` +
      `(${pyListRepr([...ignoreColumns].sort())})`;
  } else {
    sufficient = false;
    reason = `the two CSVs share no comparable column beyond the join key ` +
      `(left-only: ${pyListRepr(leftOnly)}, right-only: ${pyListRepr(rightOnly)}); ` +
      `equivalence is not established by this comparison`;
  }

  const buildIndex = (header: string[], parsed: ParsedCsv): SideIndex => {
    const keyAt = header.indexOf(joinKey);
    const byKey = new Map<string, Row[]>();
    const emptyKeyRows: Row[] = [];
    for (const row of parsed.rows) {
      const key = row.values[keyAt];
      if (key === "") {
        emptyKeyRows.push(row);
        continue;
      }
      const list = byKey.get(key);
      if (list) list.push(row);
      else byKey.set(key, [row]);
    }
    return { rows: parsed.rows, byKey, emptyKeyRows, blankRecords: parsed.blankRecordLines };
  };
  const leftIndex = buildIndex(leftHeader, leftParsed);
  const rightIndex = buildIndex(rightHeader, rightParsed);

  const ref = (source: "left" | "right", row: Row): RowRef => ({
    source,
    rowNumber: row.number,
    line: row.line,
  });
  const recordOf = (header: string[], row: Row): Map<string, string> => {
    const map = new Map<string, string>();
    header.forEach((name, i) => map.set(name, row.values[i]));
    return map;
  };

  const emptyKeyRows: ReconcileReport["emptyKeyRows"] = [];
  for (const [source, index] of [["left", leftIndex], ["right", rightIndex]] as const) {
    for (const row of index.emptyKeyRows) {
      emptyKeyRows.push({ source, key: "", ref: ref(source, row) });
    }
  }
  const blankRecords: ReconcileReport["blankRecords"] = [];
  for (const [source, index] of [["left", leftIndex], ["right", rightIndex]] as const) {
    for (const line of index.blankRecords) {
      blankRecords.push({ source, line });
    }
  }

  const duplicateKeys: ReconcileReport["duplicateKeys"] = [];
  for (const [source, index] of [["left", leftIndex], ["right", rightIndex]] as const) {
    for (const [key, rowsHere] of index.byKey) {
      if (rowsHere.length < 2) continue;
      const listed = rowsHere.slice(0, MAX_DUPLICATE_OCCURRENCES_LISTED).map((r) => ref(source, r));
      duplicateKeys.push({
        key,
        source,
        occurrenceCount: rowsHere.length,
        occurrences: listed,
        occurrencesTruncated: rowsHere.length > listed.length,
        comparison: "ambiguous",
      });
    }
  }

  const ambiguous = new Set(duplicateKeys.map((entry) => entry.key));
  const leftKeyAt = leftHeader.indexOf(joinKey);
  const rightKeyAt = rightHeader.indexOf(joinKey);
  const changed: ReconcileReport["changed"] = [];
  const missingRight: ReconcileReport["missingInRight"] = [];
  const missingRightKeys = new Set<string>();
  let matched = 0;
  for (const row of leftIndex.rows) {
    const key = row.values[leftKeyAt];
    if (key === "") continue;
    const counterparts = rightIndex.byKey.get(key);
    if (counterparts === undefined) {
      // absent is absent however many times the key repeats on this side —
      // reported against the FIRST occurrence, once.
      if (!missingRightKeys.has(key)) {
        missingRightKeys.add(key);
        missingRight.push({ key, leftRef: ref("left", row) });
      }
      continue;
    }
    if (ambiguous.has(key)) continue; // duplicated on a side: never compared through one row
    matched += 1;
    const rightRow = counterparts[0];
    const leftRecord = recordOf(leftHeader, row);
    const rightRecord = recordOf(rightHeader, rightRow);
    const differences: Array<{ column: string; left: string; right: string }> = [];
    for (const column of compared) {
      const l = leftRecord.get(column) ?? "";
      const r = rightRecord.get(column) ?? "";
      if (l !== r) differences.push({ column, left: l, right: r });
    }
    if (differences.length > 0) {
      changed.push({
        key,
        leftRef: ref("left", row),
        rightRef: ref("right", rightRow),
        differences,
      });
    }
  }

  const missingLeft: ReconcileReport["missingInLeft"] = [];
  const missingLeftKeys = new Set<string>();
  for (const row of rightIndex.rows) {
    const key = row.values[rightKeyAt];
    if (key === "") continue;
    if (!leftIndex.byKey.has(key) && !missingLeftKeys.has(key)) {
      missingLeftKeys.add(key);
      missingLeft.push({ key, rightRef: ref("right", row) });
    }
  }

  const clean = sufficient &&
    changed.length === 0 &&
    missingLeft.length === 0 &&
    missingRight.length === 0 &&
    duplicateKeys.length === 0 &&
    emptyKeyRows.length === 0;

  return {
    schemaVersion: "2",
    kind: "csv-reconciliation",
    operation: "vouch-csv-reconcile/1",
    deterministic: true,
    joinKey,
    rowCounts: { left: leftIndex.rows.length, right: rightIndex.rows.length, matched },
    comparedColumns: compared,
    schemaDivergence: { leftOnlyColumns: leftOnly, rightOnlyColumns: rightOnly },
    comparisonAdequacy: {
      comparableColumns: compared.length,
      sufficient,
      reason: reason === "" ? null : reason,
    },
    changed,
    missingInLeft: missingLeft,
    missingInRight: missingRight,
    duplicateKeys,
    emptyKeyRows,
    blankRecords,
    clean,
  };
}

/** Canonical JSON bytes of the report (digest-stable, Python-pretty form). */
export function reportBytes(report: ReconcileReport): Uint8Array {
  return new TextEncoder().encode(pythonPrettyJson(report));
}

export function reportDigest(report: ReconcileReport): string {
  return digestBytes(reportBytes(report));
}

/** Python `repr` of a string — appears verbatim in error/reason messages. */
export function pyRepr(value: string): string {
  const hasSingle = value.includes("'");
  const hasDouble = value.includes('"');
  if (hasSingle && !hasDouble) {
    return `"${value}"`;
  }
  return `'${value.replaceAll("\\", "\\\\").replaceAll("'", "\\'")}'`;
}

/** Python `repr` of a list of strings — appears verbatim in error/reason text. */
export function pyListRepr(values: string[]): string {
  return `[${values.map(pyRepr).join(", ")}]`;
}
