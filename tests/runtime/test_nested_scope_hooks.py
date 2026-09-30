"""Nested invokes, scope propagation, and the with-hook vs positional-hook split.

The load-bearing claim (ADR point 1, verified against the pinned package):
hooks installed as context managers propagate to nested invokes; the SAME
hook passed positionally to ``jaz.invoke`` does NOT — it is bound to that one
invoke's dispatcher, so nested cost/turns are neither counted nor enforced.
Vouch installs every session hook the propagating way.
"""

from __future__ import annotations

import threading
from typing import Any

import jaz
import pytest
from jaz.hooks import BudgetPool, RecursionLimit
from jaz.llm import BaseLLM, LLMResponse
from jaz.repl import PythonREPL

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import BudgetExhaustedError
from vouch_agent.runtime.jaz_engine import JazRuntime
from vouch_agent.runtime.ports import WorkerSessionConfig


class _NestedBackend(BaseLLM):
    """Serves one scripted response per call; bookkeeping like the real one."""

    def __init__(self, responses: list[str], cost_usd: float = 0.01) -> None:
        super().__init__(model="vouch-scripted/nested", max_retries=0)
        self.responses = list(responses)
        self.cost_usd = cost_usd
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, model: str, messages: list[Any], **kwargs: Any) -> LLMResponse:
        with self._lock:
            self.calls += 1
            content = self.responses.pop(0)
        return LLMResponse(
            content=content, prompt_tokens=1, completion_tokens=1, cost_usd=self.cost_usd
        )

    def can_report_cost(self) -> bool:
        return True


def test_session_nested_invoke_with_scope_propagation() -> None:
    """A step may nest sub-invokes; ambient scope reaches them; hooks see all calls."""
    session = JazRuntime().open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=4,
            wall_clock_s=30.0,
            scripted_responses=(
                "sub = invoke(task='subtask')\nreturn sub * 10",  # depth 1 delegates
                "return base + 1",  # depth 2 uses ONLY the ambient scope var
            ),
        )
    )
    try:
        result = session.step("delegate", scope={"base": 4})
        assert result.raw["return_value"] == 50
        # The usage recorder was installed as a context manager, so the
        # nested invoke's query is metered in the SAME session aggregate.
        assert session.usage()["llm_calls"] == 2
    finally:
        session.close()


def test_positional_hook_does_not_reach_nested_invokes() -> None:
    """The upstream footgun, demonstrated on the pinned package.

    A BudgetPool passed positionally covers only the invoke's own calls; the
    nested invoke's call happens off the pool entirely. With-propagation sees
    both. This is why Vouch never installs session hooks positionally.
    """
    nested_code = "sub = invoke(task='sub')\nreturn sub"
    repl = PythonREPL(exec_timeout=10.0, allow_timeout_pragma=False)

    positional_backend = _NestedBackend([nested_code, "return 5"])
    pool = BudgetPool(cost_budget=10.0)
    with jaz.ConfigOverride(llm=positional_backend, repl=repl):
        jaz.invoke(pool, task="t")
    assert positional_backend.calls == 2  # both calls happened...
    assert pool._total_cost == 0.01  # ...but only ONE was accounted (under-report)

    with_backend = _NestedBackend([nested_code, "return 5"])
    with_pool = BudgetPool(cost_budget=10.0)
    with jaz.ConfigOverride(llm=with_backend, repl=repl), with_pool:
        jaz.invoke(task="t")
    assert with_backend.calls == 2
    assert with_pool._total_cost == 0.02  # context-manager install sees both


def test_recursion_cap_refuses_sub_invokes_beyond_the_depth() -> None:
    """RecursionLimit withholds/refuses nesting past its cap (with-channel only)."""
    repl = PythonREPL(exec_timeout=10.0, allow_timeout_pragma=False)

    def deep() -> object:
        # Indirect route: a host tool that invokes internally. The sub-invoke
        # lands at depth 2 and is refused with RecursionLimitError when the
        # cap is 1 — surfacing to the agent as recoverable feedback.
        return jaz.invoke(task="deeper")

    refused = _NestedBackend(["r = deep()", "return 'refused'"])
    with jaz.ConfigOverride(llm=refused, repl=repl), RecursionLimit(max_depth=1):
        result = jaz.invoke(task="t", deep=deep)
    assert result == "refused"

    allowed = _NestedBackend(["return deep()", "return 5"])
    with jaz.ConfigOverride(llm=allowed, repl=repl):
        result = jaz.invoke(task="t", deep=deep)
    assert result == 5


def test_session_maps_recursion_and_iteration_exhaustion_to_budget_errors() -> None:
    """jaz-native limit errors never leak through the session boundary."""
    session = JazRuntime().open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=1,  # one REPL turn only; the scripted code keeps looping
            wall_clock_s=30.0,
            scripted_responses=("x = 1", "x = 2", "return x"),
        )
    )
    try:
        with pytest.raises(BudgetExhaustedError):
            session.step("loop forever")
    finally:
        session.close()
