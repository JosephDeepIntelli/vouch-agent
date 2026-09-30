"""CSV reconciliation — the shipped deterministic business utility (M3-B4).

Reconciles two supplied product/catalog CSVs on a configurable join key and
produces an exportable discrepancy report: changed values, missing rows and
duplicate keys, each with source references back to the exact input position.

This is a pure data operation: no model calls, no semantic research, no
evidence of model quality. Changing the supplied data changes the result —
the operation reads only the materialized, digest-verified bytes.

Trustworthiness rules (M4 A3, from the reviewed defects):

* duplicate header names (``sku,price,price``) are REJECTED — they used to
  silently overwrite values and report changed rows as clean;
* empty header names are rejected (an unnamed column cannot be compared);
* rows with an EMPTY join-key value never join: they are reported as
  ``emptyKeyRows`` with their source position instead of fabricating a
  match under the key ``""``;
* zero comparable columns (schema divergence, or every shared column
  ignored) is reported as an INSUFFICIENT COMPARISON — never as ``clean``;
* duplicate keys stay AMBIGUOUS: no side's row is silently authoritative —
  ambiguous keys are excluded from the VALUE comparison and from ``matched``,
  and are listed with their occurrences (capped, so large repeated keys stay
  linear). Absence is still reported: a key missing from the other side is a
  finding however many times it repeats on its own side;
* row references carry the physical source LINE as well as the data-row
  number, so blank and multiline (quoted) records stay traceable;
* size/row/column bounds are enforced WHILE parsing, never after an
  unbounded allocation.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

from vouch_agent.errors import ContractError

#: Hard bounds keep the operation deterministic and bounded (materials are
#: already capped at 8 MiB; rows/columns/field bounds keep pathological
#: inputs out — enforced DURING parsing, not after materializing).
MAX_ROWS = 200_000
MAX_COLUMNS = 256
#: Per-field allocation bound while parsing (a single quoted field cannot
#: grow without limit before the row/column checks fire).
MAX_FIELD_BYTES = 8 * 1024 * 1024
#: Occurrence refs listed per duplicate-key entry; the TOTAL count is always
#: reported, so large repeated keys cost O(1) per extra row, not O(k).
MAX_DUPLICATE_OCCURRENCES_LISTED = 16


@dataclass(frozen=True)
class RowRef:
    """A traceable position in one source file.

    ``row_number`` is the 1-based DATA-record ordinal (header and blank
    lines excluded); ``line`` is the 1-based physical line where the record
    STARTS — correct for multiline (quoted) records too, so every
    discrepancy points at the exact source position.
    """

    source: str  # "left" | "right"
    row_number: int
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "rowNumber": self.row_number, "line": self.line}


@dataclass(frozen=True)
class _Row:
    number: int  # 1-based data-record ordinal
    line: int  # physical start line
    values: tuple[str, ...]


@dataclass(frozen=True)
class ReconcileReport:
    join_key: str
    left_rows: int
    right_rows: int
    matched: int
    changed: tuple[dict[str, Any], ...]
    missing_left: tuple[dict[str, Any], ...]
    missing_right: tuple[dict[str, Any], ...]
    duplicate_keys: tuple[dict[str, Any], ...]
    compared_columns: tuple[str, ...]
    #: columns present in only one of the two inputs (schema divergence).
    left_only_columns: tuple[str, ...] = ()
    right_only_columns: tuple[str, ...] = ()
    #: rows whose join-key value is empty — never joined, always reported.
    empty_key_rows: tuple[dict[str, Any], ...] = ()
    #: blank source records skipped while parsing, with their line positions.
    blank_records: tuple[dict[str, Any], ...] = ()
    #: False when the inputs share no comparable column: equivalence is NOT
    #: established and the report must never present it as clean.
    comparison_sufficient: bool = True
    comparison_reason: str = ""

    @property
    def clean(self) -> bool:
        """True only when the comparison was sufficient AND found nothing.

        An insufficient comparison (zero comparable columns) is never clean:
        'we could not compare anything' must not read as 'the files agree'.
        """
        return (
            self.comparison_sufficient
            and not self.changed
            and not self.missing_left
            and not self.missing_right
            and not self.duplicate_keys
            and not self.empty_key_rows
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": "2",
            "kind": "csv-reconciliation",
            "operation": "vouch-csv-reconcile/1",
            "deterministic": True,
            "joinKey": self.join_key,
            "rowCounts": {
                "left": self.left_rows,
                "right": self.right_rows,
                "matched": self.matched,
            },
            "comparedColumns": list(self.compared_columns),
            "schemaDivergence": {
                "leftOnlyColumns": list(self.left_only_columns),
                "rightOnlyColumns": list(self.right_only_columns),
            },
            "comparisonAdequacy": {
                "comparableColumns": len(self.compared_columns),
                "sufficient": self.comparison_sufficient,
                "reason": self.comparison_reason or None,
            },
            "changed": [entry for entry in self.changed],
            "missingInLeft": [entry for entry in self.missing_left],
            "missingInRight": [entry for entry in self.missing_right],
            "duplicateKeys": [entry for entry in self.duplicate_keys],
            "emptyKeyRows": [entry for entry in self.empty_key_rows],
            "blankRecords": [entry for entry in self.blank_records],
            "clean": self.clean,
        }


@dataclass
class _SideIndex:
    """One side's rows indexed by join key (first-seen order preserved)."""

    rows: list[_Row] = field(default_factory=list)
    by_key: dict[str, list[_Row]] = field(default_factory=dict)
    empty_key_rows: list[_Row] = field(default_factory=list)
    blank_records: list[int] = field(default_factory=list)


