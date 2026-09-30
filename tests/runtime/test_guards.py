"""Offline integrity guards: the no-network socket block and the timeout pragma.

Two claims verified here:

* Fixture/offline sessions cannot reach the network — belt-and-braces at the
  socket level on top of the backend seam (only the scripted backend is ever
  wired in those modes). A backend that somehow attempts a live call during a
  fixture step dies with ``LiveCallBlockedError``, terminally.
* ``allow_timeout_pragma=False`` makes the ``# timeout:`` pragma inert: the
  model cannot raise its own exec bound. The contrast case (pragma allowed)
  proves the bound is genuinely extended when enabled, so the False case is a
  rejection rather than a broken timeout mechanism.
"""

from __future__ import annotations

import socket
import time
from typing import Any

import pytest
from jaz.repl import Continue as ContinueResult
from jaz.repl import PythonREPL

from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import LiveCallBlockedError
from vouch_agent.runtime.guards import no_network_guard
from vouch_agent.runtime.jaz_engine import JazRuntime
from vouch_agent.runtime.ports import WorkerSessionConfig
from vouch_agent.runtime.scripted_backend import ScriptedBackend

# --- socket guard ---------------------------------------------------------------


def test_socket_guard_blocks_connect_create_and_resolve() -> None:
    with no_network_guard("test-session"):
        with pytest.raises(LiveCallBlockedError, match="test-session"):
            socket.create_connection(("127.0.0.1", 9), timeout=0.2)
        with pytest.raises(LiveCallBlockedError):
            socket.getaddrinfo("example.com", 443)
        with pytest.raises(LiveCallBlockedError):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect(("127.0.0.1", 9))
            finally:
                s.close()
    # Restored afterwards: local resolution works again (no network needed).
    assert socket.getaddrinfo("localhost", 80)


def test_guard_is_refcounted_and_nesting_safe() -> None:
    with no_network_guard("outer"):
        with no_network_guard("inner"):
            with pytest.raises(LiveCallBlockedError):
                socket.getaddrinfo("example.com", 443)
        # Still guarded by the outer context after the inner one exited.
        with pytest.raises(LiveCallBlockedError):
            socket.getaddrinfo("example.com", 443)
    assert socket.getaddrinfo("localhost", 80)


def test_fixture_session_blocks_a_live_attempt_at_the_backend_seam() -> None:
    """A backend going live during a fixture step is terminated, not retried."""

    class RogueBackend(ScriptedBackend):
        def complete(self, model: str, messages: list[Any], **kwargs: Any):
            socket.create_connection(("127.0.0.1", 9), timeout=0.3)  # the live attempt
            return super().complete(model, messages, **kwargs)

    import vouch_agent.runtime.jaz_engine as engine

    original = engine.ScriptedBackend
    engine.ScriptedBackend = RogueBackend
    try:
        session = JazRuntime().open_session(
            WorkerSessionConfig(
                mode=RunMode.FIXTURE,
                max_steps=2,
                wall_clock_s=20.0,
                scripted_responses=("return 1",),
            )
        )
        try:
            with pytest.raises(LiveCallBlockedError, match="no-network guard"):
                session.step("t")
        finally:
            session.close()
    finally:
        engine.ScriptedBackend = original


# --- timeout pragma ----------------------------------------------------------------


def _spin(seconds: float) -> str:
    """Host-side busy wait the REPL calls as an input (imports are denied)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass
    return "spun"


def _run_exec(repl: PythonREPL, code: str) -> Any:
    state = repl.initialize(inputs={"spin": _spin}, invoke_tool=None, session_id="pragma")
    return repl.exec(state, code, "c1")


def test_timeout_pragma_is_rejected_when_not_allowed() -> None:
    repl = PythonREPL(exec_timeout=0.5, allow_timeout_pragma=False)
    started = time.monotonic()
    result = _run_exec(repl, "# timeout: 60\nspin(3.0)\nreturn 'ran-full'\n")
    elapsed = time.monotonic() - started
    assert isinstance(result, ContinueResult)
    assert type(result.exception).__name__ == "REPLTimeoutError"
    assert elapsed < 2.0, f"the 60s pragma would have been honored (took {elapsed:.1f}s)"


def test_timeout_pragma_extends_the_bound_when_allowed() -> None:
    repl = PythonREPL(exec_timeout=0.5, allow_timeout_pragma=True)
    started = time.monotonic()
    result = _run_exec(repl, "# timeout: 60\nspin(1.2)\nreturn 'ran-full'\n")
    elapsed = time.monotonic() - started
    assert not isinstance(result, ContinueResult), "exec outlived the 0.5s bound"
    assert elapsed >= 1.0  # it really ran past the configured exec_timeout


def test_session_constructs_the_repl_with_the_pragma_disabled() -> None:
    config = WorkerSessionConfig(
        mode=RunMode.FIXTURE,
        max_steps=2,
        wall_clock_s=10.0,
        scripted_responses=("return 1",),
    )
    session = JazRuntime().open_session(config)
    try:
        assert session._repl.allow_timeout_pragma is config.allow_timeout_pragma
        assert config.allow_timeout_pragma is False  # the ports default
    finally:
        session.close()


def test_pragma_advertisement_discrepancy_is_recorded() -> None:
    """Pinned-build discrepancy, recorded honestly.

    ``PythonREPL.__init__`` documents that with ``allow_timeout_pragma=False``
    the ``# timeout:`` pragma "is not described in the prompt". In the
    installed 0.2.0a4 the rendered REPL description STILL mentions it — the
    enforcement is nonetheless correct (the pragma is inert; see the tests
    above), so this only means the model may *read about* an override that
    does not work. Pinned-version finding; re-check on any JAZ upgrade.
    """
    description_off = PythonREPL(exec_timeout=5.0, allow_timeout_pragma=False).get_description()
    description_on = PythonREPL(exec_timeout=5.0, allow_timeout_pragma=True).get_description()
    assert "# timeout:" in description_on
    assert description_off == description_on  # the off-case is NOT suppressed


def test_runtime_sources_import_only_public_jaz_modules() -> None:
    """The boundary rule as an AST fact: no ``jaz._*`` imports in runtime code.

    (``import jaz`` legitimately loads jaz's own private submodules at
    runtime; the rule governs what OUR import statements name.)
    """
    import ast
    from pathlib import Path

    runtime_dir = Path(__file__).resolve().parent.parent / "src" / "vouch_agent" / "runtime"
    offenders: list[str] = []
    for source in sorted(runtime_dir.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[-1].startswith("_") and name.startswith("jaz"):
                    offenders.append(f"{source.name}: {name}")
    assert not offenders, f"private jaz imports found: {offenders}"
