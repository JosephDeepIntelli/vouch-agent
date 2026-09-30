"""Offline integrity and (best-effort) subprocess isolation guards.

Three mechanisms live here, with deliberately different strength claims:

1. ``no_network_guard`` — a *belt-and-braces* socket-level block active for
   the duration of fixture/offline steps. The primary live-call block is at
   the backend seam (only the scripted backend is ever wired in those modes),
   but if anything else in the process attempts an outbound connection while
   the guard is active, it raises a fatal ``LiveCallBlockedError``. The guard
   patches ``socket`` process-globally while active and restores it on exit —
   honest limitation: it is Python-level, so only code that goes through the
   ``socket`` module is caught.

2. ``inject_exception`` — the mechanism behind wall-clock/cancellation
   enforcement: raise an exception in another thread via
   ``PyThreadState_SetAsyncExc``. Best-effort by nature (cannot interrupt an
   arbitrary blocking C call; can be delayed). NOT a trust boundary.

3. ``GuardedWorkerChannel`` / ``run_session_isolated`` — THE one guarded
   launcher every process-isolated path goes through (M3 Gate A1): a worker
   child with validated nonempty resource limits (``RLIMIT_AS`` /
   ``RLIMIT_CPU`` / ``RLIMIT_NOFILE``), a narrow environment *allowlist*, a
   private per-run working directory, its own session (setsid), bounded
   stdout/stderr/IPC capture, a READY handshake whose reported limits must
   match what was requested, process-group termination (descendants
   included), and a parent-enforced wall clock that is exactly the
   configured deadline — no grace added on top. When any required
   protection cannot be established — non-POSIX platform, missing
   ``resource`` support, empty/invalid limits, spawn failure, or a bad
   worker handshake — it raises
   :class:`vouch_agent.errors.UnsupportedIsolationError` BEFORE any script
   runs (design §13.2: fail closed, never quietly execute on the host,
   never fall back inline).

What the process isolation does NOT give you (no production sandbox claims,
per AGENTS.md and design §9): it does not use a container/seccomp/VM, the
child runs as the same OS user unless privilege drop is explicitly configured
and supported, and filesystem access is bounded only by that user's
permissions — a same-user process can still read host files the user can
read. These are process *limits*, not a hardened sandbox. Model-authored code
still runs under the JAZ REPL's deny-all import/file allow-lists *in
addition*. Real sandbox backends must pass attack testing before carrying
real projects (design §9).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import queue as queue_mod
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, ClassVar, Protocol

from vouch_agent.errors import ContractError, ProtocolFrameError, UnsupportedIsolationError
from vouch_agent.runtime.fatal_errors import LiveCallBlockedFatal, WallClockExceededError

__all__ = [
    "WORKER_LIMITS",
    "ChannelClosed",
    "GuardedWorkerChannel",
    "IsolatedRunResult",
    "WorkerStepRequest",
    "WorkerStepResult",
    "assert_isolation_supported",
    "inject_exception",
    "no_network_guard",
    "run_session_isolated",
    "stop_process_group",
    "validate_worker_limits",
]


# --- thread-level exception injection ---------------------------------------


def inject_exception(target: threading.Thread, exc: BaseException) -> bool:
    """Raise ``exc`` in ``target`` at its next bytecode boundary.

    Returns whether the injection was accepted by CPython. Best-effort: the
    exception lands only when the target next executes Python bytecode, so a
    thread blocked in a C call is not interrupted until it returns. Callers
    that need a hard guarantee must use process-level isolation.
    """
    if target.ident is None:
        return False
    fn = ctypes.pythonapi.PyThreadState_SetAsyncExc
    # argtypes are load-bearing: without them ctypes passes the 64-bit thread
    # id as a C int, silently truncating it (returns 0 = "not found" and the
    # injection is dropped).
    fn.argtypes = [ctypes.c_ulong, ctypes.py_object]
    fn.restype = ctypes.c_int
    return fn(ctypes.c_ulong(target.ident), exc) == 1


# --- socket-level no-network guard -------------------------------------------


def _blocked(session_id: str) -> Callable[..., Any]:
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise LiveCallBlockedFatal(
            f"network access attempted during fixture/offline session {session_id}; "
            "live calls are blocked by the no-network guard (design §13.2)"
        )

    return _raise


class no_network_guard:
    """Block outbound socket use for the duration of the context.

    Patches ``socket.socket.connect`` / ``socket.create_connection`` /
    ``socket.getaddrinfo`` process-wide while active (restored on exit), so
    any library that opens a connection in this window — not just the model
    backend — fails with a fatal ``LiveCallBlockedError``. Reentrant-safe by
    refcounting: nested guards keep the outermost patch in place.
    """

    _depth = 0
    _lock = threading.Lock()
    _saved: ClassVar[dict[str, Any]] = {}

    def __init__(self, session_id: str = "session") -> None:
        self._session_id = session_id

    def __enter__(self) -> None:
        with no_network_guard._lock:
            if no_network_guard._depth == 0:
                blocker = _blocked(self._session_id)
                no_network_guard._saved = {
                    "connect": socket.socket.connect,
                    "create_connection": socket.create_connection,
                    "getaddrinfo": socket.getaddrinfo,
                }
                socket.socket.connect = blocker  # type: ignore[method-assign]
                socket.create_connection = blocker
                socket.getaddrinfo = blocker
            no_network_guard._depth += 1

    def __exit__(self, *exc_info: object) -> None:
        with no_network_guard._lock:
            no_network_guard._depth -= 1
            if no_network_guard._depth == 0:
                socket.socket.connect = no_network_guard._saved["connect"]  # type: ignore[method-assign]
                socket.create_connection = no_network_guard._saved["create_connection"]
                socket.getaddrinfo = no_network_guard._saved["getaddrinfo"]
                no_network_guard._saved = {}


# --- the shared guarded launcher (M3 Gate A1) ----------------------------------


@dataclass(frozen=True)
class WorkerStepRequest:
    """One step to run in the isolated worker (JSON-safe payload only)."""

    instruction: str
    scope: dict[str, Any] = field(default_factory=dict)
    structured: bool = False
    #: Name of a builtin return type for structured steps: int/float/str/bool/
    #: list/dict (the process boundary carries names, not classes).
    return_type: str | None = None


@dataclass(frozen=True)
class WorkerStepResult:
    """One step's outcome from the isolated worker."""

    ok: bool
    value: Any = None
    error_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IsolatedRunResult:
    """Everything the isolated run reported back."""

    steps: list[WorkerStepResult]
    limits_applied: dict[str, Any]
    worker_pid: int | None = None


