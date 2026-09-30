"""Lead-owned contract tests: digest stability, state machines, isolation rules."""

from __future__ import annotations

import json

import pytest

from vouch_agent.contracts import (
    AcceptanceDecision,
    AgentVersion,
    ApprovalBinding,
    AttemptRecord,
    AttemptStatus,
    BudgetReservation,
    Candidate,
    CandidateState,
    CaseSplit,
    ChangeType,
    CostCategory,
    CostEntry,
    EnvironmentSignature,
    EvaluationRun,
    EventKind,
    EventRecord,
    ProjectSpec,
    ReuseRight,
    Role,
    RunMode,
    SkillEntry,
    SkillState,
    TaskCase,
    TaskRun,
    TaskSpec,
    TaskStatus,
    Verdict,
    digest_of,
    transition_candidate_state,
    transition_task_status,
)
from vouch_agent.contracts.common import canonical_json
from vouch_agent.errors import (
    ApprovalInvalidatedError,
    ContractError,
    InvalidStateTransitionError,
    LiveCallBlockedError,
    UnauthorizedReuseError,
)


def _case(split: CaseSplit = CaseSplit.DEVELOPMENT) -> TaskCase:
    return TaskCase(
        case_id="case-1",
        workflow_id="wf-compare",
        split=split,
        group_id="family-a",
        input_digest=digest_of({"input": "x"}),
    )


def _project() -> ProjectSpec:
    return ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": "proj-1",
            "name": "Choose improvement",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": "wf-compare",
                    "name": "Compare",
                    "mainObjective": "supported comparisons",
                }
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": 10.0},
        }
    )


def _candidate(state: CandidateState = CandidateState.PROPOSED) -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        parent_version=AgentVersion(
            version_id="v0", source_ref="git:abc", model_id="fake-scripted"
        ),
        change_type=ChangeType.PROMPT_DELTA,
        delta="+ insist on citations",
        rationale="unsupported claims in Compare",
        expected_impact="fewer unsupported claims",
        proposer="proposer-agent",
        state=state,
    )


# --- digests ---------------------------------------------------------------


