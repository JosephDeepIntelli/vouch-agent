"""Happy path: a two-step task that actually performs bounded work.

Step 1 (model) produces a draft artifact that does NOT satisfy the schema;
step 2 refines it and the trusted schema check passes — proving completion
is driven by checked conditions, not by the model's say-so or the script
merely finishing. Cost accounting, invocation tree, checkpoints and the
ResultPackage digest binding are all asserted here.
"""

from __future__ import annotations

import pytest
from fakes import (
    ScriptedCall,
    make_harness,
    schema_spec,
    two_step_script,
)

from vouch_agent.contracts.invocation import Invocation, InvocationStatus
from vouch_agent.contracts.journal import EventKind, ReservationStatus
from vouch_agent.contracts.tasks import StepKind, TaskStatus
from vouch_agent.orchestrator import (
    KIND_BUDGET_SLICE,
    KIND_INVOCATION,
    KIND_RESULT_PACKAGE,
    KIND_TASK_RUN,
    KIND_TASK_SPEC,
    SupervisorPolicy,
)


def test_submit_persists_spec_run_reservation_and_journal() -> None:
    h = make_harness(two_step_script())
    spec = schema_spec(max_cost_usd=0.5)
    run_id = h.supervisor.submit(spec)

    run = h.supervisor.get_run(run_id)
    assert run is not None and run.status is TaskStatus.QUEUED
    assert run.task_digest == spec.digest()
    assert run.mode.value == "fixture"
    # spec + run persisted; digest binding intact
    assert h.store.load(KIND_TASK_SPEC, spec.spec_id) is not None
    assert h.store.load(KIND_TASK_RUN, run_id) is not None
    # run-level reservation taken BEFORE work, on the ledger
    assert len(h.ledger.reservations) == 1
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.amount_usd == pytest.approx(0.5)
    assert reservation.status is ReservationStatus.OPEN
    # journal: budget reserved + run started
    kinds = h.journal.kinds(run_id)
    assert EventKind.BUDGET_RESERVED.value in kinds
    assert EventKind.RUN_STARTED.value in kinds
    # nothing executed yet
    assert h.runtime.calls == 0
    assert run.steps == ()


def test_policy_default_budget_used_when_spec_has_none() -> None:
    h = make_harness(two_step_script(), policy=SupervisorPolicy(default_budget_usd=0.75))
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=None))
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.holder == run_id
    assert reservation.amount_usd == pytest.approx(0.75)


def test_happy_two_step_delivers_with_full_accounting() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    run = h.supervisor.execute(run_id)

    # --- run outcome: completed only via trusted condition check
    assert run.status is TaskStatus.COMPLETED, run.error
    assert h.runtime.calls == 2

    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable() is True
    assert package.completed_conditions_check == {"artifact_schema[0]": True}
    assert package.total_cost_usd == pytest.approx(0.03)
    assert package.conclusion.startswith("completed")
    assert not package.not_done_items
    assert not package.uncertainties
    # digest binding: run.result_digest is the package digest
    assert run.result_digest == package.digest()

    # --- steps: persist-intent + result, digests and usage recorded
    kinds = [s.kind for s in run.steps]
    assert kinds[0] is StepKind.PLAN
    assert kinds[1] is StepKind.TOOL_CALL
    assert kinds[2] is StepKind.GATE_CHECK
    assert kinds[3] is StepKind.MODEL_CALL
    assert kinds[4] is StepKind.MODEL_CALL
    assert kinds[5] is StepKind.FINALIZE
    assert all(s.status == "ok" for s in run.steps)
    model_steps = [s for s in run.steps if s.kind is StepKind.MODEL_CALL]
    assert all(s.input_digest and s.output_digest for s in run.steps)
    assert model_steps[0].usage["promptTokens"] == 120
    assert model_steps[0].usage["costUsd"] == pytest.approx(0.01)
    assert model_steps[1].usage["costUsd"] == pytest.approx(0.02)

    # --- artifacts: draft fails schema, final passes; both delivered
    draft = h.artifacts.get(model_steps[0].output_digest or "")
    final = h.artifacts.get(model_steps[1].output_digest or "")
    assert b"draft" in draft
    assert b"candidate A with sources" in final
    produced = {model_steps[0].output_digest, model_steps[1].output_digest}
    assert set(package.artifact_refs) >= produced
    assert package.artifact_refs[-1] == model_steps[1].output_digest  # final last

    # --- cost lines for everything that ran; run reservation settled
    entries = h.journal.cost_entries(run_id)
    assert [e.amount_usd for e in entries] == [pytest.approx(0.01), pytest.approx(0.02)]
    assert all(e.measurable for e in entries)
    reservation = next(iter(h.ledger.reservations.values()))
    assert reservation.status is ReservationStatus.SETTLED
    assert reservation.settled_amount_usd == pytest.approx(0.03)
    assert h.ledger.settled_usd() == pytest.approx(0.03)

    # --- journal lifecycle complete
    event_kinds = h.journal.kinds(run_id)
    assert EventKind.RUN_COMPLETED.value in event_kinds
    assert EventKind.BUDGET_SETTLED.value in event_kinds
    assert event_kinds.count(EventKind.RECOVERY_POINT.value) >= len(run.steps)

    # --- result package persisted and reloadable
    persisted = h.store.load(KIND_RESULT_PACKAGE, run_id)
    assert persisted is not None and persisted["conclusion"] == package.conclusion


