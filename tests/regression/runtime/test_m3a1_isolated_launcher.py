"""M3 Gate A1 regressions: the DEFAULT isolated path runs through ONE shared
guarded launcher.

Converted from the independent M2 review counterexample
(``reproduce_native.py`` observed ``job_has_limits=false``,
``ready_limits={}``, inherited env/cwd, no new session) with the assertions
inverted:

* the default ExecutionService path spawns its worker through the guarded
  launcher: a NONEMPTY validated limit set that the READY frame must confirm
  EXACTLY, a narrow environment ALLOWLIST (an injected canary — harmless, but
  secret-shaped — never reaches the child), a private per-run working
  directory, a new session (own process group), bounded capture and
  process-group termination at exactly the configured wall clock;
* missing/invalid limits refuse BEFORE any process is spawned, and the child
  itself refuses a job with an empty limits mapping;
* the wall clock is the deadline: no +30s margin is added on top (the M2
  defect let a configured budget run 30s longer);
* malformed worker output and launch failures fail the default path closed
  (FAILED run, honest error) instead of hanging or silently continuing.

No network, no provider, no machine-specific paths: the workers are the
repo's own ``worker_process`` module or tiny inline ``python -c`` programs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from m3_runtime_helpers import init_native_project
from test_supervisor_jaz import SCRIPT_DRAFT, SCRIPT_FINAL

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import UnsupportedIsolationError
from vouch_agent.runtime import guards
from vouch_agent.runtime.fatal_errors import WallClockExceededError
from vouch_agent.runtime.guards import (
    GuardedWorkerChannel,
    stop_process_group,
    validate_worker_limits,
)
from vouch_agent.runtime.ports import WorkerSessionConfig

_IS_POSIX = os.name == "posix"

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["recommendation", "confidence"],
    "properties": {
        "recommendation": {"type": "string"},
        "confidence": {"type": "string"},
    },
}


def _service(project: Path) -> tuple[ProjectWorkspace, ExecutionService]:
    workspace = ProjectWorkspace.open(project)
    return workspace, ExecutionService(workspace)


def _two_script_files(tmp: Path) -> tuple[Path, ...]:
    tmp.mkdir(parents=True, exist_ok=True)
    files = []
    for name, text in (("draft.py", SCRIPT_DRAFT), ("final.py", SCRIPT_FINAL)):
        path = tmp / name
        path.write_text(text, encoding="utf-8")
        files.append(path)
    return tuple(files)


def _run_default(service: ExecutionService, tmp: Path) -> Any:
    return service.run(
        goal="Produce a recommendation",
        inputs={"candidates": ["A", "B"]},
        budget_usd=0.5,
        max_steps=3,
        script_files=_two_script_files(tmp / "scripts"),
        completion_conditions=[{"type": "artifact_schema", "schema": _SCHEMA}],
    )


# --- the DEFAULT public path goes through the guarded launcher ------------------


def test_default_path_spawns_guarded_worker_with_limits_env_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ExecutionService default (isolated) path spawns via the ONE shared
    launcher: validated limits in the job, allowlist env without the canary,
    a private cwd, and a streaming (persistent) child."""
    project = init_native_project(tmp_path)
    workspace, service = _service(project)

    recorded_jobs: list[dict[str, Any]] = []
    recorded_spawns: list[dict[str, Any]] = []
    real_send_job = GuardedWorkerChannel._send_job
    real_spawn = guards._spawn_default

    def send_job_recorder(self: GuardedWorkerChannel, job: Any) -> None:
        recorded_jobs.append(dict(job))
        real_send_job(self, job)

    def spawn_recorder(argv: list[str], cwd: str, env: dict[str, str]) -> Any:
        recorded_spawns.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        return real_spawn(argv, cwd, env)

    monkeypatch.setattr(GuardedWorkerChannel, "_send_job", send_job_recorder)
    monkeypatch.setattr(guards, "_spawn_default", spawn_recorder)
    monkeypatch.setenv("VOUCH_RT3_CANARY_TOKEN", "synthetic-secret-not-for-children")

    outcome = _run_default(service, tmp_path)
    try:
        assert outcome.status.value == "completed", outcome.error
        assert recorded_jobs, "the default path must spawn at least one guarded worker"
        job = recorded_jobs[0]
        # nonempty VALIDATED limits travel with the job
        assert set(job["limits"]) == set(guards.WORKER_LIMITS)
        assert all(isinstance(v, int) and v > 0 for v in job["limits"].values())
        # the session streams steps to one persistent child (Gate A2)
        assert job["stream"] is True
        spawn = recorded_spawns[0]
        assert spawn["argv"][-1] == "vouch_agent.runtime.worker_process"
        # narrow allowlist env: the canary (and everything unlisted) is absent
        assert "VOUCH_RT3_CANARY_TOKEN" not in spawn["env"]
        allowed = set(guards._ENV_ALLOWLIST) | {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
        assert set(spawn["env"]) <= allowed
        # private per-run working directory, not the controller's cwd
        assert "vouch-worker-" in spawn["cwd"]
        assert Path(spawn["cwd"]) != Path.cwd()
    finally:
        workspace.close()


def test_default_path_starts_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Popen behind the launcher asks for a new session (setsid):
    the worker leads its own process group, so group-wide stops and terminal
    signals do not cross, and the parent can kill the whole tree."""
    project = init_native_project(tmp_path)
    workspace, service = _service(project)
    captured: list[dict[str, Any]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(guards.subprocess, "Popen", recording_popen)
    outcome = _run_default(service, tmp_path)
    workspace.close()
    assert outcome.status.value == "completed", outcome.error
    assert captured, "the default path must spawn a worker process"
    assert captured[0].get("start_new_session") is True
    assert captured[0].get("cwd"), "the worker gets its own working directory"
    assert isinstance(captured[0].get("env"), dict)


def test_default_path_env_allowlist_drops_even_benign_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deny-lists kept anything unlisted; the allowlist keeps ONLY what is
    listed — even a benign non-credential variable must not reach the child."""
    monkeypatch.setenv("VOUCH_RT3_BENIGN_SETTING", "old-deny-list-kept-this")
    env = guards._worker_env()
    assert "VOUCH_RT3_BENIGN_SETTING" not in env
    allowed = set(guards._ENV_ALLOWLIST) | {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    assert set(env) <= allowed


# --- limits: validated, matched by the child, refused when missing ---------------


def test_validate_worker_limits_refuses_empty_invalid_or_unknown() -> None:
    with pytest.raises(UnsupportedIsolationError, match="nonempty"):
        validate_worker_limits({})
    with pytest.raises(UnsupportedIsolationError, match="nonempty"):
        validate_worker_limits(None)
    with pytest.raises(UnsupportedIsolationError, match="unknown worker limit"):
        validate_worker_limits({"rlimit_as_bytes": 1 << 30, "rlimit_nofile": 64, "bogus": 1})
    with pytest.raises(UnsupportedIsolationError, match=">= 1"):
        validate_worker_limits({"rlimit_cpu_s": 0, "rlimit_nofile": 64})
    with pytest.raises(UnsupportedIsolationError, match="must be an integer"):
        validate_worker_limits({"rlimit_nofile": "many"})
    with pytest.raises(UnsupportedIsolationError, match="floor"):
        validate_worker_limits({"rlimit_as_bytes": 1024})
    assert validate_worker_limits(guards.WORKER_LIMITS) == guards.WORKER_LIMITS


def test_channel_refuses_invalid_limits_before_any_spawn() -> None:
    """Bad limits never spawn a process — the refusal happens up front."""

    def never(argv: list[str], cwd: str, env: dict[str, str]) -> Any:
        raise AssertionError(f"must not spawn with invalid limits (got {argv})")

    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=2, wall_clock_s=10.0, scripted_responses=("return 1",)
    )
    with pytest.raises(UnsupportedIsolationError, match="floor"):
        GuardedWorkerChannel(
            config, spawn=never, extra_limits={"rlimit_as_bytes": 1024, "rlimit_cpu_s": 5}
        )
    with pytest.raises(UnsupportedIsolationError, match=">= 1"):
        GuardedWorkerChannel(
            config, spawn=never, extra_limits={"rlimit_cpu_s": 0, "rlimit_nofile": 32}
        )


class _EchoLimitsProc:
    """Fake worker that reports the requested limits (optionally mangled)."""

    def __init__(self, limits: dict[str, int], *, mangle: str | None = None) -> None:
        self.stdin = io.BytesIO()
        self.stderr = io.BytesIO()
        reported = dict(limits)
        if mangle:
            reported[mangle] = int(reported[mangle]) + 1
        line = json.dumps({"frame": "ready", "pid": 4242, "limits": reported})
        self.stdout = io.BytesIO((line + "\n").encode())
        self._poll: int | None = None

    def poll(self) -> int | None:
        return self._poll

    def terminate(self) -> None:
        self._poll = 15

    def kill(self) -> None:
        self._poll = 9

    def wait(self, timeout: float | None = None) -> int:
        return self._poll or 0


def test_ready_handshake_requires_limits_to_match_the_request() -> None:
    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=2, wall_clock_s=10.0, scripted_responses=("return 1",)
    )
    channel = GuardedWorkerChannel(
        config, spawn=lambda a, c, e: _EchoLimitsProc(dict(guards.WORKER_LIMITS))
    )
    assert channel.limits == guards.WORKER_LIMITS
    channel.close()

    with pytest.raises(UnsupportedIsolationError, match="do not match the request"):
        GuardedWorkerChannel(
            config,
            spawn=lambda a, c, e: _EchoLimitsProc(
                dict(guards.WORKER_LIMITS), mangle="rlimit_nofile"
            ),
        )
    with pytest.raises(UnsupportedIsolationError, match="no limits"):
        GuardedWorkerChannel(config, spawn=lambda a, c, e: _EchoLimitsProc({}))


