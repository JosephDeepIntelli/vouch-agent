"""The JAZ-backed execution runtime (design §4.1 "Invocation Runtime").

Implements the lead-owned ports in :mod:`vouch_agent.runtime.ports` on the
pinned ``jaz-lang==0.2.0a4`` public API. Everything JAZ-facing goes through the
documented surface only: ``jaz.invoke``, ``jaz.ConfigOverride``, ``jaz.scope``,
``jaz.hooks`` (Hook/ReturnType/BudgetPool/IterationLimit/RecursionLimit),
``jaz.hooks.events`` / ``jaz.hooks.effects`` (Abort), ``jaz.repl.PythonREPL``,
``jaz.llm.BaseLLM``. No ``jaz._*`` module is imported.

How each bounded-execution dimension is enforced (and by whom):

======================  ======================================================
max_steps               JAZ ``IterationLimit`` installed as a *context manager*
                        (the propagating channel — positional hooks do NOT
                        reach nested invokes; verified in
                        tests/runtime/test_nested_scope_hooks.py) bounds each
                        invoke's own REPL turns, and the session additionally
                        refuses further ``step()`` calls once ``max_steps``
                        steps have run.
recursion depth         JAZ ``RecursionLimit`` (context manager only — it
                        *rejects* the positional channel), pinned at
                        ``_MAX_RECURSION_DEPTH``.
max_cost_usd            Reservation-style at the session edge: a step is
                        never *scheduled* when spent + the next call's known
                        price would cross the cap (accounting-only checks
                        always overshoot by one in-flight call — see the
                        oversubscription test for why the controller's ledger
                        still owns the true reservation). In-step spend is
                        bounded by a per-step ``BudgetPool`` over the
                        *remaining* allowance, aborting via JAZ's ``Abort``
                        protocol. Actuals are exposed via ``usage()``.
wall_clock_s            EXTERNAL to the worker, in three layers: (1) the
                        session refuses further steps once the deadline
                        passes; (2) a runtime-owned watchdog injects a fatal
                        error into the running step thread (best-effort — see
                        the ``inject_exception`` caveat in ``guards``); (3)
                        the step caller blocks on a result queue with a hard
                        timeout at deadline+grace and fails closed with
                        ``WallClockExceededError`` even if the worker thread
                        cannot be interrupted. Two JAZ-side bounds back those
                        up: the REPL per-exec timeout (pragma disabled, so
                        model code cannot raise it) and ``IterationLimit``,
                        so a runaway loop burns at most
                        ``max_steps x exec_timeout`` before JAZ itself aborts
                        it. The only *hard* wall-clock guarantee is the
                        worker process (design §9: in-process guards are not
                        the trust boundary).
fixture/offline safety  The scripted backend fails closed on exhaustion, a
                        quota hook aborts the over-quota query via ``Abort``
                        (same protocol ``BudgetPool`` uses), and a
                        socket-level no-network guard is active for every
                        step of the session.
======================  ======================================================

Concurrency model: each ``step()`` runs its JAZ invoke on a dedicated worker
thread and blocks the caller on a result queue. This is not cosmetic —
``contextvars`` values do not cross ``threading.Thread`` boundaries, so the
hooks/config for a step must be entered *on* the thread that runs the invoke
(verified: hooks entered around a thread pool see zero events from it). The
worker thread is also what makes wall-clock injection and cancellation
targetable.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from contextlib import ExitStack
from types import TracebackType
from typing import Any

import jaz
from jaz.hooks import BudgetPool, Hook, IterationLimit, RecursionLimit, ReturnType
from jaz.hooks.effects import Abort, Effect
from jaz.hooks.events import LLMQueryEnter, LLMQueryExit
from jaz.hooks.events.base import Completed
from jaz.repl import PythonREPL

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import (
    BudgetExhaustedError,
    ContractError,
    InvalidStateTransitionError,
    VouchError,
)
from vouch_agent.runtime import guards
from vouch_agent.runtime.failure_usage import attach_usage
from vouch_agent.runtime.fatal_errors import (
    QueryBudgetRefusedFatal,
    ReplayExhaustedFatal,
    SessionCancelledError,
    WallClockExceededError,
)
from vouch_agent.runtime.ports import ModelCallResult, WorkerSessionConfig
from vouch_agent.runtime.scripted_backend import BACKEND_TAG, ScriptedBackend

__all__ = ["JAZ_RUNTIME_ID", "JazRuntime", "JazSession"]

#: Identity reported by ``backend_id()``: what runs and on which pinned core.
JAZ_RUNTIME_ID = f"jaz-{jaz.__version__}/{BACKEND_TAG}"

#: Session-wide recursion cap. Not yet a port field; deliberately narrow and
#: fail-closed until the controller contract needs to parameterize it.
_MAX_RECURSION_DEPTH = 3

#: Per-exec REPL timeout cap; a single exec never gets more than this even
#: when the session wall clock is generous.
_EXEC_TIMEOUT_CAP_S = 30.0

#: Grace after the wall-clock deadline before the caller gives up on the
#: worker thread entirely. Verified limitation: async-exception injection
#: into jaz-instrumented code (``sys.monitoring`` LINE callbacks) is mangled
#: by CPython into a recoverable ``SystemError``, so a tight agent loop can
#: only be bounded by JAZ's own exec timeout / iteration limit — the caller
#: must still unblock, fail closed, and abandon the (daemon) worker thread.
_WALL_CLOCK_GRACE_S = 2.5


def _fail_closed_mode(mode: RunMode) -> bool:
    """Modes where live calls must be blocked and replay exhaustion is terminal."""
    return mode in (RunMode.FIXTURE, RunMode.OFFLINE_EVALUATION)


class _UsageRecorder(Hook):
    """Session-wide metering off the same event ``BudgetPool`` books on.

    Sees every completed LLM query in the session tree (installed as a
    context manager, so nested invokes dispatch to it too). Records unknown
    cost as *unmeasured* — never as zero (design §10 / MissingMeteringError).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.unmeasured_calls = 0
        self.models: set[str] = set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "llm_calls": self.calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cost_usd": round(self.cost_usd, 6),
                "unmeasured_calls": self.unmeasured_calls,
                "models": sorted(self.models),
            }

    def on_llm_query_exit(self, event: LLMQueryExit) -> list[Effect]:
        if not isinstance(event.outcome, Completed):
            return []
        response = event.outcome.result
        with self._lock:
            self.calls += 1
            self.models.add(event.model)
            if response.prompt_tokens is not None:
                self.prompt_tokens += response.prompt_tokens
            if response.completion_tokens is not None:
                self.completion_tokens += response.completion_tokens
            if response.cost_usd is None:
                self.unmeasured_calls += 1
            else:
                self.cost_usd += response.cost_usd
        return []


