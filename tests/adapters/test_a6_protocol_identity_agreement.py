"""A6 regressions: frame/payload/identity agreement on every stateful
protocol operation, and collect bound to the exact prepared run.

Inverted from the coordinator proof ``protocol-mode-and-run-mismatch``: a
synthetic child echoing ``identity.mode=fixture`` while the frame says
``runId=OTHER-RUN`` and the payload says ``mode=authorized-live`` used to be
accepted by BOTH execute and collect. Every test here drives a real
subprocess through :class:`ProcessAdapterClient` — no in-process fakes, no
network, no live effects.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from vouch_agent.adapters.process_adapter import ProcessAdapterClient
from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import AdapterExecutionError, ProtocolFrameError

VERSION = "sha256:" + "1" * 64


def _write_child(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


#: A well-behaved child: answers describe/prepare, then lets the test inject
#: the misbehaving execute/collect responses.
_PREAMBLE = """
import json, sys
def send(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\\n")
    sys.stdout.flush()
line = sys.stdin.readline()
send({"protocolVersion": "1", "seq": 0, "kind": "describe-response", "runId": None,
      "payload": {"adapterId": "liar-child@1", "protocolVersion": "1",
                  "workflows": ["W-C3"], "enforcedModes": ["fixture"]}})
line = sys.stdin.readline()
req = json.loads(line)
send({"protocolVersion": "1", "seq": 1, "kind": "prepare-response",
      "runId": req["runId"], "payload": {"ok": True}})
"""


def _execute_body(payload_mode: str, frame_run: str, identity_mode: str) -> str:
    return f"""
line = sys.stdin.readline()
req = json.loads(line)
ident = dict(req["payload"]["identity"])
ident["mode"] = "{identity_mode}"
send({{"protocolVersion": "1", "seq": 2, "kind": "execute-response",
      "runId": "{frame_run}",
      "payload": {{"ok": True, "outputs": {{}}, "usage": {{"steps": 1}},
                  "mode": "{payload_mode}", "runnerVersion": "liar-child@1",
                  "identity": ident}}}})
"""


def _client(command: list[str]) -> ProcessAdapterClient:
    return ProcessAdapterClient(command, request_timeout_s=5, execute_timeout_s=10)


def _execute(client: ProcessAdapterClient) -> object:
    return client.execute(
        run_id="requested-run",
        attempt_id="attempt-1",
        workflow_id="W-C3",
        case_input={"caseId": "case-1", "versionDigest": VERSION},
        mode=RunMode.FIXTURE,
    )


def _collect(client: ProcessAdapterClient, run_id: str = "requested-run") -> object:
    return client.collect(run_id)


# -- the exact counterexample, inverted --------------------------------------------


def test_frame_run_mismatch_is_refused_and_kills_the_child(tmp_path: Path) -> None:
    """A response frame naming ANOTHER run never speaks for the request."""
    command = _write_child(
        tmp_path, _PREAMBLE + _execute_body("fixture", "OTHER-RUN", "fixture")
    )
    client = _client(command)
    client.describe()
    client.prepare("requested-run", RunMode.FIXTURE)
    with pytest.raises(ProtocolFrameError, match="never speaks for another run"):
        _execute(client)
    assert client.dead is True  # fail closed: the child is killed


def test_payload_mode_contradicting_request_is_refused(tmp_path: Path) -> None:
    """identity.mode=fixture + payload mode=authorized-live + request fixture
    is a contradiction, not a result."""
    command = _write_child(
        tmp_path, _PREAMBLE + _execute_body("authorized-live", "requested-run", "fixture")
    )
    client = _client(command)
    client.describe()
    client.prepare("requested-run", RunMode.FIXTURE)
    with pytest.raises(ProtocolFrameError, match="mode disagreement"):
        _execute(client)
    assert client.dead is True


def test_identity_mode_contradicting_request_is_refused(tmp_path: Path) -> None:
    """The echo itself must match the request mode (v1.1 §1)."""
    command = _write_child(
        tmp_path, _PREAMBLE + _execute_body("fixture", "requested-run", "authorized-live")
    )
    client = _client(command)
    client.describe()
    client.prepare("requested-run", RunMode.FIXTURE)
    with pytest.raises(ProtocolFrameError, match="identity mismatch on 'mode'"):
        _execute(client)


def test_echo_may_not_invent_a_version_digest_the_request_never_sent(
    tmp_path: Path,
) -> None:
    """An echo is verbatim: adding provenance the request did not carry is a
    mismatch, not extra information."""
    command = _write_child(
        tmp_path,
        _PREAMBLE
        + """
line = sys.stdin.readline()
req = json.loads(line)
ident = dict(req["payload"]["identity"])
ident["versionDigest"] = "sha256:" + "e" * 64
send({"protocolVersion": "1", "seq": 2, "kind": "execute-response",
      "runId": "requested-run",
      "payload": {"ok": True, "outputs": {}, "usage": {"steps": 1},
                  "mode": "fixture", "runnerVersion": "liar-child@1",
                  "identity": ident}})
""",
    )
    client = _client(command)
    client.describe()
    client.prepare("requested-run", RunMode.FIXTURE)
    with pytest.raises(ProtocolFrameError, match="never carried"):
        client.execute(
            run_id="requested-run",
            attempt_id="attempt-1",
            workflow_id="W-C3",
            case_input={"caseId": "case-1"},  # genuinely unknown version
            mode=RunMode.FIXTURE,
        )


# -- collect is bound to the exact prepared run -------------------------------------


def test_collect_for_a_run_this_client_never_prepared_is_refused(tmp_path: Path) -> None:
    command = _write_child(tmp_path, _PREAMBLE)
    client = _client(command)
    client.describe()
    client.prepare("requested-run", RunMode.FIXTURE)
    with pytest.raises(AdapterExecutionError, match="never prepared on this adapter client"):
        _collect(client, run_id="somebody-elses-run")


def test_collect_answer_for_another_run_is_refused(tmp_path: Path) -> None:
    """Even a collect response naming a different run id is rejected at the
    transport level, before its digests can be believed."""
    command = _write_child(
        tmp_path,
        _PREAMBLE
        + """
line = sys.stdin.readline()
send({"protocolVersion": "1", "seq": 2, "kind": "collect-response",
      "runId": "OTHER-RUN",
      "payload": {"digests": ["sha256:" + "a" * 64]}})
""",
    )
    client = _client(command)
    client.describe()
    client.prepare("requested-run", RunMode.FIXTURE)
    with pytest.raises(ProtocolFrameError, match="never speaks for another run"):
        _collect(client)
    assert client.dead is True


def test_collect_after_cleanup_is_refused_client_side(tmp_path: Path) -> None:
    command = _write_child(
        tmp_path,
        _PREAMBLE
        + """
line = sys.stdin.readline()
send({"protocolVersion": "1", "seq": 2, "kind": "collect-response",
      "runId": "requested-run", "payload": {"digests": []}})
line = sys.stdin.readline()
send({"protocolVersion": "1", "seq": 3, "kind": "cleanup-response",
      "runId": "requested-run", "payload": {"ok": True}})
""",
    )
    client = _client(command)
    client.describe()
    client.prepare("requested-run", RunMode.FIXTURE)
    assert _collect(client) == ()
    client.cleanup("requested-run")
    with pytest.raises(AdapterExecutionError, match="never prepared on this adapter client"):
        _collect(client)


# -- an honest child still works end to end ------------------------------------------


def test_agreeing_identity_passes(tmp_path: Path) -> None:
    command = _write_child(
        tmp_path,
        _PREAMBLE
        + _execute_body("fixture", "requested-run", "fixture")
        + """
line = sys.stdin.readline()
send({"protocolVersion": "1", "seq": 3, "kind": "collect-response",
      "runId": "requested-run", "payload": {"digests": ["sha256:" + "a" * 64]}})
""",
    )
    client = _client(command)
    client.describe()
    client.prepare("requested-run", RunMode.FIXTURE)
    execution = _execute(client)
    assert execution.mode is RunMode.FIXTURE
    assert _collect(client) == ("sha256:" + "a" * 64,)
    client.close()


def test_explicit_protocol_version_compatibility_is_enforced(tmp_path: Path) -> None:
    """An unknown protocolVersion on the response frame is refused before any
    payload is parsed — compatibility stays explicit on both sides."""
    command = _write_child(
        tmp_path,
        """
import json, sys
sys.stdin.readline()
sys.stdout.write(json.dumps({"protocolVersion": "2", "seq": 0,
    "kind": "describe-response", "runId": None, "payload": {}}) + "\\n")
sys.stdout.flush()
""",
    )
    client = _client(command)
    with pytest.raises(ProtocolFrameError, match="unknown protocolVersion"):
        client.describe()
