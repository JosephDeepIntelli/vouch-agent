"""Runtime ports (v1) — the seam between orchestration and the JAZ layer.

Owner steering (2026-09-28): the shared runtime must *actually perform
bounded work* (business task execution), not merely score fixtures. The
orchestrator (``vouch_agent.orchestrator``) programs against these ports;
``vouch_agent.runtime.jaz`` implements them on the pinned JAZ package
(``jaz-lang==0.2.0a4``, reviewed commit 0803d497…, see docs/jaz-reuse-decision.md).

Trust boundary rules fixed here (ADR + design §9/§13.2):

* The worker session runs model-authored code with ``allow_timeout_pragma``
  disabled and an external wall-clock budget; in-process guards are not the
  trust boundary, so worker sessions never receive credentials or acceptance
  data, and the orchestrator re-validates every returned action.
* Fixture/offline sessions install a no-live-call guard that *aborts* on any
  attempt at network model access, and replay exhaustion is terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from vouch_agent.contracts.common import RunMode


@dataclass(frozen=True)
class ModelCallResult:
    """One STEP as seen by the orchestrator (post-validation).

    A step may aggregate several underlying model calls (multi-turn or
    nested); token/cost fields are step deltas. ``content`` is the raw model
    output (for real engines: model-authored code); the JSON-safe EVALUATED
    return value, when the engine provides one, rides in
    ``raw["return_value"]`` — trusted code serializes that into artifacts
    rather than trusting the model to describe its own result.
    """

    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None  # None => unmeasured; never silently zero
    model_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerSessionConfig:
    """Bounded execution config for a model-driven worker session."""

    mode: RunMode = RunMode.FIXTURE
    max_steps: int = 16
    wall_clock_s: float = 60.0
    max_cost_usd: float | None = None  # None => controller reservations apply
    # Scripted responses for fixture/replay sessions, consumed in order;
    # exhaustion in fixture/offline modes is a terminal failure.
    scripted_responses: tuple[str, ...] = ()
    # MONOTONIC QUERY CURSOR (M3 Gate A2): the number of scripted responses
    # this session's provider pool has already consumed BEFORE it opens —
    # the full pool is supplied, the session serves from position
    # ``scripted_cursor``. A session restarted between steps therefore
    # advances once and never replays consumed responses. Must be within
    # ``[0, len(scripted_responses)]``; sessions enforce that at open.
    scripted_cursor: int = 0
    allow_timeout_pragma: bool = False  # never enabled for untrusted code
    # Controller-owned reservation authority for ONE underlying model query
    # (review A2). ``None`` keeps the accounting-only session cap; sessions
    # that support it must reserve before EVERY underlying query (nested
    # invokes included) and refuse to run one whose reservation is denied.
    # In the process-isolated runtime this object stays PARENT-side: the
    # child reaches it through the worker protocol's budget RPC, so the
    # parent ledger remains the only authority (M3 Gate A3).
    query_budget: QueryBudget | None = None


class QueryBudget(Protocol):
    """Reservation authority for one underlying model query (review A2).

    The controller owns the ledger; the session owns the moment of truth —
    the instant just before a query is dispatched. Implementations reserve
    atomically against the run's budget (typically a child reservation carved
    from the run's parent) and raise a :class:`vouch_agent.errors.BudgetError`
    to refuse, which the session MUST turn into an abort issued *before* the
    query runs. No underlying model call may start without a reservation that
    covers it conservatively.
    """

    def reserve_query(self, estimate_usd: float | None) -> str:
        """Reserve before the query runs.

        ``estimate_usd`` is the backend's known per-query price when it has
        one, else ``None`` — implementations then reserve a conservative
        amount of their own choosing. Returns the reservation id. Raises a
        ``BudgetError`` to refuse the query outright.
        """

    def settle_query(self, reservation_id: str, actual_usd: float | None) -> None:
        """Book the query's actual cost; ``None`` means unmeasurable, which
        implementations must settle conservatively (never as zero)."""

    def release_query(self, reservation_id: str) -> None:
        """Give back a reservation for a query that verifiably never ran
        (aborted at a control stage before the call was dispatched)."""


class RuntimeSession(Protocol):
    """One bounded worker session executing a task with model-authored code."""

    @property
    def session_id(self) -> str: ...

    def step(self, instruction: str, scope: dict[str, Any] | None = None) -> ModelCallResult:
        """Advance one model+REPL round within the session budget."""

    def structured_step(
        self, instruction: str, return_type: type, scope: dict[str, Any] | None = None
    ) -> Any:
        """One round whose return value is validated against ``return_type``."""

    def usage(self) -> dict[str, Any]:
        """Aggregated metering for this session (tokens, cost, calls, steps).

        ``llm_calls`` counts completed underlying queries for the WHOLE
        session (nested invokes included); per-step values on
        :class:`ModelCallResult` are DELTAS. Cumulative session counters and
        per-step deltas are different numbers and must never be conflated.
        """

    def cancel(self, reason: str = "cancelled by controller") -> None:
        """Promptly stop any in-flight step and mark the session cancelled.

        An acceleration hook for external stops (a worker's cancel poller):
        implementations terminate their owned worker group so a blocked
        ``step()`` unblocks and fails closed; the controller's cancel
        authority still decides the final status. Sessions without an owned
        process may make this best-effort.
        """

    def close(self) -> None: ...


class Runtime(Protocol):
    """Factory for bounded worker sessions on the pinned JAZ core."""

    def open_session(self, config: WorkerSessionConfig) -> RuntimeSession: ...

    def backend_id(self) -> str:
        """Identity+version of the model backend actually wired (e.g. fake-scripted@1)."""
