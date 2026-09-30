"""Unit tests for the deterministic condition checkers (JSON-schema subset,
output_contains, max_cost_usd, none) and their fail-closed normalization.
"""

from __future__ import annotations

import pytest
from fakes import _RECOMMENDATION_SCHEMA

from vouch_agent.errors import ContractError
from vouch_agent.orchestrator.conditions import (
    CONDITION_MAX_COST_USD,
    CONDITION_NONE,
    CONDITION_OUTPUT_CONTAINS,
    ConditionContext,
    check_json_schema_subset,
    evaluate_conditions,
    normalize_criteria,
    validate_schema_subset,
)


def ctx(
    artifacts: dict[str, str] | None = None,
    blobs: dict[str, bytes] | None = None,
    measured: float = 0.0,
    unmeasured: bool = False,
    bound: float | None = None,
) -> ConditionContext:
    artifacts = artifacts or {}
    blobs = blobs or {}
    return ConditionContext(
        artifacts=artifacts,
        load_artifact=lambda digest: blobs[digest],
        measured_cost_usd=measured,
        has_unmeasured_cost=unmeasured,
        cost_bound_usd=bound,
    )


# --- schema subset ---------------------------------------------------------------


def test_schema_subset_accepts_valid_object() -> None:
    instance = {"recommendation": "A", "confidence": "high", "priceUsd": 12.5}
    assert check_json_schema_subset(instance, _RECOMMENDATION_SCHEMA) is None


def test_schema_subset_rejects_missing_required() -> None:
    reason = check_json_schema_subset({"recommendation": "A"}, _RECOMMENDATION_SCHEMA)
    assert reason is not None and "confidence" in reason


def test_schema_subset_rejects_wrong_types() -> None:
    bad = {"recommendation": "A", "confidence": "high", "priceUsd": "12.5"}
    reason = check_json_schema_subset(bad, _RECOMMENDATION_SCHEMA)
    assert reason is not None and "number" in reason
    # bool is not a JSON integer/number
    assert check_json_schema_subset(True, {"type": "integer"}) is not None
    assert check_json_schema_subset(True, {"type": "number"}) is not None
    assert check_json_schema_subset(7, {"type": "integer"}) is None


def test_schema_subset_recursive_properties() -> None:
    schema = {
        "type": "object",
        "required": ["result"],
        "properties": {
            "result": {
                "type": "object",
                "required": ["items"],
                "properties": {"items": {"type": "array"}},
            }
        },
    }
    assert check_json_schema_subset({"result": {"items": [1, 2]}}, schema) is None
    reason = check_json_schema_subset({"result": {"items": "nope"}}, schema)
    assert reason is not None and "array" in reason
    reason = check_json_schema_subset({"result": {}}, schema)
    assert reason is not None and "items" in reason


def test_schema_subset_all_json_types() -> None:
    for value, type_name in [
        ({}, "object"),
        ([], "array"),
        ("x", "string"),
        (1.5, "number"),
        (2, "number"),
        (3, "integer"),
        (True, "boolean"),
        (None, "null"),
    ]:
        assert check_json_schema_subset(value, {"type": type_name}) is None


def test_schema_subset_rejects_unsupported_keywords() -> None:
    with pytest.raises(ContractError, match="unsupported schema keywords"):
        validate_schema_subset({"type": "string", "pattern": "^a"})
    with pytest.raises(ContractError, match="unsupported schema keywords"):
        validate_schema_subset({"enum": [1, 2]})
    with pytest.raises(ContractError):
        validate_schema_subset("not an object")
    with pytest.raises(ContractError):
        validate_schema_subset({"type": "tuple"})


# --- normalization fails closed -----------------------------------------------------


def test_normalize_rejects_unknown_condition_type() -> None:
    with pytest.raises(ContractError, match="unknown condition type"):
        normalize_criteria({"conditions": [{"type": "model_says_ok"}]})


def test_normalize_rejects_malformed_conditions() -> None:
    with pytest.raises(ContractError):
        normalize_criteria({"conditions": [{"type": "output_contains"}]})
    with pytest.raises(ContractError):
        normalize_criteria({"conditions": [{"type": "artifact_schema"}]})
    with pytest.raises(ContractError):
        normalize_criteria({"conditions": "all good"})
    with pytest.raises(ContractError):
        normalize_criteria({"conditions": [{"type": "max_cost_usd", "maxUsd": -1}]})


