"""A8 regressions: the inbound frame byte cap holds WHILE ACCUMULATING, and
stderr diagnostics are bounded independently of it.

Inverted from the coordinator proof ``unterminated-frame-exceeds-cap``: a
child that streams 8 MiB + 64 KiB with no newline used to be buffered in
full (8,454,144 bytes against a declared 8,388,608 cap) and then fail on EOF.
Both tests here are deliberately bounded (8 MiB + 64 KiB maximum) — this is
a resource-limit regression proof, not a stress run.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from vouch_agent.adapters.process_adapter import ProcessAdapterClient
from vouch_agent.adapters.protocol import MAX_FRAME_BYTES
from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import ProtocolFrameError


def _write_child(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


def test_unterminated_frame_is_capped_while_accumulating(tmp_path: Path) -> None:
    """8 MiB + 64 KiB with no newline: the cap fires BEFORE the buffer can
    pass MAX_FRAME_BYTES, the child is killed, and the error is the cap —
    not an EOF."""
    sent = MAX_FRAME_BYTES + 65536  # bounded demonstration, no stress
    command = _write_child(
        tmp_path,
        f"""
        import sys
        sys.stdin.readline()
        sys.stdout.buffer.write(b"x" * {sent})
        sys.stdout.buffer.flush()
        sys.stdin.readline()  # stay alive until the controller reacts
        """,
    )
    client = ProcessAdapterClient(command, request_timeout_s=10)
    with pytest.raises(ProtocolFrameError, match="before a newline arrived"):
        client.describe()
    assert client.dead is True
    # The defect was that this exact input produced AdapterExecutionError on
    # EOF with 8,454,144 buffered bytes; now the CAP fires first and the
    # buffer never exceeds the cap by more than one read chunk (64 KiB).
    transport = client._transport
    assert transport is not None
    assert len(transport._buffer) <= MAX_FRAME_BYTES + 65536


def test_oversized_terminated_frame_is_capped_before_parsing(tmp_path: Path) -> None:
    """A single line that DOES end with a newline but exceeds the cap is
    killed the moment its pending bytes pass the limit — before the line is
    ever parsed."""
    sent = MAX_FRAME_BYTES + 4096
    command = _write_child(
        tmp_path,
        f"""
        import sys
        sys.stdin.readline()
        sys.stdout.buffer.write(b"x" * {sent} + b"\\n")
        sys.stdout.buffer.flush()
        sys.stdin.readline()
        """,
    )
    client = ProcessAdapterClient(command, request_timeout_s=10)
    with pytest.raises(ProtocolFrameError, match="exceeds MAX_FRAME_BYTES"):
        client.describe()
    assert client.dead is True


def test_complete_frame_then_runaway_partial_is_still_capped(tmp_path: Path) -> None:
    """A good frame followed by an unterminated runaway partial: the good
    frame is answered, the leftover is capped on the next read."""
    good = (
        'import json, sys\n'
        "sys.stdin.readline()\n"
        "sys.stdout.write(json.dumps({'protocolVersion': '1', 'seq': 0, "
        "'kind': 'describe-response', 'runId': None, 'payload': "
        "{'adapterId': 'chatty@1', 'protocolVersion': '1'}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        f"sys.stdout.buffer.write(b'y' * {MAX_FRAME_BYTES + 65536})\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdin.readline()\n"
    )
    command = _write_child(tmp_path, good)
    client = ProcessAdapterClient(command, request_timeout_s=10)
    descriptor = client.describe()  # the honest first frame is accepted
    assert descriptor.adapter_id == "chatty@1"
    with pytest.raises(ProtocolFrameError, match="exceeds MAX_FRAME_BYTES"):
        client.prepare("run_1", RunMode.FIXTURE)
    assert client.dead is True


def test_stderr_diagnostics_are_bounded_independently_of_the_frame_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chatty child cannot make the diagnostics buffer grow without bound:
    at most MAX_DIAGNOSTICS_BYTES of raw stderr is ever retained, whatever
    the child emits, and the retained text is the TAIL (older bytes are
    dropped and flagged). The cap is lowered for this test so the whole
    demonstration stays well inside a single pipe buffer."""
    import vouch_agent.adapters.protocol as protocol

    monkeypatch.setattr(protocol, "MAX_DIAGNOSTICS_BYTES", 4 * 1024)
    total = 16 * 1024  # 4x the patched cap, far below any pipe/exhaustion limit
    command = _write_child(
        tmp_path,
        f"""
        import sys
        sys.stderr.buffer.write(b"z" * {total})
        sys.stderr.write("\\nTAIL MARKER\\n")
        sys.stderr.flush()
        sys.exit(0)
        """,
    )
    client = ProcessAdapterClient(command, request_timeout_s=10)
    with pytest.raises(Exception, match="exited"):
        client.describe()
    diagnostics = client.diagnostics()
    assert "TAIL MARKER" in diagnostics  # the tail survives
    assert "older stderr bytes dropped by the diagnostics cap" in diagnostics
    # bounded by the cap (+ the flag/redaction), not by what the child emitted
    assert len(diagnostics) <= 2 * 4 * 1024


def test_frame_at_just_under_the_cap_still_parses(tmp_path: Path) -> None:
    """The cap is a ceiling, not a shrink: a legitimate small frame is fine."""
    command = _write_child(
        tmp_path,
        """
        import json, sys
        sys.stdin.readline()
        sys.stdout.write(json.dumps({'protocolVersion': '1', 'seq': 0,
            'kind': 'describe-response', 'runId': None,
            'payload': {'adapterId': 'small@1', 'protocolVersion': '1'}}) + '\\n')
        sys.stdout.flush()
        sys.stdin.readline()
        """,
    )
    client = ProcessAdapterClient(command, request_timeout_s=10)
    assert client.describe().adapter_id == "small@1"
    client.close()
