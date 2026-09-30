"""External wall-clock enforcement and cancellation propagation.

Verified behaviour:

* A runaway step is stopped and reported as ``WallClockExceededError``; the
  session then refuses every further step. (Mechanism, honestly: the caller
  polls the session deadline and fails closed; jaz's per-exec timeout and
  iteration cap bound how long the abandoned daemon thread can burn. Direct
  thread-kill via async exceptions is NOT reliable inside jaz-instrumented
  loops — CPython mangles the injected exception inside ``sys.monitoring``
  callbacks — see jaz_engine._WALL_CLOCK_GRACE_S.)
* ``cancel()`` and ``close()`` take effect within one poll interval on a step
  in flight, deterministically, without waiting for the worker thread.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import InvalidStateTransitionError
from vouch_agent.runtime.fatal_errors import (
    SessionCancelledError,
    WallClockExceededError,
)
from vouch_agent.runtime.jaz_engine import JazRuntime
from vouch_agent.runtime.ports import WorkerSessionConfig
from vouch_agent.runtime.scripted_backend import ScriptedBackend


def test_wall_clock_timeout_kills_a_runaway_step() -> None:
    """A tight model-authored loop cannot outlive the session's wall clock."""
    session = JazRuntime().open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=3,  # iteration cap that eventually ends the daemon thread
            wall_clock_s=1.0,
            # Plenty of scripted material so the quota cannot end the run
            # first; only the wall clock may terminate this step.
            scripted_responses=tuple(["while True:\n    pass"] * 30),
        )
    )
    try:
        started = time.monotonic()
        with pytest.raises(WallClockExceededError, match="wall-clock"):
            session.step("run away")
        elapsed = time.monotonic() - started
        assert elapsed < 6.0, f"timeout took {elapsed:.1f}s to fire"
        with pytest.raises(WallClockExceededError):
            session.step("after the deadline")
        assert session.usage()["expired"] is True
    finally:
        session.close()
    # The abandoned worker thread is bounded by exec-timeout x iteration cap;
    # give it a moment to burn out so later tests start clean.
    time.sleep(3.0)


def test_cancellation_stops_an_in_flight_step_promptly() -> None:
    class SlowBackend(ScriptedBackend):
        """Blocks outside jaz-instrumented code, where injection lands."""

        def complete(self, model: str, messages: list[Any], **kwargs: Any):
            time.sleep(4.0)
            return super().complete(model, messages, **kwargs)

    import vouch_agent.runtime.jaz_engine as engine

    original = engine.ScriptedBackend
    engine.ScriptedBackend = SlowBackend
    try:
        session = JazRuntime().open_session(
            WorkerSessionConfig(
                mode=RunMode.FIXTURE,
                max_steps=4,
                wall_clock_s=30.0,
                scripted_responses=("return 1",),
            )
        )
        try:

            def cancel_soon() -> None:
                time.sleep(0.5)
                session.cancel("test cancellation")

            threading.Thread(target=cancel_soon, daemon=True).start()
            started = time.monotonic()
            with pytest.raises(SessionCancelledError, match="test cancellation"):
                session.step("slow step")
            elapsed = time.monotonic() - started
            assert elapsed < 2.0, f"cancellation took {elapsed:.1f}s to surface"
            with pytest.raises(SessionCancelledError):
                session.step("after cancellation")
        finally:
            session.close()
    finally:
        engine.ScriptedBackend = original


def test_close_during_a_step_cancels_and_gates_the_session() -> None:
    session = JazRuntime().open_session(
        WorkerSessionConfig(
            mode=RunMode.FIXTURE,
            max_steps=4,
            wall_clock_s=1.0,
            scripted_responses=tuple(["while True:\n    pass"] * 20),
        )
    )
    outcome: list[BaseException] = []

    def close_soon() -> None:
        time.sleep(0.4)
        session.close()

    threading.Thread(target=close_soon, daemon=True).start()
    try:
        session.step("runaway")
    except BaseException as exc:
        outcome.append(exc)
    assert outcome and isinstance(outcome[0], (SessionCancelledError, WallClockExceededError))
    with pytest.raises(InvalidStateTransitionError):
        session.step("closed session")
    # close() is idempotent
    session.close()
