"""Vouch error taxonomy — the v1 cross-module error semantics.

Every Vouch failure mode maps to exactly one class here so that the trusted
controller, adapters, workers and the CLI/TUI can distinguish failure kinds
without string matching. Two rules shape the taxonomy (design doc §13.2, ADR
"拒绝必须用正确的协议"):

* Fail closed: anything unknown, unmeasurable, exhausted or unsupported must
  raise, never silently degrade to a success-shaped result.
* Offline integrity: fixture/offline runs must never fall back to live calls;
  replay exhaustion is a terminal failure, not "switch to network".
"""

from __future__ import annotations


class VouchError(Exception):
    """Base class for every Vouch failure."""

    #: Stable machine-readable code, part of the v1 semantics.
    code: str = "vouch/error"


class ContractError(VouchError):
    """A record failed schema/version validation or could not be serialized."""

    code = "vouch/contract"


class DigestMismatchError(VouchError):
    """Content digest does not match the referenced/stored digest."""

    code = "vouch/digest-mismatch"


class UnknownVersionError(ContractError):
    """A record or protocol frame carries an unsupported schema/protocol version."""

    code = "vouch/unknown-version"


# --- budget ---------------------------------------------------------------


class BudgetError(VouchError):
    code = "vouch/budget"


class BudgetExhaustedError(BudgetError):
    """Total cap reached; the request must not be scheduled."""

    code = "vouch/budget-exhausted"


class ReservationError(BudgetError):
    """A reservation could not be made, settled or released (unknown id, double settle...)."""

    code = "vouch/budget-reservation"


class UnmeasurableCostError(BudgetError):
    """Cost is unknown and the policy forbids proceeding on unpriced work."""

    code = "vouch/budget-unmeasurable"


# --- permissions & data isolation ------------------------------------------


class GateDeniedError(VouchError):
    """The Vouch Gate refused an action (risk class, scope, budget or approval)."""

    code = "vouch/gate-denied"


class SplitAccessError(VouchError):
    """A role tried to read a case split it must not see (e.g. proposer vs final acceptance)."""

    code = "vouch/split-access"


class UnauthorizedReuseError(VouchError):
    """A ledger asset lacks reuse rights for the target environment/project."""

    code = "vouch/unauthorized-reuse"


# --- offline integrity ------------------------------------------------------


class LiveCallBlockedError(VouchError):
    """A fixture/offline run attempted a live (network) model or tool call."""

    code = "vouch/live-call-blocked"


class ReplayExhaustedError(VouchError):
    """Replay material ran out; offline mode must fail closed, not go live."""

    code = "vouch/replay-exhausted"


class SideEffectBlockedError(VouchError):
    """A side effect was attempted in a mode that forbids side effects."""

    code = "vouch/side-effect-blocked"


class UnsupportedIsolationError(VouchError):
    """The platform cannot provide the required isolation for untrusted code.

    Per design §13.2 this must fail closed with an explicit unsupported result
    (or a pure-fixture fallback), never silently execute on the host.
    """

    code = "vouch/unsupported-isolation"


# --- lifecycle & recovery ---------------------------------------------------


class ReconciliationRequiredError(VouchError):
    """A crash/restart left side-effect state unknown; human/query verification required."""

    code = "vouch/reconciliation-required"


class InvalidStateTransitionError(VouchError):
    """The commit state machine received an event invalid for its current state."""

    code = "vouch/invalid-transition"


class ApprovalInvalidatedError(VouchError):
    """An approval was consumed after one of its bound digests changed."""

    code = "vouch/approval-invalidated"


# --- adapter protocol --------------------------------------------------------


class ProtocolFrameError(VouchError):
    """An adapter protocol frame was malformed, out of order, oversized or unschema'd."""

    code = "vouch/protocol-frame"


class AdapterExecutionError(VouchError):
    """An adapter-reported execution failure (non-zero outcome inside the adapter)."""

    code = "vouch/adapter-execution"


class MissingMeteringError(VouchError):
    """Usage/cost metering missing from a response that policy requires to be metered."""

    code = "vouch/missing-metering"
