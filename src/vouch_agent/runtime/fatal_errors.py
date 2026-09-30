"""Fatal-category Vouch errors for propagation through a JAZ invoke tree.

Why this module exists (ADR "拒绝必须用正确的协议", design §13.2): a Vouch
worker session runs inside a real JAZ invoke tree, and JAZ renders ordinary
exceptions raised under an agent's REPL as *recoverable feedback* the model
sees and can retry around — including retrying into a different (e.g. live)
path. Fail-closed conditions (replay exhaustion, a blocked live call, the
external wall-clock budget, cancellation) must instead terminate the whole
tree, so they are raised as :class:`jaz.exceptions.FatalError`-category errors
(JAZ's documented routing for "addressed to the caller, not the agent").

Each class multiply-inherits the Vouch error the orchestrator already catches
and JAZ's ``FatalError``. ``jaz.exceptions.is_fatal`` then answers True for
every one of them, so no agent-authored ``except`` clause can swallow them and
no parent invoke can convert them into feedback. At the session boundary they
surface as ordinary Vouch exceptions — the caller never needs to know JAZ is
involved.

Verified against pinned jaz-lang 0.2.0a4 (tests/runtime/test_jaz_protocol.py):
a plain ``ReplayExhaustedError`` raised at the backend seam is caught by agent
code (``except Exception``) and the run continues; the fatal bridge is not.

Known limits, honestly: JAZ documents two ways fatality can be degraded — a
truthy ``__exit__`` on a context manager the agent controls, and a fatal error
wrapped in an ``ExceptionGroup`` the agent catches wholesale. Both require the
agent to already control code we ran; the REPL's import allow-list (deny-all by
default) keeps those out of reach of ordinary model-authored code, but this is
a hardening layer, not a security boundary (design §9: in-process guards are
not the trust boundary; the worker process is).
"""

from __future__ import annotations

from jaz.exceptions import FatalError

from vouch_agent.errors import (
    BudgetExhaustedError,
    LiveCallBlockedError,
    ReplayExhaustedError,
    VouchError,
)

__all__ = [
    "LiveCallBlockedFatal",
    "QueryBudgetRefusedFatal",
    "ReplayExhaustedFatal",
    "SessionCancelledError",
    "WallClockExceededError",
]


class ReplayExhaustedFatal(ReplayExhaustedError, FatalError):
    """Scripted/replay material ran out — terminal, never a live fallback."""


class LiveCallBlockedFatal(LiveCallBlockedError, FatalError):
    """A network/live call was attempted in a mode that forbids it."""


class QueryBudgetRefusedFatal(BudgetExhaustedError, FatalError):
    """The controller's per-query reservation refused this query (review A2).

    Raised at ``LLMQueryEnter`` — before the query runs — so the second
    $0.01 turn under a $0.015 task cap is refused instead of executed and
    reconciled afterwards. Fatal-category for the same reason replay
    exhaustion is: the agent must not retry around a controller refusal.
    """


class WallClockExceededError(BudgetExhaustedError, FatalError):
    """The session's external wall-clock budget ran out.

    Runtime-local for now (wall-clock is part of the budget envelope per design
    §10, hence the ``BudgetExhaustedError`` parent); promote to
    ``vouch_agent.errors`` when the lead extends the shared taxonomy.
    """


class SessionCancelledError(VouchError, FatalError):
    """The session was cancelled by the controller; in-flight work stops."""

    code = "vouch/session-cancelled"
