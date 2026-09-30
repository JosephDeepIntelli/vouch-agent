"""Child entry for isolated worker sessions: ``python -m vouch_agent.runtime.worker_process``.

Protocol (JSON lines; stdout carries ONLY protocol frames, diagnostics go to
stderr — design §4.2). v2 adds streamed steps and the parent budget RPC
(M3 Gate A2/A3)::

    stdin  -> one job frame:
        {"config": {...WorkerSessionConfig fields incl. scriptedCursor...},
         "limits": {"rlimit_as_bytes": int, "rlimit_cpu_s": int, "rlimit_nofile": int},
         "stream": bool, "queryBudgetRpc": bool,
         "steps": [{"instruction": str, "scope": {...}, "structured": bool,
                    "return_type": "int" | ... | null}, ...]}
      then, when "stream" or "queryBudgetRpc" is true, further lines routed by
      frame kind:
        {"frame": "step", "index": i, ...}            (streamed step request)
        {"frame": "budget_response", "rid": n, ...}   (reply to a budget RPC)

    stdout <- {"frame": "ready", "pid": int, "limits": {...applied...}}
           |  {"frame": "step_ack", "index": i}           (streamed steps only;
           |   emitted the moment the step is dequeued, BEFORE it runs)
           |  {"frame": "step_result", "index": i, "ok": true, "value": ...,
               "usage": {...}}
           |  {"frame": "step_result", "index": i, "ok": false, "code": ...,
               "type": ..., "message": ..., "usage": {...}}
           |  {"frame": "budget_request", "rid": n, "op": "reserve"|"settle"|"release",
               "estimateUsd": float|null, "reservationId": str, "actualUsd": float|null}
           |  {"frame": "error", "code": ..., "type": ..., "message": ...}
    exit 0 on clean completion, 2 on a terminal session error, 3 if the
    isolation limits could not be applied (the controller maps that to
    ``UnsupportedIsolationError`` and fails closed).

Session semantics (Gate A2): with ``stream: true`` ONE child process serves
the WHOLE supervisor session — the JAZ REPL namespace and the scripted pool
position persist across steps. A job whose ``config.scriptedCursor`` is N
skips the first N scripted responses, so a restarted child NEVER replays
consumed responses. What dies with the child is NOT reconstructible: only
declared state (persisted step artifacts, the cursor) survives, never the
arbitrary heap/namespace.

Budget authority (Gate A3): with ``queryBudgetRpc: true`` every underlying
query — nested invokes and retries included — asks the PARENT for a
reservation through the RPC above BEFORE it is dispatched and settles with
the parent afterwards. The child cannot certify itself: the parent ledger is
the only authority; a refused reply aborts the query before it runs.

The child applies its resource limits BEFORE importing the runtime stack, so
the whole JAZ/REPL machinery runs under the ceiling. A job with an EMPTY
limits mapping is refused (exit 3): no limits, no run. It inherits only the
launcher's narrow environment allowlist (see ``guards._worker_env``) and no
credentials by construction.

Protocol transport (M4 A1): frames travel on a DEDICATED DESCRIPTOR — a
private ``dup`` of the real stdout captured at module import, BEFORE any JAZ
machinery can install its capturing ``sys.stdout`` proxy. JAZ's REPL capture
redirects dynamic ``sys.stdout`` writes (including anything the runtime stack
emits while a nested invoke is executing) into the current exec buffer; the
budget RPC and every other frame therefore write straight to the dup'd
descriptor instead. Generated-code output can never become a protocol
message, and a protocol message can never be swallowed as generated output.

What this process is NOT (no production sandbox claims): same OS user as the
controller (no privilege drop is implemented — attempted only under an
explicitly configured root setup, which this milestone does not ship), no
container/seccomp/VM, and filesystem access bounded only by the user's
permissions — a same-user process can still read host files the user can
read. It is one honest layer — OS-enforced memory/CPU/file-descriptor
ceilings plus process-group wall-clock kill — on top of the JAZ REPL's
deny-all allow-lists, not a hardened sandbox.
"""

from __future__ import annotations

import json
import os
import queue
import resource
import sys
import threading
from typing import Any

_EMIT_LOCK = threading.Lock()