def test_real_child_refuses_a_job_without_limits(tmp_path: Path) -> None:
    """The worker child itself fails closed (exit 3) on an empty limit set —
    the second line of defense behind the parent's validation."""
    job = {
        "config": {
            "mode": "fixture",
            "max_steps": 1,
            "wall_clock_s": 10.0,
            "scripted_responses": ["return 1"],
            "allow_timeout_pragma": False,
            "scripted_cursor": 0,
        },
        "limits": {},
        "steps": [],
    }
    src_root = str(Path(__file__).resolve().parents[3] / "src")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    env["PYTHONPATH"] = src_root
    completed = subprocess.run(
        [sys.executable, "-m", "vouch_agent.runtime.worker_process"],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
        env=env,
    )
    assert completed.returncode == 3, completed.stdout + completed.stderr
    frames = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    prep = next(f for f in frames if f.get("frame") == "prep_error")
    assert prep["code"] == "vouch/unsupported-isolation"
    assert "no resource limits" in prep["message"]


# --- wall clock: the deadline IS the configured budget ---------------------------


class _NeverAnswerProc:
    """Alive but silent worker: never answers the handshake."""

    class _Silent:
        def read(self, n: int = -1) -> bytes:
            time.sleep(0.02)
            return b""

    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stderr = io.BytesIO()
        self.stdout = self._Silent()  # type: ignore[assignment]

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_wall_clock_deadline_has_no_extra_margin() -> None:
    """A 1.2s wall clock gives up at ~1.2s, not at +30s (the M2 defect's
    ``wall_clock + 30`` timeout)."""
    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=1, wall_clock_s=1.2, scripted_responses=("return 1",)
    )
    started = time.monotonic()
    with pytest.raises(UnsupportedIsolationError, match="handshake"):
        GuardedWorkerChannel(
            config,
            handshake_timeout_s=15.0,
            spawn=lambda a, c, e: _NeverAnswerProc(),
        )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"a +30s margin would have taken ~31s (took {elapsed:.1f}s)"


