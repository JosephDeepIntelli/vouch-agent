"""Gate A3 regressions — parent-authoritative per-QUERY reservations in the
DEFAULT isolated path.

The reviewer proved step-granular reservation let two $0.01 queries dispatch
under a $0.015 cap. Through the real guarded worker (queryBudgetRpc), every
UNDERLYING query — including nested invokes — must reserve with the parent's
ledger BEFORE dispatch; the second query is refused, accounting retains the
first query's cost everywhere, and no path double-charges.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from m3_runtime_helpers import init_native_project
from test_supervisor_jaz import SCRIPT_DRAFT, SCRIPT_FINAL

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.workspace import ProjectWorkspace

_SCHEMA = {
    "type": "object",
    "required": ["recommendation", "confidence", "priceUsd"],
    "properties": {
        "recommendation": {"type": "string"},
        "confidence": {"type": "string"},
        "priceUsd": {"type": "number"},
    },
}

# Each scripted response triggers exactly one $0.01 model query.
_TWO_QUERIES = (SCRIPT_DRAFT, SCRIPT_FINAL)

# A REAL nested invoke (M4 A1 §1): the first response is model code that
# calls ``invoke(...)`` itself, so the step's SECOND underlying query is the
# nested invoke's own — exactly the path whose reserve RPC used to vanish
# into JAZ's stdout capture and stall to the wall clock. The nested invoke's
# REPL turn is the second response.
_NESTED_INVOKE_CODE = 'sub = invoke(task="Verify the primary candidate")\nreturn sub'
_NESTED_INNER_RETURN = (
    'nested = {"recommendation": "candidate A (verified)", '
    '"confidence": "high", "priceUsd": 12.5}\nreturn nested'
)
_NESTED = (_NESTED_INVOKE_CODE, _NESTED_INNER_RETURN)

# Nested error then retry: the nested invoke's first attempt raises, the
# agent iterates (one more underlying query), the third response returns.
_NESTED_ERROR_RETRY = (_NESTED_INVOKE_CODE, "boom = 1 // 0", _NESTED_INNER_RETURN)

# Two nested invokes inside ONE step (a "concurrent" fan-out from the outer
# agent's perspective): three underlying queries, each reserved separately.
_TWO_NESTED_CODE = (
    "a = invoke(task=\"Check source A\")\n"
    'b = invoke(task="Check source B")\n'
    'return {"recommendation": f"{a} and {b}", "confidence": "high", "priceUsd": 3.0}'
)
_TWO_NESTED = (_TWO_NESTED_CODE, 'return "source-A-ok"', 'return "source-B-ok"')


def _scripts_from(tmp: Path, scripts: tuple[str, ...]) -> tuple[Path, ...]:
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, text in enumerate(scripts):
        path = tmp / f"step{index}.py"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def _run(service: ExecutionService, tmp: Path, scripts: tuple[str, ...], cap: float):
    return service.run(
        goal="Produce a recommendation",
        inputs={"candidates": ["A", "B"]},
        budget_usd=cap,
        max_steps=4,
        script_files=_scripts_from(tmp, scripts),
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )


def test_second_query_refused_under_tight_cap_through_default_worker(tmp_path: Path) -> None:
    """Cap $0.015, two $0.01 queries: the SECOND is refused BEFORE dispatch —
    the run fails having spent exactly $0.01, everywhere."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        outcome = _run(service, tmp_path / "s", _TWO_QUERIES, cap=0.015)
        assert (
            outcome.status.value != "completed"
        ), "two queries must not complete under a $0.015 cap"
        result = outcome.result
        cost = result.total_cost_usd if result else None
        assert cost is None or cost <= 0.0101, f"overspent: {cost}"
        # the ledger settled exactly the ONE dispatched query, nothing outstanding
        assert workspace.ledger.settled_usd() <= 0.0101 + 1e-9
    finally:
        workspace.close()