#: The dedicated protocol descriptor: a private dup of fd 1 taken at module
#: import — before JAZ (imported later, under the applied limits) can install
#: its capturing ``sys.stdout`` proxy. All frames are written here directly,
#: so swapping/routing ``sys.stdout`` cannot redirect or forge protocol
#: traffic (M4 A1: model output must never become protocol messages).
_PROTOCOL_FD: int | None = None
try:  # pragma: no cover - trivial, but keep the fallback honest
    _PROTOCOL_FD = os.dup(1)
except (OSError, ValueError):
    _PROTOCOL_FD = None

#: Immutable pre-capture stream fallback for environments where fd 1 cannot
#: be dup'd (no real descriptor behind stdout). Holding the OBJECT — not
#: looking up ``sys.stdout`` at write time — is what makes it pre-capture.
_PRE_CAPTURE_STDOUT = sys.stdout


def _emit(frame: dict[str, object]) -> None:
    """Write one protocol frame on the dedicated protocol channel."""
    data = (json.dumps(frame) + "\n").encode("utf-8")
    with _EMIT_LOCK:
        if _PROTOCOL_FD is not None:
            view = memoryview(data)
            while view:
                view = view[os.write(_PROTOCOL_FD, view) :]
        else:  # pragma: no cover - only when fd 1 is not dup-able
            _PRE_CAPTURE_STDOUT.write(data.decode("utf-8", "replace"))
            _PRE_CAPTURE_STDOUT.flush()


def _apply_limits(limits: dict[str, Any]) -> dict[str, Any]:
    """Apply the job's resource limits; raise on any failure (fail closed)."""
    applied: dict[str, Any] = {}

    as_bytes = limits.get("rlimit_as_bytes")
    if as_bytes is not None:
        soft = int(as_bytes)
        resource.setrlimit(resource.RLIMIT_AS, (soft, soft))
        applied["rlimit_as_bytes"] = resource.getrlimit(resource.RLIMIT_AS)[0]

    cpu_s = limits.get("rlimit_cpu_s")
    if cpu_s is not None:
        cpu = int(cpu_s)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        applied["rlimit_cpu_s"] = resource.getrlimit(resource.RLIMIT_CPU)[0]

    nofile = limits.get("rlimit_nofile")
    if nofile is not None:
        nf = int(nofile)
        resource.setrlimit(resource.RLIMIT_NOFILE, (nf, nf))
        applied["rlimit_nofile"] = resource.getrlimit(resource.RLIMIT_NOFILE)[0]

    return applied


class _StdinRouter:
    """Route stdin lines: budget replies to waiting RPCs, steps to a queue.

    Runs as a daemon thread so a parent that disappears (closed pipes) is
    noticed: EOF fails every pending RPC instead of leaving the child hung.
    """

    def __init__(self) -> None:
        self._steps: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._waiters: dict[int, list[Any]] = {}
        self._closed = False

    def start(self) -> None:
        threading.Thread(target=self._read_loop, daemon=True).start()

    # -- step queue -----------------------------------------------------------

    def next_step(self, timeout_s: float | None = None) -> dict[str, Any] | None:
        try:
            step = self._steps.get(timeout=timeout_s)
        except queue.Empty:
            return None
        return step if step is not None else None

    # -- RPC plumbing ------------------------------------------------------------

    def register(self, rid: int) -> list[Any]:
        """Register a waiter slot for ``rid``; returns its mutable cell."""
        cell: list[Any] = [threading.Event(), None]
        with self._lock:
            if self._closed:
                cell[1] = {"ok": False, "error": {"message": "parent stdin closed"}}
                cell[0].set()
            else:
                self._waiters[rid] = cell
        return cell

    def unregister(self, rid: int, cell: list[Any]) -> None:
        with self._lock:
            self._waiters.pop(rid, None)

    def _fail_all(self, message: str) -> None:
        with self._lock:
            self._closed = True
            waiters = list(self._waiters.values())
            self._waiters.clear()
        for cell in waiters:
            cell[1] = {"ok": False, "error": {"message": message}}
            cell[0].set()
        self._steps.put(None)

    # -- reader ------------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue  # never let diagnostics on stdin kill the session
                if not isinstance(frame, dict):
                    continue
                if frame.get("frame") == "budget_response" and isinstance(
                    frame.get("rid"), int
                ):
                    with self._lock:
                        cell = self._waiters.pop(int(frame["rid"]), None)
                    if cell is not None:
                        cell[1] = frame
                        cell[0].set()
                elif frame.get("frame") == "step":
                    self._steps.put(frame)
        except OSError:
            pass
        finally:
            self._fail_all("parent closed the worker's stdin (parent died?)")