def test_step_deadline_is_the_wall_clock_not_extended() -> None:
    """Past the handshake, frame reads are bounded by the same deadline: a
    worker that answers the handshake but never a step is cut at the
    configured wall clock (mapped to WallClockExceededError)."""
    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=1, wall_clock_s=1.0, scripted_responses=("return 1",)
    )
    ready_line = json.dumps({"frame": "ready", "pid": 7, "limits": dict(guards.WORKER_LIMITS)})

    class _ReadyThenBlock:
        """Handshakes, then blocks without ever producing another frame."""

        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stderr = io.BytesIO()
            self.stdout = self  # the channel reads _proc.stdout.read1()
            self._ready = (ready_line + "\n").encode()
            self._sent = False

        def read1(self, n: int = -1) -> bytes:
            if not self._sent:
                self._sent = True
                return self._ready[:n] if n > 0 else self._ready
            time.sleep(30)  # alive, silent; the parent's deadline cuts this
            return b""

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    started = time.monotonic()
    channel = GuardedWorkerChannel(config, spawn=lambda a, c, e: _ReadyThenBlock())
    try:
        channel.send_step(guards.WorkerStepRequest(instruction="x"))
        with pytest.raises(WallClockExceededError):
            channel.read_step(0)
    finally:
        channel.close()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"deadline was extended beyond the wall clock ({elapsed:.1f}s)"


