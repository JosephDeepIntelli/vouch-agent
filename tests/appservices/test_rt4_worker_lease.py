"""Fenced worker lease (M4 A1 §5, review §5) — CAS on a unique generation.

The M3 review proved a LOSING worker destroyed the live owner's public lease
(unconditional overwrite before acquiring ownership + unconditional clear on
exit), erasing reconnect/liveness information while the real owner still
ran. Corrected contract: every attempt carries a unique generation plus a
process-start identity (/proc starttime, not a process name); only the owner
may heartbeat/release via CAS; a second worker must not hide the winner;
stale cleanup must not clear a replacement's record; a reused pid is stale,
not live.
"""

from __future__ import annotations

import importlib.util as _il
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.worker_lifecycle import (
    KIND_WORKER_LEASE,
    acquire_worker_lease,
    heartbeat_worker_lease,
    release_worker_lease,
    worker_lease,
)
from vouch_agent.appservices.workspace import ProjectWorkspace

_spec = _il.spec_from_file_location(
    "m3_runtime_helpers",
    Path(__file__).parents[1] / "regression" / "runtime" / "m3_runtime_helpers.py",
)
_m3 = _il.module_from_spec(_spec)
_spec.loader.exec_module(_m3)
init_native_project = _m3.init_native_project

_SCHEMA = {
    "type": "object",
    "required": ["finding"],
    "properties": {"finding": {"type": "string"}},
}

#: Real worker child that parks at a trusted pre-model barrier while holding
#: the real Supervisor ownership AND the fenced worker lease.
_BARRIER_CHILD = """
import sys, time
from pathlib import Path
from vouch_agent.orchestrator.supervisor import Supervisor
from vouch_agent.appservices.worker_lifecycle import execute_run_detached
project, run_id, ready, release = sys.argv[1:5]
original = Supervisor._phase_model_call
def barrier(self, *args):
    Path(ready).write_text('ready')
    deadline = time.monotonic() + 30
    while not Path(release).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return original(self, *args)
Supervisor._phase_model_call = barrier
sys.exit(execute_run_detached(Path(project), run_id))
"""


def _submit(service: ExecutionService) -> str:
    return service.submit(
        goal="Synthetic fact",
        inputs={"fact": {"value": "synthetic", "source": "test"}},
        budget_usd=0.5,
        max_steps=4,
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"barrier file {path} never appeared"


def test_second_worker_cannot_hide_the_live_owners_lease(tmp_path: Path) -> None:
    """The review's exact reproduction, inverted: the second real worker is
    refused, but the FIRST owner's lease stays live with its generation
    intact; the owner then finishes normally and the lease reads clean."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    service = ExecutionService(workspace)
    run_id = _submit(service)
    ready, release = tmp_path / "ready", tmp_path / "release"
    first = subprocess.Popen(
        [sys.executable, "-c", _BARRIER_CHILD, str(project), run_id, str(ready), str(release)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    second = None
    try:
        _wait_for(ready)
        initial = worker_lease(workspace, run_id)
        assert initial is not None and initial["alive"] is True
        assert initial.get("generation"), "lease must carry a worker generation"

        second = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "vouch_agent.appservices.worker_main",
                "--project",
                str(project),
                "--run",
                run_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        second.wait(timeout=15)
        assert second.returncode != 0, "the losing worker must refuse, not run"

        after = worker_lease(workspace, run_id)
        assert after is not None, "the losing worker erased the live owner's lease"
        assert after["alive"] is True and after["state"] == "live"
        assert after["generation"] == initial["generation"], (
            "the losing worker rewrote the winner's lease generation"
        )
        assert first.poll() is None, "the losing worker must not kill the owner"
        assert service.status(run_id).status.value == "running"

        release.write_text("continue", encoding="utf-8")
        first.wait(timeout=30)
        assert first.returncode == 0
        assert service.status(run_id).status.value == "completed"
        assert worker_lease(workspace, run_id) is None  # clean end, not erased mid-flight
    finally:
        release.touch()
        for process in (first, second):
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        workspace.close()


def test_reused_pid_is_stale_not_live(tmp_path: Path) -> None:
    """Liveness is pid PLUS process-start identity: a record whose pid now
    belongs to a DIFFERENT process start reads stale — never 'alive'."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    run_id = _submit(ExecutionService(workspace))
    try:
        # our own pid, but a start identity from a different boot of it
        workspace.store.save(
            KIND_WORKER_LEASE,
            run_id,
            {
                "runId": run_id,
                "generation": "gen-old",
                "pid": os.getpid(),
                "startIdentity": "1",
                "startedAt": "2026-01-01T00:00:00Z",
                "heartbeatAt": "2026-01-01T00:00:00Z",
                "endedAt": None,
            },
        )
        record = worker_lease(workspace, run_id)
        assert record is not None, "a stale record must SURFACE, not vanish"
        assert record["alive"] is False
        assert record["state"] == "stale"
    finally:
        workspace.close()


def test_stale_cleanup_never_clears_a_replacements_record(tmp_path: Path) -> None:
    """Takeover replaces a provably-dead record under CAS; the OLD owner's
    late release/heartbeat cannot clear or touch the replacement's record."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    run_id = _submit(ExecutionService(workspace))
    try:
        # a provably dead owner: a short-lived real process, already reaped
        dead = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.05)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        dead.wait(timeout=5)
        workspace.store.save(
            KIND_WORKER_LEASE,
            run_id,
            {
                "runId": run_id,
                "generation": "gen-dead",
                "pid": dead.pid,
                "startIdentity": "999999",
                "startedAt": "2026-01-01T00:00:00Z",
                "heartbeatAt": "2026-01-01T00:00:00Z",
                "endedAt": None,
            },
        )
        assert worker_lease(workspace, run_id)["state"] == "stale"

        # the replacement takes over (own pid + own real start identity)
        acquired = acquire_worker_lease(workspace, run_id, generation="gen-new")
        assert acquired is not None and acquired["generation"] == "gen-new"

        # the DEAD owner's stale cleanup attempts must not clear it
        assert heartbeat_worker_lease(workspace, run_id, "gen-dead") is False
        assert release_worker_lease(workspace, run_id, "gen-dead") is False
        record = worker_lease(workspace, run_id)
        assert record is not None and record["state"] == "live"
        assert record["generation"] == "gen-new"
        assert record.get("endedAt") is None

        # the real owner may heartbeat and release
        assert heartbeat_worker_lease(workspace, run_id, "gen-new") is True
        assert release_worker_lease(workspace, run_id, "gen-new") is True
        assert worker_lease(workspace, run_id) is None
    finally:
        workspace.close()


def test_acquire_refuses_while_owner_is_live(tmp_path: Path) -> None:
    """In-process CAS: a live lease blocks a second generation without
    modifying the record."""
    project = init_native_project(tmp_path)
    workspace = ProjectWorkspace.open(project)
    run_id = _submit(ExecutionService(workspace))
    try:
        first = acquire_worker_lease(workspace, run_id, generation="gen-1")
        assert first is not None
        assert acquire_worker_lease(workspace, run_id, generation="gen-2") is None
        record = worker_lease(workspace, run_id)
        assert record is not None
        assert record["generation"] == "gen-1" and record["startedAt"] == first["startedAt"]
    finally:
        workspace.close()
