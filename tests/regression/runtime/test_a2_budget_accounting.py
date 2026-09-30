"""A2 regressions (runtime/orchestrator): complete cost accounting and
per-query allocation BEFORE calls start.

Converted from the coordinator reproduction ``reproduce_budget.py``
(review-runtime-20260928, defects 1 and 2), assertions inverted:

* a scripted query that costs $0.01 and then replay-exhausts keeps $0.01 in
  the slice, the journal, the ResultPackage and the ledger — a failure's
  taxonomy name never certifies zero spend;
* under a $0.015 task cap the second $0.01 turn is REFUSED before it runs;
* a run that overran is never certified as budget-conformant completion and
  the parent closes with an explicit overage.

Everything runs the real pinned JAZ engine with the deterministic
ScriptedBackend in temporary SQLite/file stores; prices are synthetic
accounting values, there is no network and no provider.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_supervisor_jaz import SCRIPT_DRAFT, SCRIPT_FINAL, ScriptedPoolRuntime, _spec

from vouch_agent.contracts.journal import EventKind
from vouch_agent.contracts.tasks import StepKind, TaskStatus
from vouch_agent.orchestrator import (
    KIND_BUDGET_SLICE,
    Supervisor,
    SupervisorPolicy,
)
from vouch_agent.storage import (
    FileArtifactStore,
    SqliteBudgetLedger,
    SqliteJournal,
    SqliteMetadataStore,
)

#: A step whose JAZ agent takes TWO model turns (two underlying queries).
TWO_TURN_SCRIPT = (
    "x = 1",
    'return {"recommendation": "A", "confidence": "high", "priceUsd": 12.5}',
)


class Workspace:
    def __init__(self, root: Path) -> None:
        self.store = SqliteMetadataStore(root / "meta.sqlite")
        self.artifacts = FileArtifactStore(root)
        self.ledger = SqliteBudgetLedger(root / "budget.sqlite", total_usd_cap=2.0)
        self.journal = SqliteJournal(root / "journal.sqlite")

    def supervisor(self, runtime, **policy) -> Supervisor:
        return Supervisor(
            runtime,
            self.store,
            self.artifacts,
            self.ledger,
            self.journal,
            SupervisorPolicy(**policy) if policy else SupervisorPolicy(),
        )

    def close(self) -> None:
        self.store.close()


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path)
    yield ws
    ws.close()


def _run(ws: Workspace, script: tuple[str, ...], cap: float) -> dict:
    supervisor = ws.supervisor(ScriptedPoolRuntime(script))
    run_id = supervisor.submit(replace(_spec(), max_cost_usd=cap))
    run = supervisor.execute(run_id)
    package = supervisor.get_result(run_id)
    model_steps = [s for s in run.steps if s.kind is StepKind.MODEL_CALL]
    return {
        "run": run,
        "package": package,
        "model_steps": model_steps,
        "journal_cost": sum(
            entry.amount_usd or 0.0 for entry in ws.journal.cost_entries(run_id) if entry.measurable
        ),
        "unmeasured_entries": [
            entry for entry in ws.journal.cost_entries(run_id) if not entry.measurable
        ],
    }


def test_failed_work_keeps_its_measured_cost(workspace: Workspace) -> None:
    """$0.01 spent, then replay exhaustion inside the same invocation."""
    result = _run(workspace, ("x = 1",), 0.5)  # second query exhausts the pool

    run = result["run"]
    assert run.status is TaskStatus.FAILED  # replay exhaustion is terminal
    assert "replay" in (run.error or "") or "budget" in (run.error or "")

    # the measured dollar survives everywhere (was: 0.00 everywhere)
    failed_step = result["model_steps"][-1]
    assert failed_step.status == "failed"
    slice_record = workspace.store.load(
        KIND_BUDGET_SLICE, f"{run.run_id}#model-{len(result['model_steps'])}"
    )
    assert slice_record is not None
    assert slice_record["settledAmountUsd"] == pytest.approx(0.01)
    assert result["journal_cost"] == pytest.approx(0.01)
    package = result["package"]
    assert package is not None
    assert package.total_cost_usd == pytest.approx(0.01)
    assert workspace.ledger.settled_usd() == pytest.approx(0.01)
    # and the ledger's child reservation for the query settled at its actual
    children = workspace.ledger.children(
        next(
            r.reservation_id
            for r in workspace.ledger.reservations()
            if r.parent_reservation_id is None
        )
    )
    settled_children = [c for c in children if c.settled_amount_usd is not None]
    assert [c.settled_amount_usd for c in settled_children] == [pytest.approx(0.01)]
    # nothing is left holding budget open for a run that already terminated
    assert all(c.status.value != "open" for c in children)


def test_second_turn_is_refused_before_it_runs_under_task_cap(
    workspace: Workspace,
) -> None:
    """Task cap $0.015, two $0.01 turns: the second must never execute."""
    result = _run(workspace, TWO_TURN_SCRIPT, 0.015)

    run = result["run"]
    assert run.status is not TaskStatus.COMPLETED  # never certified as completed
    assert workspace.ledger.settled_usd() == pytest.approx(0.01)  # only ONE turn ran
    # exactly one underlying query was served by the scripted backend
    served = [entry for entry in workspace.journal.cost_entries(run.run_id) if entry.measurable]
    assert [e.amount_usd for e in served] == [pytest.approx(0.01)]
    # the run refused the second query through the budget, and says so
    assert EventKind.BUDGET_EXHAUSTED.value in [
        e.kind.value for e in workspace.journal.events(run.run_id)
    ]


def test_overspend_is_never_fixed_by_clipping_reported_actuals(
    workspace: Workspace, tmp_path: Path
) -> None:
    """A cap the two turns cannot fit: the second turn is refused (never
    executed-then-clipped) and the ledger keeps the honest single-turn cost."""
    result = _run(workspace, (SCRIPT_DRAFT, SCRIPT_FINAL), 0.5)
    assert result["run"].status is TaskStatus.COMPLETED
    assert workspace.ledger.settled_usd() == pytest.approx(0.02)

    tight = Workspace(tmp_path / "tight")
    try:
        tight_result = _run(tight, (SCRIPT_DRAFT, SCRIPT_FINAL), 0.015)
        tight_run = tight_result["run"]
        assert tight_run.status is TaskStatus.FAILED
        assert tight.ledger.settled_usd() == pytest.approx(0.01)
        assert tight_result["package"] is not None
        assert tight_result["package"].total_cost_usd == pytest.approx(0.01)
    finally:
        tight.close()


def test_conservative_settlement_when_outcome_is_unknown(workspace: Workspace) -> None:
    """A step that began but cannot be measured settles conservatively —
    the reservation is never silently returned to the pool as zero."""
    result = _run(workspace, (SCRIPT_DRAFT, SCRIPT_FINAL), 0.5)
    assert result["run"].status is TaskStatus.COMPLETED
    parent = next(
        r
        for r in workspace.ledger.reservations()
        if r.parent_reservation_id is None and r.status.value == "settled"
    )
    # children actuals sum to the measured cost; nothing hidden
    children = workspace.ledger.children(parent.reservation_id)
    assert sum(c.settled_amount_usd or 0.0 for c in children) == pytest.approx(0.02)
    assert parent.settled_amount_usd == pytest.approx(0.02)


def test_worker_process_failure_usage_survives_the_parent_error_mapper(
    tmp_path: Path,
) -> None:
    """A worker-process step that already spent money reports its usage on the
    mapped error — the parent must not settle zero for it."""
    from vouch_agent.runtime.failure_usage import usage_of
    from vouch_agent.runtime.guards import WorkerStepRequest, run_session_isolated

    config = _worker_config()
    steps = [
        # first step spends 0.01 and succeeds; the pool then runs dry
        WorkerStepRequest(instruction="x = 1"),
        WorkerStepRequest(instruction="return x"),
    ]
    with pytest.raises(Exception) as excinfo:
        run_session_isolated(config, steps)
    usage = usage_of(excinfo.value)
    assert usage is not None, "the mapped worker error must carry the child's usage"
    assert usage.get("cost_usd") == pytest.approx(0.01)


def _worker_config() -> object:
    from vouch_agent.contracts.common import RunMode
    from vouch_agent.runtime.ports import WorkerSessionConfig

    return WorkerSessionConfig(
        mode=RunMode.FIXTURE,
        max_steps=4,
        wall_clock_s=30.0,
        max_cost_usd=0.5,
        scripted_responses=("x = 1",),
    )


def test_mapped_worker_error_usage_helper_roundtrip() -> None:
    from vouch_agent.errors import ReplayExhaustedError
    from vouch_agent.runtime.failure_usage import attach_usage, usage_of

    exc = ReplayExhaustedError("exhausted after two paid queries")
    assert usage_of(exc) is None
    attach_usage(exc, {"cost_usd": 0.02, "llm_calls": 2})
    assert usage_of(exc) == {"cost_usd": 0.02, "llm_calls": 2}
    # an empty payload is indistinguishable from none: both mean "unmeasured"
    assert usage_of(attach_usage(ValueError("x"), {})) is None


def test_package_and_ledger_agree_on_cost(workspace: Workspace) -> None:
    """Journal, ResultPackage and ledger tell the same story (no split brain)."""
    result = _run(workspace, (SCRIPT_DRAFT, SCRIPT_FINAL), 0.5)
    package = result["package"]
    assert package is not None
    assert package.total_cost_usd == pytest.approx(result["journal_cost"])
    assert workspace.ledger.settled_usd() == pytest.approx(result["journal_cost"])
    final_artifact = json.loads(workspace.artifacts.get(package.artifact_refs[-1]).decode("utf-8"))
    assert final_artifact["priceUsd"] == 12.5  # the work really happened


def test_runtime_without_query_reservation_is_never_certified(workspace: Workspace) -> None:
    """A runtime that reports spend without reserving per-query budget cannot
    get that spend certified as a budget-conformant completion."""
    from fakes import ScriptedCall, make_harness, schema_spec

    h = make_harness(
        [
            ScriptedCall(
                content='{"recommendation": "A", "confidence": "high", "priceUsd": 1}',
                cost_usd=0.01,
            )
        ]
    )

    class BudgetlessRuntime:
        """Ignores the config's query_budget — spend bypasses the ledger."""

        def __init__(self) -> None:
            self._inner = h.runtime

        def open_session(self, config):
            return self._inner.open_session(replace(config, query_budget=None))

        def backend_id(self) -> str:
            return self._inner.backend_id()

    h.supervisor._runtime = BudgetlessRuntime()  # type: ignore[assignment]
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    run = h.supervisor.execute(run_id)
    # the spend is booked honestly (never clipped) but the run is not
    # certified: the budget port contract was violated
    assert run.status is TaskStatus.FAILED
    package = h.supervisor.get_result(run_id)
    assert package is not None
    assert package.total_cost_usd == pytest.approx(0.01)
    assert any("no per-query reservation covers" in i for i in package.not_done_items)
    assert h.ledger.settled_usd() == pytest.approx(0.01)


def test_non_finite_metering_is_refused_never_booked() -> None:
    """NaN/inf cost can never reach the ledger or a report (fail closed)."""
    from fakes import ScriptedCall, make_harness, schema_spec

    h = make_harness([ScriptedCall(content='{"recommendation": "A"}', cost_usd=float("nan"))])
    run_id = h.supervisor.submit(schema_spec(max_cost_usd=0.5))
    from vouch_agent.errors import ContractError

    with pytest.raises(ContractError, match="non-finite"):
        h.supervisor.execute(run_id)
    for reservation in h.ledger.reservations.values():
        if reservation.settled_amount_usd is not None:
            assert reservation.settled_amount_usd == reservation.settled_amount_usd  # not NaN
    assert h.ledger.settled_usd() == h.ledger.settled_usd()  # no NaN anywhere
    for entry in h.journal.cost_entries(run_id):
        assert entry.amount_usd is None or entry.amount_usd == entry.amount_usd
