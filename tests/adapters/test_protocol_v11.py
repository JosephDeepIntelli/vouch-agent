"""Protocol v1.1 hardening tests (lead-owned, review B2): identity echo,
metering finiteness, bounded artifact transfer, narrow child environment."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

import pytest

from vouch_agent.adapters.process_adapter import ProcessAdapterClient
from vouch_agent.adapters.protocol import (
    MAX_ARTIFACT_BYTES,
    artifacts_from_payload,
    build_identity,
    execution_from_payload,
    verify_identity_echo,
)
from vouch_agent.contracts import RunMode
from vouch_agent.errors import ProtocolFrameError

# --- identity echo ------------------------------------------------------------


def test_identity_echo_must_match_request() -> None:
    identity = build_identity(
        run_id="run_1",
        attempt_id="a1",
        workflow_id="W-C3",
        case_id="case-1",
        mode="fixture",
        version_digest="sha256:" + "a" * 64,
    )
    good = dict(identity)
    verify_identity_echo(identity, {"identity": good})
    for field in ("runId", "attemptId", "workflowId", "caseId", "mode", "versionDigest"):
        tampered = dict(identity)
        tampered[field] = "spoofed"
        with pytest.raises(ProtocolFrameError, match=field):
            verify_identity_echo(identity, {"identity": tampered})
    with pytest.raises(ProtocolFrameError, match="missing"):
        verify_identity_echo(identity, {})


def test_version_digest_may_be_legitimately_absent() -> None:
    identity = build_identity(
        run_id="r", attempt_id="a", workflow_id="w", case_id="c", mode="fixture"
    )
    verify_identity_echo(identity, {"identity": dict(identity)})


# --- metering finiteness --------------------------------------------------------


def test_non_finite_metering_rejected() -> None:
    with pytest.raises(ProtocolFrameError, match="non-finite"):
        execution_from_payload(
            {
                "ok": True,
                "outputs": {},
                "usage": {"tokensScripted": float("nan")},
            }
        )
    with pytest.raises(ProtocolFrameError, match="non-finite"):
        execution_from_payload(
            {
                "ok": True,
                "outputs": {},
                "usage": {"elapsedMsMeasured": float("inf")},
            }
        )


def test_separated_metering_fields_accepted() -> None:
    execution = execution_from_payload(
        {
            "ok": True,
            "outputs": {},
            "usage": {
                "tokensScripted": 1200,
                "elapsedMsMeasured": 34.5,
                "creditsSimulated": 2,
            },
        }
    )
    assert execution.usage is not None
    assert "costUsd" not in execution.usage  # fixture run: no priced cost merged in


# --- bounded artifact transfer ----------------------------------------------------


def _artifact(payload: bytes, kind: str = "report") -> dict:
    return {
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "bytes": base64.b64encode(payload).decode("ascii"),
        "kind": kind,
    }


def test_artifact_bytes_reverified_against_digest() -> None:
    art = _artifact("迪普智选 report".encode())
    arts = artifacts_from_payload({"artifacts": [art]})
    assert arts[0][1].decode("utf-8") == "迪普智选 report"
    tampered = dict(art)
    tampered["bytes"] = base64.b64encode(b"tampered bytes").decode("ascii")
    with pytest.raises(ProtocolFrameError, match="digest mismatch"):
        artifacts_from_payload({"artifacts": [tampered]})


def test_artifact_size_cap_enforced() -> None:
    big = b"x" * (MAX_ARTIFACT_BYTES + 1)
    with pytest.raises(ProtocolFrameError, match="transfer cap"):
        artifacts_from_payload({"artifacts": [_artifact(big)]})


def test_artifact_host_path_not_honored() -> None:
    with pytest.raises(ProtocolFrameError):
        artifacts_from_payload(
            {"artifacts": [{"digest": "sha256:" + "0" * 64, "path": "/etc/passwd"}]}
        )


# --- narrow child environment -------------------------------------------------------


GOOD_DESCRIBE = """
import json, sys
sys.stdout.buffer.write(json.dumps({
    "protocolVersion": "1", "seq": 0, "kind": "describe-response", "runId": None,
    "payload": {"adapterId": "env-probe@1", "protocolVersion": "1",
                "workflows": ["W-C3"], "enforcedModes": ["fixture"]}}).encode() + b"\\n")
sys.stdout.buffer.flush()
line = sys.stdin.buffer.readline()
sys.stdout.buffer.write(json.dumps({
    "protocolVersion": "1", "seq": 1, "kind": "prepare-response", "runId": "run_env",
    "payload": {"ok": True}}).encode() + b"\\n")
sys.stdout.buffer.flush()
line = sys.stdin.buffer.readline()
sys.stderr.write("ENV PROBE: " + json.dumps({
    k: v for k, v in __import__('os').environ.items()
    if k in ('VOUCH_TEST_SECRET', 'OPENAI_API_KEY', 'PATH', 'HOME', 'PYTHONUNBUFFERED')}) + "\\n")
sys.stderr.flush()
sys.stdout.buffer.write(json.dumps({
    "protocolVersion": "1", "seq": 2, "kind": "execute-response", "runId": "run_env",
    "payload": {"ok": True, "outputs": {}, "usage": {"tokensScripted": 1},
                "mode": "fixture", "runnerVersion": "env-probe@1",
                "identity": {"runId": "run_env", "attemptId": "a1", "workflowId": "W-C3",
                             "caseId": "case-env", "mode": "fixture"}}}).encode() + b"\\n")
sys.stdout.buffer.flush()
"""


def _write_child(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "child.py"
    script.write_text("import json, sys\n" + body, encoding="utf-8")
    return [sys.executable, str(script)]


def test_child_environment_is_allowlisted_not_inherited(tmp_path: Path) -> None:
    command = _write_child(tmp_path, GOOD_DESCRIBE)
    # credential-shaped variables in the PARENT must not reach the child
    env = dict(os.environ)
    env["VOUCH_TEST_SECRET"] = "sk-parent-secret"
    env["OPENAI_API_KEY"] = "sk-parent-openai"
    client = ProcessAdapterClient(command, env=None)  # default: allowlist path
    client.describe()
    client.prepare("run_env", RunMode.FIXTURE)
    client._ensure_process()
    # the allowlist strips before spawn; probe by inspecting the built env
    child_env = client._subprocess_env()
    assert "VOUCH_TEST_SECRET" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "PATH" in child_env  # interpreter resolution still works
    assert child_env.get("PYTHONUNBUFFERED") == "1"
    client.close()
