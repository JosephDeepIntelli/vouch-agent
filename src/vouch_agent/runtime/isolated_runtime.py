"""Process-isolated step runtime — the DEFAULT execution boundary (Stage C, M3).

A :class:`RuntimeSession` implementation backed by ONE guarded worker child
per session (``python -m vouch_agent.runtime.worker_process``) instead of the
controller's process. Every step is streamed to that child over the worker
protocol, so within a running session the JAZ REPL namespace and the scripted
provider pool position persist across steps — a draft→final task behaves
exactly like the in-process control (Gate A2's reviewer proof).

Isolation is delegated entirely to the ONE shared guarded launcher
(:class:`vouch_agent.runtime.guards.GuardedWorkerChannel`): validated
nonempty rlimits, narrow environment allowlist, private per-run working
directory, new session (own process group), bounded stdout/stderr/IPC, a
READY handshake whose reported limits must match the request, and
process-group termination at exactly the configured wall clock (no margin).
Missing limits or failed setup raises
:class:`vouch_agent.errors.UnsupportedIsolationError` BEFORE any script
runs; there is never a silent in-process fallback.

Honest isolation statement (design §9/§13.2, unchanged in substance): the
child runs under OS-enforced memory/CPU/file-descriptor ceilings with a
scrubbed environment and parent-enforced wall clock — same OS user, no
privilege drop, no container/seccomp/VM, and filesystem access bounded only
by that user's permissions. These are process limits, not a hardened
sandbox.

Session/restart semantics (Gate A2) — what survives a worker crash:

* RECONSTRUCTIBLE: the durable run state the supervisor persists (declared
  steps and their verified artifacts) and the MONOTONIC QUERY CURSOR — the
  number of provider responses consumed so far, taken from each step
  result's usage. A child that must be replaced between steps is respawned
  with ``scripted_cursor`` set, so consumed responses are never replayed; a
  later step receives earlier steps' VERIFIED artifacts as scoped context
  (``prior_artifacts``), not just their digests.
* NOT RECONSTRUCTIBLE: the child's arbitrary heap and REPL namespace. A
  child that dies MID-step may have consumed queries for it — that step
  fails (fail closed); it is retried only when the child provably never
  started it (no ``step_ack``).

Budget (Gate A3): the parent-side session hands the controller-owned
:class:`~vouch_agent.runtime.ports.QueryBudget` to the channel, which serves
the child's reserve/settle/release RPCs for EVERY underlying query (nested
invokes and retries included). The parent ledger is the only authority: the
child cannot certify itself by returning metering, and a refused reservation
aborts the query before it is dispatched. There is deliberately NO
step-granular reservation anymore — per-query authority is the contract.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import BudgetExhaustedError, UnsupportedIsolationError
from vouch_agent.runtime import guards
from vouch_agent.runtime.fatal_errors import WallClockExceededError
from vouch_agent.runtime.guards import GuardedWorkerChannel, WorkerStepRequest
from vouch_agent.runtime.ports import (
    ModelCallResult,
    Runtime,
    RuntimeSession,
    WorkerSessionConfig,
)

#: Numeric usage keys aggregated across worker incarnations.
_USAGE_NUMERIC_KEYS = ("llm_calls", "prompt_tokens", "completion_tokens", "cost_usd",
                       "unmeasured_calls")

_ZERO_USAGE: dict[str, Any] = {
    "llm_calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cost_usd": 0.0,
    "unmeasured_calls": 0,
}


def _merge_usage(base: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key in _USAGE_NUMERIC_KEYS:
        value = snapshot.get(key)
        if key == "cost_usd":
            merged[key] = round(float(merged.get(key, 0.0)) + float(value or 0.0), 9)
        elif key == "unmeasured_calls":
            merged[key] = int(merged.get(key, 0)) + int(value or 0)
        else:
            merged[key] = int(merged.get(key, 0)) + int(value or 0)
    return merged


class IsolatedStepSession(RuntimeSession):
    """One supervisor session served by one guarded worker child.

    The child is spawned lazily on the first step and kept for the whole
    session (namespace + pool position persist). If it dies BETWEEN steps,
    the next step respawns it with the monotonic cursor — consumed responses
    are never replayed. If it dies MID-step, the step fails closed.
    """

    def __init__(self, config: WorkerSessionConfig) -> None:
        self._config = config
        self._parent_budget = config.query_budget
        self._cursor = int(config.scripted_cursor)
        #: The cursor base the CURRENT child was spawned with (its usage
        #: counts are relative to this position).
        self._spawn_cursor = int(config.scripted_cursor)
        self._steps_used = 0
        self._session_id = f"isolated-{uuid.uuid4().hex[:12]}"
        self._deadline = time.monotonic() + float(config.wall_clock_s)
        self._channel: GuardedWorkerChannel | None = None
        self._closed_usage = dict(_ZERO_USAGE)
        self._cancel_reason: str | None = None
        self._child_snapshot: dict[str, Any] = dict(_ZERO_USAGE)
        #: The current child's snapshot AFTER its previous step — the base the
        #: next step's DELTA is measured from (M4 A1 review §2: the child
        #: reports a per-child CUMULATIVE snapshot; per-step usage must be the
        #: delta, never the cumulative value re-labeled as a step).
        self._prev_child_snapshot: dict[str, Any] = dict(_ZERO_USAGE)

    @property
    def session_id(self) -> str:
        return self._session_id

    def step(self, instruction: str, scope: dict[str, Any] | None = None) -> ModelCallResult:
        frame, delta = self._run_child_step(instruction, scope or {}, structured=None)
        value = frame.get("value")
        raw: dict[str, Any] = {}
        try:
            import json as _json

            _json.dumps(value)
            raw["return_value"] = value
        except (TypeError, ValueError):
            raw["return_value_repr"] = repr(value)
        calls = delta.get("llm_calls")
        unmeasured = int(delta.get("unmeasured_calls", 0) or 0)
        # Per-step DELTA usage (port contract: token/cost fields are step
        # deltas), matching the in-process jaz engine. The child's cumulative
        # session snapshot stays available through usage().
        return ModelCallResult(
            content=value if isinstance(value, str) else repr(value),
            prompt_tokens=delta.get("prompt_tokens"),
            completion_tokens=delta.get("completion_tokens"),
            cost_usd=(
                float(delta["cost_usd"])
                if isinstance(delta.get("cost_usd"), int | float)
                else None
            ),
            model_id="scripted-isolated",
            raw={
                **raw,
                "llm_calls": int(calls or 0),
                "unmeasured": unmeasured > 0,
            },
        )

    def structured_step(
        self, instruction: str, return_type: type, scope: dict[str, Any] | None = None
    ) -> Any:
        frame, _delta = self._run_child_step(
            instruction, scope or {}, structured=return_type.__name__
        )
        return frame.get("value")

    def usage(self) -> dict[str, Any]:
        merged = _merge_usage(self._closed_usage, self._child_snapshot)
        merged.update(
            {
                "session_id": self._session_id,
                "mode": self._config.mode.value,
                "steps": self._steps_used,
                "max_steps": self._config.max_steps,
                "max_cost_usd": self._config.max_cost_usd,
                "scripted_cursor": self._cursor,
                "wall_clock_s": self._config.wall_clock_s,
            }
        )
        return merged

    def close(self) -> None:
        self._drop_channel()

    def cancel(self, reason: str = "cancelled by controller") -> None:
        """Stop the session's guarded worker group promptly (external stop).

        The channel's group stop (TERM -> bounded grace -> KILL) unblocks any
        in-flight ``step()`` with a channel-closed error; per the isolated
        contract the step then fails closed (it may have consumed queries)
        and the supervisor's cancel authority finalizes the run CANCELLED.
        """
        self._cancel_reason = reason
        self._drop_channel()

    # --- internals ---------------------------------------------------------

    def _require_budget_for_step(self) -> None:
        if self._steps_used >= self._config.max_steps:
            raise BudgetExhaustedError(
                f"isolated session reached max_steps={self._config.max_steps}"
            )
        spent = float(self.usage().get("cost_usd") or 0.0)
        if self._config.max_cost_usd is not None and spent > self._config.max_cost_usd + 1e-9:
            raise BudgetExhaustedError(
                f"isolated session spent ${spent:.4f} exceeding "
                f"max_cost_usd={self._config.max_cost_usd}"
            )

    def _remaining_s(self) -> float:
        return self._deadline - time.monotonic()

    def _drop_channel(self) -> None:
        channel, self._channel = self._channel, None
        if channel is not None:
            self._closed_usage = _merge_usage(self._closed_usage, self._child_snapshot)
            self._child_snapshot = dict(_ZERO_USAGE)
            self._prev_child_snapshot = dict(_ZERO_USAGE)
            channel.close()

    def _ensure_channel(self) -> GuardedWorkerChannel:
        if self._channel is not None and self._channel.alive():
            return self._channel
        self._drop_channel()
        remaining = self._remaining_s()
        if remaining <= 0.05:
            raise WallClockExceededError(
                f"session {self._session_id} exceeded its wall-clock budget of "
                f"{self._config.wall_clock_s}s before spawning a worker"
            )
        # The child gets the session's REMAINING wall clock (never a fresh
        # full budget) and the pool position from the monotonic cursor.
        child_config = replace(
            self._config,
            scripted_responses=self._config.scripted_responses,
            scripted_cursor=self._cursor,
            wall_clock_s=remaining,
            max_steps=max(1, self._config.max_steps - self._steps_used),
            max_cost_usd=self._child_cost_cap(),
            query_budget=None,  # the budget travels via the channel RPC, not the job
        )
        self._channel = GuardedWorkerChannel(
            child_config,
            budget=self._parent_budget,
            stream=True,
        )
        self._spawn_cursor = self._cursor
        return self._channel

    def _child_cost_cap(self) -> float | None:
        cap = self._config.max_cost_usd
        if cap is None:
            return None
        remaining = cap - float(self.usage().get("cost_usd") or 0.0)
        return round(remaining, 6) if remaining > 1e-9 else None

    def _run_child_step(
        self, instruction: str, scope: dict[str, Any], *, structured: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one step; returns ``(result frame, per-step usage DELTA)``."""
        self._require_budget_for_step()
        request = WorkerStepRequest(
            instruction=instruction,
            scope=dict(scope or {}),
            structured=structured is not None,
            return_type=structured,
        )
        frame: dict[str, Any] | None = None
        for attempt in range(2):
            channel = self._ensure_channel()
            try:
                index = channel.send_step(request)
            except guards.ChannelClosed as exc:
                # The step frame was never delivered — the child cannot have
                # started the step. Respawn (same cursor) and retry once.
                self._drop_channel()
                if attempt == 0:
                    continue
                raise UnsupportedIsolationError(
                    f"could not deliver the step to the isolated worker: {exc}"
                ) from exc
            try:
                frame = channel.read_step(index)
                break
            except guards.ChannelClosed as exc:
                started = exc.step_started
                self._drop_channel()
                if started or attempt == 1:
                    # The child ACKED this step (or we are out of retries):
                    # it may have consumed queries for it, so the step fails
                    # closed — never retried, never replayed against the
                    # same cursor.
                    raise UnsupportedIsolationError(
                        "isolated worker died mid-step (after acknowledging it); "
                        "the step may have consumed provider queries, so it "
                        f"fails closed instead of replaying: {exc}"
                    ) from exc
                # Un-acked death BETWEEN steps: respawn with the same cursor
                # and retry — consumed responses are never replayed.
                continue
        if frame is None:  # pragma: no cover - the loop always breaks or raises
            raise UnsupportedIsolationError("isolated step produced no result frame")
        self._steps_used += 1
        snapshot = frame.get("usage")
        delta = dict(_ZERO_USAGE)
        if isinstance(snapshot, dict):
            prev = self._prev_child_snapshot
            delta = {
                "llm_calls": max(
                    0, int(snapshot.get("llm_calls", 0) or 0) - int(prev.get("llm_calls", 0) or 0)
                ),
                "prompt_tokens": max(
                    0,
                    int(snapshot.get("prompt_tokens", 0) or 0)
                    - int(prev.get("prompt_tokens", 0) or 0),
                ),
                "completion_tokens": max(
                    0,
                    int(snapshot.get("completion_tokens", 0) or 0)
                    - int(prev.get("completion_tokens", 0) or 0),
                ),
                "cost_usd": round(
                    max(
                        0.0,
                        float(snapshot.get("cost_usd", 0.0) or 0.0)
                        - float(prev.get("cost_usd", 0.0) or 0.0),
                    ),
                    9,
                ),
                "unmeasured_calls": max(
                    0,
                    int(snapshot.get("unmeasured_calls", 0) or 0)
                    - int(prev.get("unmeasured_calls", 0) or 0),
                ),
            }
            self._child_snapshot = dict(snapshot)
            self._prev_child_snapshot = dict(snapshot)
            # The monotonic cursor advances from the child's own count.
            calls = int(snapshot.get("llm_calls", 0) or 0)
            self._cursor = max(self._cursor, self._spawn_cursor + calls)
        return frame, delta


class IsolatedStepRuntime(Runtime):
    """Runtime factory: one guarded worker child per session (the default)."""

    def open_session(self, config: WorkerSessionConfig) -> RuntimeSession:
        return IsolatedStepSession(config)

    def backend_id(self) -> str:
        return "scripted-isolated@worker-process"


def isolated_default_config(
    *, mode: RunMode = RunMode.FIXTURE, scripted: tuple[str, ...] = (), **overrides: Any
) -> WorkerSessionConfig:
    """Convenience builder for fixture-mode isolated sessions."""
    config = WorkerSessionConfig(
        mode=mode,
        max_steps=8,
        wall_clock_s=60.0,
        max_cost_usd=0.5,
        scripted_responses=scripted,
        allow_timeout_pragma=False,
    )
    if overrides:
        config = replace(config, **overrides)
    return config
