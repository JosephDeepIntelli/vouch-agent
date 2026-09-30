"""Subprocess isolation: real child runs, and preparation failures fail closed.

The real-child tests spawn ``python -m vouch_agent.runtime.worker_process``
(actual subprocess, rlimits applied, JSON protocol). The fault-injection
tests use the ``spawn`` seam to prove that a worker which cannot be prepared
(dies, garbles the handshake, never answers, or emits non-JSON) never leads
to executing the session on the host — it raises
``UnsupportedIsolationError`` (design §13.2).
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import (
    ReplayExhaustedError,
    UnsupportedIsolationError,
)
from vouch_agent.runtime.guards import (
    WorkerStepRequest,
    assert_isolation_supported,
    run_session_isolated,
)
from vouch_agent.runtime.ports import WorkerSessionConfig


def _config(responses: tuple[str, ...], **kwargs: Any) -> WorkerSessionConfig:
    return WorkerSessionConfig(
        mode=RunMode.FIXTURE,
        max_steps=4,
        wall_clock_s=30.0,
        scripted_responses=responses,
        **kwargs,
    )


class _BlockingStdout:
    """A stdout that yields blank lines forever — alive, but never answers."""

    def __iter__(self) -> Iterator[str]:
        while True:
            time.sleep(0.05)
            yield "\n"


class _FakeProc:
    """Test double implementing the runner's process protocol."""

    def __init__(
        self,
        *,
        exit_before_handshake: bool = False,
        lines: list[str] | None = None,
        never_answer: bool = False,
    ) -> None:
        # Binary pipes, matching the guarded launcher's text=False spawn.
        self.stdin = io.BytesIO()
        self.stderr: Any = io.BytesIO()
        self._lines = list(lines or [])
        self._never_answer = never_answer
        self.stdout: Any = _BlockingStdout() if never_answer else self._bytes_stdout()
        self._poll: int | None = 3 if exit_before_handshake else None
        self.terminated = False

    def _bytes_stdout(self) -> io.BytesIO:
        return io.BytesIO(
            b"".join((line + "\n").encode("utf-8") for line in self._lines)
        )

    def poll(self) -> int | None:
        return self._poll

    def terminate(self) -> None:
        self.terminated = True
        self._poll = 15

    def kill(self) -> None:
        self._poll = 9

    def wait(self, timeout: float | None = None) -> int:
        return self._poll or 0


def test_isolation_supported_on_this_platform() -> None:
    # This suite runs on Linux; if it ever fails here, the platform genuinely
    # cannot isolate and everything below must fail closed.
    assert_isolation_supported()


def test_real_worker_process_runs_steps_with_limits() -> None:
    result = run_session_isolated(
        _config(("return base * 2", "return {'v': base}")),
        [
            WorkerStepRequest(instruction="double", scope={"base": 20}),
            WorkerStepRequest(
                instruction="dict", scope={"base": 7}, structured=True, return_type="dict"
            ),
        ],
    )
    assert result.worker_pid is not None
    assert result.limits_applied["rlimit_as_bytes"] > 0
    assert result.limits_applied["rlimit_cpu_s"] >= 1
    assert result.limits_applied["rlimit_nofile"] >= 1
    assert [s.ok for s in result.steps] == [True, True]
    assert result.steps[0].value == 40
    assert result.steps[1].value == {"v": 7}
    usage = result.steps[-1].usage
    assert usage["llm_calls"] == 2
    assert usage["cost_usd"] == 0.02


def test_worker_process_maps_child_exhaustion_to_vouch_error() -> None:
    with pytest.raises(ReplayExhaustedError):
        run_session_isolated(
            _config(("return 1",)),
            [WorkerStepRequest(instruction="a"), WorkerStepRequest(instruction="b")],
        )


def test_worker_that_dies_before_handshake_fails_closed() -> None:
    with pytest.raises(UnsupportedIsolationError):
        run_session_isolated(
            _config(("return 1",)),
            [WorkerStepRequest(instruction="a")],
            spawn=lambda argv, cwd, env: _FakeProc(exit_before_handshake=True),
        )


def test_worker_with_wrong_first_frame_fails_closed() -> None:
    wrong = _FakeProc(lines=[json.dumps({"frame": "step_result", "index": 0, "ok": True})])
    with pytest.raises(UnsupportedIsolationError, match="handshake"):
        run_session_isolated(
            _config(("return 1",)),
            [WorkerStepRequest(instruction="a")],
            spawn=lambda argv, cwd, env: wrong,
        )


def test_worker_that_never_answers_handshake_fails_closed() -> None:
    silent = _FakeProc(never_answer=True)
    with pytest.raises(UnsupportedIsolationError, match="handshake"):
        run_session_isolated(
            _config(("return 1",)),
            [WorkerStepRequest(instruction="a")],
            spawn=lambda argv, cwd, env: silent,
            handshake_timeout_s=0.5,
        )


def test_worker_emitting_non_json_fails_closed_as_protocol_error() -> None:
    garbage = _FakeProc(lines=["this is not json"])
    # A protocol violation DURING the handshake means no guarded channel was
    # established: it fails closed as an isolation failure (never an unguarded
    # run), with the protocol cause chained for diagnosis.
    with pytest.raises(UnsupportedIsolationError, match="handshake protocol failure"):
        run_session_isolated(
            _config(("return 1",)),
            [WorkerStepRequest(instruction="a")],
            spawn=lambda argv, cwd, env: garbage,
        )


def test_missing_python_executable_fails_closed() -> None:
    with pytest.raises(UnsupportedIsolationError, match="python executable"):
        run_session_isolated(
            _config(("return 1",)),
            [WorkerStepRequest(instruction="a")],
            python_exe="/nonexistent/python-for-vouch-tests",
        )


def test_worker_env_is_a_narrow_allowlist(monkeypatch: Any) -> None:
    """Gate A1: the child env is an ALLOWLIST — a benign-looking variable the
    old deny-list kept (no KEY/TOKEN/SECRET substring) is dropped too, and a
    credential-shaped canary obviously never reaches the worker."""
    from vouch_agent.runtime import guards

    monkeypatch.setenv("VOUCH_TEST_API_KEY", "secret-value")
    monkeypatch.setenv("VOUCH_TEST_TOKEN", "secret-value")
    monkeypatch.setenv("VOUCH_TEST_PLAIN_SETTING", "old-deny-list-kept-me")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = guards._worker_env()
    assert "VOUCH_TEST_API_KEY" not in env
    assert "VOUCH_TEST_TOKEN" not in env
    # the allowlist drops even non-credential-shaped variables
    assert "VOUCH_TEST_PLAIN_SETTING" not in env
    # and keeps only what a worker genuinely needs to boot
    assert env["PATH"] == "/usr/bin"
    assert "vouch_agent" in env["PYTHONPATH"] or "src" in env["PYTHONPATH"]
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    allowed = set(guards._ENV_ALLOWLIST) | {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    assert set(env) <= allowed
