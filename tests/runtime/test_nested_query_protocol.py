"""Nested-query budget RPC transport (M4 A1 §1) — protocol-level regressions.

The M3 review traced the nested-invoke stall to the child's ``_emit`` writing
protocol frames to the DYNAMIC ``sys.stdout`` while JAZ's REPL capture routed
them into the current exec buffer. Frames now travel on a dedicated protocol
descriptor (a dup of the real stdout taken at module import, before JAZ can
install its capture). These tests drive the REAL default guarded worker and
assert the parent receives and resolves every reservation exactly once,
model output never becomes a protocol message, and refusals arrive before
dispatch instead of stalling to the wall clock.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import BudgetError
from vouch_agent.runtime.guards import GuardedWorkerChannel, WorkerStepRequest
from vouch_agent.runtime.ports import WorkerSessionConfig

#: Short INNER deadlines with a strict outer timeout, per the M4 acceptance.
_WALL_CLOCK_S = 3.0
_STALL_BOUND_S = 2.5  # refusal/completion bound well under the wall clock

NESTED_INVOKE_CODE = 'sub = invoke(task="Inner synthetic sub-task")\nreturn sub'
INNER_RETURN = 'inner = {"value": "inner"}\nreturn inner'


class RecordingBudget:
    """Parent-owned budget: records every RPC; refuses past a synthetic cap."""

    def __init__(self, cap: float = 1.0) -> None:
        self.cap = cap
        self.spent = 0.0
        self.reserves: list[float | None] = []
        self.settles: list[tuple[str, float | None]] = []
        self.releases: list[str] = []

    def reserve_query(self, estimate_usd: float | None) -> str:
        self.reserves.append(estimate_usd)
        amount = float(estimate_usd or 0.0)
        if self.spent + amount > self.cap + 1e-9:
            raise BudgetError("synthetic cap refuses this query")
        self.spent += amount
        return f"rsv-{len(self.reserves)}"

    def settle_query(self, reservation_id: str, actual_usd: float | None) -> None:
        self.settles.append((reservation_id, actual_usd))

    def release_query(self, reservation_id: str) -> None:
        self.releases.append(reservation_id)


def _channel(budget: RecordingBudget, responses: tuple[str, ...]) -> GuardedWorkerChannel:
    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE,
        max_steps=4,
        wall_clock_s=_WALL_CLOCK_S,
        scripted_responses=responses,
    )
    return GuardedWorkerChannel(config, budget=budget, stream=True)


def _run_step(budget: RecordingBudget, responses: tuple[str, ...]) -> dict[str, Any]:
    channel = _channel(budget, responses)
    try:
        index = channel.send_step(WorkerStepRequest(instruction="Return a result"))
        return channel.read_step(index)
    finally:
        channel.close()


def test_nested_invoke_reserves_and_settles_each_query_once() -> None:
    """The nested invoke's reserve RPC reaches the parent over the dedicated
    protocol channel: TWO reserves, each settled exactly once, prompt."""
    budget = RecordingBudget(cap=1.0)
    started = time.monotonic()
    frame = _run_step(budget, (NESTED_INVOKE_CODE, INNER_RETURN))
    elapsed = time.monotonic() - started
    assert frame["ok"] is True, frame
    assert frame["value"] == {"value": "inner"}
    assert len(budget.reserves) == 2
    assert len(budget.settles) == 2
    assert {rid for rid, _ in budget.settles} == {"rsv-1", "rsv-2"}
    assert not budget.releases
    assert elapsed < _STALL_BOUND_S, f"nested RPC took {elapsed:.2f}s (capture stall?)"


def test_tight_cap_refuses_nested_query_before_dispatch() -> None:
    """A cap covering one query refuses the NESTED reservation before the
    query runs: one settle only, prompt refusal, worker fails closed."""
    from vouch_agent.runtime.fatal_errors import QueryBudgetRefusedFatal

    budget = RecordingBudget(cap=0.015)
    started = time.monotonic()
    with pytest.raises(QueryBudgetRefusedFatal, match="refused"):
        _run_step(budget, (NESTED_INVOKE_CODE, INNER_RETURN))
    elapsed = time.monotonic() - started
    assert len(budget.reserves) == 2  # the nested query DID ask...
    assert len(budget.settles) == 1  # ...but only the first query ran
    assert not budget.releases
    assert elapsed < _STALL_BOUND_S, f"nested refusal took {elapsed:.2f}s (capture stall?)"


def test_nested_error_retry_reserves_every_attempt() -> None:
    """A failed nested attempt that the agent retries is a NEW underlying
    query with its own reservation (three total)."""
    budget = RecordingBudget(cap=1.0)
    frame = _run_step(budget, (NESTED_INVOKE_CODE, "boom = 1 // 0", INNER_RETURN))
    assert frame["ok"] is True, frame
    assert len(budget.reserves) == 3
    assert len(budget.settles) == 3


def test_two_nested_invokes_in_one_step_reserve_separately() -> None:
    """Two nested invokes inside one outer step: three distinct reservations,
    each received and resolved once by the parent."""
    budget = RecordingBudget(cap=1.0)
    code = (
        "a = invoke(task=\"Inner A\")\n"
        "b = invoke(task=\"Inner B\")\n"
        'return {"a": a, "b": b}'
    )
    frame = _run_step(budget, (code, 'return "A"', 'return "B"'))
    assert frame["ok"] is True, frame
    assert len(budget.reserves) == 3
    assert len(budget.settles) == 3
    assert {rid for rid, _ in budget.settles} == {"rsv-1", "rsv-2", "rsv-3"}


def test_generated_print_output_is_not_a_protocol_message() -> None:
    """Model-generated ``print`` output rides the captured stdout (generated
    code channel) and must neither corrupt the protocol nor kill the step:
    the frame stream stays valid JSON protocol frames only.

    JAZ semantics: a printing turn is not final, so the agent takes another
    turn (a second underlying query) — both reservations travel the protocol
    channel; the printed text never appears as a frame."""
    budget = RecordingBudget(cap=1.0)
    code = 'print("generated noise that is NOT protocol")\nreturn {"printed": True}'
    frame = _run_step(budget, (code, 'return {"second": True}'))
    assert frame["ok"] is True, frame
    assert frame["value"] == {"second": True}
    assert len(budget.reserves) == 2
    assert len(budget.settles) == 2


def test_nested_invoke_with_print_inside_still_reserves() -> None:
    """The nastiest mix: generated print output in the SAME turn that runs
    the nested invoke, while the capture buffer is active — the nested
    reserve must still reach the parent on the dedicated descriptor."""
    budget = RecordingBudget(cap=1.0)
    code = 'print("outer generated noise")\nsub = invoke(task="deeper")\nreturn sub'
    frame = _run_step(
        budget, (code, 'return {"from": "deeper"}', 'return {"wrapped": True}')
    )
    assert frame["ok"] is True, frame
    assert frame["value"] == {"wrapped": True}
    # outer query + the NESTED query (reserved while the capture buffer held
    # the printed output) + the post-print continuation turn
    assert len(budget.reserves) == 3
    assert len(budget.settles) == 3


def test_budget_rpc_timeout_fails_closed() -> None:
    """A parent that never answers the reserve RPC cannot hang the session
    past the wall clock: the child aborts with a budget protocol error."""

    class Silent:
        def reserve_query(self, estimate_usd: float | None) -> str:
            time.sleep(_WALL_CLOCK_S + 10.0)
            raise AssertionError("never reached")  # pragma: no cover

        def settle_query(self, reservation_id: str, actual_usd: float | None) -> None: ...

        def release_query(self, reservation_id: str) -> None: ...

    started = time.monotonic()
    with pytest.raises(Exception) as excinfo:
        _run_step(Silent(), (NESTED_INVOKE_CODE, INNER_RETURN))  # type: ignore[arg-type]
    elapsed = time.monotonic() - started
    assert elapsed < _WALL_CLOCK_S + 3.0, "the wall clock must bound a dead RPC channel"
    assert excinfo.value
