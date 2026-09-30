"""Durable control-path records shared by the supervisor and recovery (review A3).

Three metadata-store record kinds live here because two modules need them:

* ``run-ownership`` — who may checkpoint/finalize a run right now (a lease
  with an expiry, refreshed at every checkpoint). A second supervisor may
  *request* cancel/pause at any time; it may not finalize a run another
  supervisor actively owns.
* ``run-finalization`` — the write-ahead intent for a run's terminal writes.
  Terminal status alone is not proof that finalization completed: the record
  lists each terminal write (result package, ledger settlement, metadata/
  journal finalization, ownership release) and whether it is done, so a
  restart can finish the missing ones exactly once without replaying work.
* ``run-clock`` — the run's wall-clock allowance. The deadline is persisted
  at first execution so a resume cannot reset the original allowance, and a
  paused run carries an explicit expiry.

The metadata store is kind-agnostic; these kinds are owned by the
orchestrator and must not be written by any other component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vouch_agent.contracts.common import utc_now_iso
from vouch_agent.errors import VouchError

KIND_RUN_OWNERSHIP = "run-ownership"
KIND_RUN_FINALIZATION = "run-finalization"
KIND_RUN_CLOCK = "run-clock"

#: Terminal writes of the finalization protocol, in execution order.
FINALIZATION_WRITES = ("packageSaved", "ledgerSettled", "metadataFinalized", "ownershipReleased")


class RunOwnershipError(VouchError):
    """Another supervisor actively owns this run, or this snapshot is stale.

    Ownership is a persisted lease refreshed at each checkpoint; a fenced
    write (the persisted run revision moved past ours) raises this too, so a
    superseded owner can never overwrite the new owner's decisions.
    """

    code = "vouch/run-ownership"


@dataclass(frozen=True)
class OwnershipRecord:
    run_id: str
    owner_id: str
    acquired_at: str
    lease_expires_at_epoch_s: float
    heartbeat_at: str
    revision: int = 1
    released_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": "1",
            "runId": self.run_id,
            "ownerId": self.owner_id,
            "acquiredAt": self.acquired_at,
            "leaseExpiresAtEpochS": self.lease_expires_at_epoch_s,
            "heartbeatAt": self.heartbeat_at,
            "revision": self.revision,
            "releasedAt": self.released_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OwnershipRecord:
        return cls(
            run_id=str(data["runId"]),
            owner_id=str(data["ownerId"]),
            acquired_at=str(data.get("acquiredAt", "")),
            lease_expires_at_epoch_s=float(data.get("leaseExpiresAtEpochS", 0.0)),
            heartbeat_at=str(data.get("heartbeatAt", "")),
            revision=int(data.get("revision", 0)),
            released_at=data.get("releasedAt"),
        )

    def lease_live(self, now_epoch_s: float) -> bool:
        return self.released_at is None and self.lease_expires_at_epoch_s > now_epoch_s


@dataclass(frozen=True)
class FinalizationIntent:
    """Write-ahead intent for one run's terminal writes (review A3).

    Written durably BEFORE the first terminal write; each completed write
    flips its flag. Recovery performs only the writes whose flag is false,
    with exactly the parameters recorded here — never recomputed from state
    that may since have changed, never replaying model/effect work.
    """

    run_id: str
    target_status: str
    result_digest: str | None
    package: dict[str, Any]
    conservative_usd: float
    childless_overage_usd: float
    reservation_id: str | None
    #: "close" settles the run reservation from its children (+ overage);
    #: "keep-open" leaves it open (needs-reconciliation may resume).
    ledger_action: str
    unmeasured: bool
    package_saved: bool = False
    ledger_settled: bool = False
    metadata_finalized: bool = False
    ownership_released: bool = False
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def pending_writes(self) -> tuple[str, ...]:
        flags = {
            "packageSaved": self.package_saved,
            "ledgerSettled": self.ledger_settled,
            "metadataFinalized": self.metadata_finalized,
            "ownershipReleased": self.ownership_released,
        }
        return tuple(name for name in FINALIZATION_WRITES if not flags[name])

    def complete(self) -> bool:
        return not self.pending_writes()

    def with_flag(self, name: str) -> FinalizationIntent:
        import dataclasses

        if name not in FINALIZATION_WRITES:
            raise ValueError(f"unknown finalization write {name!r}")
        updates: dict[str, Any] = {
            FinalizationIntent._field_for(name): True,
            "updated_at": utc_now_iso(),
        }
        return dataclasses.replace(self, **updates)

    @staticmethod
    def _field_for(name: str) -> str:
        return {
            "packageSaved": "package_saved",
            "ledgerSettled": "ledger_settled",
            "metadataFinalized": "metadata_finalized",
            "ownershipReleased": "ownership_released",
        }[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": "1",
            "runId": self.run_id,
            "targetStatus": self.target_status,
            "resultDigest": self.result_digest,
            "package": self.package,
            "conservativeUsd": self.conservative_usd,
            "childlessOverageUsd": self.childless_overage_usd,
            "reservationId": self.reservation_id,
            "ledgerAction": self.ledger_action,
            "unmeasured": self.unmeasured,
            "packageSaved": self.package_saved,
            "ledgerSettled": self.ledger_settled,
            "metadataFinalized": self.metadata_finalized,
            "ownershipReleased": self.ownership_released,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FinalizationIntent:
        package = data.get("package")
        return cls(
            run_id=str(data["runId"]),
            target_status=str(data["targetStatus"]),
            result_digest=data.get("resultDigest"),
            package=dict(package) if isinstance(package, dict) else {},
            conservative_usd=float(data.get("conservativeUsd", 0.0)),
            childless_overage_usd=float(data.get("childlessOverageUsd", 0.0)),
            reservation_id=data.get("reservationId"),
            ledger_action=str(data.get("ledgerAction", "close")),
            unmeasured=bool(data.get("unmeasured", False)),
            package_saved=bool(data.get("packageSaved", False)),
            ledger_settled=bool(data.get("ledgerSettled", False)),
            metadata_finalized=bool(data.get("metadataFinalized", False)),
            ownership_released=bool(data.get("ownershipReleased", False)),
            created_at=str(data.get("createdAt", "")),
            updated_at=str(data.get("updatedAt", "")),
        )


def read_ownership(store: Any, run_id: str) -> OwnershipRecord | None:
    data = store.load(KIND_RUN_OWNERSHIP, run_id)
    return OwnershipRecord.from_dict(data) if data is not None else None


def read_finalization(store: Any, run_id: str) -> FinalizationIntent | None:
    data = store.load(KIND_RUN_FINALIZATION, run_id)
    return FinalizationIntent.from_dict(data) if data is not None else None


def read_clock(store: Any, run_id: str) -> dict[str, Any] | None:
    return store.load(KIND_RUN_CLOCK, run_id)


@dataclass(frozen=True)
class NativeOperationResult:
    """Outcome of one trusted DETERMINISTIC operation (M4 A1 test-gap repair).

    ``payload`` becomes the run's final artifact bytes (trusted controller
    code computed them — never model output); ``artifacts`` maps logical
    names to digests of supplementary artifacts (e.g. each input snapshot)
    recorded alongside it; ``usage`` is persisted on the step; ``not_done``
    carries honest caveats (e.g. "discrepancies found") into the package.
    """

    payload: bytes
    artifacts: dict[str, str] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    not_done: tuple[str, ...] = ()
