"""Adapter port (protocol v1, design §4.2) — the Python-side seam.

Adapters connect the system-under-improvement (Choose's Node/test/browser
runners, Visibility's Python runner, a customer agent, or an in-repo fixture
adapter) to Vouch through versioned JSON/JSONL process contracts. Adapters
never share source, secrets or databases with Vouch; Vouch never imports
sibling product code.

Frame-level details (schema, sequence numbers, size limits, metering
requirements) are owned by ``vouch_agent.adapters.protocol``; this module
fixes only the port the trusted controller programs against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from vouch_agent.contracts.common import RunMode


@dataclass(frozen=True)
class AdapterDescriptor:
    """Result of ``describe`` — what an adapter can do and how it is metered."""

    adapter_id: str
    protocol_version: str = "1"
    workflows: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    # Declares which modes the adapter itself can enforce. An adapter that
    # cannot enforce fixture/offline integrity must say so here — Vouch then
    # refuses to claim full gate protection for it (design §4.2).
    enforced_modes: tuple[RunMode, ...] = (RunMode.FIXTURE,)
    notes: str = ""


@dataclass(frozen=True)
class AdapterExecution:
    """Result of ``execute`` — outputs, evidence refs, metering, errors.

    ``usage`` must be present whenever the descriptor says metering applies;
    a missing usage makes the attempt immeasurable, never free.
    """

    ok: bool
    outputs: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()  # digests of artifacts the adapter sealed
    tool_events: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] | None = None  # None => immeasurable, policy decides
    error: str | None = None
    mode: RunMode = RunMode.FIXTURE
    runner_version: str = ""  # actual runner that executed (report requirement §13.2)


class AdapterClient(Protocol):
    """Lifecycle: describe -> prepare -> execute* -> collect -> cleanup."""

    def describe(self) -> AdapterDescriptor: ...

    def prepare(self, run_id: str, mode: RunMode) -> None:
        """Create isolated run artifacts/workspace for ``run_id``."""

    def execute(
        self,
        *,
        run_id: str,
        attempt_id: str,
        workflow_id: str,
        case_input: dict[str, Any],
        mode: RunMode,
    ) -> AdapterExecution: ...

    def collect(self, run_id: str) -> tuple[str, ...]:
        """Seal artifacts for the run; returns their digests."""

    def cleanup(self, run_id: str) -> None: ...
