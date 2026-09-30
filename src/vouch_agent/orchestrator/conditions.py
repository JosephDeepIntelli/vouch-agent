"""Deterministic completion-condition checkers (design §4.4).

The trusted supervisor — never the model — decides whether a run completed:
a model asserting "done" is data, not a verdict. A ``TaskSpec`` declares its
``success_criteria`` as a structured condition list; this module validates
the list at submit time (fail closed on anything undecidable) and evaluates
it against the run's produced artifacts and cost accounting.

v1 condition types:

* ``{"type": "artifact_schema", "artifact": "final", "schema": {...}}`` —
  the named artifact must parse as JSON and match a JSON-schema subset
  (``type`` / ``required`` / ``properties``, recursive). Unsupported schema
  keywords are rejected at submit time, not silently ignored.
* ``{"type": "output_contains", "artifact": "final", "contains": "text"}`` —
  the named artifact's decoded text must contain ``contains``.
* ``{"type": "max_cost_usd", "maxUsd": 2.0}`` — total measured cost must not
  exceed the bound (``maxUsd`` overrides the TaskSpec's ``max_cost_usd``).
  An unmeasured cost fails the check honestly instead of guessing.
* ``{"type": "none"}`` — explicit human-acceptance marker: never machine
  checkable, so the check value is ``False`` and the run ends
  incomplete-but-delivered with the marker recorded in
  ``ResultPackage.not_done_items`` and a pending external action.

Artifact keys addressed by conditions: ``inputs`` (the materialized
TaskSpec inputs), ``step:<n>`` (the n-th model artifact, 1-based),
``draft`` (the first model artifact) and ``final`` (the latest).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from vouch_agent.contracts.common import digest_of
from vouch_agent.errors import ContractError

CONDITION_ARTIFACT_SCHEMA = "artifact_schema"
CONDITION_OUTPUT_CONTAINS = "output_contains"
CONDITION_MAX_COST_USD = "max_cost_usd"
CONDITION_NONE = "none"

KNOWN_CONDITION_TYPES = frozenset(
    {
        CONDITION_ARTIFACT_SCHEMA,
        CONDITION_OUTPUT_CONTAINS,
        CONDITION_MAX_COST_USD,
        CONDITION_NONE,
    }
)

#: JSON-schema subset keywords vouch understands. Anything else fails closed.
SUPPORTED_SCHEMA_KEYWORDS = frozenset({"type", "required", "properties"})

_JSON_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})

DEFAULT_ARTIFACT_KEY = "final"

#: Cost comparisons use this tolerance so float bookkeeping never "passes" a
#: bound by a rounding hair.
_COST_EPSILON = 1e-9


# --- JSON-schema subset ------------------------------------------------------


def validate_schema_subset(schema: Any, where: str = "schema") -> None:
    """Reject schemas this module cannot fully check (fail closed)."""
    if not isinstance(schema, dict):
        raise ContractError(f"{where} must be a JSON object, got {type(schema).__name__}")
    unknown = sorted(set(schema) - SUPPORTED_SCHEMA_KEYWORDS)
    if unknown:
        raise ContractError(
            f"{where} uses unsupported schema keywords {unknown}; "
            "v1 subset is type/required/properties"
        )
    type_name = schema.get("type")
    if type_name is not None and type_name not in _JSON_TYPES:
        raise ContractError(f"{where}.type must be one of {sorted(_JSON_TYPES)}")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(k, str) for k in required)
    ):
        raise ContractError(f"{where}.required must be a list of strings")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ContractError(f"{where}.properties must be a JSON object")
        for name, sub_schema in properties.items():
            validate_schema_subset(sub_schema, f"{where}.properties.{name}")


def _type_matches(value: Any, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    if type_name == "integer":
        # bool is an int subclass; JSON booleans are not JSON integers.
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise ContractError(f"unknown JSON type {type_name!r}")  # pragma: no cover - guarded above


def check_json_schema_subset(instance: Any, schema: dict[str, Any]) -> str | None:
    """Check ``instance`` against the schema subset; ``None`` means valid."""
    type_name = schema.get("type")
    if type_name is not None and not _type_matches(instance, type_name):
        return f"expected type {type_name}, got {type(instance).__name__}"
    if "required" in schema:
        if not isinstance(instance, dict):
            return "required fields apply to JSON objects"
        missing = sorted(k for k in schema["required"] if k not in instance)
        if missing:
            return f"missing required fields {missing}"
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(instance, dict):
            return "properties apply to JSON objects"
        for name, sub_schema in properties.items():
            if name not in instance:
                continue
            reason = check_json_schema_subset(instance[name], sub_schema)
            if reason is not None:
                return f"field {name!r}: {reason}"
    return None


# --- condition normalization --------------------------------------------------


def normalize_criteria(criteria: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Validate and normalize ``spec.success_criteria`` into condition dicts.

    Raises :class:`ContractError` for malformed or undecidable criteria — at
    submit time, before any budget or work is spent.
    """
    if not isinstance(criteria, dict):
        raise ContractError("success_criteria must be a JSON object")
    raw = criteria.get("conditions", [])
    if not isinstance(raw, list):
        raise ContractError("success_criteria.conditions must be a list")
    normalized: list[dict[str, Any]] = []
    for index, condition in enumerate(raw):
        where = f"success_criteria.conditions[{index}]"
        if not isinstance(condition, dict):
            raise ContractError(f"{where} must be a JSON object")
        condition_type = condition.get("type")
        if condition_type not in KNOWN_CONDITION_TYPES:
            raise ContractError(
                f"{where} has unknown condition type {condition_type!r}; "
                f"known types: {sorted(KNOWN_CONDITION_TYPES)}"
            )
        if condition_type == CONDITION_ARTIFACT_SCHEMA:
            schema = condition.get("schema")
            validate_schema_subset(schema, f"{where}.schema")
            normalized.append(
                {
                    "type": condition_type,
                    "artifact": _artifact_key(condition, where),
                    "schema": schema,
                }
            )
        elif condition_type == CONDITION_OUTPUT_CONTAINS:
            contains = condition.get("contains")
            if not isinstance(contains, str) or not contains:
                raise ContractError(f"{where}.contains must be a non-empty string")
            normalized.append(
                {
                    "type": condition_type,
                    "artifact": _artifact_key(condition, where),
                    "contains": contains,
                }
            )
        elif condition_type == CONDITION_MAX_COST_USD:
            max_usd = condition.get("maxUsd")
            if max_usd is not None:
                if not isinstance(max_usd, (int, float)) or isinstance(max_usd, bool):
                    raise ContractError(f"{where}.maxUsd must be a number")
                if max_usd < 0:
                    raise ContractError(f"{where}.maxUsd must be >= 0")
            normalized.append({"type": condition_type, "maxUsd": max_usd})
        else:  # CONDITION_NONE — no parameters
            normalized.append({"type": condition_type})
    return tuple(normalized)


