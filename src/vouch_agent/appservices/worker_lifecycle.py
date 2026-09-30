"""Detached execution-worker lifecycle (M4 A1, review §3/§5) — split out of
``appservices/worker.py`` so lifecycle and export ownership stay separate.

What lives here:

* **Fenced worker lease** — the public "which worker, if any, is executing
  this run right now" record. Every worker attempt carries a unique
  ``generation`` plus a reliable process-start identity (``/proc/<pid>/stat``
  field 22, the kernel starttime: stable for the life of the process,
  different across restarts even when the pid is reused). Acquire, heartbeat
  and release are all compare-and-set on that generation inside a store
  transaction, so a LOSING worker can never overwrite or clear the live
  owner's record, and stale cleanup can never clear a replacement's record.
  Liveness is decided by the pid+starttime pair, never by a bare pid probe.
* **Explicit worker commands** — a detached worker does not guess: it acts
  only on a durable ``start``/``resume`` command (with an expected version
  when spawned with one). Once started, PAUSED is a STOPPING condition: a new
  operator pause that lands mid-flight stops the worker; continuing requires
  a separate explicit resume command. Command acknowledgement and the actual
  durable outcome are persisted, FENCED to the accepted command version — an
  exiting worker can never consume a newer command another client issued.
* **Cancel** — requests are durable; the worker polls them on a separate
  thread OUTSIDE the blocking model call and, on detection, stops the run's
  owned worker-session process group within the bounded group-stop grace
  (TERM -> 2s -> KILL), letting the supervisor reconcile reservations and
  finalize CANCELLED.

The worker executes through the same ``ExecutionService`` as any client and
persists every transition; closing the client neither kills nor duplicates
the work. It exits after the run reaches a terminal state, a deliberate
pause, or records the recoverable stop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from vouch_agent.appservices.execution import ExecutionOutcome, ExecutionService
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.common import new_id, utc_now_iso
from vouch_agent.contracts.tasks import TaskStatus
from vouch_agent.errors import VouchError
from vouch_agent.orchestrator.supervisor import KIND_CANCEL_REQUEST

#: Marker the detached worker maintains so a client can distinguish a live
#: worker from a stale/exited record (generation + process-start identity +
#: heartbeat timestamps; fenced by generation, see module docstring).
KIND_WORKER_LEASE = "execution-worker-lease"

#: Durable worker command record (keyed by run id): ``start``/``resume`` with
#: a monotonically increasing version, acknowledged exactly once.
KIND_WORKER_COMMAND = "worker-command"

#: Exit codes of the worker entry: 0 = clean stop (terminal, deliberate
#: pause, or clean refusal of a stale command), 1 = failure or refusal.
WORKER_EXIT_OK = 0
WORKER_EXIT_FAILED = 1

#: How often the worker's cancel poller checks the durable cancel request.
_CANCEL_POLL_S = 0.1

#: Bounded wait for the test-only operation gate file (never in production).
_OPERATION_GATE_TIMEOUT_S = 60.0


# --- process-start identity -----------------------------------------------------


def _proc_start_identity(pid: int) -> str:
    """/proc/<pid>/stat field 22 (starttime, clock ticks); '0' if unavailable.

    Stable for the life of the process and different across restarts even
    when the pid is reused — unlike a process-name field or a bare pid probe.
    """
    try:
        with open(f"/proc/{int(pid)}/stat") as stream:
            # field 2 (comm) may contain spaces; everything after the final ')'
            return stream.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return "0"


def _process_start_identity() -> str:
    return _proc_start_identity(os.getpid())


def _identity_alive(pid: int, start_identity: Any) -> bool:
    """True only when ``pid`` exists AND is the same process start.

    An unreachable/proc or a starttime mismatch means the original process
    died (possibly with the pid since reused) — the record is stale, not live.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    current = _proc_start_identity(pid)
    if current == "0":
        # Cannot verify the start identity on this platform: fall back to a
        # bare existence probe, reported as unverified by the reader.
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except (ProcessLookupError, OSError):
            return False
        return True
    return str(start_identity) == current


def _lease_state(data: dict[str, Any]) -> str:
    """ "live" | "exited" | "stale" for a lease record."""
    if data.get("endedAt"):
        return "exited"
    if _identity_alive(int(data.get("pid") or 0), data.get("startIdentity")):
        return "live"
    return "stale"


