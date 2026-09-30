"""CSV reconciliation trustworthiness (M4 A3) — the reviewed defects,
inverted into regressions:

* duplicate headers silently overwrote changed values and reported clean;
* zero comparable columns reported clean;
* duplicate keys silently picked one row as authoritative;
* blank/multiline records had no traceable source positions;
* bounds were checked only after an unbounded allocation;
* duplicate-occurrence reporting was quadratic for large repeated keys.
"""

from __future__ import annotations

import json
import time

import pytest

import vouch_agent.appservices.csv_reconcile as csv_reconcile
from vouch_agent.appservices.csv_reconcile import reconcile_csvs, report_bytes
from vouch_agent.errors import ContractError


class TestDuplicateAndEmptyHeaders:
    def test_duplicate_headers_rejected_changed_value_not_swallowed(self) -> None:
        """Root reproduction: `sku,price,price` vs `sku,price,price` with a
        changed value under the second `price` used to report clean=true."""
        left = b"sku,price,price\nx,100,200\n"
        right = b"sku,price,price\nx,999,200\n"
        with pytest.raises(ContractError, match=r"duplicate header names.*price"):
            reconcile_csvs(left, right, join_key="sku")

    def test_duplicate_headers_rejected_even_when_values_agree(self) -> None:
        left = b"sku,price,price\nx,100,100\n"
        with pytest.raises(ContractError, match="duplicate header"):
            reconcile_csvs(left, left, join_key="sku")

    def test_empty_header_name_rejected(self) -> None:
        with pytest.raises(ContractError, match="empty header name"):
            reconcile_csvs(b"sku,,price\nx,1,2\n", b"sku,,price\nx,1,2\n", join_key="sku")

    def test_whitespace_only_header_name_rejected(self) -> None:
        with pytest.raises(ContractError, match="empty header name"):
            reconcile_csvs(b"sku, ,price\nx,1,2\n", b"sku, ,price\nx,1,2\n", join_key="sku")

    def test_one_bad_header_blocks_the_whole_reconciliation(self) -> None:
        """A clean right side does not excuse a broken left header."""
        with pytest.raises(ContractError, match="left"):
            reconcile_csvs(b"sku,price,price\nx,1,1\n", b"sku,price\nx,1\n", join_key="sku")


class TestEmptyJoinKeys:
    def test_empty_join_key_rows_never_join_and_are_reported(self) -> None:
        left = b"sku,price\nA-1,10\n,30\n,40\n"
        right = b"sku,price\nA-1,10\n,99\n"
        report = reconcile_csvs(left, right, join_key="sku")
        # the two empty keys on the left do NOT match the empty key on the
        # right (no fabricated "" == "" pair) and do not count as missing
        assert report.matched == 1
        assert not any(entry["key"] == "" for entry in report.missing_right)
        assert not any(entry["key"] == "" for entry in report.missing_left)
        assert [entry["source"] for entry in report.empty_key_rows] == ["left", "left", "right"]
        assert report.empty_key_rows[0]["ref"]["rowNumber"] == 2
        assert report.clean is False

    def test_empty_key_rows_carry_source_positions(self) -> None:
        left = b"sku,price\nA-1,10\n\n,30\n"
        report = reconcile_csvs(left, b"sku,price\nA-1,10\n", join_key="sku")
        empty = report.empty_key_rows[0]
        assert empty["source"] == "left"
        assert empty["ref"]["line"] == 4  # after the blank line at line 3
        assert empty["ref"]["rowNumber"] == 2  # data ordinal skips blanks


class TestSchemaDivergenceAndAdequacy:
    def test_disjoint_column_sets_state_insufficient_comparison(self) -> None:
        report = reconcile_csvs(b"sku,price\nA-1,1\n", b"sku,name\nA-1,x\n", join_key="sku")
        assert report.clean is False
        assert report.comparison_sufficient is False
        assert report.left_only_columns == ("price",)
        assert report.right_only_columns == ("name",)
        parsed = json.loads(report_bytes(report))
        assert parsed["comparisonAdequacy"]["comparableColumns"] == 0
        assert parsed["comparisonAdequacy"]["reason"]

    def test_same_header_clean_stays_clean(self) -> None:
        left = b"sku,price,name\nA-1,10,K\n"
        report = reconcile_csvs(left, left, join_key="sku")
        assert report.clean is True
        assert report.comparison_sufficient is True
        assert report.left_only_columns == ()
        assert report.right_only_columns == ()