class _WorkerProcess(Protocol):
    """The slice of ``subprocess.Popen`` the runner relies on (test-injectable)."""

    stdin: IO[bytes] | None
    stdout: IO[bytes] | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


#: The narrow environment ALLOWLIST a worker child may see. Everything else
#: in the controller's environment — credential-shaped or not — is dropped:
#: deny-lists proved insufficient (M3 Gate A1), so the child gets exactly
#: what it needs to boot Python plus the computed PYTHONPATH.
_ENV_ALLOWLIST = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SYSTEMROOT",
    "TMPDIR",
    "TZ",
)

#: Floor for an address-space limit: a CPython 3.12+ interpreter needs far
#: more than 64 MiB of virtual address space just to boot; anything lower
#: cannot produce a working (handshaking) worker and is refused up front.
_MIN_RLIMIT_AS_BYTES = 64 * 1024 * 1024

#: The default validated limits every guarded worker runs under. A child
#: WITHOUT a nonempty set of validated limits is refused before it runs.
WORKER_LIMITS: dict[str, int] = {
    # Address-space ceiling in bytes for the worker process. Python 3.13
    # baseline VSZ is well under this on Linux; raise only with evidence.
    "rlimit_as_bytes": 768 * 1024 * 1024,
    # CPU-seconds ceiling; the parent's wall clock is enforced separately.
    "rlimit_cpu_s": 30,
    "rlimit_nofile": 128,
}

#: Hard output bounds the launcher enforces while accumulating bytes — a
#: worker cannot buffer unbounded stdout/stderr in the parent (Gate A1).
#: One protocol frame may not exceed this many bytes...
MAX_FRAME_BYTES = 2 * 1024 * 1024
#: ... stdout as a whole may not exceed this many bytes ...
MAX_STDOUT_BYTES = 16 * 1024 * 1024
#: ... and only this tail of stderr is kept for diagnostics.
MAX_STDERR_TAIL_BYTES = 64 * 1024

#: Grace between SIGTERM and SIGKILL for process-group termination.
_TERM_GRACE_S = 2.0
#: Bound for the group to disappear after SIGKILL before giving up waiting
#: (the KILL itself cannot be ignored; this only bounds the reap wait).
_KILL_GRACE_S = 5.0
#: Poll interval while waiting for an owned process group to disappear.
_GROUP_POLL_S = 0.02

#: Builtin names a structured step may name across the process boundary.
_STRUCTURED_TYPES: dict[str, type] = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "list": list,
    "dict": dict,
}