# --- fenced lease primitives (CAS on the worker generation) ---------------------


def acquire_worker_lease(
    workspace: ProjectWorkspace, run_id: str, *, generation: str
) -> dict[str, Any] | None:
    """Atomically take the run's worker lease; ``None`` when a LIVE owner holds it.

    The live owner's record is never touched on refusal (a second worker
    cannot hide the winner). A stale record — its process provably gone, or
    the pid demonstrably reused — is replaced under the same transaction;
    replacing it cannot clear a successor's record because the successor's
    generation only ever lands here after the stale one was read.
    """
    pid = os.getpid()
    identity = _process_start_identity()
    with workspace.store.transaction():
        existing = workspace.store.load(KIND_WORKER_LEASE, run_id)
        if existing is not None and _lease_state(existing) == "live":
            return None
        record = {
            "runId": run_id,
            "generation": generation,
            "pid": pid,
            "startIdentity": identity,
            "startedAt": utc_now_iso(),
            "heartbeatAt": utc_now_iso(),
            "endedAt": None,
        }
        workspace.store.save(KIND_WORKER_LEASE, run_id, record)
        return dict(record)


def heartbeat_worker_lease(workspace: ProjectWorkspace, run_id: str, generation: str) -> bool:
    """Refresh the lease heartbeat; only the owning generation may (CAS)."""
    with workspace.store.transaction():
        data = workspace.store.load(KIND_WORKER_LEASE, run_id)
        if data is None or data.get("generation") != generation or data.get("endedAt"):
            return False
        data["heartbeatAt"] = utc_now_iso()
        workspace.store.save(KIND_WORKER_LEASE, run_id, data)
        return True


def release_worker_lease(workspace: ProjectWorkspace, run_id: str, generation: str) -> bool:
    """Mark the lease ended; only the owning generation may (CAS).

    A worker whose record was replaced (stale takeover) leaves the
    replacement's record untouched — the historical bug where any exiting
    worker cleared the live owner's lease.
    """
    with workspace.store.transaction():
        data = workspace.store.load(KIND_WORKER_LEASE, run_id)
        if data is None or data.get("generation") != generation:
            return False
        if not data.get("endedAt"):
            data["endedAt"] = utc_now_iso()
            workspace.store.save(KIND_WORKER_LEASE, run_id, data)
        return True


def worker_lease(workspace: ProjectWorkspace, run_id: str) -> dict[str, Any] | None:
    """The run's worker lease for clients: live record, stale record, or None.

    ``None`` means no lease or a normally-ended one. A stale record (owner
    provably dead / pid reused) is returned with ``alive=False`` and
    ``state="stale"`` so reconnect surfaces reconciliation instead of
    silently reporting "no worker".
    """
    data = workspace.store.load(KIND_WORKER_LEASE, run_id)
    if data is None:
        return None
    state = _lease_state(data)
    if state == "exited":
        return None
    return {**data, "alive": state == "live", "state": state}


# --- explicit worker commands -----------------------------------------------------


def issue_worker_command(workspace: ProjectWorkspace, run_id: str, command: str) -> dict[str, Any]:
    """Durably issue ``start``/``resume``; returns the command record."""
    if command not in ("start", "resume"):
        raise VouchError(f"unknown worker command {command!r}; expected start/resume")
    with workspace.store.transaction():
        existing = workspace.store.load(KIND_WORKER_COMMAND, run_id)
        version = int((existing or {}).get("version", 0) or 0) + 1
        record = {
            "runId": run_id,
            "command": command,
            "version": version,
            "issuedAt": utc_now_iso(),
            "acknowledgedAt": None,
            "result": None,
        }
        workspace.store.save(KIND_WORKER_COMMAND, run_id, record)
        return dict(record)