def test_nested_invoke_also_reserves_per_query(tmp_path: Path) -> None:
    """A nested invoke inside one step is an underlying query too: the same
    tight cap refuses the second query (nested path cannot bypass).

    The step's own query settles ($0.01); the NESTED invoke's reservation is
    refused BEFORE it is dispatched, so the run fails having spent exactly
    one query — and it fails PROMPTLY (the reserve RPC actually arrives at
    the controller now; it is not swallowed by the stdout capture and left
    to the wall clock)."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        started = time.monotonic()
        outcome = _run(service, tmp_path / "n", _NESTED, cap=0.015)
        elapsed = time.monotonic() - started
        assert outcome.status.value != "completed", (
            "a nested invoke must not bypass the per-query budget"
        )
        assert workspace.ledger.settled_usd() <= 0.0101 + 1e-9
        # the refusal is a refusal, not a stall cut by the wall clock
        assert elapsed < 20.0, f"nested refusal took {elapsed:.1f}s (RPC stall?)"
        run = service.status(outcome.run_id)
        assert run is not None and run.error and "refused" in run.error.lower(), run.error
    finally:
        workspace.close()


def test_nested_invoke_completes_when_budget_covers_both_queries(tmp_path: Path) -> None:
    """Control: the same NESTED step under an adequate cap completes both
    queries promptly, with both queries' cost accounted and the per-step
    usage recording the true DELTA (2 underlying queries)."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        started = time.monotonic()
        outcome = _run(service, tmp_path / "nok", _NESTED, cap=0.5)
        elapsed = time.monotonic() - started
        assert outcome.status.value == "completed", outcome.error
        assert elapsed < 20.0, f"nested completion took {elapsed:.1f}s (RPC stall?)"
        result = outcome.result
        assert result is not None and result.total_cost_usd == pytest.approx(0.02)
        assert workspace.ledger.settled_usd() == pytest.approx(0.02)
        assert workspace.ledger.outstanding_usd() == pytest.approx(0.0)
        run = service.status(outcome.run_id)
        assert run is not None
        model_steps = [s for s in run.steps if s.kind.value == "model-call" and s.status == "ok"]
        assert len(model_steps) == 1  # one step, TWO underlying queries
        assert model_steps[0].usage.get("llmCalls") == 2
        assert model_steps[0].usage.get("queryCursor") == 2
        final = json.loads(workspace.artifacts.get(result.artifact_refs[-1]).decode("utf-8"))
        assert final["recommendation"] == "candidate A (verified)"  # the nested return
    finally:
        workspace.close()


def test_nested_invoke_error_then_retry_reserves_each_attempt(tmp_path: Path) -> None:
    """A nested invoke whose first attempt raises iterates: the retry is
    ANOTHER underlying query — three reserves/settles, all accounted."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        outcome = _run(service, tmp_path / "retry", _NESTED_ERROR_RETRY, cap=0.5)
        assert outcome.status.value == "completed", outcome.error
        result = outcome.result
        assert result is not None and result.total_cost_usd == pytest.approx(0.03)
        assert workspace.ledger.settled_usd() == pytest.approx(0.03)
        run = service.status(outcome.run_id)
        assert run is not None
        model_steps = [s for s in run.steps if s.kind.value == "model-call" and s.status == "ok"]
        assert model_steps[0].usage.get("llmCalls") == 3
        assert model_steps[0].usage.get("queryCursor") == 3
    finally:
        workspace.close()


def test_two_nested_invokes_in_one_step_each_reserve(tmp_path: Path) -> None:
    """Two nested invokes inside one step: each underlying query reserves
    separately (three total), and the same tight-two-query cap refuses the
    THIRD before dispatch."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        outcome = _run(service, tmp_path / "two", _TWO_NESTED, cap=0.5)
        assert outcome.status.value == "completed", outcome.error
        result = outcome.result
        assert result is not None and result.total_cost_usd == pytest.approx(0.03)
        run = service.status(outcome.run_id)
        assert run is not None
        model_steps = [s for s in run.steps if s.kind.value == "model-call" and s.status == "ok"]
        assert model_steps[0].usage.get("llmCalls") == 3

        # and under a cap that covers only two queries, the third is refused
        project2 = init_native_project(tmp_path / "p2")
        workspace2 = ProjectWorkspace.open(project2)
        try:
            outcome2 = _run(
                ExecutionService(workspace2), tmp_path / "two-tight", _TWO_NESTED, cap=0.025
            )
            assert outcome2.status.value != "completed"
            assert workspace2.ledger.settled_usd() <= 0.0201 + 1e-9
        finally:
            workspace2.close()
    finally:
        workspace.close()


def test_completes_when_budget_genuinely_covers_both_queries(tmp_path: Path) -> None:
    """Control: the same two-query task under an adequate cap completes with
    both queries' cost accounted — the refusal above is budget, not breakage."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    try:
        outcome = _run(service, tmp_path / "ok", _TWO_QUERIES, cap=0.5)
        assert outcome.status.value == "completed", outcome.error
        result = outcome.result
        assert result is not None and result.total_cost_usd == pytest.approx(0.02)
        assert workspace.ledger.settled_usd() == pytest.approx(0.02)
        assert workspace.ledger.outstanding_usd() == pytest.approx(0.0)
    finally:
        workspace.close()