# --- process-group termination reaches descendants -------------------------------


def _group_members(pgid: int, exclude: set[int] | None = None) -> set[int]:
    """PIDs currently in process group ``pgid`` (Linux /proc scan)."""
    exclude = exclude or set()
    members: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        tail = stat[stat.rindex(")") + 2 :].split()
        try:
            if int(tail[1]) == pgid and int(entry.name) not in exclude:
                members.add(int(entry.name))
        except (IndexError, ValueError):
            continue
    return members


@pytest.mark.skipif(not _IS_POSIX or not Path("/proc").is_dir(), reason="POSIX /proc only")
def test_timeout_kills_the_whole_process_group_including_descendants() -> None:
    """A real hanging worker that spawned a child: at the wall clock the
    guarded channel kills the entire GROUP — the descendant does not
    outlive the run (timeout descendants, Gate A1)."""
    hang_argv = [
        sys.executable,
        "-c",
        "import subprocess, time\n"
        "subprocess.Popen(['sleep', '25'])\n"
        "time.sleep(25)\n",
    ]

    def spawn_hanger(argv: list[str], cwd: str, env: dict[str, str]) -> Any:
        return subprocess.Popen(
            hang_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=1, wall_clock_s=1.5, scripted_responses=("return 1",)
    )
    started = time.monotonic()
    with pytest.raises(UnsupportedIsolationError, match="handshake"):
        GuardedWorkerChannel(config, handshake_timeout_s=10.0, spawn=spawn_hanger)
    elapsed = time.monotonic() - started
    assert elapsed < 8.0, "the hang must be cut at the wall clock, not by outer timeouts"
    # find the (now dead) leader's group and prove nothing lives in it:
    # scan for any process still in a group whose leader was our python -c.
    survivors: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"sleep 25" in cmdline.replace(b"\x00", b" "):
            survivors.add(int(entry.name))
    assert not survivors, f"descendants survived the group kill: {survivors}"