def _artifact_key(condition: dict[str, Any], where: str) -> str:
    key = condition.get("artifact", DEFAULT_ARTIFACT_KEY)
    if not isinstance(key, str) or not key:
        raise ContractError(f"{where}.artifact must be a non-empty string")
    return key


def is_human_marker(condition: Mapping[str, Any]) -> bool:
    return condition.get("type") == CONDITION_NONE


# --- evaluation ---------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one condition, with the reason trusted code computed."""

    key: str
    condition_type: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ConditionEvaluation:
    results: tuple[CheckResult, ...]

    @property
    def checks(self) -> dict[str, bool]:
        return {r.key: r.passed for r in self.results}

    def all_passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    def machine_checks_passed(self) -> bool:
        """All non-human-marker conditions passed (vacuously true if none)."""
        machine = [r for r in self.results if r.condition_type != CONDITION_NONE]
        return all(r.passed for r in machine)

    def human_markers(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.condition_type == CONDITION_NONE)


@dataclass(frozen=True)
class ConditionContext:
    """Trusted inputs the checkers run against — artifacts and cost facts."""

    artifacts: Mapping[str, str]
    load_artifact: Callable[[str], bytes]
    measured_cost_usd: float
    has_unmeasured_cost: bool
    cost_bound_usd: float | None