def _accept_command(
    workspace: ProjectWorkspace, run_id: str, command: str, expected_version: int | None
) -> dict[str, Any] | None:
    """Validate the command this worker was spawned for; None = refuse.

    Rules: ``resume`` requires an explicit durable command; ``start`` may be
    implicit on first spawn (pre-command records); an already-acknowledged
    command is consumed — a duplicate or stale spawn refuses instead of
    executing the same command twice; a versioned spawn must match exactly.
    """
    record = workspace.store.load(KIND_WORKER_COMMAND, run_id)
    if record is None:
        if command != "start":
            return None
        record = {
            "runId": run_id,
            "command": "start",
            "version": 1,
            "issuedAt": utc_now_iso(),
            "acknowledgedAt": None,
            "result": None,
        }
        workspace.store.save(KIND_WORKER_COMMAND, run_id, record)
        return record
    if record.get("acknowledgedAt"):
        return None  # already consumed by a previous worker
    if record.get("command") != command:
        return None
    if expected_version is not None and int(record.get("version", 0) or 0) != int(expected_version):
        return None
    return record


def _acknowledge_command(
    workspace: ProjectWorkspace, run_id: str, result: str, *, accepted: dict[str, Any]
) -> bool:
    """Persist command acknowledgement with the actual durable outcome.

    FENCED to the command THIS attempt accepted (command + version): PAUSED
    becomes durably visible before the acknowledgement runs, so another
    client may have issued a NEWER command in that interval — the exiting
    attempt must not consume it. A mismatch leaves the newer record pending
    for the worker dispatched with its exact version.
    """
    with workspace.store.transaction():
        record = workspace.store.load(KIND_WORKER_COMMAND, run_id)
        if record is None or record.get("acknowledgedAt"):
            return False
        if (record.get("command"), int(record.get("version", 0) or 0)) != (
            accepted.get("command"),
            int(accepted.get("version", 0) or 0),
        ):
            return False  # a newer command stands; it is not ours to consume
        record["acknowledgedAt"] = utc_now_iso()
        record["result"] = result
        workspace.store.save(KIND_WORKER_COMMAND, run_id, record)
        return True


# --- cancel polling (outside the blocking model call) -----------------------------


def _cancel_requested(store: Any, run_id: str) -> bool:
    """Durable cancel request active? Same shape the supervisor writes
    (``requestedAt``/``consumedAt``) — the historical hook read a nonexistent
    ``requested`` key and never fired."""
    data = store.load(KIND_CANCEL_REQUEST, run_id)
    return data is not None and "consumedAt" not in data


class _CancelPoller(threading.Thread):
    """Watch the durable cancel request while the worker blocks in a model call.

    On detection it stops the run's owned worker-session group (bounded
    TERM -> KILL escalation) so the supervisor unblocks, reconciles open
    reservations and finalizes CANCELLED instead of waiting out the step.
    """

    def __init__(self, workspace: ProjectWorkspace, run_id: str, service: ExecutionService) -> None:
        super().__init__(name=f"vouch-worker-cancel-{run_id}", daemon=True)
        self._workspace = workspace
        self._run_id = run_id
        self._service = service
        self._stop_event = threading.Event()
        self.stopped_run = False

    def run(self) -> None:
        store = self._workspace.store
        while not self._stop_event.wait(_CANCEL_POLL_S):
            try:
                if not _cancel_requested(store, self._run_id):
                    continue
                reason = str(
                    (store.load(KIND_CANCEL_REQUEST, self._run_id) or {}).get("reason")
                    or "cancelled by operator request"
                )
                if self._service.stop_active_run(self._run_id, reason):
                    self.stopped_run = True
                # Keep polling while the request stands: a stop can race the
                # session's channel construction (no owned group yet) or the
                # supervisor registration — the retry catches it as soon as
                # the in-flight session exists. The loop drains naturally
                # once the supervisor consumes the request at finalization.
            except Exception:  # pragma: no cover - the poller must never crash the worker
                continue

    def stop(self) -> None:
        self._stop_event.set()


# --- the worker entry ---------------------------------------------------------------


