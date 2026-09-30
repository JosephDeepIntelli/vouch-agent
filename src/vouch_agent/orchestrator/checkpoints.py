"""Durable run checkpoints and restart classification (design §10).

Recovery rule: persist intent, do the side-effecting work, persist the
result. After *every* persisted step the supervisor writes a recovery point
— a ``RECOVERY_POINT`` journal event plus a full ``TaskRun`` snapshot record
in the metadata store — enough to rebuild run state after a process restart.

``recover`` classifies a persisted run **without executing anything**:

* ``terminal`` — completed/failed/cancelled with every terminal write done.
* ``finalization-pending`` — the run reached a terminal decision but its
  terminal writes (result package, ledger settlement, metadata/journal
  finalization, ownership release) did not all complete: a crash during
  finalization. Terminal status alone is not proof the bookkeeping finished;
  the next client finishes it exactly once without replaying work.
* ``needs-reconciliation`` — a step has an unknown side-effect outcome
  (marked ``side_effect_unknown``/``unknown``, or a side-effecting step was
  still in flight when the process died). Never blindly replayed; resuming
  requires a recorded, human-verified reconciliation note.
* ``clean-resume`` — every persisted step has a known outcome (a crash at
  most left a deterministic supervisor step in flight, which is safely
  redone). Deterministic fixture runs may auto-continue.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import digest_of, new_id
from vouch_agent.contracts.journal import EventKind, EventRecord
from vouch_agent.contracts.tasks import RunStep, StepKind, TaskRun, TaskStatus
from vouch_agent.errors import ContractError
from vouch_agent.orchestrator.records import read_finalization
from vouch_agent.storage.interfaces import Journal, MetadataStore

CHECKPOINT_KIND = "checkpoint"
TASK_RUN_KIND = "task-run"

#: Step kinds whose interrupted outcome cannot be assumed known. A crash
#: while one of these is in flight leaves the run needing reconciliation.
#: (MODEL_CALL reaches the model backend; TOOL_CALL writes artifacts.)
SIDE_EFFECTING_STEP_KINDS = frozenset({StepKind.MODEL_CALL, StepKind.TOOL_CALL})

#: Statuses that mean a step was persisted mid-flight (intent saved, result
#: not yet).
STEP_STATUS_RUNNING = "running"
STEP_STATUS_OK = "ok"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_SKIPPED = "skipped"
STEP_STATUS_UNKNOWN = "unknown"

_TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class RecoveryClassification(StrEnum):
    CLEAN_RESUME = "clean-resume"
    NEEDS_RECONCILIATION = "needs-reconciliation"
    FINALIZATION_PENDING = "finalization-pending"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class RecoveryReport:
    """Classification of a persisted run after a (simulated) restart."""

    run_id: str
    classification: RecoveryClassification
    run: TaskRun
    detail: str
    unknown_steps: tuple[RunStep, ...] = ()
    in_flight_steps: tuple[RunStep, ...] = ()


def write_checkpoint(
    store: MetadataStore,
    journal: Journal,
    run: TaskRun,
    *,
    sequence: int,
    budget: dict[str, Any] | None = None,
) -> str:
    """Persist one recovery point: store snapshot + journal event."""
    record: dict[str, Any] = {
        "schemaVersion": "1",
        "runId": run.run_id,
        "sequence": sequence,
        "status": run.status.value,
        "stepCount": len(run.steps),
        "run": run.to_dict(),
        "budget": dict(budget or {}),
    }
    store.save(CHECKPOINT_KIND, run.run_id, record)
    journal.append(
        EventRecord(
            event_id=new_id("evt"),
            kind=EventKind.RECOVERY_POINT,
            subject=run.run_id,
            actor="supervisor",
            mode=run.mode,
            data={
                "sequence": sequence,
                "status": run.status.value,
                "stepCount": len(run.steps),
            },
        )
    )
    return digest_of(record)


def load_checkpoint(store: MetadataStore, run_id: str) -> dict[str, Any] | None:
    return store.load(CHECKPOINT_KIND, run_id)


def checkpoint_sequence(store: MetadataStore, run_id: str) -> int:
    record = load_checkpoint(store, run_id)
    if record is None:
        return 0
    return int(record.get("sequence", 0))


def _load_run(store: MetadataStore, run_id: str) -> TaskRun:
    """Rebuild TaskRun from persisted state.

    The supervisor always persists the task-run record BEFORE the checkpoint
    that snapshots it, so the task-run record is never staler than the last
    checkpoint — and when a process dies during a checkpoint write it is the
    fresher truth. Prefer it; fall back to the checkpoint's embedded run
    (e.g. for readers that only received checkpoints).
    """
    data = store.load(TASK_RUN_KIND, run_id)
    if data is not None:
        return TaskRun.from_dict(data)
    record = load_checkpoint(store, run_id)
    if record is not None and isinstance(record.get("run"), dict):
        return TaskRun.from_dict(record["run"])
    raise ContractError(f"unknown run {run_id!r}: no task-run record or checkpoint")


def _pending_finalization_writes(store: MetadataStore, run_id: str) -> tuple[str, ...]:
    """Terminal writes a crash left undone (empty when none/complete)."""
    intent = read_finalization(store, run_id)
    if intent is None:
        return ()
    return intent.pending_writes()


def recover(store: MetadataStore, run_id: str) -> RecoveryReport:
    """Classify a run for restart. Reads persisted state only; executes nothing."""
    run = _load_run(store, run_id)
    unknown = tuple(
        s for s in run.steps if s.side_effect_unknown or s.status == STEP_STATUS_UNKNOWN
    )
    in_flight = tuple(s for s in run.steps if s.status == STEP_STATUS_RUNNING)
    if run.status in _TERMINAL_STATUSES:
        pending = _pending_finalization_writes(store, run_id)
        if pending:
            return RecoveryReport(
                run_id=run_id,
                classification=RecoveryClassification.FINALIZATION_PENDING,
                run=run,
                detail=(
                    f"run is {run.status.value} but its terminal writes are incomplete "
                    f"({', '.join(pending)}); finish them exactly once — a terminal "
                    "status alone does not prove the bookkeeping completed"
                ),
                unknown_steps=unknown,
                in_flight_steps=in_flight,
            )
        return RecoveryReport(
            run_id=run_id,
            classification=RecoveryClassification.TERMINAL,
            run=run,
            detail=f"run is terminal ({run.status.value}) with {len(run.steps)} steps",
            unknown_steps=unknown,
            in_flight_steps=in_flight,
        )
    if _pending_finalization_writes(store, run_id):
        # The terminal decision was made (intent written) but the run record
        # was never transitioned: the same recovery applies.
        return RecoveryReport(
            run_id=run_id,
            classification=RecoveryClassification.FINALIZATION_PENDING,
            run=run,
            detail=(
                f"run is {run.status.value} with an unfinished finalization intent; "
                "finish its terminal writes exactly once — no work may be replayed"
            ),
            unknown_steps=unknown,
            in_flight_steps=in_flight,
        )
    if unknown:
        ids = [s.step_id for s in unknown]
        return RecoveryReport(
            run_id=run_id,
            classification=RecoveryClassification.NEEDS_RECONCILIATION,
            run=run,
            detail=(
                f"{len(unknown)} step(s) with unknown side-effect outcome: {ids}; "
                "verified reconciliation note required before resume, never replayed"
            ),
            unknown_steps=unknown,
            in_flight_steps=in_flight,
        )
    side_effecting = tuple(s for s in in_flight if s.kind in SIDE_EFFECTING_STEP_KINDS)
    if side_effecting:
        ids = [s.step_id for s in side_effecting]
        return RecoveryReport(
            run_id=run_id,
            classification=RecoveryClassification.NEEDS_RECONCILIATION,
            run=run,
            detail=(f"crash left side-effecting step(s) in flight with unknown outcome: {ids}"),
            unknown_steps=unknown,
            in_flight_steps=in_flight,
        )
    if in_flight:
        kinds = sorted({s.kind.value for s in in_flight})
        return RecoveryReport(
            run_id=run_id,
            classification=RecoveryClassification.CLEAN_RESUME,
            run=run,
            detail=(
                f"crash left deterministic step(s) in flight ({kinds}); "
                "safe to mark skipped and redo"
            ),
            unknown_steps=unknown,
            in_flight_steps=in_flight,
        )
    return RecoveryReport(
        run_id=run_id,
        classification=RecoveryClassification.CLEAN_RESUME,
        run=run,
        detail=f"status {run.status.value} with all {len(run.steps)} step outcomes known",
        unknown_steps=unknown,
        in_flight_steps=in_flight,
    )
