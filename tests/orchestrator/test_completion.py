"""Completion honesty: the model's claims never complete a run.

Covers: model-says-done with unmet conditions, schema failure delivering an
honest failed package, output_contains, the max_cost_usd condition, and the
``none`` human-acceptance marker ending a run incomplete-but-delivered.
"""

from __future__ import annotations

from fakes import ScriptedCall, make_harness, schema_spec

from vouch_agent.contracts.tasks import TaskStatus


def test_model_says_done_does_not_complete_the_run() -> None:
    # The model output *claims* completion in every way a model can; the
    # schema condition is unmet, so the run must not complete.
    says_done = '{"status": "done", "message": "TASK COMPLETE, nothing more to do"}'
    h = make_harness(
        [
            ScriptedCall(content=says_done, cost_usd=0.01),
            ScriptedCall(content=says_done, cost_usd=0.01),
        ]
    )
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0, max_steps=3))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.FAILED
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable() is False
    assert package.completed_conditions_check == {"artifact_schema[0]": False}
    # the claim was recorded as an artifact (data), never as a verdict
    assert package.artifact_refs


def test_schema_failure_delivers_honest_failed_package() -> None:
    h = make_harness([ScriptedCall(content='{"recommendation": "A"}', cost_usd=0.01)])
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=1.0, max_steps=4))
    run = h.supervisor.execute(run_id)

    assert run.status is TaskStatus.FAILED
    package = h.supervisor.get_result(run_id)
    assert package is not None
    detail = next(item for item in package.not_done_items if item.startswith("condition"))
    assert "confidence" in detail  # the actual missing field is named
    assert package.artifact_refs  # partial artifact still delivered
    assert package.total_cost_usd is not None  # costs accounted even on failure


def test_output_contains_condition_pass_and_fail() -> None:
    conditions = [{"type": "output_contains", "artifact": "final", "contains": "sourced"}]
    ok = make_harness([ScriptedCall(content="fully sourced comparison", cost_usd=0.01)])
    run_id = ok.supervisor.submit(schema_spec(conditions=conditions))
    assert ok.supervisor.execute(run_id).status is TaskStatus.COMPLETED

    bad = make_harness([ScriptedCall(content="no citations here", cost_usd=0.01)])
    run_id = bad.supervisor.submit(
        schema_spec(conditions=conditions, max_cost_usd=1.0, max_steps=4)
    )
    run = bad.supervisor.execute(run_id)
    assert run.status is TaskStatus.FAILED
    package = bad.supervisor.get_result(run_id)
    assert package is not None
    assert package.completed_conditions_check == {"output_contains[0]": False}


def test_max_cost_usd_condition_completes_when_within_bound() -> None:
    h = make_harness(
        [
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.01,
            )
        ]
    )
    spec = schema_spec(
        conditions=[
            {
                "type": "artifact_schema",
                "schema": {
                    "type": "object",
                    "required": ["recommendation"],
                    "properties": {"recommendation": {"type": "string"}},
                },
            },
            {"type": "max_cost_usd", "maxUsd": 0.05},
        ]
    )
    run_id = h.supervisor.submit(spec)
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.COMPLETED
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.completed_conditions_check == {
        "artifact_schema[0]": True,
        "max_cost_usd[1]": True,
    }


def test_max_cost_usd_condition_fails_when_exceeded() -> None:
    h = make_harness(
        [
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.10,
            )
        ]
    )
    spec = schema_spec(
        conditions=[
            {
                "type": "artifact_schema",
                "schema": {
                    "type": "object",
                    "required": ["recommendation"],
                    "properties": {"recommendation": {"type": "string"}},
                },
            },
            {"type": "max_cost_usd", "maxUsd": 0.05},
        ],
        max_cost_usd=1.0,
        max_steps=4,
    )
    run_id = h.supervisor.submit(spec)
    run = h.supervisor.execute(run_id)
    # schema passed but the cost condition is false => not completed
    assert run.status is TaskStatus.FAILED
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.completed_conditions_check["max_cost_usd[1]"] is False
    assert package.completed_conditions_check["artifact_schema[0]"] is True


def test_none_condition_ends_incomplete_but_delivered() -> None:
    h = make_harness(
        [
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.01,
            )
        ]
    )
    spec = schema_spec(
        conditions=[
            {
                "type": "artifact_schema",
                "schema": {
                    "type": "object",
                    "required": ["recommendation"],
                    "properties": {"recommendation": {"type": "string"}},
                },
            },
            {"type": "none"},
        ]
    )
    run_id = h.supervisor.submit(spec)
    run = h.supervisor.execute(run_id)

    # machine conditions met, human marker pending => NOT completed
    assert run.status is TaskStatus.FAILED
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable() is False
    assert package.completed_conditions_check["artifact_schema[0]"] is True
    assert package.completed_conditions_check["none[1]"] is False
    assert package.external_actions == {"human-acceptance:none[1]": "pending"}
    assert any("pending human acceptance" in i for i in package.not_done_items)
    assert "incomplete-but-delivered" in package.conclusion
    # the deliverable artifact IS handed over
    assert package.artifact_refs
    assert h.runtime.calls == 1  # stopped deliberately, no budget burn


def test_none_only_spec_stops_after_first_model_step() -> None:
    h = make_harness([ScriptedCall(content="anything at all", cost_usd=0.01)])
    spec = schema_spec(conditions=[{"type": "none"}])
    run_id = h.supervisor.submit(spec)
    run = h.supervisor.execute(run_id)
    assert run.status is TaskStatus.FAILED
    assert h.runtime.calls == 1
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.external_actions["human-acceptance:none[0]"] == "pending"
