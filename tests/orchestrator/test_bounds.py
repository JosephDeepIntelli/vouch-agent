"""Bounded execution: max-steps stop, wall-clock timeout, and the
no-success-criteria fail-closed rule. Every bound stop leaves an honest
package, never a silent success.
"""

from __future__ import annotations

import time

import pytest
from fakes import ScriptedCall, make_harness, schema_spec

from vouch_agent.contracts.tasks import TaskStatus


def test_max_steps_stop_is_explicit_and_honest() -> None:
    # max_steps counts consumed work steps (context + model calls): with
    # max_steps=2 the context step plus ONE model call hit the bound before
    # the schema condition could ever be met.
    h = make_harness(
        [
            ScriptedCall(content='{"recommendation": "A"}', cost_usd=0.01),
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.01,
            ),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0, max_steps=2))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.FAILED
    assert h.runtime.calls == 1
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable() is False
    assert package.completed_conditions_check == {"artifact_schema[0]": False}
    assert any("step bound" in item for item in package.not_done_items)
    # the partial artifact is still delivered, honestly referenced
    assert package.artifact_refs


def test_wall_clock_timeout_stops_explicitly() -> None:
    h = make_harness(
        [
            ScriptedCall(content='{"a": 1}', cost_usd=0.01, delay_s=0.06),
            ScriptedCall(content='{"a": 2}', cost_usd=0.01, delay_s=0.06),
            ScriptedCall(content='{"a": 3}', cost_usd=0.01, delay_s=0.06),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0, max_wall_clock_s=0.1))
    started = time.monotonic()
    run = h.supervisor.execute(run_id)
    elapsed = time.monotonic() - started

    assert run.status is TaskStatus.FAILED
    assert "wall-clock" in (run.error or "")
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert any("wall-clock" in item for item in package.not_done_items)
    # bounded: at most two delayed calls fit inside the bound
    assert 1 <= h.runtime.calls <= 2
    assert elapsed < 1.0


def test_no_success_criteria_fails_closed_before_model_work() -> None:
    h = make_harness([ScriptedCall(content='{"a": 1}', cost_usd=0.01)])
    spec = schema_spec(conditions=[])
    spec_id = spec.spec_id
    run_id = h.supervisor.submit(spec)
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.FAILED
    assert h.runtime.calls == 0  # no model work without checkable criteria
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert any("no success criteria" in item for item in package.not_done_items)
    assert package.completed_conditions_check == {}
    assert spec_id  # spec persisted and addressable


def test_malformed_criteria_rejected_at_submit() -> None:
    from vouch_agent.errors import ContractError

    h = make_harness()
    bad = schema_spec(conditions=[{"type": "definitely_not_a_condition"}])
    try:
        h.supervisor.submit(bad)
        raise AssertionError("expected ContractError")
    except ContractError as exc:
        assert "unknown condition type" in str(exc)
    assert h.ledger.outstanding_usd() == 0.0  # no reservation leaked
    assert h.runtime.calls == 0


def test_invalid_spec_bounds_rejected_at_submit() -> None:
    from vouch_agent.errors import ContractError

    h = make_harness()
    for kwargs in ({"max_wall_clock_s": 0}, {"max_steps": -1}, {"max_cost_usd": -0.1}):
        with pytest.raises(ContractError):
            h.supervisor.submit(schema_spec(**kwargs))  # type: ignore[arg-type]
