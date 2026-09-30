"""Structured returns through the real JAZ ReturnType hook, with fake responses."""

from __future__ import annotations

import pytest

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import ReplayExhaustedError
from vouch_agent.runtime.jaz_engine import JazRuntime
from vouch_agent.runtime.ports import WorkerSessionConfig


def _config(responses: tuple[str, ...]) -> WorkerSessionConfig:
    return WorkerSessionConfig(
        mode=RunMode.FIXTURE,
        max_steps=4,
        wall_clock_s=30.0,
        scripted_responses=responses,
    )


def test_structured_step_returns_the_declared_type() -> None:
    session = JazRuntime().open_session(_config(("return {'count': base, 'ok': True}",)))
    try:
        value = session.structured_step("summarize", dict, scope={"base": 3})
        assert value == {"count": 3, "ok": True}
        assert isinstance(value, dict)
    finally:
        session.close()


def test_structured_step_enforces_the_type_at_runtime() -> None:
    """A wrong-typed return is rejected by ReturnType, never passed through.

    The scripted model first returns a str where an int was declared. JAZ's
    ReturnType hook downgrades that Return to recoverable feedback; with no
    further scripted material the session then fails closed — an unvalidated
    value can never escape the session.
    """
    session = JazRuntime().open_session(_config(("return 'not-an-int'",)))
    try:
        with pytest.raises(ReplayExhaustedError):
            session.structured_step("count", int)
    finally:
        session.close()


def test_structured_step_recovers_after_type_feedback() -> None:
    session = JazRuntime().open_session(_config(("return 'oops'", "return 41 + 1")))
    try:
        value = session.structured_step("count", int)
        assert value == 42
        assert session.usage()["llm_calls"] == 2  # the rejected turn + the good one
    finally:
        session.close()


def test_plain_step_reports_metering_and_return_value() -> None:
    session = JazRuntime().open_session(_config(("return base * 2",)))
    try:
        result = session.step("double", scope={"base": 21})
        assert result.content == "42"
        assert result.raw["return_value"] == 42
        assert result.cost_usd == 0.01
        assert result.prompt_tokens > 0
        assert result.completion_tokens > 0
        assert "vouch-scripted" in result.model_id
    finally:
        session.close()
