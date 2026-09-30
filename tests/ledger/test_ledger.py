"""Skill ledger behavior: probation/trust/quarantine, rights, drift, health."""

from __future__ import annotations

import threading

from vouch_agent.contracts.journal import CostEntry, EventRecord
from vouch_agent.contracts.skill import EnvironmentSignature, ReuseRight, SkillState
from vouch_agent.errors import ContractError, UnauthorizedReuseError
from vouch_agent.ledger import SkillLedger
from vouch_agent.ledger.store import UnauthorizedReuseGuard


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}
        self.lock = threading.Lock()

    def save(self, kind: str, record_id: str, data: dict) -> None:
        with self.lock:
            self.data[(kind, record_id)] = dict(data)

    def load(self, kind: str, record_id: str) -> dict | None:
        with self.lock:
            entry = self.data.get((kind, record_id))
            return dict(entry) if entry else None

    def list_ids(self, kind: str) -> list[str]:
        with self.lock:
            return [rid for (k, rid) in self.data if k == kind]

    def transaction(self):  # pragma: no cover
        return threading.Lock()


class FakeJournal:
    def __init__(self) -> None:
        self._events: list[EventRecord] = []

    def append(self, event: EventRecord) -> None:
        self._events.append(event)

    def append_cost(self, entry: CostEntry) -> None:  # pragma: no cover
        pass

    def events(self, subject: str | None = None) -> list[EventRecord]:
        return [e for e in self._events if subject is None or e.subject == subject]

    def cost_entries(self, subject: str | None = None) -> list[CostEntry]:  # pragma: no cover
        return []


def env(model: str = "m1", locale: str = "en") -> EnvironmentSignature:
    return EnvironmentSignature(
        model_id=model,
        tool_schema_version="1",
        workflow_id="wf-compare",
        domain="buying-research",
        locale=locale,
        data_policy="internal",
        acceptance_version="1",
    )


def make_ledger() -> tuple[SkillLedger, FakeJournal]:
    store, journal = FakeStore(), FakeJournal()
    return SkillLedger(store, journal), journal


def probation_skill(ledger: SkillLedger) -> str:
    entry = ledger.distill(content="prefer cited sources", environment=env())
    moved = ledger.start_probation(
        entry.skill_id, verification_refs=("sha256:" + "a" * 64,), reuse_right=ReuseRight.PROJECT
    )
    assert moved.state is SkillState.PROBATION
    return entry.skill_id


# --- lifecycle -------------------------------------------------------------


def test_distilled_has_no_rights_until_probation_with_evidence() -> None:
    ledger, _ = make_ledger()
    entry = ledger.distill(content="x", environment=env())
    assert entry.state is SkillState.DISTILLED
    assert entry.reuse_right is ReuseRight.NONE
    try:
        ledger.acquire(
            entry.skill_id,
            env(),
            project_id="proj-1",
            authorized_projects=frozenset({"proj-1"}),
            role="engineer",
        )
        raise AssertionError("distilled skill must not be acquirable")
    except UnauthorizedReuseError:
        pass
    # probation without evidence is refused
    try:
        ledger.start_probation(entry.skill_id, verification_refs=())
        raise AssertionError("empty verification refs must be refused")
    except ContractError:
        pass


def test_probation_to_trusted_requires_independent_verification() -> None:
    ledger, _ = make_ledger()
    skill_id = probation_skill(ledger)
    promoted = ledger.promote(skill_id, verification_ref="sha256:" + "b" * 64)
    assert promoted.state is SkillState.TRUSTED
    # re-promoting a trusted skill is refused
    try:
        ledger.promote(skill_id, verification_ref="sha256:" + "c" * 64)
        raise AssertionError("double promotion must be refused")
    except ContractError:
        pass


def test_harmful_outcome_quarantines_and_keeps_counts() -> None:
    ledger, _ = make_ledger()
    skill_id = probation_skill(ledger)
    updated = ledger.record_outcome(skill_id, outcome="helpful", environment=env())
    assert updated.helpful == 1
    quarantined = ledger.record_outcome(skill_id, outcome="harmful", environment=env())
    assert quarantined.state is SkillState.QUARANTINED
    assert quarantined.harmful == 1
    # quarantined entries are not acquirable even with rights on paper
    try:
        ledger.acquire(
            skill_id,
            env(),
            project_id="proj-1",
            authorized_projects=frozenset({"proj-1"}),
            role="engineer",
        )
        raise AssertionError("quarantined skill must not be acquirable")
    except UnauthorizedReuseError:
        pass


def test_environment_drift_degrades_trust_to_probation() -> None:
    ledger, _ = make_ledger()
    skill_id = probation_skill(ledger)
    ledger.promote(skill_id, verification_ref="sha256:" + "b" * 64)
    drifted = ledger.record_outcome(skill_id, outcome="unknown", environment=env(model="m2"))
    assert drifted.state is SkillState.PROBATION


def test_project_scope_blocks_unauthorized_cross_project_reuse() -> None:
    ledger, _ = make_ledger()
    skill_id = probation_skill(ledger)
    try:
        ledger.acquire(
            skill_id,
            env(),
            project_id="proj-other",
            authorized_projects=frozenset({"proj-1"}),
            role="engineer",
        )
        raise AssertionError("cross-project reuse without authorization must fail")
    except UnauthorizedReuseError:
        pass
    granted = ledger.acquire(
        skill_id,
        env(),
        project_id="proj-1",
        authorized_projects=frozenset({"proj-1"}),
        role="engineer",
    )
    assert granted.skill_id == skill_id


def test_environment_authorized_right_requires_signature_match() -> None:
    ledger, _ = make_ledger()
    entry = ledger.distill(content="x", environment=env())
    ledger.start_probation(
        entry.skill_id,
        verification_refs=("sha256:" + "a" * 64,),
        reuse_right=ReuseRight.ENVIRONMENT_AUTHORIZED,
    )
    try:
        ledger.acquire(
            entry.skill_id,
            env(locale="zh"),  # different locale in signature
            project_id="proj-1",
            authorized_projects=frozenset({"proj-1"}),
            role="engineer",
        )
        raise AssertionError("environment mismatch must block environment-authorized reuse")
    except UnauthorizedReuseError:
        pass


def test_static_cross_project_asset_guard() -> None:
    UnauthorizedReuseGuard.assert_project_may_read("proj-1", "proj-1", frozenset())
    UnauthorizedReuseGuard.assert_project_may_read("proj-1", "proj-2", frozenset({"proj-2"}))
    try:
        UnauthorizedReuseGuard.assert_project_may_read("proj-1", "proj-2", frozenset())
        raise AssertionError("unauthorized cross-project read must fail")
    except UnauthorizedReuseError:
        pass


def test_health_reports_rates_not_just_hits() -> None:
    ledger, _ = make_ledger()
    skill_id = probation_skill(ledger)
    ledger.record_outcome(skill_id, outcome="helpful", environment=env())
    ledger.record_outcome(skill_id, outcome="helpful", environment=env())
    ledger.record_outcome(skill_id, outcome="unknown", environment=env())
    health = ledger.health()
    assert health["uses"] == 3
    assert health["helpfulRate"] == round(2 / 3, 4)
    assert health["unknownRate"] == round(1 / 3, 4)