class _ScriptedQuota(Hook):
    """Abort the query that would exceed the scripted pool — JAZ's protocol.

    ``BudgetPool`` hard-stops via ``Abort(error=...)`` at ``LLMQueryEnter``;
    replay exhaustion uses exactly the same channel (an ``Abort`` carrying a
    FatalError-category error) so the whole tree terminates rather than the
    backend error becoming recoverable agent feedback. The backend's own
    ``ReplayExhaustedFatal`` raise remains as the seam-level backstop.
    """

    def __init__(self, backend: ScriptedBackend) -> None:
        self._backend = backend

    def on_llm_query_enter(self, event: LLMQueryEnter) -> list[Effect]:
        if self._backend.responses_remaining <= 0:
            return [
                Abort(
                    error=ReplayExhaustedFatal(
                        f"scripted responses exhausted before query for model "
                        f"{event.model!r}; offline/fixture sessions fail closed "
                        "(no live fallback)"
                    )
                )
            ]
        return []


class _QueryReservationHook(Hook):
    """Reserve controller budget BEFORE every underlying query (review A2).

    Installed as a context manager, so nested invokes dispatch to it too —
    the accounting-only ``BudgetPool`` only ever books *returned* cost, which
    let a later turn overspend a task cap by one in-flight call. This hook
    asks the controller's :class:`~vouch_agent.runtime.ports.QueryBudget` for
    a reservation at ``LLMQueryEnter`` and aborts the query (fatally, before
    it runs) when the ledger refuses.

    Settlement, at ``LLMQueryExit``:

    * ``Completed`` — settle at the response's measured cost (``None`` is
      handed to the controller, which books it conservatively, never zero);
    * ``Aborted`` — the query was stopped at a control stage before any
      request was dispatched, so the reservation is *released* (verified
      pre-execution rejection, no spend);
    * ``Failed`` — the query was attempted and raised. A
      ``ReplayExhaustedFatal`` is the scripted backend's own fail-closed
      refusal raised *before* it serves or charges anything, so it releases
      too; anything else may have partially spent and settles conservatively.
    """

    def __init__(self, budget: Any, estimate_usd: float | None) -> None:
        self._budget = budget
        self._estimate_usd = estimate_usd
        self._in_flight: list[str] = []

    def on_llm_query_enter(self, event: LLMQueryEnter) -> list[Effect]:
        try:
            reservation_id = self._budget.reserve_query(self._estimate_usd)
        except VouchError as exc:
            return [
                Abort(
                    error=QueryBudgetRefusedFatal(
                        f"the controller budget refused this query before it ran: {exc}"
                    )
                )
            ]
        self._in_flight.append(reservation_id)
        return []

    def on_llm_query_exit(self, event: LLMQueryExit) -> list[Effect]:
        if not self._in_flight:
            return []  # aborted before this hook reserved anything
        reservation_id = self._in_flight.pop()
        outcome = event.outcome
        if isinstance(outcome, Completed):
            self._budget.settle_query(reservation_id, getattr(outcome.result, "cost_usd", None))
        elif _is_pre_call_refusal(outcome):
            self._budget.release_query(reservation_id)
        else:
            # Attempted but not completed: spend is unknown, book the full
            # reservation conservatively — never zero, never silently freed.
            self._budget.settle_query(reservation_id, None)
        return []


