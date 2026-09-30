"""Gate A4 regressions — broker authorization durability across processes.

The reviewer proved: a fresh broker re-executed a durably-claimed token
(journal phase 'claimed' unrecognized), and a cached OPEN token executed
after another broker consumed it. The durable claim is a compare-and-swap
built on the journal's refuse-duplicate-event-ids invariant, so these tests
use the REAL SqliteJournal — an in-memory fake without that invariant proves
nothing.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import GateDeniedError
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass
from vouch_agent.gate.broker import _claim_event_id
from vouch_agent.gate.proposal import ActionProposal
from vouch_agent.storage import SqliteBudgetLedger, SqliteJournal

_POLICY = GatePolicy(
    allowed_actions=frozenset({"run-adapter-attempt"}),
    max_risk_class=RiskClass.R1,
    allowed_resources=frozenset({"case-1"}),
    max_reservation_usd=0.0,
)


def _proposal() -> ActionProposal:
    return ActionProposal(
        action="run-adapter-attempt",
        arguments={"caseId": "case-1"},
        risk_class=RiskClass.R1,
        resource_ids=("case-1",),
    )


def _brokers(tmp: Path):
    journal = SqliteJournal(tmp / "journal.sqlite")
    ledger = SqliteBudgetLedger(tmp / "budget.sqlite", total_usd_cap=1.0)
    first = CapabilityBroker(_POLICY, ledger, journal)
    fresh = lambda: CapabilityBroker(_POLICY, ledger, journal)  # noqa: E731
    return first, fresh


def test_unfinished_durable_claim_is_unresolved_never_open(tmp_path: Path) -> None:
    """Hard crash right after the durable claim: only the claim event exists.
    A FRESH broker must refuse to execute the token (never back to OPEN)."""
    broker, fresh = _brokers(tmp_path)
    auth = broker.authorize(_proposal(), mode=RunMode.FIXTURE)
    # 'die' between claim and settlement: write exactly the claim the broker
    # itself would have written (deterministic event id), then abandon it.
    record = broker._resolve(auth.authorization_id)
    assert record is not None
    broker._record_phase_event(
        record, "in-flight", event_id=_claim_event_id(auth.authorization_id)
    )
    effects: list[str] = []
    with pytest.raises(GateDeniedError):
        fresh().execute(auth, lambda: effects.append("effect"), proposal=_proposal())
    assert effects == []


def test_sequential_two_broker_reuse_refused(tmp_path: Path) -> None:
    """Broker A consumes the token; a fresh broker with the same durable
    journal must refuse — stale local state can never reauthorize."""
    broker, fresh = _brokers(tmp_path)
    auth = broker.authorize(_proposal(), mode=RunMode.FIXTURE)
    effects: list[str] = []
    broker.execute(auth, lambda: effects.append("first"), proposal=_proposal())
    with pytest.raises(GateDeniedError):
        fresh().execute(auth, lambda: effects.append("second"), proposal=_proposal())
    assert effects == ["first"]


def _contender(path: str, authorization: dict, out) -> None:
    tmp = Path(path)
    journal = SqliteJournal(tmp / "journal.sqlite")
    ledger = SqliteBudgetLedger(tmp / "budget.sqlite", total_usd_cap=1.0)
    broker = CapabilityBroker(_POLICY, ledger, journal)
    proposal = _proposal()
    from vouch_agent.gate.broker import Authorization

    auth = Authorization(
        authorization_id=authorization["authorization_id"],
        proposal_digest=authorization["proposal_digest"],
        policy_digest=authorization["policy_digest"],
        risk_class=RiskClass(authorization["risk_class"]),
        reservation_id=None,
    )
    import contextlib

    effects: list[str] = []
    with contextlib.suppress(GateDeniedError):
        broker.execute(auth, lambda: effects.append("proc"), proposal=proposal)
    out.put(len(effects))


def test_two_processes_claim_one_token_at_most_once(tmp_path: Path) -> None:
    """Two brokers in two PROCESSES execute the SAME authorization against
    the shared journal: the deterministic-claim CAS allows at most one."""
    broker, _ = _brokers(tmp_path)
    auth = broker.authorize(_proposal(), mode=RunMode.FIXTURE)
    payload = {
        "authorization_id": auth.authorization_id,
        "proposal_digest": auth.proposal_digest,
        "policy_digest": auth.policy_digest,
        "risk_class": auth.risk_class.value,
    }
    out: multiprocessing.Queue = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(target=_contender, args=(str(tmp_path), payload, out))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
    effects = [out.get() for _ in procs]
    assert sum(effects) <= 1, f"the durable claim CAS allowed {sum(effects)} effects"
