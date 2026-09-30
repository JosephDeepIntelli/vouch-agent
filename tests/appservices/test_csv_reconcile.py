"""CSV reconciliation utility: deterministic, source-linked, bounded."""

from __future__ import annotations

import json

import pytest

from vouch_agent.appservices.csv_reconcile import reconcile_csvs, report_bytes
from vouch_agent.errors import ContractError

LEFT = "sku,name,price,stock\nA-1,Kettle,95.00,4\nA-2,Grinder,129.00,2\nA-3,Scale,39.00,7\n"
RIGHT = "sku,name,price,stock\nA-1,Kettle,95.00,4\nA-2,Grinder,139.00,2\nB-9,Mug,19.00,10\n"


def test_changed_missing_duplicate_reported_with_row_refs() -> None:
    left = LEFT + "A-3,Scale,39.00,7\n"  # duplicate A-3 row
    report = reconcile_csvs(left.encode(), RIGHT.encode(), join_key="sku")
    assert report.matched == 2  # A-1, A-2; A-3 is duplicated -> ambiguous
    assert report.clean is False
    changed_keys = [entry["key"] for entry in report.changed]
    assert changed_keys == ["A-2"]
    diff = report.changed[0]["differences"]
    assert diff == [{"column": "price", "left": "129.00", "right": "139.00"}]
    assert report.changed[0]["leftRef"] == {"source": "left", "rowNumber": 2, "line": 3}
    assert report.changed[0]["rightRef"] == {"source": "right", "rowNumber": 2, "line": 3}
    assert [entry["key"] for entry in report.missing_right] == ["A-3"]
    assert [entry["key"] for entry in report.missing_left] == ["B-9"]
    assert report.duplicate_keys and report.duplicate_keys[0]["key"] == "A-3"
    assert report.duplicate_keys[0]["source"] == "left"
    assert report.duplicate_keys[0]["occurrenceCount"] == 2
    assert len(report.duplicate_keys[0]["occurrences"]) == 2


def test_clean_reconcile_is_clean_and_deterministic() -> None:
    report = reconcile_csvs(LEFT.encode(), LEFT.encode(), join_key="sku")
    assert report.clean
    payload_a = report_bytes(report)
    report_b = reconcile_csvs(LEFT.encode(), LEFT.encode(), join_key="sku")
    assert report_bytes(report_b) == payload_a
    parsed = json.loads(payload_a)
    assert parsed["operation"] == "vouch-csv-reconcile/1"
    assert parsed["schemaVersion"] == "2"
    assert parsed["comparisonAdequacy"]["sufficient"] is True
    assert parsed["blankRecords"] == []
    assert parsed["emptyKeyRows"] == []


def test_changed_input_changes_result() -> None:
    a = reconcile_csvs(LEFT.encode(), RIGHT.encode(), join_key="sku")
    b = reconcile_csvs(LEFT.encode(), RIGHT.replace("139.00", "149.00").encode(), join_key="sku")
    assert a.changed[0]["differences"][0]["right"] == "139.00"
    assert b.changed[0]["differences"][0]["right"] == "149.00"


def test_join_key_and_shape_validation() -> None:
    with pytest.raises(ContractError, match="join key"):
        reconcile_csvs(LEFT.encode(), RIGHT.encode(), join_key="nope")
    with pytest.raises(ContractError, match="ragged"):
        reconcile_csvs(b"sku,name\nA-1,Kettle,extra\n", b"sku,name\n", join_key="sku")
    with pytest.raises(ContractError, match="empty"):
        reconcile_csvs(b"", b"sku\n", join_key="sku")
    with pytest.raises(ContractError, match="delimiter"):
        reconcile_csvs(LEFT.encode(), RIGHT.encode(), join_key="sku", delimiter="|")


def test_delimiters_and_ignore_columns() -> None:
    semi = b"sku;price\nA-1;9,50\n"
    report = reconcile_csvs(semi, semi, join_key="sku", delimiter=";")
    assert report.clean
    left_two = b"sku,name,price\nA-1,K,1\n"
    right_two = b"sku,note,price\nA-1,other,1\n"
    divergent = reconcile_csvs(left_two, right_two, join_key="sku")
    # 'name' vs 'note' are not shared columns, but 'price' is comparable:
    assert divergent.compared_columns == ("price",)
    assert divergent.clean  # the one shared comparable column agrees


def test_ignoring_the_only_comparable_column_is_never_clean() -> None:
    """Zero comparable columns = insufficient comparison, never 'clean'
    (M4 A3: 'we compared nothing' must not read as 'the files agree')."""
    ignored = reconcile_csvs(
        b"sku,price\nA-1,1\n", b"sku,price\nA-1,2\n", join_key="sku", ignore_columns=("price",)
    )
    assert ignored.compared_columns == ()
    assert ignored.comparison_sufficient is False
    assert ignored.clean is False
    assert "ignored" in ignored.comparison_reason


def test_disjoint_schemas_report_insufficient_comparison_not_clean() -> None:
    """The reproduced root: `sku,price` vs `sku,name` reported clean with
    zero comparable fields. Now the schema divergence and the insufficient
    comparison are stated explicitly."""
    report = reconcile_csvs(b"sku,price\nA-1,100\n", b"sku,name\nA-1,Kettle\n", join_key="sku")
    assert report.compared_columns == ()
    assert report.comparison_sufficient is False
    assert report.clean is False
    assert report.left_only_columns == ("price",)
    assert report.right_only_columns == ("name",)
    assert "not established" in report.comparison_reason
    parsed = json.loads(report_bytes(report))
    assert parsed["schemaDivergence"] == {
        "leftOnlyColumns": ["price"],
        "rightOnlyColumns": ["name"],
    }
    assert parsed["comparisonAdequacy"]["sufficient"] is False
    assert parsed["clean"] is False