class _ParentBudgetRpc:
    """The child-side :class:`~vouch_agent.runtime.ports.QueryBudget`.

    Every underlying model query — nested invokes and retries included —
    reserves through the parent BEFORE it is dispatched. The parent ledger is
    the ONLY authority: this object can ask and report, never decide. A
    refused reserve raises a ``BudgetError`` which the JAZ reservation hook
    turns into a fatal abort before the query runs.
    """

    def __init__(self, router: _StdinRouter, *, timeout_s: float) -> None:
        self._router = router
        self._timeout_s = max(1.0, float(timeout_s))
        self._rid = 0

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        from vouch_agent.errors import BudgetError

        self._rid += 1
        rid = self._rid
        cell = self._router.register(rid)
        try:
            _emit({"frame": "budget_request", "rid": rid, **payload})
            if not cell[0].wait(timeout=self._timeout_s):
                raise BudgetError(
                    f"parent budget RPC {payload.get('op')!r} timed out after "
                    f"{self._timeout_s:.0f}s (the parent enforces the wall clock; "
                    "a timeout here means the protocol is broken)"
                )
            reply = cell[1]
        finally:
            self._router.unregister(rid, cell)
        if not isinstance(reply, dict) or not reply.get("ok"):
            error = dict((reply or {}).get("error") or {})
            message = str(error.get("message") or "parent budget refused the call")
            code = str(error.get("code") or "vouch/budget")
            raise BudgetError(f"parent budget authority refused: {code}: {message}")
        return dict(reply)

    def reserve_query(self, estimate_usd: float | None) -> str:
        reply = self._call(
            {
                "op": "reserve",
                "estimateUsd": None if estimate_usd is None else float(estimate_usd),
            }
        )
        reservation_id = reply.get("reservationId")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ValueError("parent budget reserve reply carried no reservation id")
        return reservation_id

    def settle_query(self, reservation_id: str, actual_usd: float | None) -> None:
        self._call(
            {
                "op": "settle",
                "reservationId": reservation_id,
                "actualUsd": None if actual_usd is None else float(actual_usd),
            }
        )

    def release_query(self, reservation_id: str) -> None:
        self._call({"op": "release", "reservationId": reservation_id})


def _read_job() -> dict[str, Any] | None:
    try:
        line = sys.stdin.readline()
        if not line.strip():
            _emit(
                {
                    "frame": "error",
                    "code": "vouch/protocol-frame",
                    "type": "ProtocolFrameError",
                    "message": "worker received an empty job frame",
                }
            )
            return None
        job = json.loads(line)
        if not isinstance(job, dict):
            raise ValueError("job frame is not an object")
        return job
    except (json.JSONDecodeError, ValueError) as exc:
        _emit(
            {
                "frame": "error",
                "code": "vouch/protocol-frame",
                "type": "ProtocolFrameError",
                "message": f"worker received a non-JSON job frame: {exc}",
            }
        )
        return None


