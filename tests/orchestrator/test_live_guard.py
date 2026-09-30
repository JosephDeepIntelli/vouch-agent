"""Offline integrity: a fixture run whose runtime raises LiveCallBlockedError
fails the step and the run cleanly (never a crash, never
needs-reconciliation — the outcome is KNOWN), and replay exhaustion is a
terminal failure with no live fallback.
"""

from __future__ import annotations

import pytest
from fakes import ScriptedCall, make_harness, schema_spec

from vouch_agent.contracts.tasks import StepKind, TaskStatus
from vouch_agent.errors import LiveCallBlockedError, ReplayExhaustedError


def test_live_call_blocked_step_fails_run_without_crash() -> None:
    h = make_harness(
        [
            ScriptedCall(content='{"recommendation": "draft"}', cost_usd=0.01),
            ScriptedCall(
                content="would have gone live",
                cost_usd=0.01,
                error=LiveCallBlockedError("live calls are blocked in fixture mode"),
            ),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    run = h.supervisor.execute(run_id)

    # a known protocol failure is NOT an unknown-outcome crash
    assert run.status is TaskStatus.FAILED
    assert run.status is not TaskStatus.NEEDS_RECONCILIATION
    assert not any(s.side_effect_unknown for s in run.steps)

    failed = [s for s in run.steps if s.status == "failed"]
    assert len(failed) == 1
    assert failed[0].kind is StepKind.MODEL_CALL
    assert "vouch/live-call-blocked" in (failed[0].error or "")

    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable() is False
    assert any("live-call-blocked" in i for i in package.not_done_items)
    # no cost booked for a call that never reached a model
    entries = h.journal.cost_entries(run_id)
    assert [e.amount_usd for e in entries] == [0.01]


def test_replay_exhausted_is_terminal_failure() -> None:
    h = make_harness([ScriptedCall(content='{"recommendation": "draft"}', cost_usd=0.01)])
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0, max_steps=6))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.FAILED
    assert "replay-exhausted" in (run.error or "")
    # the fake runtime has NO live path at all — exhaustion cannot "fall back"
    failed = [s for s in run.steps if s.status == "failed"]
    assert len(failed) == 1
    assert ReplayExhaustedError.__name__ not in (failed[0].error or "")  # code, not type
    assert "vouch/replay-exhausted" in (failed[0].error or "")
    package = h.supervisor.get_result(run_id)
    assert package is not None and package.deliverable() is False


def test_blocked_call_releases_nothing_to_completed_state() -> None:
    # Cross-check the state machine: a blocked fixture run stays terminal and
    # execute refuses to touch it again.
    from vouch_agent.errors import InvalidStateTransitionError

    h = make_harness([ScriptedCall(content="x", error=LiveCallBlockedError("blocked"))])
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    h.supervisor.execute(run_id)
    with pytest.raises(InvalidStateTransitionError):
        h.supervisor.execute(run_id)


def test_empty_backend_id_fails_closed_at_gate_check() -> None:
    from fakes import make_harness

    from vouch_agent.contracts.invocation import InvocationStatus
    from vouch_agent.orchestrator import KIND_INVOCATION

    h = make_harness()
    h.runtime.backend_id = lambda: ""  # type: ignore[method-assign]
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.FAILED
    assert h.runtime.calls == 0  # gate refused before any model work
    assert "backend" in (run.error or "")
    invocations = [
        d
        for i in h.store.list_ids(KIND_INVOCATION)
        if (d := h.store.load(KIND_INVOCATION, i)) is not None
    ]
    root = next(d for d in invocations if d["parentId"] is None)
    assert root["status"] == InvocationStatus.FAILED.value
