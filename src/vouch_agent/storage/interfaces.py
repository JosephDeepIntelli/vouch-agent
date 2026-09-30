"""Storage ports (v1). Implementation lives in vouch_agent.storage.store.

Three ports, implemented over SQLite (metadata, journal — transactional) and
a content-addressed file tree (artifacts):

* ``MetadataStore`` — typed record persistence by kind+id, with transactions.
* ``ArtifactStore`` — content-addressed bytes (``sha256:...`` names).
* ``BudgetLedger`` — atomic reserve/settle/release against a project cap.

The split-visibility rule (design §7.1) is enforced here: readers must
declare a Role, and final-acceptance data is refused to proposer/engineer
roles regardless of what ids they name.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from vouch_agent.contracts.common import Role
from vouch_agent.contracts.journal import BudgetReservation, CostEntry, EventRecord
from vouch_agent.errors import SplitAccessError


class MetadataStore(Protocol):
    """Transactional metadata persistence for contract records."""

    def save(self, kind: str, record_id: str, data: dict) -> None: ...

    def load(self, kind: str, record_id: str) -> dict | None: ...

    def list_ids(self, kind: str) -> list[str]: ...

    @contextmanager
    def transaction(self) -> Iterator[None]: ...


class ArtifactStore(Protocol):
    """Content-addressed artifact bytes, namespaced per project."""

    def put(self, payload: bytes) -> str:
        """Store bytes, return their digest."""

    def get(self, digest: str) -> bytes: ...

    def exists(self, digest: str) -> bool: ...


class BudgetLedger(Protocol):
    """Atomic reservation-based budget accounting against the project cap.

    Invariants (design §10 + JAZ ADR #2):
    * sum(settled) + sum(open reservations) <= total cap, always, even under
      concurrent reservation attempts.
    * settlement books the actual amount; over- and under-settlement both
      adjust the pool atomically.
    * unknown prices either reserve conservatively or refuse scheduling —
      never book zero.
    """

    def reserve(self, holder: str, amount_usd: float) -> BudgetReservation: ...

    def reserve_child(
        self, parent_reservation_id: str, holder: str, amount_usd: float
    ) -> BudgetReservation:
        """Atomically carve a child allocation from an OPEN parent (A2).

        Children never draw from the cap; committed children cannot exceed
        the parent amount. Implementations must be safe under concurrent
        reserve_child calls from multiple processes."""

    def settle(self, reservation_id: str, actual_usd: float) -> BudgetReservation: ...

    def release(self, reservation_id: str) -> BudgetReservation: ...

    def outstanding_usd(self) -> float: ...

    def settled_usd(self) -> float: ...

    def remaining_usd(self) -> float: ...


class Journal(Protocol):
    """Append-only event + cost journal. Workers get a writer restricted to
    their own subject; only the controller can append audit events."""

    def append(self, event: EventRecord) -> None: ...

    def append_cost(self, entry: CostEntry) -> None: ...

    def events(self, subject: str | None = None) -> list[EventRecord]: ...

    def cost_entries(self, subject: str | None = None) -> list[CostEntry]: ...


def enforce_split_visibility(role: Role, split: str) -> None:
    """Gate helper: refuse final-acceptance data to proposer-side roles."""
    from vouch_agent.contracts.cases import CaseSplit

    if CaseSplit(split) is CaseSplit.FINAL_ACCEPTANCE and role in (
        Role.PROPOSER,
        Role.ENGINEER,
    ):
        raise SplitAccessError(
            f"role {role.value} may not read {CaseSplit.FINAL_ACCEPTANCE.value} data"
        )