def _parse_csv(payload: bytes, name: str, delimiter: str) -> tuple[list[str], _SideIndex]:
    """Parse with bounds enforced WHILE reading (never materialize first)."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError(f"CSV material {name!r} is not valid UTF-8: {exc}") from exc
    # Apply the per-field allocation bound BEFORE reading: a single quoted
    # field cannot grow without limit ahead of the row/column checks.
    csv.field_size_limit(MAX_FIELD_BYTES)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header: list[str] | None = None
    index = _SideIndex()
    end_line = 0
    try:
        for record in reader:
            start_line = end_line + 1
            end_line = reader.line_num
            if header is None:
                header = record
                if not header or len(header) > MAX_COLUMNS:
                    raise ContractError(
                        f"CSV material {name!r} header must be 1..{MAX_COLUMNS} columns, "
                        f"got {len(header) if header else 0}"
                    )
                seen: set[str] = set()
                duplicated: list[str] = []
                for position, column in enumerate(header, start=1):
                    if not column.strip():
                        raise ContractError(
                            f"CSV material {name!r} has an empty header name at column "
                            f"{position}; unnamed columns cannot be compared"
                        )
                    if column in seen and column not in duplicated:
                        duplicated.append(column)
                    seen.add(column)
                if duplicated:
                    raise ContractError(
                        f"CSV material {name!r} has duplicate header names {duplicated}; "
                        "duplicate columns silently overwrite values — fix the header "
                        "before reconciling"
                    )
                continue
            if not record or record == [""]:
                index.blank_records.append(start_line)
                continue
            if len(record) != len(header):
                raise ContractError(
                    f"CSV material {name!r} has a ragged row ({len(record)} fields, expected "
                    f"{len(header)}) at line {start_line}"
                )
            if len(index.rows) >= MAX_ROWS:
                raise ContractError(
                    f"CSV material {name!r} exceeds {MAX_ROWS} data rows (first exceeded at "
                    f"line {start_line})"
                )
            index.rows.append(
                _Row(number=len(index.rows) + 1, line=start_line, values=tuple(record))
            )
    except csv.Error as exc:
        raise ContractError(f"CSV material {name!r} could not be parsed: {exc}") from exc
    if header is None:
        raise ContractError(f"CSV material {name!r} is empty")
    return header, index


def reconcile_csvs(
    left: bytes,
    right: bytes,
    *,
    join_key: str,
    left_name: str = "left.csv",
    right_name: str = "right.csv",
    delimiter: str = ",",
    ignore_columns: tuple[str, ...] = (),
) -> ReconcileReport:
    """Reconcile two CSVs by ``join_key``. Deterministic; order of outputs
    follows first-appearance order in the LEFT file, then the RIGHT file.

    Empty join-key rows never join (reported separately). Keys duplicated on
    either side stay AMBIGUOUS: they are reported with their occurrences and
    are never compared through one silently chosen authoritative row.
    """
    if delimiter not in (",", ";", "\t"):
        raise ContractError(f"unsupported CSV delimiter {delimiter!r}")
    left_header, left_index = _parse_csv(left, left_name, delimiter)
    right_header, right_index = _parse_csv(right, right_name, delimiter)
    for side, header in (("left", left_header), ("right", right_header)):
        if join_key not in header:
            raise ContractError(
                f"join key {join_key!r} is not a column of the {side} CSV "
                f"({left_name if side == 'left' else right_name}); columns: {header}"
            )
    compared = tuple(
        column
        for column in left_header
        if column in right_header and column != join_key and column not in ignore_columns
    )
    left_only = tuple(c for c in left_header if c not in right_header)
    right_only = tuple(c for c in right_header if c not in left_header)
    if compared:
        sufficient, reason = True, ""
    elif ignore_columns:
        sufficient = False
        reason = (
            "no comparable column remains: every shared non-key column is ignored "
            f"({sorted(ignore_columns)})"
        )
    else:
        sufficient = False
        reason = (
            "the two CSVs share no comparable column beyond the join key "
            f"(left-only: {list(left_only)}, right-only: {list(right_only)}); "
            "equivalence is not established by this comparison"
        )

    def keyed(header: list[str], index: _SideIndex) -> None:
        key_at = header.index(join_key)
        for row in index.rows:
            key = row.values[key_at]
            if key == "":
                index.empty_key_rows.append(row)
                continue
            index.by_key.setdefault(key, []).append(row)

    keyed(left_header, left_index)
    keyed(right_header, right_index)

    def record_of(header: list[str], row: _Row) -> dict[str, str]:
        return dict(zip(header, row.values, strict=True))

    def ref(source: str, row: _Row) -> dict[str, Any]:
        return RowRef(source, row.number, row.line).to_dict()

    empty_key_rows: list[dict[str, Any]] = [
        {"source": source, "key": "", "ref": ref(source, row)}
        for source, index in (("left", left_index), ("right", right_index))
        for row in index.empty_key_rows
    ]
    blank_records: list[dict[str, Any]] = [
        {"source": source, "line": line}
        for source, index in (("left", left_index), ("right", right_index))
        for line in index.blank_records
    ]

    duplicate_keys: list[dict[str, Any]] = []
    for source, index in (("left", left_index), ("right", right_index)):
        for key, rows_here in index.by_key.items():
            if len(rows_here) < 2:
                continue
            listed = [
                ref(source, row)
                for row in rows_here[:MAX_DUPLICATE_OCCURRENCES_LISTED]
            ]
            duplicate_keys.append(
                {
                    "key": key,
                    "source": source,
                    "occurrenceCount": len(rows_here),
                    "occurrences": listed,
                    "occurrencesTruncated": len(rows_here) > len(listed),
                    "comparison": "ambiguous",
                }
            )

    ambiguous = {entry["key"] for entry in duplicate_keys}
    left_key_at = left_header.index(join_key)
    right_key_at = right_header.index(join_key)
    changed: list[dict[str, Any]] = []
    missing_right: list[dict[str, Any]] = []
    missing_right_keys: set[str] = set()
    matched = 0
    for row in left_index.rows:
        key = row.values[left_key_at]
        if key == "":
            continue
        counterparts = right_index.by_key.get(key)
        if not counterparts:
            # absent is absent however many times the key repeats on this
            # side — reported against the FIRST occurrence, once.
            if key not in missing_right_keys:
                missing_right_keys.add(key)
                missing_right.append({"key": key, "leftRef": ref("left", row)})
            continue
        if key in ambiguous:
            continue  # duplicated on a side: values stay ambiguous, not compared
        matched += 1
        right_row = counterparts[0]  # unique: duplicated keys are ambiguous
        left_record = record_of(left_header, row)
        right_record = record_of(right_header, right_row)
        differences = [
            {
                "column": column,
                "left": left_record.get(column, ""),
                "right": right_record.get(column, ""),
            }
            for column in compared
            if left_record.get(column, "") != right_record.get(column, "")
        ]
        if differences:
            changed.append(
                {
                    "key": key,
                    "leftRef": ref("left", row),
                    "rightRef": ref("right", right_row),
                    "differences": differences,
                }
            )

    missing_left: list[dict[str, Any]] = []
    missing_left_keys: set[str] = set()
    for row in right_index.rows:
        key = row.values[right_key_at]
        if key == "":
            continue
        if key not in left_index.by_key and key not in missing_left_keys:
            missing_left_keys.add(key)
            missing_left.append({"key": key, "rightRef": ref("right", row)})

    return ReconcileReport(
        join_key=join_key,
        left_rows=len(left_index.rows),
        right_rows=len(right_index.rows),
        matched=matched,
        changed=tuple(changed),
        missing_left=tuple(missing_left),
        missing_right=tuple(missing_right),
        duplicate_keys=tuple(duplicate_keys),
        compared_columns=compared,
        left_only_columns=left_only,
        right_only_columns=right_only,
        empty_key_rows=tuple(empty_key_rows),
        blank_records=tuple(blank_records),
        comparison_sufficient=sufficient,
        comparison_reason=reason,
    )


def report_bytes(report: ReconcileReport) -> bytes:
    """Canonical JSON bytes of the report (digest-stable)."""
    text = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
    return text.encode("utf-8")
