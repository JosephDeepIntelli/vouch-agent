"""Orchestrator package: the Supervisor (bounded business task execution).

Public surface for the CLI/TUI and controller layers:

* :class:`Supervisor` — submit/execute/resume/cancel/pause + readers
* :class:`SupervisorPolicy` — trusted bounds and capability defaults
* :func:`recover` / :class:`RecoveryReport` — restart classification
  (clean-resume / needs-reconciliation / terminal), executes nothing
* :func:`write_checkpoint` — durable recovery points (used by the supervisor)
* condition checkers (:mod:`vouch_agent.orchestrator.conditions`) — the
  deterministic success-criteria evaluation behind ResultPackage honesty

The supervisor talks only to the runtime port
(:mod:`vouch_agent.runtime.ports`) and the storage ports
(:mod:`vouch_agent.storage.interfaces`); the JAZ-backed runtime engine and
the SQLite stores are wired in at integration time.
"""

from vouch_agent.orchestrator.checkpoints import (
    CHECKPOINT_KIND,
    RecoveryClassification,
    RecoveryReport,
    checkpoint_sequence,
    load_checkpoint,
    recover,
    write_checkpoint,
)
from vouch_agent.orchestrator.conditions import (
    CONDITION_ARTIFACT_SCHEMA,
    CONDITION_MAX_COST_USD,
    CONDITION_NONE,
    CONDITION_OUTPUT_CONTAINS,
    ConditionContext,
    ConditionEvaluation,
    evaluate_conditions,
    normalize_criteria,
)
from vouch_agent.orchestrator.records import (
    KIND_RUN_CLOCK,
    KIND_RUN_FINALIZATION,
    KIND_RUN_OWNERSHIP,
    FinalizationIntent,
    OwnershipRecord,
    RunOwnershipError,
)
from vouch_agent.orchestrator.supervisor import (
    KIND_BUDGET_SLICE,
    KIND_CANCEL_REQUEST,
    KIND_INVOCATION,
    KIND_PAUSE_REQUEST,
    KIND_RECONCILIATION_NOTE,
    KIND_RESULT_PACKAGE,
    KIND_RUN_INDEX,
    KIND_RUN_RESERVATION,
    KIND_TASK_RUN,
    KIND_TASK_SPEC,
    Supervisor,
    SupervisorPolicy,
)

__all__ = [
    "CHECKPOINT_KIND",
    "CONDITION_ARTIFACT_SCHEMA",
    "CONDITION_MAX_COST_USD",
    "CONDITION_NONE",
    "CONDITION_OUTPUT_CONTAINS",
    "KIND_BUDGET_SLICE",
    "KIND_CANCEL_REQUEST",
    "KIND_INVOCATION",
    "KIND_PAUSE_REQUEST",
    "KIND_RECONCILIATION_NOTE",
    "KIND_RESULT_PACKAGE",
    "KIND_RUN_CLOCK",
    "KIND_RUN_FINALIZATION",
    "KIND_RUN_INDEX",
    "KIND_RUN_OWNERSHIP",
    "KIND_RUN_RESERVATION",
    "KIND_TASK_RUN",
    "KIND_TASK_SPEC",
    "ConditionContext",
    "ConditionEvaluation",
    "FinalizationIntent",
    "OwnershipRecord",
    "RecoveryClassification",
    "RecoveryReport",
    "RunOwnershipError",
    "Supervisor",
    "SupervisorPolicy",
    "checkpoint_sequence",
    "evaluate_conditions",
    "load_checkpoint",
    "normalize_criteria",
    "recover",
    "write_checkpoint",
]