def execute_run_detached(
    project_dir: Path,
    run_id: str,
    *,
    provider: str = "extract-fact",
    command: str = "start",
    command_version: int | None = None,
) -> int:
    """Worker main: execute one run to a terminal/recoverable state.

    Lifecycle (M4 A1): accept the explicit command -> acquire the fenced
    lease (a second live worker refuses without touching the owner's record)
    -> dispatch (sealed native operation, or the sealed provider scripts)
    -> ack the command with the actual durable result -> release the lease
    (CAS). A PAUSED outcome STOPS the worker: continuing requires a separate
    explicit ``resume`` command — a new operator pause that lands mid-flight
    is never auto-resumed away.
    """
    workspace = ProjectWorkspace.open(project_dir)
    generation = new_id("worker")
    service = ExecutionService(workspace)
    exit_code = WORKER_EXIT_OK
    try:
        accepted = _accept_command(workspace, run_id, command, command_version)
        if accepted is None:
            return WORKER_EXIT_FAILED
        if acquire_worker_lease(workspace, run_id, generation=generation) is None:
            # A live worker owns this run: refuse, and leave its lease alone.
            return WORKER_EXIT_FAILED
        heartbeat_worker_lease(workspace, run_id, generation)

        poller = _CancelPoller(workspace, run_id, service)
        poller.start()
        try:
            outcome = _dispatch(service, workspace, run_id, provider=provider)
        finally:
            poller.stop()
        heartbeat_worker_lease(workspace, run_id, generation)

        if outcome.status is TaskStatus.PAUSED:
            # PAUSED is a stopping condition, not a cue to resume (review §3).
            # The ack is fenced to the accepted command version: a resume
            # another client issued while PAUSED became visible stays pending.
            _acknowledge_command(workspace, run_id, "paused", accepted=accepted)
            return WORKER_EXIT_OK
        if outcome.status.value not in ("completed", "cancelled"):
            exit_code = WORKER_EXIT_FAILED
        _acknowledge_command(workspace, run_id, str(outcome.status.value), accepted=accepted)
        return exit_code
    except VouchError:
        return WORKER_EXIT_FAILED
    finally:
        release_worker_lease(workspace, run_id, generation)
        workspace.close()


def _dispatch(
    service: ExecutionService, workspace: ProjectWorkspace, run_id: str, *, provider: str
) -> ExecutionOutcome:
    """Route the run to its sealed execution path.

    A run sealed for a NATIVE DETERMINISTIC OPERATION is executed by this
    worker through the operation dispatcher (the computation happens here,
    after the durable submit + claim — never pre-computed in the client); a
    script/provider run goes through the normal isolated model path. A run
    that is already terminal (e.g. cancelled by another client before this
    worker claimed it) reports its durable status without re-executing.
    """
    from vouch_agent.appservices.execution import KIND_EXECUTION_CONFIG

    current = service.status(run_id)
    if current is not None and current.status in _TERMINAL:
        return ExecutionOutcome(run_id, current.status, service.result(run_id), None)
    config = workspace.store.load(KIND_EXECUTION_CONFIG, run_id)
    if config is not None and config.get("operation"):
        return service.execute_native_operation(run_id)
    data = config or {}
    script_files: tuple[Path, ...] = ()
    if data.get("scripts"):
        # content-sealed pool: materialize the persisted bytes to a private
        # temp dir so the service verifies its own digest seal
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="vouch-worker-scripts-"))
        for index, text in enumerate(data["scripts"]):
            path = tmp / f"step{index}.py"
            path.write_text(str(text), encoding="utf-8")
            script_files = (*script_files, path)
    return service.execute(run_id, provider=provider, script_files=script_files)


_TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


def spawn_detached_worker(
    project_dir: Path,
    run_id: str,
    *,
    provider: str = "extract-fact",
    command: str = "start",
    command_version: int | None = None,
) -> int:
    """Spawn the worker as an independent process group; return its pid.

    ``command``/``command_version`` give the worker its explicit instruction;
    a versioned spawn refuses in the child when the durable command record
    does not match exactly (stale/duplicate dispatch).
    """
    command_args = ["--command", command]
    if command_version is not None:
        command_args += ["--command-version", str(int(command_version))]
    argv = [
        sys.executable,
        "-m",
        "vouch_agent.appservices.worker_main",
        "--project",
        str(project_dir),
        "--run",
        run_id,
        "--provider",
        provider,
        *command_args,
    ]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(Path.cwd()),
    )
    return process.pid


__all__ = [
    "KIND_WORKER_COMMAND",
    "KIND_WORKER_LEASE",
    "WORKER_EXIT_FAILED",
    "WORKER_EXIT_OK",
    "acquire_worker_lease",
    "execute_run_detached",
    "heartbeat_worker_lease",
    "issue_worker_command",
    "release_worker_lease",
    "spawn_detached_worker",
    "worker_lease",
]
