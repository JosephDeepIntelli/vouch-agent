"""Budget accounting: fake-cost correctness, in-step caps, and the documented
concurrency gap that forces a controller-side ledger.

Key demonstrated fact (ADR point 2): ``BudgetPool`` is an *accounting* hook —
it checks the aggregate at query ENTER and books cost at query EXIT. N
concurrent in-flight queries therefore all pass the check before any of them
books, so accounting-only budgets oversubscribe under concurrency. The Vouch
controller's ledger must RESERVE atomically before a call is scheduled; the
session-level cap here refuses *further* steps and bounds each step's spend,
but only reservation removes the race.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Any

import jaz
import pytest
from jaz.hooks import BudgetPool
from jaz.llm import BaseLLM, LLMResponse
from jaz.repl import PythonREPL

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import BudgetExhaustedError
from vouch_agent.runtime.jaz_engine import JazRuntime
from vouch_agent.runtime.ports import WorkerSessionConfig


class _SlowBackend(BaseLLM):
    """Every call costs the same and takes a moment, so TOCTOU windows open."""

    def __init__(self, cost_usd: float = 0.30, delay_s: float = 0.05) -> None:
        super().__init__(model="vouch-scripted/slow", max_retries=0)
        self.cost_usd = cost_usd
        self.delay_s = delay_s
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, model: str, messages: list[Any], **kwargs: Any) -> LLMResponse:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay_s)
        return LLMResponse(
            content="return 1", prompt_tokens=1, completion_tokens=1, cost_usd=self.cost_usd
        )

    def can_report_cost(self) -> bool:
        return True


def test_session_accounts_fake_costs_exactly() -> None:
    session = JazRuntime().open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=4,
            wall_clock_s=30.0,
            scripted_responses=("return 1", "return 2", "return 3"),
        )
    )
    try:
        first = session.step("t")
        second = session.step("t")
        assert first.cost_usd == 0.01
        assert second.cost_usd == 0.01
        usage = session.usage()
        assert usage["llm_calls"] == 2
        assert usage["cost_usd"] == 0.02  # never zero, never rounded away
    finally:
        session.close()


def test_session_refuses_to_continue_once_cost_cap_reached() -> None:
    session = JazRuntime().open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=8,
            wall_clock_s=30.0,
            max_cost_usd=0.012,  # one 0.01 call fits; a second does not
            scripted_responses=("return 1", "return 2"),
        )
    )
    try:
        assert session.step("t").cost_usd == 0.01
        with pytest.raises(BudgetExhaustedError, match="cost budget exhausted"):
            session.step("t2")
    finally:
        session.close()


def test_in_step_budget_pool_aborts_at_the_remaining_allowance() -> None:
    """A multi-turn step is cut by the per-step pool over the REMAINING budget.

    Scripted turns each book $0.01. With $0.025 remaining, turns 1-3 book to
    $0.03 and the fourth query is aborted at LLMQueryEnter by the pool (JAZ's
    own Abort protocol), which the session maps to BudgetExhaustedError.
    """
    session = JazRuntime().open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=8,
            wall_clock_s=30.0,
            max_cost_usd=0.025,
            scripted_responses=("x = 1", "x = 2", "x = 3", "return 99"),
        )
    )
    try:
        with pytest.raises(BudgetExhaustedError):
            session.step("many turns")
        usage = session.usage()
        assert usage["llm_calls"] == 3
        assert usage["scripted_remaining"] == 1  # the aborted query never ran
    finally:
        session.close()


def test_concurrent_calls_oversubscribe_an_accounting_only_pool() -> None:
    """THE documented gap: BudgetPool accounts returned cost, so concurrent
    in-flight calls all pass the enter-check before any books.

    Four concurrent invokes under one $0.50 pool with $0.30 calls: all four
    are admitted ($1.20 booked, $0.70 over). This is behaviour of the pinned
    upstream hook, demonstrated here as the reason the controller ledger must
    reserve atomically before scheduling (design §10) — a session-local pool
    cannot close this race by itself.
    """
    backend = _SlowBackend(cost_usd=0.30, delay_s=0.05)
    pool = BudgetPool(cost_budget=0.50)
    repl = PythonREPL(exec_timeout=10.0, allow_timeout_pragma=False)

    def run_one(i: int, errors: list[str]) -> None:
        try:
            with jaz.ConfigOverride(llm=backend, repl=repl):
                jaz.invoke(task="t")
        except Exception as exc:
            errors.append(f"{i}:{type(exc).__name__}")

    errors: list[str] = []
    with pool:
        # contextvars do not cross threading.Thread boundaries; the supported
        # carrier is a copied context taken while the pool is active.
        contexts = [contextvars.copy_context() for _ in range(4)]
        threads = [
            threading.Thread(target=ctx.run, args=(run_one, i, errors))
            for i, ctx in enumerate(contexts)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert backend.calls == 4, "all four concurrent calls were admitted"
    assert pool._total_cost == pytest.approx(1.20)
    assert not errors, "no caller saw a budget error — the race is silent"