def main() -> int:
    job = _read_job()
    if job is None:
        return 2

    limits = dict(job.get("limits") or {})
    if not limits:
        # Gate A1: a guarded child without a nonempty limit set refuses to
        # run — the controller must fail closed, never run us unbounded.
        _emit(
            {
                "frame": "prep_error",
                "code": "vouch/unsupported-isolation",
                "type": "UnsupportedIsolationError",
                "message": "job carried no resource limits; a guarded worker "
                "refuses to run without them (fail closed)",
            }
        )
        return 3
    try:
        applied = _apply_limits(limits)
    except (ValueError, OSError) as exc:
        # Isolation could not be established — say so loudly; the controller
        # must fail closed rather than run unbounded (design §13.2).
        _emit(
            {
                "frame": "prep_error",
                "code": "vouch/unsupported-isolation",
                "type": "UnsupportedIsolationError",
                "message": f"could not apply worker resource limits: {exc}",
            }
        )
        return 3

    # Heavy imports happen only under the applied limits.
    from vouch_agent.contracts.common import RunMode
    from vouch_agent.errors import VouchError
    from vouch_agent.runtime import guards
    from vouch_agent.runtime.jaz_engine import JazRuntime
    from vouch_agent.runtime.ports import WorkerSessionConfig

    _emit({"frame": "ready", "pid": os.getpid(), "limits": applied})

    cfg = job.get("config") or {}
    stream = bool(job.get("stream"))
    rpc = bool(job.get("queryBudgetRpc"))
    router: _StdinRouter | None = None
    budget: Any = None
    if stream or rpc:
        router = _StdinRouter()
        router.start()
    if rpc and router is not None:
        budget = _ParentBudgetRpc(router, timeout_s=float(cfg.get("wall_clock_s", 60.0)))

    config = WorkerSessionConfig(
        mode=RunMode(str(cfg.get("mode", "fixture"))),
        max_steps=int(cfg.get("max_steps", 16)),
        wall_clock_s=float(cfg.get("wall_clock_s", 60.0)),
        max_cost_usd=(float(cfg["max_cost_usd"]) if cfg.get("max_cost_usd") is not None else None),
        scripted_responses=tuple(cfg.get("scripted_responses") or ()),
        allow_timeout_pragma=bool(cfg.get("allow_timeout_pragma", False)),
        scripted_cursor=int(cfg.get("scripted_cursor", 0) or 0),
        query_budget=budget,
    )

    runtime = JazRuntime()
    steps: list[dict[str, Any]] = list(job.get("steps") or [])
    try:
        session = runtime.open_session(config)
    except VouchError as exc:
        _emit(
            {
                "frame": "error",
                "code": exc.code,
                "type": type(exc).__name__,
                "message": str(exc),
            }
        )
        return 2

    def _run_step(index: int, step: dict[str, Any]) -> int:
        """Execute one step; emit its result frame. Returns the exit code."""
        instruction = str(step.get("instruction", ""))
        scope = dict(step.get("scope") or {})
        structured = bool(step.get("structured"))
        try:
            if structured:
                from vouch_agent.errors import ContractError

                return_type = guards.structured_type_for(
                    step.get("return_type") if step.get("return_type") else None
                )
                if return_type is None:
                    raise ContractError(
                        "structured worker step requires return_type in "
                        f"{sorted(guards._STRUCTURED_TYPES)}"
                    )
                value = session.structured_step(instruction, return_type, scope=scope)
            else:
                result = session.step(instruction, scope=scope)
                # Plain steps return a ModelCallResult; the JSON frame
                # carries the agent's return value when it is JSON-safe.
                value = result.raw.get("return_value", result.content)
            try:
                json.dumps(value)
                payload: dict[str, object] = {"value": value}
            except (TypeError, ValueError):
                payload = {"value_repr": repr(value)}
            _emit(
                {
                    "frame": "step_result",
                    "index": index,
                    "ok": True,
                    "usage": session.usage(),
                    **payload,
                }
            )
            return 0
        except VouchError as exc:
            _emit(
                {
                    "frame": "step_result",
                    "index": index,
                    "ok": False,
                    "code": exc.code,
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "usage": session.usage(),
                }
            )
            return 2

    try:
        for index, step in enumerate(steps):
            if _run_step(index, step) != 0:
                return 2
        if stream and router is not None:
            # Streamed steps keep this child alive for the whole session:
            # REPL namespace and scripted-pool position persist across steps
            # (Gate A2). step_ack is emitted BEFORE the step runs so a parent
            # that sees the channel die can tell "never started" (retryable
            # with the same cursor) from "may have consumed queries" (fatal).
            while True:
                streamed_step: dict[str, Any] | None = router.next_step()
                if streamed_step is None:
                    break
                step = streamed_step
                index = int(step.get("index", 0))
                _emit({"frame": "step_ack", "index": index})
                if _run_step(index, step) != 0:
                    return 2
        return 0
    except VouchError as exc:
        _emit(
            {
                "frame": "error",
                "code": exc.code,
                "type": type(exc).__name__,
                "message": str(exc),
            }
        )
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