@pytest.mark.skipif(not _IS_POSIX or not Path("/proc").is_dir(), reason="POSIX /proc only")
def test_stop_process_group_direct() -> None:
    """Direct check of the exported group-stop helper: leader + descendant
    both die, and repeated calls are safe."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, time\nsubprocess.Popen(['sleep', '20'])\ntime.sleep(20)\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not _group_members(pgid, exclude={proc.pid}):
        time.sleep(0.05)
    assert _group_members(pgid, exclude={proc.pid}), "hang child never spawned a descendant"
    stop_process_group(proc)
    assert proc.poll() is not None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _group_members(pgid):
        time.sleep(0.05)
    assert not _group_members(pgid), "descendants survived stop_process_group"
    stop_process_group(proc)  # idempotent


# --- malformed output / launch failure close the DEFAULT path --------------------


def test_malformed_worker_output_fails_the_default_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_native_project(tmp_path)
    workspace, service = _service(project)

    def garbage_spawn(argv: list[str], cwd: str, env: dict[str, str]) -> Any:
        return subprocess.Popen(
            [sys.executable, "-c", "print('this is not json'); import time; time.sleep(5)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

    monkeypatch.setattr(guards, "_spawn_default", garbage_spawn)
    outcome = _run_default(service, tmp_path)
    workspace.close()
    assert outcome.status.value == "failed"
    assert outcome.error and (
        "protocol" in outcome.error.lower() or "unsupported-isolation" in outcome.error.lower()
    )


def test_launch_failure_fails_the_default_run_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_native_project(tmp_path)
    workspace, service = _service(project)

    def broken_spawn(argv: list[str], cwd: str, env: dict[str, str]) -> Any:
        raise OSError("simulated: no such executable")

    monkeypatch.setattr(guards, "_spawn_default", broken_spawn)
    outcome = _run_default(service, tmp_path)
    workspace.close()
    assert outcome.status.value == "failed"
    assert outcome.error and "spawn" in outcome.error.lower()


def test_missing_python_executable_fails_closed_before_work() -> None:
    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=1, wall_clock_s=10.0, scripted_responses=("return 1",)
    )
    with pytest.raises(UnsupportedIsolationError, match="python executable"):
        GuardedWorkerChannel(config, python_exe="/nonexistent/python-for-vouch-m3-tests")


def test_bounded_capture_refuses_an_unterminated_oversize_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output bounds are enforced WHILE ACCUMULATING bytes — a frame that
    never terminates cannot buffer unbounded memory in the parent."""

    class OversizeProc(_NeverAnswerProc):
        def __init__(self) -> None:
            super().__init__()
            self.stdout = io.BytesIO(b'{"frame": "ready", "pid": 1, "limits": 1, "junk": "'
                                     + b"x" * 5000)

    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=1, wall_clock_s=5.0, scripted_responses=("return 1",)
    )
    monkeypatch.setattr(guards, "MAX_FRAME_BYTES", 1024)
    with pytest.raises(UnsupportedIsolationError, match="handshake"):
        GuardedWorkerChannel(
            config, handshake_timeout_s=1.0, spawn=lambda a, c, e: OversizeProc()
        )


def test_worker_emitting_non_json_fails_closed_as_protocol_error() -> None:
    """A non-JSON stdout line is a bounded, terminal protocol failure."""

    class GarbageProc(_NeverAnswerProc):
        def __init__(self) -> None:
            super().__init__()
            self.stdout = io.BytesIO(b"this is not json\n")

    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=1, wall_clock_s=5.0, scripted_responses=("return 1",)
    )
    with pytest.raises(UnsupportedIsolationError, match="handshake"):
        GuardedWorkerChannel(config, handshake_timeout_s=1.0, spawn=lambda a, c, e: GarbageProc())


# --- M4 A1 §4: group cleanup must escalate TERM-resistant survivors ----------------