def test_result_package_roundtrip_and_readers() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec())
    h.supervisor.execute(run_id)

    from vouch_agent.contracts.tasks import ResultPackage

    package = h.supervisor.get_result(run_id)
    assert package is not None
    reloaded = ResultPackage.from_dict(package.to_dict())
    assert reloaded == package
    assert h.supervisor.get_result("run_missing") is None
    assert h.supervisor.get_run("run_missing") is None


def test_draft_and_final_artifact_keys_address_conditions() -> None:
    # condition targets "draft" explicitly: step 1 already satisfies it, so
    # the loop stops after one model call without burning budget on step 2.
    h = make_harness(two_step_script())
    spec = schema_spec(
        conditions=[
            {
                "type": "artifact_schema",
                "artifact": "draft",
                "schema": {
                    "type": "object",
                    "required": ["recommendation"],
                    "properties": {"recommendation": {"type": "string"}},
                },
            }
        ]
    )
    run_id = h.supervisor.submit(spec)
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.COMPLETED
    assert h.runtime.calls == 1


def test_inputs_artifact_is_materialized_and_addressable() -> None:
    h = make_harness(two_step_script())
    spec = schema_spec(
        inputs={"candidates": ["A", "B"], "market": "US"},
        conditions=[
            {
                "type": "output_contains",
                "artifact": "inputs",
                "contains": "candidates",
            }
        ],
    )
    run_id = h.supervisor.submit(spec)
    run = h.supervisor.execute(run_id)
    # machine conditions met after the first model step (inputs were checked)
    assert run.status is TaskStatus.COMPLETED
    package = h.supervisor.get_result(run_id)
    assert package is not None and package.deliverable()
    context_step = next(s for s in run.steps if s.kind is StepKind.TOOL_CALL)
    payload = h.artifacts.get(context_step.output_digest or "")
    assert b"candidates" in payload and b"US" in payload


def test_invocation_tree_records_run_and_model_steps() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec())
    h.supervisor.execute(run_id)

    invocations = {
        record_id: Invocation.from_dict(data)
        for record_id in (i for i in h.store.list_ids(KIND_INVOCATION))
        if (data := h.store.load(KIND_INVOCATION, record_id)) is not None
    }
    roots = [i for i in invocations.values() if i.parent_id is None]
    children = [i for i in invocations.values() if i.parent_id is not None]
    assert len(roots) == 1 and len(children) == 2
    root = roots[0]
    assert root.status is InvocationStatus.COMPLETED
    assert root.granted_capabilities == h.policy.run_capabilities
    # run-level reservation id on the root
    assert root.budget_reservation_id in h.ledger.reservations
    for child in children:
        assert child.parent_id == root.invocation_id
        assert child.task_ref == root.task_ref
        # capabilities narrow-only
        assert set(child.granted_capabilities) <= set(root.granted_capabilities)
        assert child.depth == 1
        assert child.status is InvocationStatus.COMPLETED
        # the step's budget slice reservation id is persisted via the store
        slice_ids = set(h.store.list_ids(KIND_BUDGET_SLICE))
        assert child.budget_reservation_id
        slices = (
            h.store.load(KIND_BUDGET_SLICE, f"{run_id}#model-1"),
            h.store.load(KIND_BUDGET_SLICE, f"{run_id}#model-2"),
        )
        assert {s["reservationId"] for s in slices if s} == {
            c.budget_reservation_id for c in children
        }
        assert slice_ids


def test_sessions_closed_and_config_bounded() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    h.supervisor.execute(run_id)
    assert h.runtime.sessions
    assert all(s.closed for s in h.runtime.sessions)
    config = h.runtime.sessions[0]._config
    assert config.mode.value == "fixture"
    assert config.allow_timeout_pragma is False
    assert config.max_cost_usd == pytest.approx(0.5)


def test_scripted_json_drives_schema_check_for_real() -> None:
    # If both model outputs fail the schema, the run must NOT complete even
    # though the script ran to exhaustion of the step bound.
    h = make_harness(
        [
            ScriptedCall(content='{"recommendation": "x"}', cost_usd=0.01),
            ScriptedCall(content="not json at all", cost_usd=0.01),
        ]
    )
    spec = schema_spec(max_cost_usd=0.5)
    run_id = h.supervisor.submit(spec)
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.FAILED
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.completed_conditions_check == {"artifact_schema[0]": False}
    assert package.deliverable() is False