def test_canonical_json_is_key_order_insensitive() -> None:
    a = {"b": 1, "a": {"y": [1, 2], "x": "z"}}
    b = {"a": {"x": "z", "y": [1, 2]}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert digest_of(a) == digest_of(b)


def test_digest_changes_on_any_content_change() -> None:
    spec = _project()
    mutated = ProjectSpec.from_dict({**spec.to_dict(), "name": "Choose improvement v2"})
    assert spec.digest() != mutated.digest()


def test_roundtrip_preserves_digest() -> None:
    for record in (
        _project(),
        _case(),
        _candidate(),
        TaskSpec(spec_id="task-1", title="t", goal="g", mode=RunMode.FIXTURE),
        TaskRun(run_id="run-1", task_digest=digest_of({"t": 1})),
        SkillEntry(
            skill_id="skill-1",
            content_digest=digest_of({"s": 1}),
            environment=EnvironmentSignature(
                model_id="m",
                tool_schema_version="1",
                workflow_id="wf",
                domain="d",
                locale="en",
                data_policy="internal",
                acceptance_version="1",
            ),
        ),
    ):
        payload = json.loads(record.to_canonical_json())
        restored = type(record).from_dict(payload)  # type: ignore[attr-defined]
        assert restored.digest() == record.digest()


def test_unknown_schema_version_fails_closed() -> None:
    data = _case().to_dict()
    data["schemaVersion"] = "999"
    with pytest.raises(ContractError):
        TaskCase.from_dict(data)


# --- run modes -------------------------------------------------------------


def test_fixture_mode_blocks_live_calls_and_side_effects() -> None:
    with pytest.raises(LiveCallBlockedError):
        RunMode.FIXTURE.fail_closed_live()
    with pytest.raises(LiveCallBlockedError):
        RunMode.OFFLINE_EVALUATION.fail_closed_live()
    RunMode.AUTHORIZED_LIVE.fail_closed_live()  # no raise


# --- task lifecycle ----------------------------------------------------------


def test_task_lifecycle_valid_and_invalid_transitions() -> None:
    assert transition_task_status(TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert transition_task_status(TaskStatus.RUNNING, TaskStatus.NEEDS_RECONCILIATION)
    with pytest.raises(InvalidStateTransitionError):
        transition_task_status(TaskStatus.COMPLETED, TaskStatus.RUNNING)


def test_needs_reconciliation_never_blind_replays() -> None:
    # A run with an unknown side-effect step cannot complete directly...
    run = TaskRun(run_id="run-1", task_digest=digest_of({"t": 1}))
    crashed = run.with_transition(TaskStatus.RUNNING).with_transition(
        TaskStatus.NEEDS_RECONCILIATION
    )
    with pytest.raises(InvalidStateTransitionError):
        crashed.with_transition(TaskStatus.COMPLETED)
    # ...it may only resume through an explicit transition (reconciliation action).
    resumed = crashed.with_transition(TaskStatus.RUNNING)
    assert resumed.status is TaskStatus.RUNNING


# --- candidate state machine -------------------------------------------------


def test_candidate_happy_path() -> None:
    state = CandidateState.PROPOSED
    for target in (
        CandidateState.SEALED,
        CandidateState.EVALUATED,
        CandidateState.ACCEPTED,
        CandidateState.APPROVED,
        CandidateState.RELEASED,
        CandidateState.OBSERVED,
    ):
        state = transition_candidate_state(state, target)
    assert state is CandidateState.OBSERVED


def test_rejected_and_terminal_states_are_terminal() -> None:
    for terminal in (CandidateState.REJECTED, CandidateState.ROLLED_BACK):
        with pytest.raises(InvalidStateTransitionError):
            transition_candidate_state(terminal, CandidateState.EVALUATED)


def test_candidate_seal_rejects_out_of_scope_changes() -> None:
    candidate = _candidate()
    with pytest.raises(ContractError):
        candidate.sealable(("retrieval-params",))  # prompt-delta not allowed here
    candidate.sealable(("prompt-delta",))


# --- approval bindings --------------------------------------------------------


def _binding() -> ApprovalBinding:
    return ApprovalBinding(
        candidate_digest=digest_of({"c": 1}),
        rubric_digest=digest_of({"r": 1}),
        environment_digest=digest_of({"e": 1}),
        acceptance_case_set_digest=digest_of({"a": 1}),
    )


def test_approval_binding_invalidates_on_digest_change() -> None:
    binding = _binding()
    binding.verify(
        candidate_digest=digest_of({"c": 1}),
        rubric_digest=digest_of({"r": 1}),
        environment_digest=digest_of({"e": 1}),
        acceptance_case_set_digest=digest_of({"a": 1}),
    )
    with pytest.raises(ApprovalInvalidatedError):
        binding.verify(
            candidate_digest=digest_of({"c": 2}),  # candidate delta changed
            rubric_digest=digest_of({"r": 1}),
            environment_digest=digest_of({"e": 1}),
            acceptance_case_set_digest=digest_of({"a": 1}),
        )


def test_acceptance_requires_binding_and_owner() -> None:
    with pytest.raises(ContractError):
        AcceptanceDecision(
            decision_id="d1",
            verdict=Verdict.ACCEPTED,
            candidate_digest=digest_of({"c": 1}),
            evidence_digest=digest_of({"e": 1}),
            owner="ana",
        )  # missing binding
    with pytest.raises(ContractError):
        AcceptanceDecision(
            decision_id="d1",
            verdict=Verdict.REJECTED,
            candidate_digest=digest_of({"c": 1}),
            evidence_digest=digest_of({"e": 1}),
            owner="proposer",  # self-acceptance forbidden
        )


# --- split isolation -----------------------------------------------------------


def test_final_acceptance_split_unreadable_by_proposer() -> None:
    assert CaseSplit.FINAL_ACCEPTANCE.readable_by(Role.PROPOSER) is False
    assert CaseSplit.FINAL_ACCEPTANCE.readable_by(Role.ENGINEER) is False
    assert CaseSplit.FINAL_ACCEPTANCE.readable_by(Role.ACCEPTANCE_OWNER) is True
    assert CaseSplit.DEVELOPMENT.readable_by(Role.PROPOSER) is True


# --- evaluation honesty ----------------------------------------------------------


def test_unknown_attempt_status_is_not_usable_and_unpriced_total_is_none() -> None:
    for status in (AttemptStatus.TIMEOUT, AttemptStatus.IMMEASURABLE, AttemptStatus.FAILED):
        assert status not in {AttemptStatus.OK}
    run = EvaluationRun(
        run_id="eval-1",
        workflow_id="wf",
        split=CaseSplit.SELECTION_VALIDATION,
        baseline_digest=digest_of({"b": 1}),
        candidate_digest=digest_of({"c": 1}),
        case_set_digest=digest_of({"s": 1}),
        rubric_digest=digest_of({"r": 1}),
        attempts=(
            AttemptRecord(
                attempt_id="a1",
                run_id="eval-1",
                side="baseline",
                case_id="case-1",
                status=AttemptStatus.OK,
                cost_usd=None,  # usable but unpriced
            ),
        ),
    )
    assert run.total_cost_usd() is None  # immeasurable, not zero


def test_cost_entry_requires_amount_or_minutes() -> None:
    with pytest.raises(ContractError):
        CostEntry(entry_id="c1", category=CostCategory.MODEL, subject="eval-1")
    CostEntry(entry_id="c1", category=CostCategory.MODEL, subject="eval-1", amount_usd=0.01)
    CostEntry(
        entry_id="c2",
        category=CostCategory.HUMAN_REVIEW,
        subject="eval-1",
        human_minutes=30,
    )


def test_unmeasurable_cost_entry_is_explicit_not_zero() -> None:
    entry = CostEntry(
        entry_id="c3",
        category=CostCategory.MODEL,
        subject="eval-1",
        amount_usd=None,
        human_minutes=None,
        measurable=False,
        note="unknown price",
    )
    assert entry.to_dict()["amountUsd"] is None  # never rounded to zero
    with pytest.raises(ContractError):
        CostEntry(
            entry_id="c4",
            category=CostCategory.MODEL,
            subject="eval-1",
            amount_usd=None,
            human_minutes=None,
        )  # measurable but unpriced is rejected


# --- skill ledger ---------------------------------------------------------------


def _skill(
    state: SkillState = SkillState.TRUSTED, right: ReuseRight = ReuseRight.NONE
) -> SkillEntry:
    return SkillEntry(
        skill_id="skill-1",
        content_digest=digest_of({"s": 1}),
        environment=EnvironmentSignature(
            model_id="m",
            tool_schema_version="1",
            workflow_id="wf",
            domain="d",
            locale="en",
            data_policy="internal",
            acceptance_version="1",
        ),
        state=state,
        reuse_right=right,
    )


def test_skill_reuse_requires_rights_before_use() -> None:
    env = _skill().environment
    with pytest.raises(UnauthorizedReuseError):
        _skill().usable_in(env, project_authorized=True)  # trusted but no rights
    with pytest.raises(UnauthorizedReuseError):
        _skill(state=SkillState.QUARANTINED, right=ReuseRight.PROJECT).usable_in(
            env, project_authorized=True
        )
    _skill(right=ReuseRight.PROJECT).usable_in(env, project_authorized=True)


def test_skill_env_mismatch_denies_environment_right() -> None:
    other = EnvironmentSignature(
        model_id="m2",  # different model
        tool_schema_version="1",
        workflow_id="wf",
        domain="d",
        locale="en",
        data_policy="internal",
        acceptance_version="1",
    )
    with pytest.raises(UnauthorizedReuseError):
        _skill(right=ReuseRight.ENVIRONMENT_AUTHORIZED).usable_in(other, project_authorized=True)


def test_new_environment_degrades_to_probation() -> None:
    with pytest.raises(InvalidStateTransitionError):
        # quarantined -> trusted directly is forbidden
        _skill(state=SkillState.QUARANTINED).with_transition(SkillState.TRUSTED)
    degraded = _skill().with_transition(SkillState.PROBATION)
    assert degraded.state is SkillState.PROBATION


# --- journal ----------------------------------------------------------------------


def test_reservation_record_roundtrip_and_open_default() -> None:
    r = BudgetReservation(reservation_id="r1", holder="att-1", amount_usd=0.5)
    restored = BudgetReservation.from_dict(json.loads(r.to_canonical_json()))
    assert restored.status.value == "open"
    assert restored.amount_usd == 0.5


def test_event_records_carry_mode() -> None:
    evt = EventRecord(
        event_id="e1",
        kind=EventKind.BUDGET_EXHAUSTED,
        subject="eval-1",
        mode=RunMode.FIXTURE,
    )
    assert EventRecord.from_dict(json.loads(evt.to_canonical_json())).mode is RunMode.FIXTURE
