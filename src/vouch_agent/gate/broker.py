"""Capability broker — the only authorized path to real side effects (design §6).

MVP scope: R0-R2. R3 (release, payment, permission change, irreversible
delete) has NO capability token and is always denied; those stay with the
product's own human release process.

Authorization is resolved **by id from broker-owned trusted state** (review
A1): the caller's ``Authorization`` object is a handle, never a source of
authority. Every field the gate decided on — the full proposal digest
(action + normalized arguments + risk class + resources), the policy digest,
the run mode and the backing budget reservation — is read from the record the
broker itself stored at ``authorize()`` time, so a caller-side
``dataclasses.replace`` on the token changes nothing.

Execution is single-use and claimed *before* the executor runs:

* the claim (OPEN -> IN_FLIGHT) is journaled under a DETERMINISTIC event id
  (``gate-claim:<authorization_id>``) before the executor is invoked. The
  journal port's documented append-only invariant — duplicate event ids are
  refused — makes that append a durable compare-and-swap: two brokers (in
  two threads or two PROCESSES) cannot both claim the same token; the loser
  is refused. Within one broker the claim is additionally serialized by the
  broker lock, so re-entry from inside the executor and a concurrent second
  thread see a token that is no longer claimable;
* the persisted lifecycle uses ONE representation everywhere: journal phases
  are exactly the :class:`AuthorizationStatus` values (``open`` never
  appears as a phase; creation is the ``authorized`` event). A claim that
  was never completed — a hard process exit between claim and settlement —
  reconstructs as UNKNOWN on restart: unresolved, never OPEN by default;
* broker-local caches are never authoritative: every dispatch consults the
  durable journal state, so a token another broker consumed (or claimed and
  crashed on) is refused here too — a stale OPEN cache cannot authorize
  execution after another owner changed the lifecycle;
* a token authorized in one run mode never executes in another (a
  live-authorized R2 action cannot be driven through a fixture-mode call).

Honest limits: the cross-process claim CAS relies on the journal actually
enforcing the port's documented event-id uniqueness (``SqliteJournal`` does;
a journal fake that accepts duplicate ids voids this guarantee — see the
tests). Uncertain effects (executor crash) require reconciliation: the token
is dead and can never be re-authorized for the same effect by replay.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vouch_agent.contracts.common import RunMode, digest_of, new_id, utc_now_iso
from vouch_agent.contracts.journal import EventKind, EventRecord, ReservationStatus
from vouch_agent.errors import ContractError
from vouch_agent.gate.proposal import ActionProposal, RiskClass
from vouch_agent.storage.interfaces import BudgetLedger, Journal


@dataclass(frozen=True)
class GatePolicy:
    """Deterministic constraint set. The model can never edit this object."""

    allowed_actions: frozenset[str]
    # Highest risk class this policy may authorize. R3 is never authorizable.
    max_risk_class: RiskClass = RiskClass.R1
    # Resource scope: resource ids proposals may name (case ids, digests, paths).
    allowed_resources: frozenset[str] = frozenset()
    resource_prefixes: tuple[str, ...] = ()  # e.g. ("sha256:", "case-")
    max_reservation_usd: float = 1.0
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_actions", frozenset(self.allowed_actions))
        object.__setattr__(self, "allowed_resources", frozenset(self.allowed_resources))

    def digest(self) -> str:
        return digest_of(
            {
                "schemaVersion": self.schema_version,
                "allowedActions": sorted(self.allowed_actions),
                "maxRiskClass": self.max_risk_class.value,
                "allowedResources": sorted(self.allowed_resources),
                "resourcePrefixes": list(self.resource_prefixes),
                "maxReservationUsd": self.max_reservation_usd,
            }
        )

    def risk_rank(self, risk: RiskClass) -> int:
        return {"r0": 0, "r1": 1, "r2": 2, "r3": 3}[risk.value]

    def resource_allowed(self, resource_id: str) -> bool:
        if resource_id in self.allowed_resources:
            return True
        return any(resource_id.startswith(p) for p in self.resource_prefixes)


#: Risk classes that imply side effects beyond disposable scratch state.
SIDE_EFFECT_RISK = frozenset({RiskClass.R2, RiskClass.R3})


class AuthorizationStatus(StrEnum):
    """Lifecycle of one authorization as the broker itself records it."""

    OPEN = "open"
    IN_FLIGHT = "in-flight"
    CONSUMED = "consumed"
    #: The executor was invoked and never reported back (crash). The effect
    #: may or may not have happened; the token is dead either way.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Authorization:
    """A handle on one granted permission.

    Callers hold this object, but only ``authorization_id`` is ever honored:
    every other field is informational (a copy of what the broker stored) and
    a caller-side replacement of them changes nothing about execution.
    """

    authorization_id: str
    proposal_digest: str  # digest over action + arguments + risk + resources
    policy_digest: str
    risk_class: RiskClass
    reservation_id: str | None  # budget reservation backing this action
    created_at: str = field(default_factory=utc_now_iso)
    consumed_at: str | None = None
    mode: RunMode = RunMode.FIXTURE
    schema_version: str = "1"


@dataclass(frozen=True)
class _AuthorizationRecord:
    """Broker-owned trusted state for one authorization (never caller-supplied)."""

    authorization_id: str
    proposal_digest: str
    policy_digest: str
    risk_class: RiskClass
    reservation_id: str | None
    mode: RunMode
    status: AuthorizationStatus
    created_at: str
    status_at: str
    action: str

    def with_status(self, status: AuthorizationStatus) -> _AuthorizationRecord:
        return _AuthorizationRecord(
            authorization_id=self.authorization_id,
            proposal_digest=self.proposal_digest,
            policy_digest=self.policy_digest,
            risk_class=self.risk_class,
            reservation_id=self.reservation_id,
            mode=self.mode,
            status=status,
            created_at=self.created_at,
            status_at=utc_now_iso(),
            action=self.action,
        )

    def public(self) -> Authorization:
        return Authorization(
            authorization_id=self.authorization_id,
            proposal_digest=self.proposal_digest,
            policy_digest=self.policy_digest,
            risk_class=self.risk_class,
            reservation_id=self.reservation_id,
            created_at=self.created_at,
            consumed_at=self.status_at if self.status is AuthorizationStatus.CONSUMED else None,
            mode=self.mode,
        )


#: Deterministic event id for one authorization's claim: the journal's
#: documented refuse-duplicate-ids invariant turns this append into a
#: durable cross-process compare-and-swap (exactly one claim ever wins).
def _claim_event_id(authorization_id: str) -> str:
    return f"gate-claim:{authorization_id}"


def status_from_phases(phases: list[Any]) -> AuthorizationStatus | None:
    """ONE lifecycle interpretation for every persisted phase list (A4).

    * ``consumed`` present -> CONSUMED
    * ``in-flight`` present (a claim that never completed — including a hard
      process exit after claim) -> UNKNOWN, never OPEN by default
    * ``unknown`` present -> UNKNOWN
    * any UNRECOGNIZED phase (e.g. a legacy ``claimed`` row) -> UNKNOWN,
      fail closed rather than guessing OPEN
    * only ``authorized`` -> OPEN
    * nothing -> None (unknown authorization id)
    """
    seen = {str(phase) for phase in phases if phase is not None}
    if not seen:
        return None
    if AuthorizationStatus.CONSUMED.value in seen:
        return AuthorizationStatus.CONSUMED
    known_open_only = seen <= {"authorized"}
    if known_open_only:
        return AuthorizationStatus.OPEN
    # Everything else — an in-flight claim, an unknown close, or a phase this
    # build does not recognize — means the token is not provably open.
    return AuthorizationStatus.UNKNOWN


class CapabilityBroker:
    """Authorize -> execute. All denials are recorded and fail closed."""

    def __init__(
        self,
        policy: GatePolicy,
        ledger: BudgetLedger,
        journal: Journal,
        reservation_status: Callable[[str], ReservationStatus] | None = None,
    ) -> None:
        """``reservation_status`` resolves a reservation id to its status for
        execution-time re-validation (the controller wires this to persisted
        BudgetReservation records); when absent, reservation openness cannot
        be re-verified and R2 executions are refused (fail closed)."""
        self._policy = policy
        self._ledger = ledger
        self._journal = journal
        self._reservation_status = reservation_status
        self._records: dict[str, _AuthorizationRecord] = {}
        # Reentrant: _resolve() re-checks durable state while execute()
        # holds the lock for its check-and-claim sequence.
        self._lock = threading.RLock()

    @property
    def policy_digest(self) -> str:
        return self._policy.digest()

    def authorization_status(self, authorization_id: str) -> AuthorizationStatus | None:
        """Trusted read of one authorization's lifecycle state (by id).

        The DURABLE journal is authoritative: a broker-local OPEN cache can
        never report OPEN after another broker claimed or consumed the token
        (Gate A4). A local non-OPEN record (this broker's own claim or close)
        wins only because the journal has already recorded the same fact.
        """
        record = self._resolve(authorization_id)
        return None if record is None else record.status

    # --- authorize ---------------------------------------------------------------

    def authorize(
        self,
        proposal: ActionProposal,
        *,
        mode: RunMode,
        reservation_id: str | None = None,
    ) -> Authorization:
        denial: str | None = None
        if proposal.risk_class is RiskClass.R3:
            denial = "R3 actions have no capability token in the MVP; use the human release process"
        elif self._policy.risk_rank(proposal.risk_class) > self._policy.risk_rank(
            self._policy.max_risk_class
        ):
            denial = f"risk class {proposal.risk_class.value} exceeds policy maximum"
        elif proposal.action not in self._policy.allowed_actions:
            denial = f"action {proposal.action!r} not in policy allowlist"
        elif proposal.arguments_digest() != self._normalize(proposal):
            denial = "arguments failed normalization"
        elif not self._resources_allowed(proposal):
            denial = "proposal names resources outside the policy scope"
        elif proposal.risk_class in SIDE_EFFECT_RISK and not mode.allows_side_effects():
            denial = f"{mode.value} mode forbids side-effecting actions"
        elif reservation_id is None and self._policy.max_reservation_usd > 0:
            denial = "budget reservation required"

        if denial is not None:
            self._journal.append(
                EventRecord(
                    event_id=new_id("evt"),
                    kind=EventKind.GATE_DENIED,
                    subject=proposal.action,
                    data={"reason": denial, "riskClass": proposal.risk_class.value},
                    mode=mode,
                )
            )
            from vouch_agent.errors import GateDeniedError

            raise GateDeniedError(denial)

        record = _AuthorizationRecord(
            authorization_id=new_id("auth"),
            proposal_digest=proposal.proposal_digest(),
            policy_digest=self._policy.digest(),
            risk_class=proposal.risk_class,
            reservation_id=reservation_id,
            mode=mode,
            status=AuthorizationStatus.OPEN,
            created_at=utc_now_iso(),
            status_at=utc_now_iso(),
            action=proposal.action,
        )
        with self._lock:
            self._records[record.authorization_id] = record
        self._journal.append(
            EventRecord(
                event_id=new_id("evt"),
                kind=EventKind.GATE_DECISION,
                subject=record.authorization_id,
                data={
                    "phase": "authorized",
                    "action": proposal.action,
                    "riskClass": proposal.risk_class.value,
                    "proposalDigest": record.proposal_digest,
                    "policyDigest": record.policy_digest,
                    "reservationId": reservation_id,
                    "mode": mode.value,
                },
                mode=mode,
            )
        )
        return record.public()

    # --- execute -----------------------------------------------------------------

    def execute(
        self,
        authorization: Authorization,
        executor: Callable[[], Any],
        *,
        proposal: ActionProposal,
        mode: RunMode = RunMode.FIXTURE,
    ) -> Any:
        """Run ``executor`` once, after every binding re-checks cleanly.

        ``proposal`` is REQUIRED: the concrete effect proposal is re-normalized
        and compared against the *stored* full proposal digest — an approval
        granted for one parameter set, risk class or resource scope cannot be
        replayed onto another (post-approval parameter swap). ``mode`` must
        equal the mode the authorization was granted in.
        """
        from vouch_agent.errors import GateDeniedError, ReservationError

        with self._lock:
            record = self._resolve(authorization.authorization_id)
            if record is None:
                raise GateDeniedError("unknown authorization; authorize through the broker")
            if record.status is AuthorizationStatus.CONSUMED:
                raise GateDeniedError(
                    "authorization already consumed; each authorization executes at most once"
                )
            if record.status is AuthorizationStatus.IN_FLIGHT:
                raise GateDeniedError(
                    "authorization is already in flight (re-entrant or concurrent use); "
                    "each authorization executes at most once"
                )
            if record.status is AuthorizationStatus.UNKNOWN:
                raise GateDeniedError(
                    "authorization outcome is unknown (the executor never reported "
                    "back); the effect may or may not have happened and must be "
                    "reconciled by a human — the token can never be reused, and a "
                    "new authorization requires that reconciliation first"
                )
            # --- every binding is read from broker-owned state, never the caller
            if record.policy_digest != self._policy.digest():
                raise GateDeniedError("policy changed since authorization; re-authorize")
            if proposal.proposal_digest() != record.proposal_digest:
                raise GateDeniedError(
                    "proposal no longer matches the authorized digest (action, arguments, "
                    "risk class or resources changed); re-authorize"
                )
            if mode is not record.mode:
                raise GateDeniedError(
                    f"authorization was granted in {record.mode.value} mode and cannot be "
                    f"executed in {mode.value} mode"
                )
            if record.reservation_id is not None:
                if self._reservation_status is None:
                    if record.risk_class in SIDE_EFFECT_RISK:
                        raise GateDeniedError(
                            "cannot re-verify the backing reservation for a side-effecting "
                            "action; refusing (fail closed)"
                        )
                else:
                    status = self._reservation_status(record.reservation_id)
                    if status is not ReservationStatus.OPEN:
                        raise ReservationError(
                            f"backing reservation {record.reservation_id} is {status.value}"
                        )
            # --- claim atomically, BEFORE the executor runs (review A1).
            # The durable claim append uses a DETERMINISTIC event id, so the
            # journal's refuse-duplicate-ids invariant makes OPEN -> IN_FLIGHT
            # a cross-process compare-and-swap: exactly one broker ever wins.
            claimed = record.with_status(AuthorizationStatus.IN_FLIGHT)
            self._records[claimed.authorization_id] = claimed
        try:
            self._record_phase_event(
                claimed,
                AuthorizationStatus.IN_FLIGHT.value,
                event_id=_claim_event_id(claimed.authorization_id),
            )
        except ContractError as exc:
            # Another broker's claim is already durably recorded: we lost the
            # CAS. Mark our stale local view dead and refuse — never execute.
            with self._lock:
                loser = self._records.get(claimed.authorization_id)
                if loser is not None and loser.status is AuthorizationStatus.IN_FLIGHT:
                    self._records[claimed.authorization_id] = loser.with_status(
                        AuthorizationStatus.UNKNOWN
                    )
            raise GateDeniedError(
                "the claim was taken by another broker concurrently "
                f"(durable claim already exists): {exc}"
            ) from exc

        try:
            result = executor()
        except BaseException:
            # The executor died mid-flight: the effect outcome is unknown and
            # the token is dead — never silently reusable.
            self._close(claimed, AuthorizationStatus.UNKNOWN)
            raise
        self._close(claimed, AuthorizationStatus.CONSUMED)
        return result

    # --- internals ------------------------------------------------------------------

    def _resolve(self, authorization_id: str) -> _AuthorizationRecord | None:
        """Broker-owned fields, DURABLE lifecycle state (Gate A4).

        The local cache answers fast only while its status is one this broker
        itself durably recorded (its own claim or close). A local OPEN view is
        always re-checked against the journal: if another broker claimed or
        consumed the token — same process or another process — the durable
        state wins and the cached OPEN can never authorize execution.
        """
        with self._lock:
            local = self._records.get(authorization_id)
        if local is not None and local.status is not AuthorizationStatus.OPEN:
            return local
        durable = self._record_from_journal(authorization_id)
        if durable is None:
            return local
        if local is None:
            return durable
        if durable.status is local.status:
            return local
        # A stale local OPEN against a changed durable lifecycle: durable wins.
        with self._lock:
            reconciled = local.with_status(durable.status)
            self._records[authorization_id] = reconciled
        return reconciled

    def _record_phase(self, record: _AuthorizationRecord, phase: str) -> None:
        """Durable lifecycle record for one authorization (append-only).

        Phases are exactly the :class:`AuthorizationStatus` values (plus the
        creation phase ``authorized``): one lifecycle representation
        everywhere, so restart replay and live dispatch agree (Gate A4).
        """
        self._record_phase_event(record, phase, event_id=new_id("evt"))

    def _record_phase_event(
        self, record: _AuthorizationRecord, phase: str, *, event_id: str
    ) -> None:
        self._journal.append(
            EventRecord(
                event_id=event_id,
                kind=EventKind.GATE_DECISION,
                subject=record.authorization_id,
                data={
                    "phase": phase,
                    "action": record.action,
                    "riskClass": record.risk_class.value,
                    "proposalDigest": record.proposal_digest,
                    "policyDigest": record.policy_digest,
                    "reservationId": record.reservation_id,
                    "mode": record.mode.value,
                },
                mode=record.mode,
            )
        )

    def _close(
        self, record: _AuthorizationRecord, status: AuthorizationStatus
    ) -> _AuthorizationRecord:
        closed = _AuthorizationRecord(
            authorization_id=record.authorization_id,
            proposal_digest=record.proposal_digest,
            policy_digest=record.policy_digest,
            risk_class=record.risk_class,
            reservation_id=record.reservation_id,
            mode=record.mode,
            status=status,
            created_at=record.created_at,
            status_at=utc_now_iso(),
            action=record.action,
        )
        with self._lock:
            self._records[closed.authorization_id] = closed
        self._record_phase(closed, status.value)
        return closed

    def _journal_events(self, authorization_id: str) -> list[dict[str, Any]]:
        try:
            events = self._journal.events(authorization_id)
        except Exception:  # pragma: no cover - a journal that cannot be read is fatal
            return []
        data: list[dict[str, Any]] = []
        for event in events:
            if event.kind is EventKind.GATE_DECISION:
                data.append(dict(event.data))
        return data

    def _status_from_journal(self, authorization_id: str) -> AuthorizationStatus | None:
        """Durable lifecycle state — ONE interpretation (see status_from_phases)."""
        return status_from_phases([d.get("phase") for d in self._journal_events(authorization_id)])

    def _record_from_journal(self, authorization_id: str) -> _AuthorizationRecord | None:
        """Rebuild one authorization's trusted record from the durable journal.

        Any durable claim/close is honored: a claim that was never completed
        (a hard crash after claim) reconstructs as UNKNOWN — unresolved, never
        OPEN by default — so the effect cannot run twice (Gate A4).
        """
        events = self._journal_events(authorization_id)
        if not events:
            return None
        authorized = next((d for d in events if d.get("phase") == "authorized"), None)
        if authorized is None:
            return None
        status = self._status_from_journal(authorization_id)
        return _AuthorizationRecord(
            authorization_id=authorization_id,
            proposal_digest=str(authorized.get("proposalDigest", "")),
            policy_digest=str(authorized.get("policyDigest", "")),
            risk_class=RiskClass(str(authorized.get("riskClass", "r0"))),
            reservation_id=authorized.get("reservationId"),
            mode=RunMode(str(authorized.get("mode", "fixture"))),
            status=status if status is not None else AuthorizationStatus.UNKNOWN,
            created_at="",
            status_at="",
            action=str(authorized.get("action", "")),
        )

    def _normalize(self, proposal: ActionProposal) -> str:
        """Canonicalize arguments before digesting (sorted keys, no whitespace)."""
        return proposal.arguments_digest()  # arguments_digest is already canonical

    def _resources_allowed(self, proposal: ActionProposal) -> bool:
        return all(self._policy.resource_allowed(r) for r in proposal.resource_ids)