def _is_pre_call_refusal(outcome: Any) -> bool:
    """True for outcomes that provably spent nothing.

    ``Aborted`` outcomes happen at a query *control stage* — before the model
    call is dispatched. A ``Failed`` outcome carries the exception the call
    (or the span machinery) raised; the scripted backend's replay-exhausted
    ``FatalError`` is raised before it serves or charges anything, so it is a
    verified no-spend refusal too. Every other failure keeps its reservation
    booked conservatively: an exception's class alone never certifies zero
    spend for a call that was actually attempted.
    """
    from jaz.hooks.events import Aborted

    if isinstance(outcome, Aborted):
        return True
    exception = getattr(outcome, "exception", None)
    return isinstance(exception, ReplayExhaustedFatal)


class JazSession:
    """One bounded worker session (see ``JazRuntime.open_session``)."""

    def __init__(self, config: WorkerSessionConfig) -> None:
        if config.max_steps < 1:
            raise ContractError(f"max_steps must be >= 1, got {config.max_steps!r}")
        if config.wall_clock_s <= 0:
            raise ContractError(f"wall_clock_s must be > 0, got {config.wall_clock_s!r}")
        if config.max_cost_usd is not None and config.max_cost_usd <= 0:
            raise ContractError(f"max_cost_usd must be > 0 when set, got {config.max_cost_usd!r}")
        if config.scripted_cursor < 0 or config.scripted_cursor > len(config.scripted_responses):
            raise ContractError(
                f"scripted_cursor {config.scripted_cursor} is outside the pool "
                f"[0, {len(config.scripted_responses)}]; the cursor is monotonic "
                "and must never invent or replay responses"
            )
        self._config = config
        self._session_id = f"sess_{uuid.uuid4().hex[:16]}"
        # Monotonic query cursor (Gate A2): a restarted session skips the
        # responses the previous session already consumed — consumed work is
        # never replayed.
        self._backend = ScriptedBackend(
            responses=tuple(config.scripted_responses)[config.scripted_cursor :]
        )
        exec_timeout = min(config.wall_clock_s, _EXEC_TIMEOUT_CAP_S)
        self._repl = PythonREPL(
            allow_timeout_pragma=config.allow_timeout_pragma,
            exec_timeout=exec_timeout,
        )
        self._recorder = _UsageRecorder()
        self._quota = _ScriptedQuota(self._backend) if _fail_closed_mode(config.mode) else None
        self._reservations = (
            _QueryReservationHook(config.query_budget, self._query_estimate_usd())
            if config.query_budget is not None
            else None
        )
        self._steps_used = 0
        self._closed = False
        self._cancelled = False
        self._cancel_reason = ""
        self._expired = False
        self._state_lock = threading.Lock()
        self._active_step_thread: threading.Thread | None = None
        self._deadline = time.monotonic() + config.wall_clock_s
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop,
            name=f"vouch-watchdog-{self._session_id}",
            daemon=True,
        )
        self._watchdog.start()

    # --- lifecycle ------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    def _query_estimate_usd(self) -> float | None:
        """The backend's own per-query price when it has one.

        Only the scripted backend exposes a known price; anything else is an
        unknown price, told to the controller as ``None`` so it reserves
        conservatively rather than guessing.
        """
        estimate = getattr(self._backend, "cost_usd", None)
        return float(estimate) if isinstance(estimate, int | float) else None

    def _timeout_message(self) -> str:
        return (
            f"session {self._session_id} exceeded its wall-clock budget of "
            f"{self._config.wall_clock_s}s; the step was terminated by the "
            "external runtime watchdog"
        )

    def _watchdog_loop(self) -> None:
        """External wall-clock enforcement: kill the active step at deadline.

        Injection is *opportunistic* — it works when the target is between
        monitored frames (e.g. blocked in a C call outside the REPL) and is
        unreliable inside jaz-instrumented tight loops (see
        ``_WALL_CLOCK_GRACE_S``). The binding guarantees live elsewhere: the
        gate refuses further steps, and the step caller's queue timeout fails
        closed. Runs as a runtime-owned daemon thread model code cannot reach
        (the REPL denies imports by default, and the thread object is never
        handed out).
        """
        while not self._watchdog_stop.wait(timeout=0.1):
            if time.monotonic() >= self._deadline:
                break
        if self._watchdog_stop.is_set():
            return  # session closed before the deadline — nothing to do
        self._expired = True
        injections = 0
        while injections < 8 and not self._watchdog_stop.wait(timeout=0.25):
            target = self._active_step_thread
            if target is None or not target.is_alive():
                break
            guards.inject_exception(target, WallClockExceededError(self._timeout_message()))
            injections += 1

    def _inject_into_active_step(self, exc: BaseException) -> None:
        """Best-effort single injection into the in-flight step thread.

        Acceleration only — promptness comes from the step caller polling the
        session state (see ``_execute``), not from this. Reliable when the
        thread is outside jaz-instrumented frames; for tight monitored loops
        the step still ends via exec-timeout/iteration-limit, and the
        cancelled/expired flags gate every later step either way.
        """
        with self._state_lock:
            target = self._active_step_thread
        if target is not None and target.is_alive():
            guards.inject_exception(target, exc)

    def cancel(self, reason: str = "cancelled by controller") -> None:
        """Stop any in-flight step and mark the session cancelled."""
        with self._state_lock:
            self._cancelled = True
            self._cancel_reason = reason
        self._inject_into_active_step(SessionCancelledError(reason))

    def close(self) -> None:
        """Terminate the session (idempotent). In-flight steps are cancelled."""
        with self._state_lock:
            already_closed = self._closed
            self._closed = True
        self._watchdog_stop.set()
        if not already_closed:
            self._inject_into_active_step(SessionCancelledError("session closed"))

    def __enter__(self) -> JazSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- pre-step gate ----------------------------------------------------------

    def _gate(self) -> None:
        if self._cancelled:
            reason = f": {self._cancel_reason}" if self._cancel_reason else ""
            raise SessionCancelledError(f"session {self._session_id} was cancelled{reason}")
        if self._closed:
            raise InvalidStateTransitionError(
                f"session {self._session_id} is closed; open a new session"
            )
        if self._expired or time.monotonic() >= self._deadline:
            self._expired = True
            raise WallClockExceededError(self._timeout_message())
        if self._steps_used >= self._config.max_steps:
            raise BudgetExhaustedError(
                f"step budget exhausted: {self._steps_used} step(s) run, "
                f"max_steps={self._config.max_steps}"
            )
        cap = self._config.max_cost_usd
        if cap is not None:
            spent = self._recorder.snapshot()["cost_usd"]
            # Reservation-style refusal (design §10): with a known per-call
            # price, never SCHEDULE a step whose first call would cross the
            # cap — accounting-only checks (spent >= cap) always overshoot by
            # one in-flight call. Exact multi-call reservation stays in the
            # controller ledger; within a step the pool bounds further turns.
            next_call_cost = self._backend.cost_usd
            if spent + next_call_cost > cap:
                raise BudgetExhaustedError(
                    f"cost budget exhausted: ${spent:.6f} spent of ${cap:.6f}, "
                    f"next call would cost ~${next_call_cost:.6f} (the controller "
                    "ledger must reserve before re-opening)"
                )

    def _validate_scope(self, scope: dict[str, Any]) -> dict[str, Any]:
        for name in scope:
            if not name.isidentifier():
                raise ContractError(f"scope key {name!r} is not a valid identifier")
            if name == "task":
                raise ContractError("scope key 'task' is reserved for the step instruction")
        return scope

    # --- step execution ---------------------------------------------------------

    def _enter_step_hooks(self, stack: ExitStack) -> None:
        """Install the session's propagating hooks on the STEP thread.

        Must run on the worker thread itself: hook/config contextvars do not
        cross ``threading.Thread`` boundaries, so entering them on the
        caller's thread would leave the invoke (and every nested invoke)
        unhooked.
        """
        config = self._config
        stack.enter_context(jaz.ConfigOverride(llm=self._backend, repl=self._repl))
        stack.enter_context(IterationLimit(max_iterations=config.max_steps))
        stack.enter_context(RecursionLimit(max_depth=_MAX_RECURSION_DEPTH))
        stack.enter_context(self._recorder)
        if self._reservations is not None:
            stack.enter_context(self._reservations)
        if self._quota is not None:
            stack.enter_context(self._quota)
        if config.max_cost_usd is not None:
            remaining = config.max_cost_usd - self._recorder.snapshot()["cost_usd"]
            if remaining > 0:
                # BudgetPool books *returned* cost only; hand it just the
                # remaining allowance so a step cannot spend a fresh full cap
                # on top of money earlier steps already burned.
                stack.enter_context(BudgetPool(cost_budget=remaining))
        if _fail_closed_mode(config.mode):
            stack.enter_context(guards.no_network_guard(self._session_id))

    def _run_step(
        self,
        result_q: queue.Queue[tuple[str, Any]],
        instruction: str,
        return_type: type | None,
        scope: dict[str, Any],
    ) -> None:
        try:
            with ExitStack() as stack:
                self._enter_step_hooks(stack)
                if scope:
                    stack.enter_context(jaz.scope(**scope))
                if return_type is None:
                    value: object = jaz.invoke(task=instruction)
                else:
                    value = jaz.invoke(ReturnType(return_type), task=instruction)
            result_q.put(("ok", value))
        except BaseException as exc:
            result_q.put(("error", exc))

    def _translate_step_error(self, exc: BaseException) -> BaseException:
        """Map jaz-native failures to the Vouch taxonomy at the session boundary.

        The orchestrator programs against ``vouch_agent.errors``; JAZ's own
        exhaustion errors (iteration/recursion/budget pool) are budget-shaped
        and surface as ``BudgetExhaustedError``; anything unrecognized is
        wrapped (original preserved as ``__cause__``) rather than leaked.

        Whatever the taxonomy maps to, the usage this session already measured
        rides along on the exception (review A2): a failed step's class name
        must never be read as "nothing was spent".
        """
        from jaz import exceptions as jaz_exc

        def _with_usage(mapped: BaseException) -> BaseException:
            return attach_usage(mapped, self.usage())

        if isinstance(exc, (VouchError, WallClockExceededError, SessionCancelledError)):
            return _with_usage(exc)  # already Vouch-shaped (fatal bridges included)
        # A step that died after the wall-clock deadline reports the timeout,
        # whatever the proximate jaz error was (the run was killed by the
        # timeout regime: watchdog injection, exec timeout, or iteration cap).
        if self._expired or time.monotonic() >= self._deadline:
            self._expired = True
            return _with_usage(WallClockExceededError(self._timeout_message()))
        if isinstance(
            exc,
            (
                jaz_exc.IterationLimitExhaustedError,
                jaz_exc.BudgetPoolExhaustedError,
                jaz_exc.RecursionLimitError,
            ),
        ):
            mapped = BudgetExhaustedError(f"{type(exc).__name__}: {exc}")
            mapped.__cause__ = exc
            return _with_usage(mapped)
        detail = f"runtime step failed: {type(exc).__name__}: {exc}"
        sub_errors = getattr(exc, "exceptions", None)
        if sub_errors:
            detail += " [" + "; ".join(f"{type(sub).__name__}: {sub}" for sub in sub_errors) + "]"
        wrapped = VouchError(detail)
        wrapped.__cause__ = exc
        return _with_usage(wrapped)

    def _execute(
        self, instruction: str, return_type: type | None, scope: dict[str, Any] | None
    ) -> tuple[Any, dict[str, Any]]:
        """Run one invoke on the worker thread; return (value, metering-before).

        The caller *polls* the result queue in small increments and checks the
        session's cancelled/closed/expired flags each round, so cancellation
        and close take effect promptly and deterministically — independent of
        whether exception injection into the worker thread happened to land
        (see the ``_WALL_CLOCK_GRACE_S`` caveat).
        """
        self._gate()
        scope = self._validate_scope(dict(scope or {}))
        before = self._recorder.snapshot()
        with self._state_lock:
            if self._active_step_thread is not None and self._active_step_thread.is_alive():
                raise InvalidStateTransitionError(
                    "another step is still running on this session; sessions "
                    "execute one step at a time"
                )
        result_q: queue.Queue[tuple[str, Any]] = queue.Queue()
        worker = threading.Thread(
            target=self._run_step,
            args=(result_q, instruction, return_type, scope),
            name=f"vouch-step-{self._session_id}-{self._steps_used}",
            daemon=True,
        )
        with self._state_lock:
            self._active_step_thread = worker
        try:
            worker.start()
            hard_deadline = self._deadline + _WALL_CLOCK_GRACE_S
            outcome: tuple[str, Any] | None = None
            while outcome is None:
                try:
                    outcome = result_q.get(timeout=0.1)
                    continue
                except queue.Empty:
                    pass
                if self._cancelled:
                    reason = f": {self._cancel_reason}" if self._cancel_reason else ""
                    self._inject_into_active_step(
                        SessionCancelledError(f"cancelled while running{reason}")
                    )
                    raise SessionCancelledError(
                        f"session {self._session_id} was cancelled while a step was running{reason}"
                    ) from None
                if self._closed:
                    raise SessionCancelledError(
                        f"session {self._session_id} was closed while a step was running"
                    ) from None
                if time.monotonic() >= hard_deadline:
                    # Injections did not stop the worker (monitored tight loop
                    # or an uninterruptible C call). Fail closed anyway and
                    # abandon the daemon thread; JAZ's exec-timeout and
                    # iteration cap bound how long it can keep burning.
                    self._expired = True
                    self._inject_into_active_step(WallClockExceededError(self._timeout_message()))
                    raise WallClockExceededError(self._timeout_message()) from None
        finally:
            with self._state_lock:
                self._active_step_thread = None

        kind, value = outcome
        self._steps_used += 1
        if kind == "ok":
            return value, before
        raise self._translate_step_error(value)

    def step(self, instruction: str, scope: dict[str, Any] | None = None) -> ModelCallResult:
        """Advance one model+REPL round within the session budget."""
        value, before = self._execute(instruction, None, scope)
        after = self._recorder.snapshot()
        raw_value: dict[str, Any] = {}
        try:
            json.dumps(value)
            raw_value["return_value"] = value
        except (TypeError, ValueError):
            raw_value["return_value_repr"] = repr(value)
        # The underlying query count rides along (Gate A2): the supervisor
        # persists it per step so a resumed session can compute the exact
        # monotonic scripted-response cursor from durable state.
        raw_value["llm_calls"] = after["llm_calls"] - before["llm_calls"]
        return ModelCallResult(
            content=value if isinstance(value, str) else repr(value),
            prompt_tokens=after["prompt_tokens"] - before["prompt_tokens"],
            completion_tokens=after["completion_tokens"] - before["completion_tokens"],
            cost_usd=round(after["cost_usd"] - before["cost_usd"], 6),
            model_id=",".join(after["models"]) or self._backend.model,
            raw=raw_value,
        )

    def structured_step(
        self, instruction: str, return_type: type, scope: dict[str, Any] | None = None
    ) -> Any:
        """One round whose return value is validated against ``return_type``."""
        value, _before = self._execute(instruction, return_type, scope)
        return value

    # --- metering ---------------------------------------------------------------

    def usage(self) -> dict[str, Any]:
        """Aggregated metering for this session (tokens, cost, calls, steps)."""
        snap = self._recorder.snapshot()
        snap.update(
            {
                "session_id": self._session_id,
                "mode": self._config.mode.value,
                "steps": self._steps_used,
                "max_steps": self._config.max_steps,
                "max_cost_usd": self._config.max_cost_usd,
                "scripted_remaining": self._backend.responses_remaining,
                "wall_clock_s": self._config.wall_clock_s,
                "wall_clock_remaining_s": round(max(self._deadline - time.monotonic(), 0.0), 3),
                "cancelled": self._cancelled,
                "expired": self._expired,
            }
        )
        return snap


class JazRuntime:
    """Factory for bounded JAZ worker sessions (ports.Runtime)."""

    def open_session(self, config: WorkerSessionConfig) -> JazSession:
        """Open a bounded session.

        Raises contract errors for invalid bounds — never silently degrades.
        Cost-wise this opens a *session*, not a reservation: the controlling
        ledger must reserve before calling (design §10).
        """
        return JazSession(config)

    def backend_id(self) -> str:
        """Identity+version of the model backend actually wired."""
        return JAZ_RUNTIME_ID
