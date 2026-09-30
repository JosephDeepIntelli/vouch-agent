"""ScriptedBackend: a real JAZ backend, deterministic, never networked, fail-closed."""

from __future__ import annotations

import pytest
from jaz.exceptions import FatalError
from jaz.llm import BaseLLM, LLMResponse

from vouch_agent.errors import ReplayExhaustedError
from vouch_agent.runtime.scripted_backend import (
    BACKEND_TAG,
    DEFAULT_MODEL_ID,
    ScriptedBackend,
)


def _messages(text: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


def test_is_a_real_jaz_backend() -> None:
    assert issubclass(ScriptedBackend, BaseLLM)
    backend = ScriptedBackend(responses=["return 1"])
    assert backend.model == DEFAULT_MODEL_ID
    assert backend.max_retries == 0  # never re-drive an exhausted pool


def test_complete_returns_proper_llmresponse_shapes() -> None:
    backend = ScriptedBackend(responses=["return 41 + 1"], cost_usd=0.02)
    response = backend.complete(DEFAULT_MODEL_ID, _messages("x" * 40))
    assert isinstance(response, LLMResponse)
    assert response.content == "return 41 + 1"
    assert response.prompt_tokens == 10  # 40 chars / 4 per token — deterministic
    assert response.completion_tokens == 3
    assert response.cost_usd == 0.02
    assert backend.can_report_cost() is True  # BudgetPool with cost_budget is enforceable
    assert backend.responses_remaining == 0


def test_exhaustion_fails_closed_as_fatal_vouch_error() -> None:
    backend = ScriptedBackend(responses=[])
    with pytest.raises(ReplayExhaustedError) as excinfo:
        backend.complete(DEFAULT_MODEL_ID, _messages("t"))
    # Fatal-category: un-catchable inside the JAZ tree by model-authored code.
    assert isinstance(excinfo.value, FatalError)
    assert "fail closed" in str(excinfo.value)
    assert backend.exhausted_at == 0


def test_exhaustion_is_not_retried_by_the_base_retry_wrapper() -> None:
    backend = ScriptedBackend(responses=[])
    with pytest.raises(ReplayExhaustedError):
        backend.complete_with_retry(DEFAULT_MODEL_ID, _messages("t"))
    # A retry would have called complete again; exhausted_at stays at the
    # first (and only) attempt's call count.
    assert backend.exhausted_at == 0


def test_responses_consumed_in_order_across_calls() -> None:
    backend = ScriptedBackend(responses=["return 1", "return 2"])
    first = backend.complete(DEFAULT_MODEL_ID, _messages("a"))
    second = backend.complete(DEFAULT_MODEL_ID, _messages("b"))
    assert (first.content, second.content) == ("return 1", "return 2")
    assert backend.served_calls == 2


def test_wired_through_the_instantiate_seam_with_a_resolver() -> None:
    """The backend is selectable by tag like any JAZ backend, from data."""
    from jaz.instantiate import build_component

    component = build_component(
        "llm",
        {
            "backend": BACKEND_TAG,
            "params": {"responses": ["return 7"], "model": DEFAULT_MODEL_ID, "cost_usd": 0.03},
        },
        resolvers={"llm": lambda tag: ScriptedBackend if tag == BACKEND_TAG else None},
    )
    assert isinstance(component, ScriptedBackend)
    response = component.complete(DEFAULT_MODEL_ID, _messages("t"))
    assert response.content == "return 7"
    assert response.cost_usd == 0.03