def evaluate_conditions(
    conditions: Sequence[dict[str, Any]], context: ConditionContext
) -> ConditionEvaluation:
    results: list[CheckResult] = []
    for index, condition in enumerate(conditions):
        condition_type = condition["type"]
        key = f"{condition_type}[{index}]"
        if condition_type == CONDITION_NONE:
            results.append(
                CheckResult(
                    key=key,
                    condition_type=condition_type,
                    passed=False,
                    detail="explicit human-acceptance marker: not machine-checkable",
                )
            )
            continue
        if condition_type in (CONDITION_ARTIFACT_SCHEMA, CONDITION_OUTPUT_CONTAINS):
            results.append(_check_artifact_condition(key, condition_type, condition, context))
            continue
        if condition_type == CONDITION_MAX_COST_USD:
            results.append(_check_max_cost(key, condition, context))
    return ConditionEvaluation(tuple(results))


def _resolve_artifact(key: str, context: ConditionContext) -> tuple[bytes | None, str | None]:
    """Load the artifact bytes for ``key``; the second element is the failure
    detail when the artifact is unavailable (a failed check, not a crash)."""
    digest = context.artifacts.get(key)
    if digest is None:
        return None, f"artifact {key!r} was not produced by the run"
    try:
        payload = context.load_artifact(digest)
    except Exception as exc:  # missing/corrupt artifact content is a failed check
        return None, f"artifact {key!r} could not be loaded: {exc}"
    return payload, None


def _check_artifact_condition(
    key: str,
    condition_type: str,
    condition: Mapping[str, Any],
    context: ConditionContext,
) -> CheckResult:
    artifact_key = condition["artifact"]
    payload, problem = _resolve_artifact(artifact_key, context)
    if payload is None:
        return CheckResult(key, condition_type, False, problem or "artifact unavailable")
    text = payload.decode("utf-8", errors="replace")
    if condition_type == CONDITION_OUTPUT_CONTAINS:
        contains = condition["contains"]
        passed = contains in text
        return CheckResult(
            key,
            condition_type,
            passed,
            f"artifact {artifact_key!r} "
            + ("contains" if passed else "does not contain")
            + f" {contains!r}",
        )
    try:
        instance = json.loads(text)
    except ValueError as exc:
        return CheckResult(
            key,
            condition_type,
            False,
            f"artifact {artifact_key!r} is not valid JSON: {exc}",
        )
    reason = check_json_schema_subset(instance, condition["schema"])
    digest = digest_of(condition["schema"])
    passed = reason is None
    return CheckResult(
        key,
        condition_type,
        passed,
        f"artifact {artifact_key!r} against schema {digest}: "
        + ("matched" if passed else f"mismatch — {reason}"),
    )


def _check_max_cost(
    key: str, condition: Mapping[str, Any], context: ConditionContext
) -> CheckResult:
    bound = condition.get("maxUsd")
    if bound is None:
        bound = context.cost_bound_usd
    if bound is None:
        return CheckResult(
            key, CONDITION_MAX_COST_USD, False, "no cost bound declared to check against"
        )
    if context.has_unmeasured_cost:
        return CheckResult(
            key,
            CONDITION_MAX_COST_USD,
            False,
            "total cost is unmeasured (at least one unmeasurable model call); "
            "cannot verify the bound",
        )
    spent = context.measured_cost_usd
    passed = spent <= float(bound) + _COST_EPSILON
    return CheckResult(
        key,
        CONDITION_MAX_COST_USD,
        passed,
        f"measured ${spent:.6f} vs bound ${float(bound):.6f}",
    )
