"""JAZ termination-protocol facts the runtime relies on (ADR point 6).

Verified against the pinned jaz-lang 0.2.0a4, on real ``jaz.invoke`` trees:

1. A hook that RAISES is swallowed by the dispatcher — raising is NOT a
   fail-closed mechanism. Termination must go through ``Abort(error=...)``.
2. An ``Abort`` carrying a FatalError-category error ends the whole tree and
   cannot be caught by model-authored ``except`` clauses.
3. A non-fatal error CAN be caught by model-authored code — the exact hazard
   offline integrity must not depend on (an agent "falling back to live").
4. ``BudgetPool`` exhaustion terminates via ``Abort`` at ``LLMQueryEnter``
   with ``BudgetPoolExhaustedError`` — the protocol the scripted-quota hook
   copies for replay exhaustion.
"""

from __future__ import annotations

from typing import Any

import jaz
import pytest
from jaz.exceptions import (
    BudgetPoolExhaustedError,
    FatalError,
)
from jaz.hooks import BudgetPool, Hook
from jaz.hooks.effects import Effect
from jaz.hooks.events import LLMQueryEnter
from jaz.llm import BaseLLM, LLMResponse
from jaz.repl import PythonREPL

from vouch_agent.runtime.fatal_errors import ReplayExhaustedFatal


class _Scripted(BaseLLM):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(model="vouch-scripted/proto", max_retries=0)
        self.responses = list(responses)

    def complete(self, model: str, messages: list[Any], **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content=self.responses.pop(0) if self.responses else "return None",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.01,
        )

    def can_report_cost(self) -> bool:
        return True


class _PlainExhausted(Exception):
    """Deliberately NOT fatal — the contrast case."""


class _PlainExhaustedBackend(_Scripted):
    def complete(self, model: str, messages: list[Any], **kwargs: Any) -> LLMResponse:
        if not self.responses:
            raise _PlainExhausted("exhausted")
        return super().complete(model, messages, **kwargs)

    @property
    def non_retryable_exceptions(self) -> tuple[type[BaseException], ...]:
        return (*super().non_retryable_exceptions, _PlainExhausted)


_CATCH_AND_FALLBACK_CODE = (
    "try:\n"
    "    sub = invoke(task='sub')\n"
    "    return ('no-exc', sub)\n"
    "except Exception as e:\n"
    "    return ('agent-caught', type(e).__name__)\n"
)


def _repl() -> PythonREPL:
    return PythonREPL(exec_timeout=10.0, allow_timeout_pragma=False)


def test_a_raising_hook_is_swallowed_not_fatal() -> None:
    class RaisingHook(Hook):
        def on_llm_query_enter(self, event: LLMQueryEnter) -> list[Effect]:
            raise RuntimeError("hook raised")

    backend = _Scripted(["return 'ran-anyway'"])
    with jaz.ConfigOverride(llm=backend, repl=_repl()), RaisingHook():
        result = jaz.invoke(task="t")
    assert result == "ran-anyway"  # the raise never reached anyone


def test_fatal_exhaustment_is_uncatchable_by_model_code() -> None:
    """Nested replay exhaustion propagates past agent try/except (fail closed)."""

    class ExhaustingBackend(_Scripted):
        def __init__(self) -> None:
            super().__init__([_CATCH_AND_FALLBACK_CODE])

        def complete(self, model: str, messages: list[Any], **kwargs: Any) -> LLMResponse:
            if not self.responses:
                raise ReplayExhaustedFatal("exhausted (fatal)")
            return super().complete(model, messages, **kwargs)

        @property
        def non_retryable_exceptions(self) -> tuple[type[BaseException], ...]:
            return (*super().non_retryable_exceptions, ReplayExhaustedFatal)

    backend = ExhaustingBackend()
    with jaz.ConfigOverride(llm=backend, repl=_repl()):
        with pytest.raises(ReplayExhaustedFatal) as excinfo:
            jaz.invoke(task="t")
    assert isinstance(excinfo.value, FatalError)


def test_plain_exhaustion_IS_catchable_by_model_code() -> None:
    """The contrast: without the fatal category the agent catches and 'recovers'.

    This is precisely the live-fallback hazard the fatal bridges exist to
    close: the agent code above explicitly tries to catch a failing nested
    call and continue with a substitute answer.
    """
    backend = _PlainExhaustedBackend([_CATCH_AND_FALLBACK_CODE])
    with jaz.ConfigOverride(llm=backend, repl=_repl()):
        result = jaz.invoke(task="t")
    assert result == ("agent-caught", "_PlainExhausted")


def test_abort_effect_at_query_enter_is_the_budget_termination_protocol() -> None:
    backend = _Scripted(["return 1"])
    with jaz.ConfigOverride(llm=backend, repl=_repl()), BudgetPool(cost_budget=0.005):
        # First (and only) query is admitted with $0 booked; it books $0.01.
        result = jaz.invoke(task="t")
    assert result == 1

    multi = _Scripted(["x = 1", "return 2"])
    with jaz.ConfigOverride(llm=multi, repl=_repl()), BudgetPool(cost_budget=0.005):
        # Turn 2's enter sees $0.01 >= $0.005 and aborts before its query.
        with pytest.raises(BudgetPoolExhaustedError):
            jaz.invoke(task="t")


def test_quota_hook_aborts_via_the_same_protocol() -> None:
    """The scripted-quota hook's Abort terminates a session mid-tree."""
    from vouch_agent.contracts.common import RunMode
    from vouch_agent.errors import ReplayExhaustedError
    from vouch_agent.runtime.jaz_engine import JazRuntime
    from vouch_agent.runtime.ports import WorkerSessionConfig

    session = JazRuntime().open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=4,
            wall_clock_s=30.0,
            scripted_responses=("return 1",),
        )
    )
    try:
        assert session.step("t").content == "1"
        with pytest.raises(ReplayExhaustedError, match="exhausted"):
            session.step("t2")
    finally:
        session.close()