class TestDuplicateKeyAmbiguity:
    def test_duplicated_key_never_compares_through_one_chosen_row(self) -> None:
        """Left has A-1 twice with different prices; right has A-1 once.
        The old code picked right's row / left's FIRST row and reported a
        'changed' value against an arbitrary counterpart."""
        left = b"sku,price\nA-1,10\nA-1,11\n"
        right = b"sku,price\nA-1,10\n"
        report = reconcile_csvs(left, right, join_key="sku")
        assert list(report.changed) == []
        assert report.matched == 0  # A-1 is ambiguous, not a clean pair
        assert len(report.duplicate_keys) == 1
        entry = report.duplicate_keys[0]
        assert entry["key"] == "A-1"
        assert entry["source"] == "left"
        assert entry["comparison"] == "ambiguous"
        assert entry["occurrenceCount"] == 2
        assert [occ["rowNumber"] for occ in entry["occurrences"]] == [1, 2]
        assert report.clean is False

    def test_duplicated_on_right_is_also_ambiguous(self) -> None:
        left = b"sku,price\nA-1,10\n"
        right = b"sku,price\nA-1,10\nA-1,99\n"
        report = reconcile_csvs(left, right, join_key="sku")
        assert list(report.changed) == []
        assert report.duplicate_keys[0]["source"] == "right"
        assert report.clean is False

    def test_missing_side_still_reported_for_duplicated_key(self) -> None:
        left = b"sku,price\nA-1,10\nA-1,11\n"
        right = b"sku,price\nB-2,20\n"
        report = reconcile_csvs(left, right, join_key="sku")
        assert [entry["key"] for entry in report.missing_right] == ["A-1"]
        assert [entry["key"] for entry in report.missing_left] == ["B-2"]

    def test_duplicate_occurrence_reporting_is_capped_not_quadratic(self) -> None:
        """5k repeats of one key stay linear: total count reported, listed
        occurrences capped."""
        rows = ",".join(["sku", "price"]).encode() + b"\n" + b"A-1,10\n" * 5000
        started = time.monotonic()
        report = reconcile_csvs(rows, b"sku,price\nA-1,10\n", join_key="sku")
        elapsed = time.monotonic() - started
        entry = report.duplicate_keys[0]
        assert entry["occurrenceCount"] == 5000
        assert len(entry["occurrences"]) == csv_reconcile.MAX_DUPLICATE_OCCURRENCES_LISTED
        assert entry["occurrencesTruncated"] is True
        payload = report_bytes(report)
        assert payload.count(b"rowNumber") < 200  # the report itself stays small
        assert elapsed < 10.0


class TestSourcePositions:
    def test_multiline_quoted_records_carry_start_lines(self) -> None:
        left = (
            b'sku,name,price\n'
            b'A-1,"multi\nline\nname",10\n'
            b'A-2,Plain,20\n'
        )
        right = b"sku,name,price\nA-1,rewritten,10\nA-2,Plain,20\n"
        report = reconcile_csvs(left, right, join_key="sku")
        assert [entry["key"] for entry in report.changed] == ["A-1"]
        # A-1's record STARTS at physical line 2 even though it spans 3 lines;
        # A-2 starts at line 5 — every discrepancy traces to the right record
        assert report.changed[0]["leftRef"]["line"] == 2
        assert report.changed[0]["leftRef"]["rowNumber"] == 1
        assert report.changed[0]["rightRef"]["line"] == 2

    def test_blank_records_are_reported_with_their_line(self) -> None:
        left = b"sku,price\nA-1,10\n\n\nA-2,20\n"
        right = b"sku,price\nA-1,10\nA-2,20\n"
        report = reconcile_csvs(left, right, join_key="sku")
        assert [entry["line"] for entry in report.blank_records] == [3, 4]
        assert report.blank_records[0]["source"] == "left"
        # blank records do not shift data-row ordinals of later records
        assert list(report.changed) == []
        assert report.matched == 2

    def test_utf8_sig_and_deterministic_bytes(self) -> None:
        left = "sku,name\nA-1,电水壶\n".encode("utf-8-sig")
        right = "sku,name\nA-1,磨豆机\n".encode("utf-8-sig")
        report = reconcile_csvs(left, right, join_key="sku")
        assert report.changed[0]["differences"][0]["left"] == "电水壶"
        again = reconcile_csvs(left, right, join_key="sku")
        assert report_bytes(again) == report_bytes(report)


class TestBoundsWhileParsing:
    def test_row_bound_enforced_while_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(csv_reconcile, "MAX_ROWS", 5)
        payload = b"sku,price\n" + b"A-1,10\n" * 6
        with pytest.raises(ContractError, match="exceeds 5 data rows"):
            reconcile_csvs(payload, b"sku,price\nA-1,10\n", join_key="sku")

    def test_column_bound_enforced_on_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(csv_reconcile, "MAX_COLUMNS", 3)
        header = ",".join(f"c{i}" for i in range(4)).encode()
        with pytest.raises(ContractError, match=r"header must be 1\.\.3 columns"):
            reconcile_csvs(header + b"\n", header + b"\n", join_key="c0")

    def test_ragged_row_reports_line_number(self) -> None:
        with pytest.raises(ContractError, match="at line 3"):
            reconcile_csvs(b"sku,price\nA-1,10\nA-2\n", b"sku,price\n", join_key="sku")

    def test_field_bound_enforced_while_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(csv_reconcile, "MAX_FIELD_BYTES", 64)
        giant = b"sku,price\nA-1," + b"9" * 200 + b"\n"
        with pytest.raises(ContractError):
            reconcile_csvs(giant, b"sku,price\nA-1,1\n", join_key="sku")
