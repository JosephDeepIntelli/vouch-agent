"""Invocation tree bookkeeping: run-level root invocation, per-model-step
children with narrowing capabilities, budget-slice reservation ids, and the
narrow-only rule enforced at both policy construction and the contract.
"""

from __future__ import annotations

import pytest
from fakes import make_harness, schema_spec, two_step_script

from vouch_agent.contracts.invocation import Invocation, InvocationStatus, new_invocation_id
from vouch_agent.contracts.tasks import TaskStatus
from vouch_agent.errors import ContractError
from vouch_agent.orchestrator import (
    KIND_BUDGET_SLICE,
    KIND_INVOCATION,
    SupervisorPolicy,
)


def _invocations(h) -> dict[str, Invocation]:
    return {
        record_id: Invocation.from_dict(data)
        for record_id in h.store.list_ids(KIND_INVOCATION)
        if (data := h.store.load(KIND_INVOCATION, record_id)) is not None
    }


def test_invocation_children_carry_slice_reservations() -> None:
    h = make_harness(two_step_script())
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    h.supervisor.execute(run_id)

    invocations = _invocations(h)
    root = next(i for i in invocations.values() if i.parent_id is None)
    children = [i for i in invocations.values() if i.parent_id is not None]
    assert len(children) == 2

    # root carries the RUN-level ledger reservation id
    assert root.budget_reservation_id in h.ledger.reservations
    assert root.status is InvocationStatus.COMPLETED
    assert root.started_at and root.ended_at

    for child in children:
        assert child.parent_id == root.invocation_id
        assert child.depth == 1
        assert child.status is InvocationStatus.COMPLETED
        # narrowing: child capabilities are a strict subset of the run's
        assert set(child.granted_capabilities) < set(root.granted_capabilities)
        assert child.policy_digest == root.policy_digest == h.policy.digest()
        # the budgetReservationId points at a persisted step slice
        slices = [h.store.load(KIND_BUDGET_SLICE, i) for i in h.store.list_ids(KIND_BUDGET_SLICE)]
        assert child.budget_reservation_id in {s["reservationId"] for s in slices}
        assert child.started_at and child.ended_at and child.result_digest


def test_invocation_contract_rejects_widening_child() -> None:
    root = Invocation(
        invocation_id=new_invocation_id(),
        task_ref="task_x",
        granted_capabilities=("model.invoke", "artifact.read"),
    )
    with pytest.raises(ContractError, match="cannot widen capabilities"):
        root.child(new_invocation_id(), capabilities=("model.invoke", "tool.use"))
    # narrowing is fine
    child = root.child(new_invocation_id(), capabilities=("model.invoke",))
    assert child.depth == 1 and child.parent_id == root.invocation_id


def test_policy_rejects_widening_step_capabilities() -> None:
    with pytest.raises(ContractError, match="cannot widen run_capabilities"):
        SupervisorPolicy(
            run_capabilities=("model.invoke", "artifact.read"),
            model_step_capabilities=("model.invoke", "tool.use"),
        )
    with pytest.raises(ContractError):
        SupervisorPolicy(model_step_capabilities=())
    with pytest.raises(ContractError):
        SupervisorPolicy(default_budget_usd=0)
    with pytest.raises(ContractError):
        SupervisorPolicy(step_budget_usd=-1)
    with pytest.raises(ContractError, match="duplicates"):
        SupervisorPolicy(run_capabilities=("a", "a", "b"))


def test_failed_run_marks_invocations_failed() -> None:
    from fakes import ScriptedCall

    h = make_harness([ScriptedCall(content="nope", cost_usd=0.01)])
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0, max_steps=4))
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.FAILED

    invocations = _invocations(h)
    root = next(i for i in invocations.values() if i.parent_id is None)
    assert root.status is InvocationStatus.FAILED


def test_cancelled_run_marks_invocations_cancelled() -> None:
    holder: dict[str, object] = {}

    def cancel_now(index: int) -> None:
        holder["supervisor"].cancel(str(holder["run_id"]))

    h = make_harness(two_step_script(), on_call=cancel_now)
    holder["supervisor"] = h.supervisor
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    holder["run_id"] = run_id
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.CANCELLED
    invocations = _invocations(h)
    root = next(i for i in invocations.values() if i.parent_id is None)
    assert root.status is InvocationStatus.CANCELLED
