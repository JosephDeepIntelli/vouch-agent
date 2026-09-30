"""A1 regressions (gate): authorization is broker-owned state, claimed before
the executor runs.

Converted from the coordinator reproduction ``reproduce_broker.py``
(review-runtime-20260928, defect 3), with its assertions inverted: every
variant below must be REFUSED *before* the sentinel effect runs. All effects
are local Python list appends; nothing leaves the process.
"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest
from fakes import FakeBudgetLedger, FakeJournal

from vouch_agent.contracts.common import RunMode
from vouch_agent.contracts.journal import ReservationStatus
from vouch_agent.errors import GateDeniedError, ReservationError
from vouch_agent.gate.broker import AuthorizationStatus, CapabilityBroker, GatePolicy
from vouch_agent.gate.proposal import ActionProposal, RiskClass


def make_broker(journal: FakeJournal | None = None):
    ledger = FakeBudgetLedger(2.0)
    reservation = ledger.reserve("review-local", 0.1)
    broker = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"write-approved"}),
            max_risk_class=RiskClass.R2,
            allowed_resources=frozenset({"approved"}),
        ),
        ledger,
        journal if journal is not None else FakeJournal(),
        lambda rid: ledger.reservations[rid].status,
    )
    proposal = ActionProposal(
        "write-approved", {"target": "approved"}, RiskClass.R2, resource_ids=("approved",)
    )
    authorization = broker.authorize(
        proposal, mode=RunMode.AUTHORIZED_LIVE, reservation_id=reservation.reservation_id
    )
    return broker, proposal, authorization, ledger


def test_live_authorized_token_never_executes_in_fixture_mode() -> None:
    """A live-authorized R2 action must not run through a fixture-mode call."""
    broker, proposal, authorization, _ = make_broker()
    effects: list[str] = []

    with pytest.raises(GateDeniedError, match="mode"):
        broker.execute(
            authorization,
            lambda: effects.append("LOCAL_SENTINEL_EXECUTED"),
            proposal=proposal,
            mode=RunMode.FIXTURE,
        )
    assert effects == []  # refused BEFORE the executor ran


def test_caller_modified_token_fields_cannot_swap_the_proposal() -> None:
    """Replacing fields of the token (same id) must not authorize an
    un-allowlisted R3 proposal, downgrade the risk or drop the reservation."""
    broker, _proposal, authorization, _ = make_broker()
    unapproved = ActionProposal(
        "not-allowlisted", {"target": "not-allowed"}, RiskClass.R3, resource_ids=("x",)
    )
    modified = replace(
        authorization,
        proposal_digest=unapproved.arguments_digest(),
        risk_class=RiskClass.R0,
        reservation_id=None,
    )
    effects: list[str] = []

    with pytest.raises(GateDeniedError, match="no longer match"):
        broker.execute(
            modified,
            lambda: effects.append("LOCAL_SENTINEL_EXECUTED"),
            proposal=unapproved,
            mode=RunMode.FIXTURE,
        )
    assert effects == []


def test_resource_scope_is_bound_by_the_full_proposal_digest() -> None:
    """Same action/arguments, different resource scope: the digest binds it."""
    broker, _proposal, authorization, _ = make_broker()
    effects: list[str] = []
    foreign_resource = ActionProposal(
        "write-approved", {"target": "approved"}, RiskClass.R2, resource_ids=("someone-elses",)
    )
    with pytest.raises(GateDeniedError, match="no longer match"):
        broker.execute(
            authorization,
            lambda: effects.append("ran"),
            proposal=foreign_resource,
            mode=RunMode.AUTHORIZED_LIVE,
        )
    assert effects == []


def test_reentrant_use_of_the_same_token_runs_the_effect_once() -> None:
    """The token is claimed BEFORE the executor: re-entry from inside it fails."""
    broker, proposal, authorization, _ = make_broker()
    effects: list[str] = []

    def reentrant_executor() -> str:
        effects.append("outer")
        try:
            broker.execute(
                authorization,
                lambda: effects.append("inner"),
                proposal=proposal,
                mode=RunMode.AUTHORIZED_LIVE,
            )
        except GateDeniedError:
            return "outer-done"  # the re-entry was refused — exactly what we want
        raise AssertionError("re-entry must be refused")

    result = broker.execute(
        authorization, reentrant_executor, proposal=proposal, mode=RunMode.AUTHORIZED_LIVE
    )
    assert result == "outer-done"
    assert effects == ["outer"]  # the inner execution never ran
    # the legitimate outer execution consumed the token exactly once
    assert broker.authorization_status(authorization.authorization_id) is (
        AuthorizationStatus.CONSUMED
    )


def test_concurrent_threads_cannot_double_execute_one_token() -> None:
    """Two threads racing the same authorization: exactly one effect runs."""
    broker, proposal, authorization, _ = make_broker()
    effects: list[str] = []
    start = threading.Barrier(2)
    failures: list[BaseException] = []

    def worker() -> None:
        try:
            start.wait(timeout=5)
            broker.execute(
                authorization,
                lambda: effects.append("LOCAL_SENTINEL_EXECUTED"),
                proposal=proposal,
                mode=RunMode.AUTHORIZED_LIVE,
            )
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert effects == ["LOCAL_SENTINEL_EXECUTED"]
    assert len(failures) == 1
    assert isinstance(failures[0], GateDeniedError)


def test_crashed_executor_leaves_the_token_dead_for_a_restarted_broker() -> None:
    """A crash mid-execution persists an in-flight/unknown outcome: a broker
    rebuilt over the same journal can never reuse the token."""
    journal = FakeJournal()
    broker, proposal, authorization, _ = make_broker(journal)

    def crashing_executor() -> str:
        raise RuntimeError("process died mid-effect")

    with pytest.raises(RuntimeError):
        broker.execute(
            authorization, crashing_executor, proposal=proposal, mode=RunMode.AUTHORIZED_LIVE
        )
    assert broker.authorization_status(authorization.authorization_id) is (
        AuthorizationStatus.UNKNOWN
    )

    # a restarted broker instance over the same durable journal
    restarted = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"write-approved"}),
            max_risk_class=RiskClass.R2,
            allowed_resources=frozenset({"approved"}),
        ),
        FakeBudgetLedger(2.0),
        journal,
        lambda rid: (
            FakeBudgetLedger(2.0).reservations[rid].status
            if rid in FakeBudgetLedger(2.0).reservations
            else ReservationStatus.OPEN
        ),
    )
    effects: list[str] = []
    with pytest.raises(GateDeniedError, match="unknown"):
        restarted.execute(
            authorization,
            lambda: effects.append("LOCAL_SENTINEL_EXECUTED"),
            proposal=proposal,
            mode=RunMode.AUTHORIZED_LIVE,
        )
    assert effects == []


def test_legitimate_single_execution_still_works() -> None:
    broker, proposal, authorization, _ = make_broker()
    result = broker.execute(
        authorization,
        lambda: "LOCAL_SENTINEL_EXECUTED",
        proposal=proposal,
        mode=RunMode.AUTHORIZED_LIVE,
    )
    assert result == "LOCAL_SENTINEL_EXECUTED"
    with pytest.raises(GateDeniedError, match="already consumed"):
        broker.execute(
            authorization, lambda: "again", proposal=proposal, mode=RunMode.AUTHORIZED_LIVE
        )


def test_policy_change_invalidates_authorization() -> None:
    journal = FakeJournal()
    ledger = FakeBudgetLedger(2.0)
    reservation = ledger.reserve("review-local", 0.1)
    policy = GatePolicy(
        allowed_actions=frozenset({"write-approved"}),
        max_risk_class=RiskClass.R2,
        allowed_resources=frozenset({"approved"}),
    )
    broker = CapabilityBroker(policy, ledger, journal, lambda rid: ledger.reservations[rid].status)
    proposal = ActionProposal(
        "write-approved", {"target": "approved"}, RiskClass.R2, resource_ids=("approved",)
    )
    authorization = broker.authorize(
        proposal, mode=RunMode.AUTHORIZED_LIVE, reservation_id=reservation.reservation_id
    )
    # the trusted policy object changes after authorization
    object.__setattr__(policy, "allowed_actions", frozenset({"write-approved", "extra-action"}))
    effects: list[str] = []
    with pytest.raises(GateDeniedError, match="policy changed"):
        broker.execute(
            authorization,
            lambda: effects.append("ran"),
            proposal=proposal,
            mode=RunMode.AUTHORIZED_LIVE,
        )
    assert effects == []


def test_reservation_change_invalidates_authorization() -> None:
    broker, proposal, authorization, ledger = make_broker()
    effects: list[str] = []
    # the backing reservation is settled externally before execution
    ledger.settle(next(iter(ledger.reservations)), 0.0)
    with pytest.raises(ReservationError, match="settled"):
        broker.execute(
            authorization,
            lambda: effects.append("ran"),
            proposal=proposal,
            mode=RunMode.AUTHORIZED_LIVE,
        )
    assert effects == []


def test_parameter_change_invalidates_authorization() -> None:
    broker, _proposal, authorization, _ = make_broker()
    effects: list[str] = []
    changed_args = ActionProposal(
        "write-approved",
        {"target": "approved", "extra": 1},
        RiskClass.R2,
        resource_ids=("approved",),
    )
    with pytest.raises(GateDeniedError, match="no longer match"):
        broker.execute(
            authorization,
            lambda: effects.append("ran"),
            proposal=changed_args,
            mode=RunMode.AUTHORIZED_LIVE,
        )
    assert effects == []


def test_unknown_authorization_id_is_refused() -> None:
    broker, proposal, authorization, _ = make_broker()
    forged = replace(authorization, authorization_id="auth_forged")
    effects: list[str] = []
    with pytest.raises(GateDeniedError, match="unknown authorization"):
        broker.execute(
            forged, lambda: effects.append("ran"), proposal=proposal, mode=RunMode.AUTHORIZED_LIVE
        )
    assert effects == []