def assert_isolation_supported() -> None:
    """Fail closed (``UnsupportedIsolationError``) where isolation can't exist."""
    if os.name != "posix":
        raise UnsupportedIsolationError(
            f"process isolation requires a POSIX platform (resource limits, "
            f"process groups); refusing to run untrusted code on {os.name!r}"
        )
    try:
        import resource  # noqa: F401
    except ImportError:
        raise UnsupportedIsolationError(
            "the resource module (RLIMIT_*) is unavailable; cannot establish "
            "the required resource limits for the worker process"
        ) from None


def validate_worker_limits(limits: Mapping[str, Any] | None) -> dict[str, int]:
    """Validate a worker limit set; refuse anything not safely enforceable.

    A guarded child must run under a NONEMPTY set of positive integer limits
    the child knows how to apply; empty, wrongly-typed, zero/negative or
    unknown-key sets are refused with ``UnsupportedIsolationError`` BEFORE
    any process is spawned (Gate A1: no limits, no run).
    """
    if not isinstance(limits, Mapping) or not limits:
        raise UnsupportedIsolationError(
            "a guarded worker requires a nonempty set of validated resource "
            "limits; refusing to run without them (fail closed)"
        )
    validated: dict[str, int] = {}
    for key, value in dict(limits).items():
        if key not in WORKER_LIMITS:
            raise UnsupportedIsolationError(
                f"unknown worker limit {key!r}; supported: {sorted(WORKER_LIMITS)}"
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise UnsupportedIsolationError(
                f"worker limit {key!r} must be an integer, got {value!r}"
            )
        if value < 1:
            raise UnsupportedIsolationError(f"worker limit {key!r} must be >= 1, got {value!r}")
        if key == "rlimit_as_bytes" and value < _MIN_RLIMIT_AS_BYTES:
            raise UnsupportedIsolationError(
                f"rlimit_as_bytes={value} is below the {_MIN_RLIMIT_AS_BYTES} byte floor "
                "a CPython worker needs to boot; refusing a limit that cannot "
                "produce a working child"
            )
        validated[key] = value
    return validated


def _worker_env() -> dict[str, str]:
    """The worker environment: a narrow ALLOWLIST plus computed PYTHONPATH.

    Deny-list scrubbing is not used anymore: the child receives ONLY the
    explicitly allowed keys, so a credential-shaped variable that matches no
    deny pattern still never reaches the worker (Gate A1).
    """
    src_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (src_root, os.environ.get("PYTHONPATH", "")) if p
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal a whole process group; False when it no longer exists."""
    if not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - group exists, not ours to signal
        return True
    except OSError:  # pragma: no cover - defensive
        return False


def _group_alive(pgid: int | None) -> bool:
    """Whether ANY process still belongs to the group (M4 A1 review §4).

    ``killpg(pgid, 0)`` succeeds while the group has at least one member —
    including an unreaped zombie leader, which callers must reap for the
    group to read as gone. ``None`` means "no group identity is known".
    """
    if pgid is None or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - members exist, not ours
        return True
    except OSError:  # pragma: no cover - defensive
        return True


def _reap_leader(proc: _WorkerProcess) -> None:
    """Non-blocking reap of the known leader (a zombie keeps its group id)."""
    with contextlib.suppress(Exception):
        proc.poll()


def stop_process_group(proc: _WorkerProcess, *, pgid: int | None = None) -> None:
    """Terminate a worker's whole process group and VERIFY it is gone.

    The guarded child runs in its own session (``start_new_session=True``),
    so its process group covers everything it spawned. The group identity is
    resolved ONCE up front (``pgid`` when the caller captured it at spawn,
    else ``os.getpgid(pid)``) and retained: escalation is driven by GROUP
    liveness, independent of whether the leader already exited — a leader
    that dies on TERM while a TERM-resistant descendant lives must not end
    the stop early (M4 A1 review §4). TERM the group, wait a bounded grace
    for the group to disappear (reaping the known leader along the way so a
    zombie leader cannot keep it nominally alive), then SIGKILL the group
    and wait a final bounded grace. Degrades to leader-only terminate/kill
    for process objects without a real pid or any resolvable group
    (fault-injection fakes).
    """
    pid = getattr(proc, "pid", None)
    if pgid is None and isinstance(pid, int) and pid > 0 and hasattr(os, "getpgid"):
        with contextlib.suppress(OSError):
            pgid = os.getpgid(int(pid))

    if pgid is None:
        # No group identity (fault-injection fakes, or the leader was already
        # reaped before any group was captured): leader-only fallback with the
        # same TERM -> bounded grace -> KILL shape.
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=_TERM_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=_KILL_GRACE_S)
        return

    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_S
    while True:
        _reap_leader(proc)
        if not _group_alive(pgid):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_GROUP_POLL_S, remaining))
    # Bounded grace expired with group members still alive — escalate hard,
    # regardless of leader state. A SIGKILLed group cannot survive; the wait
    # only bounds reaping.
    _signal_group(pgid, signal.SIGKILL)
    kill_deadline = time.monotonic() + _KILL_GRACE_S
    while True:
        _reap_leader(proc)
        if not _group_alive(pgid):
            break
        remaining = kill_deadline - time.monotonic()
        if remaining <= 0:  # pragma: no cover - KILL cannot be ignored
            break
        time.sleep(min(_GROUP_POLL_S, remaining))
    with contextlib.suppress(Exception):
        proc.wait(timeout=_GROUP_POLL_S * 5)


def _spawn_default(argv: list[str], cwd: str, env: dict[str, str]) -> _WorkerProcess:
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        # New session: the worker leads its own process group, so terminal
        # signals and group-wide stops do not reach it (and vice versa) and
        # stop_process_group() can kill the whole tree at the wall clock.
        start_new_session=True,
    )


def _map_worker_error(frame: Mapping[str, Any]) -> BaseException:
    """Rebuild the worker's Vouch error on the controller side (fail closed).

    The usage the worker measured before failing rides along on the rebuilt
    error (review A2): the parent must not settle zero spend for queries the
    child already paid for just because the step failed.
    """
    from vouch_agent import errors as vouch_errors
    from vouch_agent.runtime import fatal_errors as runtime_fatal
    from vouch_agent.runtime.failure_usage import attach_usage

    registry: dict[str, type[BaseException]] = {
        "ReplayExhaustedError": vouch_errors.ReplayExhaustedError,
        "ReplayExhaustedFatal": runtime_fatal.ReplayExhaustedFatal,
        "LiveCallBlockedError": vouch_errors.LiveCallBlockedError,
        "LiveCallBlockedFatal": runtime_fatal.LiveCallBlockedFatal,
        "BudgetExhaustedError": vouch_errors.BudgetExhaustedError,
        "QueryBudgetRefusedFatal": runtime_fatal.QueryBudgetRefusedFatal,
        "WallClockExceededError": runtime_fatal.WallClockExceededError,
        "SessionCancelledError": runtime_fatal.SessionCancelledError,
        "ContractError": vouch_errors.ContractError,
        "InvalidStateTransitionError": vouch_errors.InvalidStateTransitionError,
        "UnsupportedIsolationError": vouch_errors.UnsupportedIsolationError,
        "ProtocolFrameError": vouch_errors.ProtocolFrameError,
    }
    type_name = str(frame.get("type", ""))
    message = str(frame.get("message", "worker-reported failure"))
    cls = registry.get(type_name)
    if cls is None:
        raise ProtocolFrameError(
            f"worker reported unmapped error type {type_name!r}: {message} "
            "(fail closed: unknown worker failures are never success)"
        ) from None
    mapped = cls(message)
    usage = frame.get("usage")
    if isinstance(usage, dict) and usage:
        attach_usage(mapped, dict(usage))
    return mapped


class ChannelClosed(UnsupportedIsolationError):
    """The worker channel died mid-protocol.

    ``step_started`` records whether the step's ``step_ack`` frame had
    arrived: only a step the child never acked is safe to retry (no query
    can have been consumed for it).
    """

    def __init__(self, message: str, *, step_started: bool) -> None:
        super().__init__(message)
        self.step_started = step_started


class GuardedWorkerChannel:
    """One guarded worker process speaking the JSON-lines worker protocol.

    This is THE shared launcher (Gate A1) — the batch
    :func:`run_session_isolated` and the per-step isolated session both go
    through it, so there is exactly one place where limits, environment,
    working directory, output bounds, the handshake and process-group
    termination are decided:

    * limits: :func:`validate_worker_limits` runs BEFORE spawn; the child
      reports the limits it applied in its READY frame and the handshake
      fails unless they match the request exactly;
    * environment: the ``_ENV_ALLOWLIST`` scrubbed env — nothing else, no
      credentials by construction;
    * cwd: a private per-run temporary directory, removed on ``close()``;
    * output: bounded readers for stdout (per-frame and total caps) and
      stderr (a capped diagnostic tail); overflow kills the worker group and
      raises ``ProtocolFrameError``;
    * lifetime: the child leads its own session; ``close()`` SIGTERMs then
      SIGKILLs the whole process GROUP, so timeout/cancel reaches
      descendants too;
    * wall clock: the deadline is spawn time + ``config.wall_clock_s`` —
      exactly the configured budget, no extra margin on top. Every read
      (handshake, steps, budget RPC replies) is bounded by the remaining
      time.

    The channel also forwards the worker's ``budget_request`` frames to a
    parent-owned :class:`~vouch_agent.runtime.ports.QueryBudget` (when one is
    supplied) and writes the reply back — the parent ledger stays the only
    budget authority; the child cannot certify itself by returning metering
    (Gate A3).

    ``spawn(argv, cwd, env)`` is the test seam for fault injection (a fake
    worker that dies, garbles the handshake, or never answers); production
    callers leave it None.
    """

    def __init__(
        self,
        config: Any,
        *,
        python_exe: str | None = None,
        spawn: Callable[[list[str], str, dict[str, str]], _WorkerProcess] | None = None,
        handshake_timeout_s: float = 15.0,
        extra_limits: Mapping[str, Any] | None = None,
        budget: Any = None,
        stream: bool = False,
        job_steps: Sequence[WorkerStepRequest] = (),
        work_root: str | Path | None = None,
    ) -> None:
        assert_isolation_supported()
        merged = {**WORKER_LIMITS, **(extra_limits or {})} if extra_limits else WORKER_LIMITS
        self._limits = validate_worker_limits(merged)
        self._config = config
        self._budget = budget
        self._stream = bool(stream)
        argv = [python_exe or sys.executable, "-m", "vouch_agent.runtime.worker_process"]
        if spawn is None and shutil.which(argv[0]) is None and not os.path.exists(argv[0]):
            raise UnsupportedIsolationError(f"python executable not found: {argv[0]!r}")

        self._workdir = Path(
            tempfile.mkdtemp(prefix="vouch-worker-", dir=str(work_root) if work_root else None)
        )
        job = {
            "config": _job_config(config),
            "limits": dict(self._limits),
            "stream": self._stream,
            "queryBudgetRpc": budget is not None,
            "steps": [
                {
                    "instruction": s.instruction,
                    "scope": s.scope,
                    "structured": s.structured,
                    "return_type": s.return_type,
                }
                for s in job_steps
            ],
        }
        # The wall clock IS the deadline: spawn-to-close may not exceed it.
        self._deadline = time.monotonic() + float(config.wall_clock_s)
        try:
            self._proc = (spawn or _spawn_default)(argv, str(self._workdir), _worker_env())
        except OSError as exc:
            self._workdir_cleanup()
            raise UnsupportedIsolationError(f"could not spawn worker process: {exc}") from exc
        # Retain the ORIGINAL group identity while the leader is provably
        # alive: after the leader exits (and is reaped) its pid no longer
        # resolves, but TERM-resistant descendants may still hold the group.
        self._pgid: int | None = None
        spawned_pid = getattr(self._proc, "pid", None)
        if isinstance(spawned_pid, int) and spawned_pid > 0 and hasattr(os, "getpgid"):
            with contextlib.suppress(OSError):
                self._pgid = os.getpgid(spawned_pid)

        self._frames: queue_mod.Queue[dict[str, Any] | None] = queue_mod.Queue()
        self._protocol_error: BaseException | None = None
        self._write_lock = threading.Lock()
        self._stderr_tail = bytearray()
        self._acked_steps: set[int] = set()
        self._step_index = -1
        self._worker_pid: int | None = None
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        try:
            self._send_job(job)
            self._handshake(min(handshake_timeout_s, max(0.05, self._remaining())))
        except BaseException:
            self.close()
            raise

    # --- lifecycle ---------------------------------------------------------------

    @property
    def limits(self) -> dict[str, int]:
        return dict(self._limits)

    @property
    def workdir(self) -> Path:
        return self._workdir

    @property
    def worker_pid(self) -> int | None:
        return self._worker_pid

    def alive(self) -> bool:
        return self._proc.poll() is None and self._protocol_error is None

    def _workdir_cleanup(self) -> None:
        shutil.rmtree(self._workdir, ignore_errors=True)

    def close(self) -> None:
        """Stop the worker's whole process group and remove its workdir.

        Runs even when the leader has ALREADY exited: the captured group id
        still identifies its surviving descendants, which must be escalated
        within the bounded grace rather than left running (M4 A1 §4).
        """
        stop_process_group(self._proc, pgid=getattr(self, "_pgid", None))
        for reader in getattr(self, "_readers", ()):
            reader.join(timeout=2.0)
        self._workdir_cleanup()

    def stderr_tail(self) -> str:
        """A bounded tail of the child's stderr (diagnostics only)."""
        return bytes(self._stderr_tail[-4000:]).decode("utf-8", errors="replace")

    def _remaining(self) -> float:
        return self._deadline - time.monotonic()

    # --- writing -----------------------------------------------------------------

    def _write_line(self, payload: Mapping[str, Any]) -> None:
        data = json.dumps(dict(payload)) + "\n"
        try:
            assert self._proc.stdin is not None
            with self._write_lock:
                self._proc.stdin.write(data.encode("utf-8"))
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ChannelClosed(
                f"worker stdin failed (worker died?): {exc}", step_started=True
            ) from exc

    def _send_job(self, job: Mapping[str, Any]) -> None:
        try:
            self._write_line(job)
        except ChannelClosed as exc:
            raise UnsupportedIsolationError(
                f"could not deliver the job frame to the worker: {exc}"
            ) from exc

    # --- bounded readers -----------------------------------------------------------

    def _fail_protocol(self, exc: BaseException) -> None:
        """Record a fatal channel condition and kill the worker group."""
        if self._protocol_error is None:
            self._protocol_error = exc
        stop_process_group(self._proc, pgid=getattr(self, "_pgid", None))
        self._frames.put(None)

    @staticmethod
    def _read_chunk(stream: Any, size: int) -> bytes | None:
        """One non-blocking-ish read: return what is available, never wait
        for the full ``size`` (a blocking ``read(n)`` would deadlock the
        handshake waiting for n bytes). Returns None for streams the bounded
        reader cannot serve (iterator-only test fakes)."""
        read1 = getattr(stream, "read1", None)
        if read1 is not None:
            return read1(size)
        read = getattr(stream, "read", None)
        if read is not None:
            return read(size)
        return None

    def _read_stderr(self) -> None:
        stream = getattr(self._proc, "stderr", None)
        if stream is None:
            return
        try:
            while True:
                chunk = self._read_chunk(stream, 4096)
                if not chunk:
                    return
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > MAX_STDERR_TAIL_BYTES * 2:
                    del self._stderr_tail[:MAX_STDERR_TAIL_BYTES]
        except (OSError, ValueError):
            return

    def _handle_line(self, line: bytes) -> bool:
        """Parse and route one stdout line. False = fatal protocol failure."""
        if not line.strip():
            return True
        if len(line) > MAX_FRAME_BYTES:
            self._fail_protocol(
                ProtocolFrameError(
                    f"worker frame of {len(line)} bytes exceeds the "
                    f"{MAX_FRAME_BYTES} byte cap; worker terminated"
                )
            )
            return False
        try:
            frame = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._fail_protocol(
                ProtocolFrameError(
                    "worker emitted a non-JSON line on stdout (stdout "
                    f"carries only protocol frames): {line[:200]!r} ({exc})"
                )
            )
            return False
        if not isinstance(frame, dict) or "frame" not in frame:
            self._fail_protocol(
                ProtocolFrameError(f"worker frame is not a protocol frame: {line[:200]!r}")
            )
            return False
        if frame.get("frame") == "budget_request":
            self._answer_budget(frame)
        else:
            self._frames.put(frame)
        return True

    def _read_stdout(self) -> None:
        stream = self._proc.stdout
        assert stream is not None
        buffer = bytearray()
        total = 0
        try:
            while True:
                chunk = self._read_chunk(stream, 4096)
                if chunk is None:
                    # Iterator-only stdout (fault-injection fakes): fall back
                    # to line iteration so those seams keep working.
                    for line in stream:
                        if not self._handle_line(
                            str(line).rstrip("\n").encode("utf-8", errors="replace")
                        ):
                            return
                    self._frames.put(None)
                    return
                if not chunk:
                    if buffer.strip():
                        self._fail_protocol(
                            ProtocolFrameError(
                                f"worker stdout ended mid-frame ({len(buffer)} unterminated "
                                "bytes); bounded capture refused it"
                            )
                        )
                    self._frames.put(None)
                    return
                buffer.extend(chunk)
                total += len(chunk)
                if total > MAX_STDOUT_BYTES:
                    self._fail_protocol(
                        ProtocolFrameError(
                            f"worker stdout exceeded the {MAX_STDOUT_BYTES} byte cap "
                            "(bounded capture); worker terminated"
                        )
                    )
                    return
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        if len(buffer) > MAX_FRAME_BYTES:
                            self._fail_protocol(
                                ProtocolFrameError(
                                    f"worker frame exceeds the {MAX_FRAME_BYTES} byte cap "
                                    "with no newline; worker terminated"
                                )
                            )
                        break
                    line = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    if not self._handle_line(line):
                        return
        except (OSError, ValueError):
            self._frames.put(None)

    # --- parent-side budget RPC (Gate A3) -------------------------------------------

    def _answer_budget(self, request: Mapping[str, Any]) -> None:
        """Serve one worker budget RPC from the PARENT-owned ledger.

        The child can only ask; the parent's ``QueryBudget`` decides. A
        refusal travels back as an error reply the child must turn into an
        abort BEFORE the query runs.
        """
        reply: dict[str, Any] = {"frame": "budget_response", "rid": request.get("rid")}
        op = str(request.get("op", ""))
        try:
            if self._budget is None:
                raise ContractError("this channel has no parent budget wired")
            if op == "reserve":
                estimate = request.get("estimateUsd")
                reservation_id = self._budget.reserve_query(
                    None if estimate is None else float(estimate)
                )
                reply.update(ok=True, reservationId=reservation_id)
            elif op == "settle":
                actual = request.get("actualUsd")
                self._budget.settle_query(
                    str(request.get("reservationId")),
                    None if actual is None else float(actual),
                )
                reply.update(ok=True)
            elif op == "release":
                self._budget.release_query(str(request.get("reservationId")))
                reply.update(ok=True)
            else:
                raise ProtocolFrameError(f"unknown budget RPC op {op!r}")
        except Exception as exc:
            code = getattr(exc, "code", None) or "vouch/error"
            reply.update(
                ok=False, error={"code": code, "type": type(exc).__name__, "message": str(exc)}
            )
        with contextlib.suppress(Exception):
            self._write_line(reply)

    # --- frames -----------------------------------------------------------------

    def _next_frame(self, timeout_s: float) -> dict[str, Any]:
        """Next non-RPC frame, bounded by ``timeout_s`` AND the deadline."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if self._protocol_error is not None:
                raise self._protocol_error
            remaining = min(deadline, self._deadline) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("worker did not produce a protocol frame in time")
            try:
                frame = self._frames.get(timeout=min(remaining, 0.25))
            except queue_mod.Empty:
                if self._proc.poll() is not None and self._frames.empty():
                    raise ChannelClosed(
                        "worker process exited before completing the protocol "
                        f"(exit={self._proc.poll()!r}) stderr={self.stderr_tail()!r}",
                        step_started=False,
                    ) from None
                continue
            if frame is None:
                raise ChannelClosed(
                    "worker closed its stdout unexpectedly "
                    f"(exit={self._proc.poll()!r}) stderr={self.stderr_tail()!r}",
                    step_started=False,
                )
            return frame

    def _handshake(self, timeout_s: float) -> None:
        try:
            ready = self._next_frame(timeout_s)
        except TimeoutError as exc:
            raise UnsupportedIsolationError(
                f"worker did not complete the isolation handshake within "
                f"{timeout_s:.1f}s (process poll={self._proc.poll()!r})"
            ) from exc
        except ChannelClosed as exc:
            raise UnsupportedIsolationError(
                f"worker handshake failed: {exc} stderr={self.stderr_tail()!r}"
            ) from exc
        except ProtocolFrameError as exc:
            # A protocol violation DURING the handshake means the guarded
            # channel could not be established — fail closed as an isolation
            # failure, never run the session unguarded.
            raise UnsupportedIsolationError(f"worker handshake protocol failure: {exc}") from exc
        if ready.get("frame") != "ready":
            raise UnsupportedIsolationError(f"worker handshake failed; first frame was {ready!r}")
        pid = ready.get("pid")
        self._worker_pid = pid if isinstance(pid, int) else None
        applied = ready.get("limits")
        if not isinstance(applied, dict) or not applied:
            raise UnsupportedIsolationError(
                f"worker READY frame reported no limits: {ready!r} (fail closed — "
                "a guarded child must confirm the limits it applied)"
            )
        for key, requested in self._limits.items():
            if key not in applied:
                raise UnsupportedIsolationError(
                    f"worker READY limits are missing {key!r}: applied {applied!r}; "
                    "refusing to run the step"
                )
            try:
                applied_value = int(applied.get(key))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                applied_value = -1
            if applied_value != int(requested):
                raise UnsupportedIsolationError(
                    f"worker READY limits do not match the request: key {key!r} "
                    f"requested {requested}, applied {applied.get(key)!r} "
                    f"(full applied frame {applied!r}); refusing to run the step"
                )

    # --- streaming steps -----------------------------------------------------------

    def send_step(self, step: WorkerStepRequest) -> int:
        """Queue one streamed step; returns its channel-local index."""
        self._step_index += 1
        self._write_line(
            {
                "frame": "step",
                "index": self._step_index,
                "instruction": step.instruction,
                "scope": step.scope,
                "structured": step.structured,
                "return_type": step.return_type,
            }
        )
        return self._step_index

    def read_step(self, index: int) -> dict[str, Any]:
        """Read frames until step ``index``'s result arrives (deadline-bounded).

        Returns the successful ``step_result`` frame; failed steps and error
        frames raise the mapped Vouch error (with the child's usage
        attached). ``step_ack`` frames record that the child started the
        step: a channel death AFTER the ack may have consumed queries, so
        the raised :class:`ChannelClosed` carries ``step_started=True`` and
        only un-acked deaths are retryable.
        """
        while True:
            try:
                frame = self._next_frame(self._remaining())
            except ChannelClosed as exc:
                raise ChannelClosed(str(exc), step_started=index in self._acked_steps) from exc
            except TimeoutError as exc:
                raise WallClockExceededError(
                    f"isolated step exceeded the configured wall clock "
                    f"(deadline = wall clock, no margin): {exc}"
                ) from exc
            kind = frame.get("frame")
            if kind == "step_ack":
                if frame.get("index") == index:
                    self._acked_steps.add(index)
                continue
            if kind == "error":
                raise _map_worker_error(frame)
            if kind == "step_result":
                if frame.get("index") != index:
                    raise ProtocolFrameError(
                        f"worker frames out of order: expected step_result {index}, "
                        f"got {frame!r}"
                    )
                if not frame.get("ok"):
                    raise _map_worker_error(frame)
                return frame
            raise ProtocolFrameError(
                f"unexpected worker frame while waiting for step {index}: {frame!r}"
            )


def _job_config(config: Any) -> dict[str, Any]:
    return {
        "mode": config.mode.value,
        "max_steps": config.max_steps,
        "wall_clock_s": config.wall_clock_s,
        "max_cost_usd": config.max_cost_usd,
        "scripted_responses": list(config.scripted_responses),
        "allow_timeout_pragma": config.allow_timeout_pragma,
        "scripted_cursor": int(getattr(config, "scripted_cursor", 0) or 0),
    }


def run_session_isolated(
    config: Any,
    steps: Sequence[WorkerStepRequest],
    *,
    python_exe: str | None = None,
    spawn: Callable[[list[str], str, dict[str, str]], _WorkerProcess] | None = None,
    handshake_timeout_s: float = 15.0,
    extra_limits: Mapping[str, Any] | None = None,
) -> IsolatedRunResult:
    """Run a whole session in a separate guarded, resource-limited process.

    Thin batch wrapper over :class:`GuardedWorkerChannel` — the same single
    launcher the per-step isolated session uses. The parent keeps all
    authority: it feeds the job (config + limits + steps) on the worker's
    stdin, reads JSON result frames from stdout, answers the worker's budget
    RPCs from the parent-owned ledger, enforces the wall clock (group
    terminate → group kill), and maps worker-reported errors back to Vouch
    error classes. Any failure to *establish* isolation raises
    ``UnsupportedIsolationError`` — never a silent in-process fallback.

    ``spawn(argv, cwd, env)`` is the test seam for fault injection (a fake
    worker that dies, garbles the handshake, or never answers); production
    callers leave it None.
    """
    channel = GuardedWorkerChannel(
        config,
        python_exe=python_exe,
        spawn=spawn,
        handshake_timeout_s=handshake_timeout_s,
        extra_limits=extra_limits,
        budget=getattr(config, "query_budget", None),
        job_steps=steps,
    )
    try:
        results: list[WorkerStepResult] = []
        for index, _step in enumerate(steps):
            remaining = channel._remaining()
            if remaining <= 0:
                raise WallClockExceededError(
                    f"isolated run exceeded its wall-clock budget before step {index}"
                )
            frame = channel._next_frame(remaining)
            kind = frame.get("frame")
            if kind == "error":
                raise _map_worker_error(frame)
            if kind != "step_result" or frame.get("index") != index:
                raise ProtocolFrameError(
                    f"worker frames out of order: expected step_result {index}, got {frame!r}"
                )
            if not frame.get("ok"):
                # The child treats a failed step as terminal (it exits right
                # after this frame); surface the mapped Vouch error here.
                raise _map_worker_error(frame)
            results.append(
                WorkerStepResult(
                    ok=True,
                    value=frame.get("value"),
                    usage=dict(frame.get("usage") or {}),
                )
            )
        return IsolatedRunResult(
            steps=results, limits_applied=dict(channel.limits), worker_pid=channel.worker_pid
        )
    except TimeoutError as exc:
        raise WallClockExceededError(f"isolated worker timed out: {exc}") from None
    finally:
        channel.close()


def structured_type_for(name: str | None) -> type | None:
    """The builtin type a worker step named (None for unstructured steps)."""
    if name is None:
        return None
    try:
        return _STRUCTURED_TYPES[name]
    except KeyError:
        from vouch_agent.errors import ContractError

        raise ContractError(
            f"worker-process structured steps support only "
            f"{sorted(_STRUCTURED_TYPES)}, got {name!r}"
        ) from None