def test_normalize_defaults_and_order() -> None:
    conditions = normalize_criteria(
        {"conditions": [{"type": "artifact_schema", "schema": {"type": "object"}}]}
    )
    assert conditions[0]["artifact"] == "final"  # default artifact key
    assert normalize_criteria({}) == ()
    assert normalize_criteria({"conditions": []}) == ()


# --- evaluation ----------------------------------------------------------------------


def test_output_contains_pass_and_fail() -> None:
    blobs = {"sha256:a": b"hello world", "sha256:b": b"goodbye"}
    conditions = normalize_criteria(
        {"conditions": [{"type": "output_contains", "artifact": "final", "contains": "hello"}]}
    )
    ok = evaluate_conditions(conditions, ctx({"final": "sha256:a"}, blobs))
    assert ok.all_passed()
    bad = evaluate_conditions(conditions, ctx({"final": "sha256:b"}, blobs))
    assert not bad.all_passed() and "does not contain" in bad.results[0].detail


def test_missing_artifact_key_fails_with_detail() -> None:
    conditions = normalize_criteria({"conditions": [{"type": "output_contains", "contains": "x"}]})
    result = evaluate_conditions(conditions, ctx({}, {})).results[0]
    assert result.passed is False
    assert "was not produced" in result.detail


def test_artifact_schema_on_non_json_fails() -> None:
    blobs = {"sha256:a": b"not json"}
    conditions = normalize_criteria(
        {"conditions": [{"type": "artifact_schema", "schema": {"type": "object"}}]}
    )
    result = evaluate_conditions(conditions, ctx({"final": "sha256:a"}, blobs)).results[0]
    assert result.passed is False and "not valid JSON" in result.detail


def test_max_cost_condition() -> None:
    conditions = normalize_criteria({"conditions": [{"type": "max_cost_usd"}]})
    ok = evaluate_conditions(conditions, ctx(measured=0.5, bound=0.5))
    assert ok.all_passed()
    over = evaluate_conditions(conditions, ctx(measured=0.5000001, bound=0.5))
    assert not over.all_passed()
    no_bound = evaluate_conditions(conditions, ctx(measured=0.1, bound=None))
    assert not no_bound.all_passed()
    assert "no cost bound" in no_bound.results[0].detail


def test_max_cost_condition_with_unmeasured_cost_fails() -> None:
    conditions = normalize_criteria({"conditions": [{"type": "max_cost_usd"}]})
    result = evaluate_conditions(conditions, ctx(measured=0.1, unmeasured=True, bound=1.0)).results[
        0
    ]
    assert result.passed is False and "unmeasured" in result.detail


def test_max_cost_condition_override_max_usd() -> None:
    conditions = normalize_criteria({"conditions": [{"type": "max_cost_usd", "maxUsd": 0.2}]})
    result = evaluate_conditions(conditions, ctx(measured=0.15, bound=10.0)).results[0]
    assert result.passed is True
    result = evaluate_conditions(conditions, ctx(measured=0.25, bound=10.0)).results[0]
    assert result.passed is False


def test_none_condition_is_always_false_and_a_human_marker() -> None:
    conditions = normalize_criteria({"conditions": [{"type": "none"}]})
    evaluation = evaluate_conditions(conditions, ctx())
    assert evaluation.all_passed() is False
    assert evaluation.machine_checks_passed() is True  # vacuous machine checks
    assert len(evaluation.human_markers()) == 1
    assert evaluation.checks[f"{CONDITION_NONE}[0]"] is False


def test_machine_checks_passed_mixes_conditions() -> None:
    blobs = {"sha256:a": b'{"ok": 1}'}
    conditions = normalize_criteria(
        {
            "conditions": [
                {"type": "artifact_schema", "schema": {"type": "object"}},
                {"type": "none"},
            ]
        }
    )
    evaluation = evaluate_conditions(conditions, ctx({"final": "sha256:a"}, blobs))
    assert evaluation.machine_checks_passed() is True
    assert evaluation.all_passed() is False
    assert set(evaluation.checks) == {f"{CONDITION_NONE}[1]", "artifact_schema[0]"}


def test_condition_keys_are_stable_by_index() -> None:
    conditions = normalize_criteria(
        {
            "conditions": [
                {"type": "output_contains", "contains": "a"},
                {"type": "output_contains", "contains": "b"},
                {"type": "max_cost_usd"},
            ]
        }
    )
    evaluation = evaluate_conditions(conditions, ctx(bound=1.0))
    assert set(evaluation.checks) == {
        f"{CONDITION_OUTPUT_CONTAINS}[0]",
        f"{CONDITION_OUTPUT_CONTAINS}[1]",
        f"{CONDITION_MAX_COST_USD}[2]",
    }
