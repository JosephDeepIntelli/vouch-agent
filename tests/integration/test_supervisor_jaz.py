"""Lead-owned integration: the Supervisor executing real work on the pinned
JAZ runtime (design §4: one kernel, business execution mode).

No fakes on the runtime path: Supervisor -> JazRuntime -> real jaz.invoke
machinery with the deterministic ScriptedBackend (never networked). The
scripted pool is injected by a trusted-side wrapper because the supervisor
authors the WorkerSessionConfig and must stay backend-agnostic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from vouch_agent.contracts.common import RunMode
from vouch_agent.contracts.tasks import TaskSpec, TaskStatus, new_task_spec_id
from vouch_agent.orchestrator import Supervisor, SupervisorPolicy
from vouch_agent.runtime.jaz_engine import JazRuntime
from vouch_agent.runtime.ports import Runtime, RuntimeSession, WorkerSessionConfig
from vouch_agent.storage import (
    FileArtifactStore,
    SqliteBudgetLedger,
    SqliteJournal,
    SqliteMetadataStore,
)

RECOMMENDATION_SCHEMA: dict = {
    "type": "object",
    "required": ["recommendation", "confidence", "priceUsd"],
    "properties": {
        "recommendation": {"type": "string"},
        "confidence": {"type": "string"},
        "priceUsd": {"type": "number"},
    },
}

#: Model-authored code (the real JAZ agent "writes" this); the evaluated
#: return value becomes the trusted step artifact.
SCRIPT_DRAFT = 'draft = {"recommendation": "candidate A", "confidence": "low"}\nreturn draft'
SCRIPT_FINAL = (
    'final = {"recommendation": "candidate A with sources", '
    '"confidence": "high", "priceUsd": 12.5}\nreturn final'
)


class ScriptedPoolRuntime:
    """Trusted-side wrapper: injects the scripted model pool into whatever
    bounded config the supervisor builds. Implements ports.Runtime."""

    def __init__(self, responses: tuple[str, ...]) -> None:
        self._inner: Runtime = JazRuntime()
        self._responses = responses

    def open_session(self, config: WorkerSessionConfig) -> RuntimeSession:
        if config.mode is not RunMode.FIXTURE:
            raise AssertionError("this wrapper only serves fixture-mode sessions")
        return self._inner.open_session(replace(config, scripted_responses=self._responses))

    def backend_id(self) -> str:
        return self._inner.backend_id()


@pytest.fixture()
def workspace(tmp_path: Path):
    store = SqliteMetadataStore(tmp_path / "meta.sqlite")
    artifacts = FileArtifactStore(tmp_path)
    journal = SqliteJournal(tmp_path / "journal.sqlite")
    ledger = SqliteBudgetLedger(tmp_path / "budget.sqlite", total_usd_cap=2.0)
    yield store, artifacts, ledger, journal
    store.close()


def _spec() -> TaskSpec:
    return TaskSpec(
        spec_id=new_task_spec_id(),
        title="Compare two product candidates",
        goal="Produce a sourced comparison of the two candidates",
        mode=RunMode.FIXTURE,
        inputs={"candidates": ["A", "B"]},
        max_cost_usd=0.5,
        max_steps=4,
        success_criteria={
            "conditions": [{"type": "artifact_schema", "schema": RECOMMENDATION_SCHEMA}]
        },
    )


def test_supervisor_executes_real_jaz_session_to_deliverable(workspace) -> None:
    store, artifacts, ledger, journal = workspace
    supervisor = Supervisor(
        ScriptedPoolRuntime((SCRIPT_DRAFT, SCRIPT_FINAL)),
        store,
        artifacts,
        ledger,
        journal,
        SupervisorPolicy(default_budget_usd=0.5, default_max_steps=4, step_budget_usd=0.2),
    )
    run_id = supervisor.submit(_spec())
    run = supervisor.execute(run_id)

    assert run.status is TaskStatus.COMPLETED, run.error
    package = supervisor.get_result(run_id)
    assert package is not None
    assert package.deliverable()
    # the final artifact is real, schema-valid content from the evaluated
    # JAZ return value — not a model's claim about itself
    final = json.loads(artifacts.get(package.artifact_refs[-1]).decode("utf-8"))
    assert final["recommendation"] == "candidate A with sources"
    assert final["priceUsd"] == 12.5
    # full accounting: measured or conservatively-settled, never zero-by-default
    assert package.total_cost_usd is None or package.total_cost_usd >= 0
    costs = journal.cost_entries()
    assert costs, "model steps must book cost lines"
    assert all(c.measurable or c.amount_usd is None for c in costs)
    # the run reservation was settled and released back to the pool
    assert ledger.outstanding_usd() == 0.0


def test_supervisor_fails_closed_when_script_exhausts(workspace) -> None:
    """Replay exhaustion in fixture mode is terminal — no live fallback."""

    store, artifacts, ledger, journal = workspace
    supervisor = Supervisor(
        ScriptedPoolRuntime(()),  # nothing scripted: first model step must abort
        store,
        artifacts,
        ledger,
        journal,
        SupervisorPolicy(default_budget_usd=0.5, step_budget_usd=0.2),
    )
    run_id = supervisor.submit(_spec())
    run = supervisor.execute(run_id)
    assert run.status is TaskStatus.FAILED
    assert run.error, "failure must carry an honest error"


def test_schema_failure_is_failed_not_delivered(workspace) -> None:
    store, artifacts, ledger, journal = workspace
    bad_final = 'final = {"recommendation": "candidate A"}  # missing fields\nreturn final'
    supervisor = Supervisor(
        ScriptedPoolRuntime((SCRIPT_DRAFT, bad_final)),
        store,
        artifacts,
        ledger,
        journal,
        SupervisorPolicy(default_budget_usd=0.5, step_budget_usd=0.2),
    )
    run_id = supervisor.submit(_spec())
    run = supervisor.execute(run_id)
    assert run.status is TaskStatus.FAILED
    package = supervisor.get_result(run_id)
    assert package is not None
    assert not package.deliverable()
    assert package.not_done_items, "the honest package says what is missing"


def test_output_depends_on_supplied_material_not_the_prompt(workspace) -> None:
    """Stage C scoped-input proof: the SAME scripted code reads different
    facts from TaskSpec.inputs through the session scope — the output tracks
    the material, so canned outputs cannot fake data dependence."""
    store, artifacts, ledger, journal = workspace
    # the script never names the fact's value; it reads scope["materials"]
    script = (
        'value = materials["fact"]["value"]\n'
        'return {"finding": value, "source": materials["fact"]["source"]}'
    )
    schema = {
        "type": "object",
        "required": ["finding", "source"],
        "properties": {"finding": {"type": "string"}, "source": {"type": "string"}},
    }

    def run_with_fact(fact: dict) -> dict:
        supervisor = Supervisor(
            ScriptedPoolRuntime((script,)),
            store,
            artifacts,
            ledger,
            journal,
            SupervisorPolicy(default_budget_usd=0.5, step_budget_usd=0.2),
        )
        spec = TaskSpec(
            spec_id=new_task_spec_id(),
            title="Extract the supplied fact",
            goal="Report the fact from the supplied material",
            mode=RunMode.FIXTURE,
            inputs={"fact": fact},
            max_cost_usd=0.5,
            max_steps=2,
            success_criteria={"conditions": [{"type": "artifact_schema", "schema": schema}]},
        )
        run_id = supervisor.submit(spec)
        run = supervisor.execute(run_id)
        assert run.status is TaskStatus.COMPLETED, run.error
        package = supervisor.get_result(run_id)
        assert package is not None
        return json.loads(artifacts.get(package.artifact_refs[-1]).decode("utf-8"))

    first = run_with_fact({"value": "迪普智选 fact one", "source": "material-A"})
    second = run_with_fact({"value": "a completely different fact", "source": "material-B"})
    assert first == {"finding": "迪普智选 fact one", "source": "material-A"}
    assert second == {"finding": "a completely different fact", "source": "material-B"}
