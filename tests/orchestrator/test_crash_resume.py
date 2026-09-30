"""Crash semantics: unexpected exceptions during side-effecting model steps
leave the step ``side_effect_unknown`` and the run ``needs-reconciliation``;
resume demands a verified note and never replays the unknown step.
"""

from __future__ import annotations

import pytest
from fakes import ScriptedCall, make_harness, schema_spec

from vouch_agent.contracts.invocation import InvocationStatus
from vouch_agent.contracts.journal import EventKind, ReservationStatus
from vouch_agent.contracts.tasks import StepKind, TaskStatus
from vouch_agent.errors import (
    LiveCallBlockedError,
    ReconciliationRequiredError,
)
from vouch_agent.orchestrator import KIND_BUDGET_SLICE, KIND_RECONCILIATION_NOTE


def _crashing_two_step() -> list[ScriptedCall]:
    return [
        ScriptedCall(content='{"recommendation": "draft"}', cost_usd=0.01),
        ScriptedCall(
            content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
            cost_usd=0.01,
            crash=RuntimeError("worker died after writing the external draft"),
        ),
        ScriptedCall(
            content='{"recommendation": "A", "confidence": "high", "priceUsd": 2}',
            cost_usd=0.01,
        ),
    ]


def test_crash_mid_side_effect_needs_reconciliation() -> None:
    h = make_harness(_crashing_two_step())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.NEEDS_RECONCILIATION
    assert h.runtime.calls == 2
    assert h.runtime.side_effects == 2  # the crashing call DID perform its effect

    unknown = [s for s in run.steps if s.side_effect_unknown]
    assert len(unknown) == 1
    step = unknown[0]
    assert step.kind is StepKind.MODEL_CALL
    assert step.status == "unknown"
    assert "outcome unknown" in (step.error or "")
    assert EventKind.RUN_RECONCILIATION.value in h.journal.kinds(run_id)

    # the crashed step's slice settled conservatively as unmeasurable
    slice_2 = h.store.load(KIND_BUDGET_SLICE, f"{run_id}#model-2")
    assert slice_2["unmeasurable"] is True
    assert slice_2["settledAmountUsd"] == pytest.approx(slice_2["amountUsd"])
    entries = h.journal.cost_entries(run_id)
    assert any(not e.measurable for e in entries)

    # honest package persisted even in this state; run reservation stays open
    package = h.supervisor.get_result(run_id)
    assert package is not None and package.deliverable() is False
    assert any("unknown" in u for u in package.uncertainties)
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.status is ReservationStatus.OPEN


def test_resume_without_note_raises() -> None:
    h = make_harness(_crashing_two_step())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    h.supervisor.execute(run_id)
    calls_before = h.runtime.calls

    with pytest.raises(ReconciliationRequiredError):
        h.supervisor.resume(run_id, "")
    with pytest.raises(ReconciliationRequiredError):
        h.supervisor.resume(run_id, "   ")  # whitespace is not a note
    # nothing executed and no state changed by the refused resumes
    assert h.runtime.calls == calls_before
    assert h.supervisor.get_run(run_id).status is TaskStatus.NEEDS_RECONCILIATION


def test_resume_with_note_does_not_replay_unknown_side_effect() -> None:
    h = make_harness(_crashing_two_step())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    h.supervisor.execute(run_id)
    assert h.runtime.calls == 2 and h.runtime.side_effects == 2

    note = (
        "verified with the tool log: the external write for step 2 was "
        "idempotent-keyed and no duplicate charge exists"
    )
    run = h.supervisor.resume(run_id, note)

    # NOT replayed: exactly one new call (the third script item), the crashed
    # second call was never re-attempted
    assert h.runtime.calls == 3
    assert h.runtime.side_effects == 3
    assert run.status is TaskStatus.COMPLETED
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable() is True
    # cost honesty: total is unknown (the crashed call is unmeasured)
    assert package.total_cost_usd is None
    assert any("unmeasured" in u for u in package.uncertainties)
    # the note is persisted with the run
    notes = [i for i in h.store.list_ids(KIND_RECONCILIATION_NOTE) if i.startswith(run_id)]
    assert notes
    assert EventKind.RUN_RECONCILIATION.value in h.journal.kinds(run_id)
    # the unknown step stays on record, still marked unknown
    persisted_run = h.supervisor.get_run(run_id)
    assert any(s.side_effect_unknown for s in persisted_run.steps)


def test_resume_after_note_can_fail_explicitly() -> None:
    h = make_harness(
        [
            ScriptedCall(content='{"recommendation": "draft"}', cost_usd=0.01),
            ScriptedCall(content='{"a": 1}', cost_usd=0.01, crash=RuntimeError("boom")),
            ScriptedCall(
                content="irrelevant",
                cost_usd=0.01,
                error=LiveCallBlockedError("live call blocked in fixture mode"),
            ),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    h.supervisor.execute(run_id)
    run = h.supervisor.resume(run_id, "verified: no external write happened")
    # NEEDS_RECONCILIATION -> RUNNING -> FAILED is a legal, explicit path
    assert run.status is TaskStatus.FAILED
    assert "live-call-blocked" in (run.error or "")
    package = h.supervisor.get_result(run_id)
    assert package is not None and package.deliverable() is False


def test_execute_on_needs_reconciliation_state_requires_resume() -> None:
    h = make_harness(_crashing_two_step())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    h.supervisor.execute(run_id)
    with pytest.raises(ReconciliationRequiredError):
        h.supervisor.execute(run_id)


def test_unknown_step_invocation_marked_failed() -> None:
    from vouch_agent.contracts.invocation import Invocation

    h = make_harness(_crashing_two_step())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0))
    h.supervisor.execute(run_id)

    invocations = [
        Invocation.from_dict(d)
        for i in h.store.list_ids("invocation")
        if (d := h.store.load("invocation", i)) is not None
    ]
    failed_children = [
        i for i in invocations if i.parent_id is not None and i.status is InvocationStatus.FAILED
    ]
    assert len(failed_children) == 1
    assert "unknown" in (failed_children[0].error or "")
    assert h.supervisor.get_run(run_id) is not None
