"""Vouch v1 contracts — typed, versioned, digest-bound data records.

Two families share one vocabulary (owner steering 2026-09-28):

* business task execution: TaskSpec / TaskRun / RunStep (``tasks``)
* controlled improvement: TaskPack / Candidate / EvaluationRun /
  AcceptanceDecision / ReleaseRecord / SkillEntry

Everything is a frozen dataclass with ``to_dict``/``from_dict`` carrying
``schemaVersion``, and digests computed over canonical JSON
(``vouch_agent.contracts.common.digest_of``).
"""

# ruff: noqa: RUF022 (grouped __all__ with section comments is intentional)

from vouch_agent.contracts.candidate import (
    CANDIDATE_TRANSITIONS,
    DIGEST_BINDING_STATES,
    AgentVersion,
    Candidate,
    CandidateState,
    ChangeType,
    transition_candidate_state,
)
from vouch_agent.contracts.cases import (
    SELECTION_SPLITS,
    CaseSplit,
    TaskCase,
    TaskPack,
)
from vouch_agent.contracts.common import (
    DIGEST_PREFIX,
    Role,
    RunMode,
    canonical_json,
    digest_bytes,
    digest_of,
    new_id,
    utc_now_iso,
    verify_digest,
)
from vouch_agent.contracts.decision import (
    AcceptanceDecision,
    ApprovalBinding,
    ReleaseRecord,
    Verdict,
)
from vouch_agent.contracts.evaluation import (
    USABLE_STATUSES,
    AttemptRecord,
    AttemptStatus,
    EvaluationRun,
    GuardrailFinding,
    PairedOutcome,
    Rubric,
    Side,
)
from vouch_agent.contracts.invocation import (
    Invocation,
    InvocationStatus,
)
from vouch_agent.contracts.journal import (
    BudgetReservation,
    CostCategory,
    CostEntry,
    EventKind,
    EventRecord,
    ReservationStatus,
)
from vouch_agent.contracts.materials import (
    MaterialKind,
    TaskAttachment,
)
from vouch_agent.contracts.project import (
    BudgetPolicy,
    ProjectSpec,
    WorkflowDeclaration,
)
from vouch_agent.contracts.skill import (
    SKILL_TRANSITIONS,
    EnvironmentSignature,
    ReuseRight,
    SkillEntry,
    SkillState,
)
from vouch_agent.contracts.tasks import (
    TASK_TRANSITIONS,
    ResultPackage,
    RunStep,
    StepKind,
    TaskRun,
    TaskSpec,
    TaskStatus,
    transition_task_status,
)

__all__ = [
    # common
    "DIGEST_PREFIX",
    "Role",
    "RunMode",
    "canonical_json",
    "digest_bytes",
    "digest_of",
    "new_id",
    "utc_now_iso",
    "verify_digest",
    # tasks (business execution)
    "ResultPackage",
    "TASK_TRANSITIONS",
    "RunStep",
    "StepKind",
    "TaskRun",
    "TaskSpec",
    "TaskStatus",
    "transition_task_status",
    # cases (improvement evaluation)
    "SELECTION_SPLITS",
    "CaseSplit",
    "TaskCase",
    "TaskPack",
    # project
    "BudgetPolicy",
    "ProjectSpec",
    "WorkflowDeclaration",
    # materials
    "MaterialKind",
    "TaskAttachment",
    # invocation tree
    "Invocation",
    "InvocationStatus",
    # candidate
    "AgentVersion",
    "CANDIDATE_TRANSITIONS",
    "Candidate",
    "CandidateState",
    "ChangeType",
    "DIGEST_BINDING_STATES",
    "transition_candidate_state",
    # evaluation
    "USABLE_STATUSES",
    "AttemptRecord",
    "AttemptStatus",
    "EvaluationRun",
    "GuardrailFinding",
    "PairedOutcome",
    "Rubric",
    "Side",
    # decision
    "AcceptanceDecision",
    "ApprovalBinding",
    "ReleaseRecord",
    "Verdict",
    # journal
    "BudgetReservation",
    "CostCategory",
    "CostEntry",
    "EventKind",
    "EventRecord",
    "ReservationStatus",
    # skill ledger
    "SKILL_TRANSITIONS",
    "EnvironmentSignature",
    "ReuseRight",
    "SkillEntry",
    "SkillState",
]