@pytest.mark.skipif(not _IS_POSIX or not Path("/proc").is_dir(), reason="POSIX /proc only")
def test_stop_process_group_kills_term_resistant_descendant() -> None:
    """The exact review case: the leader exits on TERM but a descendant that
    IGNORES SIGTERM stays alive. stop_process_group must escalate (bounded
    grace, then SIGKILL the retained group) independent of leader state —
    not return as soon as the leader is gone."""
    marker_dir = tmp_path_dir()
    marker = marker_dir / "ready"
    child_code = (
        "import os, signal, time, pathlib\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    parent_code = (
        "import subprocess, time\n"
        f"subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    try:
        deadline = time.monotonic() + 5.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "resistant descendant never spawned"
        descendant = int(marker.read_text())
        started = time.monotonic()
        stop_process_group(proc)
        elapsed = time.monotonic() - started
        # bounded: TERM grace (2s) + KILL + reap must stay well inside 10s
        assert elapsed < 10.0, f"group stop took {elapsed:.1f}s"
        state = _proc_state(descendant)
        assert state in (None, "Z", "X"), (
            f"TERM-resistant descendant still alive in state {state!r}"
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=5)


@pytest.mark.skipif(not _IS_POSIX or not Path("/proc").is_dir(), reason="POSIX /proc only")
def test_stop_process_group_with_reaped_leader_uses_captured_pgid() -> None:
    """The leader already exited AND was reaped (pid unresolvable): the
    CAPTURED group id still identifies the survivors, and the stop must
    escalate them — the historical close() skip left them running."""
    marker_dir = tmp_path_dir()
    marker = marker_dir / "ready"
    child_code = (
        "import os, signal, time, pathlib\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    parent_code = (
        f"import subprocess\nsubprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)  # captured while the leader was alive
    try:
        deadline = time.monotonic() + 5.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        descendant = int(marker.read_text())
        proc.wait(timeout=5)  # leader exits AND is reaped here
        stop_process_group(proc, pgid=pgid)
        state = _proc_state(descendant)
        assert state in (None, "Z", "X"), (
            f"survivor of a reaped leader still alive in state {state!r}"
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=5)


@pytest.mark.skipif(not _IS_POSIX or not Path("/proc").is_dir(), reason="POSIX /proc only")
def test_channel_close_kills_surviving_group_and_releases_pipes() -> None:
    """GuardedWorkerChannel.close() must not skip group cleanup when the
    worker leader already died: a descendant holding the channel's inherited
    stdout/stderr pipes is escalated within the bound, so the bounded readers
    join and the pipe actually reaches EOF."""
    marker_dir = tmp_path_dir()
    marker = marker_dir / "ready"
    child_code = (
        "import os, signal, time, pathlib\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    # A real child speaking JUST enough protocol (ready frame with the exact
    # requested limits), then spawning the TERM-resistant descendant and
    # exiting immediately: the survivor holds the inherited stdout pipe.
    leader_code = (
        "import json, os, subprocess, sys\n"
        f"subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])\n"
        "sys.stdout.write(json.dumps({'frame': 'ready', 'pid': os.getpid(),\n"
        "  'limits': {'rlimit_as_bytes': %d, 'rlimit_cpu_s': %d,\n"
        "             'rlimit_nofile': %d}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.exit(0)\n" % (guards.WORKER_LIMITS["rlimit_as_bytes"],
                           guards.WORKER_LIMITS["rlimit_cpu_s"],
                           guards.WORKER_LIMITS["rlimit_nofile"])
    )

    def spawn_survivor(argv, cwd, env):
        return subprocess.Popen(
            [sys.executable, "-c", leader_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE, max_steps=1, wall_clock_s=10.0, scripted_responses=("return 1",)
    )
    channel = GuardedWorkerChannel(config, spawn=spawn_survivor)
    try:
        deadline = time.monotonic() + 5.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        descendant = int(marker.read_text())
        deadline = time.monotonic() + 5.0
        while channel._proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert channel._proc.poll() is not None, "leader should have exited immediately"
        started = time.monotonic()
        channel.close()  # historically SKIPPED the group cleanup here
        elapsed = time.monotonic() - started
        assert elapsed < 10.0
        state = _proc_state(descendant)
        assert state in (None, "Z", "X"), (
            f"descendant survived channel.close() in state {state!r}"
        )
        # the bounded stdout reader terminates (the pipe hit EOF once the
        # survivor died) instead of timing out its join
        for reader in channel._readers:
            reader.join(timeout=2.0)
            assert not reader.is_alive(), "stdout reader still blocked on an open pipe"
    finally:
        channel.close()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(channel._proc.pid), signal.SIGKILL)


def _proc_state(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2]
    except (FileNotFoundError, ProcessLookupError):
        return None


def tmp_path_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="vouch-m4-group-"))
