"""Adapter protocol v1 — versioned JSONL frames over stdin/stdout (design §4.2).

Every request frame carries ``protocolVersion``, a monotonic ``seq``, a
``kind`` (describe/prepare/execute/collect/cleanup + error), the ``runId``
for run-scoped kinds, and a JSON-object ``payload``. The protocol is spoken
between the trusted Vouch controller (client side) and any system-under-
improvement runner (server side: Choose's own Node runner, Visibility's
Python runner, or the in-repo fixture adapter). stdout carries protocol
frames only; stderr carries sanitized diagnostics only.

Failure semantics (fail closed, never silently success-shaped):

* unknown ``protocolVersion``  -> :class:`ProtocolFrameError`
* out-of-order / non-monotonic ``seq`` -> :class:`ProtocolFrameError`
* oversized frame (``MAX_FRAME_BYTES``) -> :class:`ProtocolFrameError`
* malformed JSON / non-object frame -> :class:`ProtocolFrameError`
* missing metering on an execute result -> :class:`MissingMeteringError`
* adapter-reported failure (error frame) -> :class:`AdapterExecutionError`
* wall-clock overrun -> process killed + :class:`AdapterExecutionError`
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from vouch_agent.adapters.base import AdapterDescriptor, AdapterExecution
from vouch_agent.contracts.common import DIGEST_PREFIX, RunMode, require_digest
from vouch_agent.errors import (
    AdapterExecutionError,
    ContractError,
    MissingMeteringError,
    ProtocolFrameError,
)

#: The only protocol version this controller speaks (design §4.2, proposed v1).
PROTOCOL_VERSION = "1"

#: Hard cap on one encoded frame. Oversized payloads fail explicitly instead
#: of being silently truncated or unbounded.
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: stderr diagnostics are tail-limited to this many characters before redaction.
MAX_DIAGNOSTICS_CHARS = 8000

#: Hard cap on raw stderr bytes retained while draining, INDEPENDENT of the
#: frame cap (A8): a child may chatter on stderr without that ever counting
#: against frame budget, and neither channel may grow without bound. Only the
#: tail is kept; anything older is dropped and flagged.
MAX_DIAGNOSTICS_BYTES = 64 * 1024

_KINDS_REQUIRING_RUN_ID = frozenset(
    {
        "prepare-request",
        "prepare-response",
        "execute-request",
        "execute-response",
        "apply-config-request",
        "apply-config-response",
        "collect-request",
        "collect-response",
        "cleanup-request",
        "cleanup-response",
    }
)


class FrameKind(StrEnum):
    """Frame kinds cover describe/prepare/execute/apply-config/collect/cleanup
    + error (``apply-config`` is the additive v1.2 application operation:
    same run-scoped, identity-echoed, metered semantics as execute)."""

    DESCRIBE_REQUEST = "describe-request"
    DESCRIBE_RESPONSE = "describe-response"
    PREPARE_REQUEST = "prepare-request"
    PREPARE_RESPONSE = "prepare-response"
    EXECUTE_REQUEST = "execute-request"
    EXECUTE_RESPONSE = "execute-response"
    APPLY_CONFIG_REQUEST = "apply-config-request"
    APPLY_CONFIG_RESPONSE = "apply-config-response"
    COLLECT_REQUEST = "collect-request"
    COLLECT_RESPONSE = "collect-response"
    CLEANUP_REQUEST = "cleanup-request"
    CLEANUP_RESPONSE = "cleanup-response"
    ERROR = "error"


#: Expected response kind for each request kind (one logical conversation).
REQUEST_TO_RESPONSE: dict[FrameKind, FrameKind] = {
    FrameKind.DESCRIBE_REQUEST: FrameKind.DESCRIBE_RESPONSE,
    FrameKind.PREPARE_REQUEST: FrameKind.PREPARE_RESPONSE,
    FrameKind.EXECUTE_REQUEST: FrameKind.EXECUTE_RESPONSE,
    FrameKind.APPLY_CONFIG_REQUEST: FrameKind.APPLY_CONFIG_RESPONSE,
    FrameKind.COLLECT_REQUEST: FrameKind.COLLECT_RESPONSE,
    FrameKind.CLEANUP_REQUEST: FrameKind.CLEANUP_RESPONSE,
}


@dataclass(frozen=True)
class Frame:
    """One protocol frame. ``seq`` is monotonic per direction from 0."""

    seq: int
    kind: FrameKind
    payload: dict[str, Any]
    run_id: str | None = None
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "seq": self.seq,
            "kind": self.kind.value,
            "runId": self.run_id,
            "payload": self.payload,
        }

    def to_json_line(self) -> str:
        line = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        _check_size(line)
        return line


def _check_size(line: str) -> None:
    if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolFrameError(
            f"frame of {len(line.encode('utf-8'))} bytes exceeds MAX_FRAME_BYTES={MAX_FRAME_BYTES}"
        )


def parse_frame(line: bytes | bytearray | str) -> Frame:
    """Decode and structurally validate one JSONL frame.

    Malformed JSON, non-object frames, unknown versions, unknown kinds, bad
    sequence numbers, missing run ids and non-object payloads all raise
    :class:`ProtocolFrameError`.
    """
    if isinstance(line, bytes | bytearray):
        if len(line) > MAX_FRAME_BYTES:
            raise ProtocolFrameError(
                f"inbound frame of {len(line)} bytes exceeds MAX_FRAME_BYTES={MAX_FRAME_BYTES}"
            )
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolFrameError(f"frame is not valid UTF-8: {exc}") from exc
    else:
        text = line
        _check_size(text)

    text = text.strip()
    if not text:
        raise ProtocolFrameError("empty frame line")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolFrameError(f"malformed JSON frame: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolFrameError(f"frame must be a JSON object, got {type(obj).__name__}")

    version = obj.get("protocolVersion")
    if version != PROTOCOL_VERSION:
        raise ProtocolFrameError(
            f"unknown protocolVersion {version!r} (only {PROTOCOL_VERSION!r} is supported)"
        )

    raw_kind = obj.get("kind")
    try:
        kind = FrameKind(str(raw_kind))
    except ValueError:
        raise ProtocolFrameError(f"unknown frame kind {raw_kind!r}") from None

    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ProtocolFrameError(f"seq must be a non-negative integer, got {seq!r}")

    run_id = obj.get("runId")
    if kind.value in _KINDS_REQUIRING_RUN_ID:
        if not isinstance(run_id, str) or not run_id:
            raise ProtocolFrameError(f"{kind.value} requires a non-empty runId, got {run_id!r}")
    elif kind is FrameKind.ERROR:
        # Error frames carry the runId when the failure is run-scoped;
        # session-level violations (bad describe frame, garbage line) have none.
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise ProtocolFrameError(
                f"error frame runId must be a non-empty string, got {run_id!r}"
            )
    elif run_id is not None:
        raise ProtocolFrameError(f"{kind.value} must not carry a runId, got {run_id!r}")

    payload = obj.get("payload")
    if not isinstance(payload, dict):
        raise ProtocolFrameError(f"payload must be a JSON object, got {type(payload).__name__}")

    return Frame(seq=seq, kind=kind, payload=payload, run_id=run_id)


class SequenceTracker:
    """Enforces per-direction monotonic sequence numbers starting at 0."""

    def __init__(self) -> None:
        self._expected = 0

    @property
    def expected(self) -> int:
        return self._expected

    def next(self) -> int:
        value = self._expected
        self._expected += 1
        return value

    def observe(self, frame: Frame) -> None:
        if frame.seq != self._expected:
            raise ProtocolFrameError(
                f"out-of-order frame: expected seq {self._expected}, got {frame.seq} "
                f"({frame.kind.value})"
            )
        self._expected += 1


# --- payload decoders (response payloads -> typed port objects) --------------


def descriptor_from_payload(payload: dict[str, Any]) -> AdapterDescriptor:
    """Decode a describe-response payload into an :class:`AdapterDescriptor`."""
    try:
        version = payload.get("protocolVersion", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise ProtocolFrameError(f"descriptor declares unknown protocolVersion {version!r}")
        modes = tuple(RunMode(m) for m in payload.get("enforcedModes", ["fixture"]))
        return AdapterDescriptor(
            adapter_id=_require_str_field(payload, "adapterId"),
            protocol_version=version,
            workflows=tuple(_str_list(payload.get("workflows", []), "workflows")),
            actions=tuple(_str_list(payload.get("actions", []), "actions")),
            enforced_modes=modes,
            notes=str(payload.get("notes", "")),
        )
    except ProtocolFrameError:
        raise
    except (KeyError, ValueError) as exc:
        raise ProtocolFrameError(f"malformed describe payload: {exc}") from exc


def execution_from_payload(
    payload: dict[str, Any], *, require_metering: bool = True
) -> AdapterExecution:
    """Decode an execute-response payload into an :class:`AdapterExecution`.

    A missing/empty usage block raises :class:`MissingMeteringError` when
    ``require_metering`` — an unmeasured attempt is never free.
    """
    try:
        ok = payload.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolFrameError(f"execute payload 'ok' must be a boolean, got {ok!r}")
        outputs = payload.get("outputs", {})
        if not isinstance(outputs, dict):
            raise ProtocolFrameError("execute payload 'outputs' must be an object")
        tool_events = payload.get("toolEvents", [])
        if not isinstance(tool_events, list) or not all(isinstance(e, dict) for e in tool_events):
            raise ProtocolFrameError("execute payload 'toolEvents' must be a list of objects")
        evidence_refs = []
        for ref in payload.get("evidenceRefs", []):
            try:
                evidence_refs.append(require_digest(ref, "evidenceRefs[*]"))
            except ContractError as exc:
                raise ProtocolFrameError(f"malformed execute payload: {exc}") from exc
        usage = payload.get("usage")
        if require_metering:
            _require_usage(usage)
        elif usage is not None and not isinstance(usage, dict):
            raise ProtocolFrameError("execute payload 'usage' must be an object when present")
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            raise ProtocolFrameError("execute payload 'error' must be a string or null")
        mode = RunMode(payload.get("mode", "fixture"))
        runner_version = payload.get("runnerVersion", "")
        if not isinstance(runner_version, str):
            raise ProtocolFrameError("execute payload 'runnerVersion' must be a string")
        return AdapterExecution(
            ok=ok,
            outputs=outputs,
            evidence_refs=tuple(evidence_refs),
            tool_events=tuple(tool_events),
            usage=usage,
            error=error,
            mode=mode,
            runner_version=runner_version,
        )
    except ProtocolFrameError:
        raise
    except (KeyError, ValueError) as exc:
        raise ProtocolFrameError(f"malformed execute payload: {exc}") from exc


def _require_usage(usage: Any) -> None:
    if not isinstance(usage, dict) or not usage:
        raise MissingMeteringError(
            "execute result is missing usage metering; an unmeasured attempt is never free"
        )
    if not any(
        isinstance(v, bool) is False and isinstance(v, (int, float)) for v in usage.values()
    ):
        raise MissingMeteringError(f"usage metering carries no measured quantity: {sorted(usage)}")
    import math

    for key, value in usage.items():
        if (
            isinstance(value, bool) is False
            and isinstance(value, (int, float))
            and not math.isfinite(value)
        ):
            raise ProtocolFrameError(
                f"usage metering field {key!r} is non-finite ({value!r}); "
                "non-finite metering is rejected (protocol v1.1 §2)"
            )


#: v1.1 identity fields an execute request/response pair must agree on.
IDENTITY_FIELDS = ("runId", "attemptId", "workflowId", "caseId", "mode", "versionDigest")


def build_identity(
    *,
    run_id: str,
    attempt_id: str,
    workflow_id: str,
    case_id: str,
    mode: str,
    version_digest: str | None = None,
) -> dict[str, str]:
    """The identity tuple v1.1 execute frames carry and echo (§1)."""
    identity: dict[str, str] = {
        "runId": run_id,
        "attemptId": attempt_id,
        "workflowId": workflow_id,
        "caseId": case_id,
        "mode": mode,
    }
    if version_digest:
        identity["versionDigest"] = version_digest
    return identity


def verify_identity_echo(request_identity: dict[str, str], payload: dict[str, Any]) -> None:
    """Reject an execute response whose identity echo mismatches the request.

    Kind/sequence alone do not prove which run/attempt/case/version/mode a
    result belongs to (coordinator review B2). The echo must be VERBATIM: a
    response may not add an identity field the request never carried either —
    an echo that names a version the request did not send is claiming proven
    ancestry it was never asked about (A6).
    """
    echoed = payload.get("identity")
    if not isinstance(echoed, dict):
        raise ProtocolFrameError("execute response is missing the v1.1 identity echo (protocol §1)")
    for field in IDENTITY_FIELDS:
        expected = request_identity.get(field)
        actual = echoed.get(field)
        if field not in request_identity:
            # versionDigest may be legitimately unknown/omitted by the caller;
            # the echo must then omit it too, not invent one.
            if field in echoed:
                raise ProtocolFrameError(
                    f"execute response identity echo adds {field!r}={actual!r} the request "
                    "never carried; an echo is verbatim, not supplementary (A6)"
                )
            continue
        if actual != expected:
            raise ProtocolFrameError(
                f"execute response identity mismatch on {field!r}: "
                f"requested {expected!r}, echoed {actual!r}"
            )


#: Default per-artifact byte cap for in-frame artifact transfer (§3).
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


def artifacts_from_payload(
    payload: dict[str, Any], *, max_bytes: int = MAX_ARTIFACT_BYTES
) -> tuple[tuple[str, bytes, str], ...]:
    """Decode+VERIFY v1.1 collect artifacts: base64 bytes re-hashed against
    their digests, size-capped, kind-labeled. Host paths are never honored
    as read capabilities."""
    import base64
    import hashlib

    raw = payload.get("artifacts", [])
    if not isinstance(raw, list):
        raise ProtocolFrameError("collect payload 'artifacts' must be a list")
    out: list[tuple[str, bytes, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ProtocolFrameError("each artifact must be an object")
        digest = entry.get("digest")
        try:
            digest = require_digest(digest, "artifacts[*].digest")
        except ContractError as exc:
            raise ProtocolFrameError(f"malformed artifact entry: {exc}") from exc
        kind = entry.get("kind", "evidence")
        if kind not in ("report", "evidence", "usage"):
            raise ProtocolFrameError(f"unknown artifact kind {kind!r}")
        encoded = entry.get("bytes")
        if not isinstance(encoded, str):
            raise ProtocolFrameError("artifact 'bytes' must be base64 text")
        try:
            payload_bytes = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ProtocolFrameError(f"artifact bytes are not valid base64: {exc}") from exc
        if len(payload_bytes) > max_bytes:
            raise ProtocolFrameError(f"artifact {digest} exceeds the {max_bytes}-byte transfer cap")
        actual = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
        if actual != digest:
            raise ProtocolFrameError(
                f"artifact digest mismatch: declared {digest}, bytes hash to {actual}"
            )
        out.append((digest, payload_bytes, kind))
    return tuple(out)


def digests_from_payload(payload: dict[str, Any]) -> tuple[str, ...]:
    """Decode a collect-response payload into sealed artifact digests."""
    raw = payload.get("digests", [])
    if not isinstance(raw, list):
        raise ProtocolFrameError("collect payload 'digests' must be a list")
    try:
        return tuple(require_digest(d, "digests[*]") for d in raw)
    except ContractError as exc:
        raise ProtocolFrameError(f"malformed collect payload: {exc}") from exc


def ok_from_payload(payload: dict[str, Any], kind: FrameKind) -> None:
    """Decode prepare/cleanup responses: ``ok`` must be true, else failure."""
    ok = payload.get("ok")
    if ok is not True:
        message = payload.get("message", "")
        code = payload.get("code", "vouch/adapter-execution")
        raise AdapterExecutionError(f"{kind.value} failed ({code}): {message}")


def _require_str_field(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolFrameError(f"payload field {key!r} must be a non-empty string")
    return value


def _str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProtocolFrameError(f"payload field {field!r} must be a list of strings")
    return value


# --- sanitized stderr diagnostics ---------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(?i)\b(api[-_]?key|authorization|bearer|token|secret|password|passwd|credential)s?\b"
    r"(\s*[:= \t]\s*(?:bearer\s+)?[^\s,;]+)"
)
_BLOB_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b|\b[A-Za-z0-9+/]{40,}={0,2}\b")


def sanitize_diagnostics(text: str, *, limit: int = MAX_DIAGNOSTICS_CHARS) -> str:
    """Redact secret-shaped content and tail-limit adapter stderr output."""
    if not text:
        return ""
    redacted = _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    redacted = _BLOB_RE.sub("[redacted-blob]", redacted)
    if len(redacted) > limit:
        redacted = "…" + redacted[-limit:]
    return redacted


# --- subprocess transport ------------------------------------------------------


class ProtocolTransport:
    """Speaks framed JSONL with a subprocess over stdin/stdout.

    The controller sends request frames and reads the matching response (or
    an error frame). Inbound frames are sequence-validated; a wall-clock
    overrun kills the process (never a silent partial read). stderr is kept
    separate and only ever surfaces through :meth:`diagnostics` in sanitized
    form.
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._send_seq = SequenceTracker()
        self._recv_seq = SequenceTracker()
        self._buffer = bytearray()
        self._diagnostics = ""
        self._closed = False

    @property
    def dead(self) -> bool:
        return self._closed or self._process.poll() is not None

    def request(
        self,
        kind: FrameKind,
        *,
        run_id: str | None,
        payload: dict[str, Any],
        timeout_s: float,
    ) -> Frame:
        """Send one request frame and return its matched response frame.

        A response is accepted only when it answers THIS request: the expected
        kind (one logical conversation) AND, transport-level (A6), a matching
        run identity — a frame that names a different run never speaks for the
        one that was asked about, no matter how well-formed it is. A
        run-identity contradiction kills the process: the child has shown it
        cannot be trusted to attribute its own work.
        """
        if self.dead:
            raise AdapterExecutionError("adapter process is not running (killed or exited)")
        self._send_frame(kind, run_id=run_id, payload=payload)
        expected = REQUEST_TO_RESPONSE[kind]
        deadline = time.monotonic() + timeout_s
        while True:
            frame = self._read_frame(deadline, request_kind=kind.value, timeout_s=timeout_s)
            if frame.run_id is not None and frame.run_id != run_id:
                self.kill_and_drain()
                raise ProtocolFrameError(
                    f"{frame.kind.value} frame carries runId {frame.run_id!r} while the "
                    f"request was for {run_id!r}: a response never speaks for another "
                    "run (protocol identity agreement, A6); process killed"
                )
            if frame.kind is FrameKind.ERROR:
                self._raise_error_frame(frame)
            if frame.kind is not expected:
                raise ProtocolFrameError(
                    f"unexpected frame kind {frame.kind.value} while awaiting "
                    f"{expected.value} (seq {frame.seq})"
                )
            if frame.protocol_version != PROTOCOL_VERSION:
                raise ProtocolFrameError(
                    f"{frame.kind.value} declares protocolVersion "
                    f"{frame.protocol_version!r}, not {PROTOCOL_VERSION!r}"
                )
            return frame

    def _send_frame(self, kind: FrameKind, *, run_id: str | None, payload: dict[str, Any]) -> None:
        frame = Frame(seq=self._send_seq.next(), kind=kind, payload=payload, run_id=run_id)
        line = frame.to_json_line().encode("utf-8") + b"\n"
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(line)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AdapterExecutionError(
                f"adapter closed its stdin before answering {kind.value}: {exc}"
            ) from exc

    def _read_frame(self, deadline: float, *, request_kind: str, timeout_s: float) -> Frame:
        """Read one complete frame line before ``deadline``; kill on overrun.

        The byte cap is enforced WHILE ACCUMULATING, before any newline is
        seen (A8): an unterminated or oversized line kills the process the
        moment the pending bytes pass ``MAX_FRAME_BYTES`` instead of buffering
        until EOF. The cap covers the bytes of the line currently being read
        (everything before the first newline, or the whole buffer when no
        newline has arrived yet), so a complete frame followed by a runaway
        partial is caught on the next pass too.
        """
        stdout_fd = self._process.stdout.fileno() if self._process.stdout else None
        if stdout_fd is None:
            raise AdapterExecutionError("adapter stdout is not connected")
        selector = selectors.DefaultSelector()
        selector.register(stdout_fd, selectors.EVENT_READ)
        try:
            while True:
                newline = self._buffer.find(b"\n")
                pending = len(self._buffer) if newline < 0 else newline
                if pending > MAX_FRAME_BYTES:
                    self.kill_and_drain()
                    raise ProtocolFrameError(
                        f"inbound frame of {pending} bytes exceeds "
                        f"MAX_FRAME_BYTES={MAX_FRAME_BYTES} before a newline arrived; "
                        "process killed (A8: the cap holds while accumulating)"
                    )
                if newline >= 0:
                    raw = bytes(self._buffer[:newline])
                    del self._buffer[: newline + 1]
                    frame = parse_frame(raw)
                    self._recv_seq.observe(frame)
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.kill_and_drain()
                    raise AdapterExecutionError(
                        f"adapter overrun: no response to {request_kind} within "
                        f"{timeout_s}s; process killed"
                    )
                events = selector.select(timeout=remaining)
                if not events:
                    self.kill_and_drain()
                    raise AdapterExecutionError(
                        f"adapter overrun: stdout timed out after {timeout_s}s; process killed"
                    )
                chunk = os.read(stdout_fd, 65536)
                if not chunk:
                    code = self._process.poll()
                    # Kill first so the stderr drain can never block on a
                    # child that closed stdout but is still running.
                    self.kill_and_drain()
                    raise AdapterExecutionError(
                        "adapter exited or closed stdout before responding"
                        f" (returncode={code}, diagnostics={self._diagnostics or 'none'})"
                    )
                self._buffer.extend(chunk)
        finally:
            selector.close()

    def _raise_error_frame(self, frame: Frame) -> None:
        payload = frame.payload
        code = payload.get("code", "vouch/adapter-execution")
        message = payload.get("message", "adapter reported an error")
        details = payload.get("details")
        suffix = f" details={details}" if details else ""
        raise AdapterExecutionError(f"adapter error ({code}): {message}{suffix}")

    def _drain_stderr(self) -> None:
        """Read the child's stderr, keeping only a bounded TAIL (A8).

        Diagnostics are bounded independently of the frame cap: at most
        ``MAX_DIAGNOSTICS_BYTES`` raw bytes are ever retained, however much
        the child chatters. Older bytes are dropped (and flagged), because
        ``sanitize_diagnostics`` keeps the tail anyway.
        """
        stderr = self._process.stderr
        if stderr is None:
            return
        tail = bytearray()
        dropped = 0
        try:
            while True:
                chunk = stderr.read(65536)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) > MAX_DIAGNOSTICS_BYTES:
                    drop = len(tail) - MAX_DIAGNOSTICS_BYTES
                    del tail[:drop]
                    dropped += drop
        except (OSError, ValueError):  # pragma: no cover - pipe torn down mid-drain
            pass
        text = tail.decode("utf-8", errors="replace")
        if dropped:
            text = f"… [{dropped} older stderr bytes dropped by the diagnostics cap]\n" + text
        self._diagnostics = sanitize_diagnostics(text)

    def kill_and_drain(self) -> None:
        """Kill the subprocess and capture sanitized stderr diagnostics."""
        if self._process.poll() is None:
            self._process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):  # kill is fatal; 5s is generous
            self._process.wait(timeout=5)
        self._drain_stderr()
        self._closed = True

    def close(self) -> None:
        """Terminate politely; fall back to kill. Idempotent."""
        if self._closed or self._process.poll() is not None:
            self._drain_stderr()
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            self._process.kill()
            self._process.wait(timeout=5)
        self._drain_stderr()
        self._closed = True

    def diagnostics(self) -> str:
        """Sanitized stderr tail (secret-shaped content redacted)."""
        if not self._diagnostics and self._process.poll() is not None:
            self._drain_stderr()
        return self._diagnostics


def frame_error_payload(code: str, message: str, **details: Any) -> dict[str, Any]:
    """Build an error-frame payload (used by adapters and tests)."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if details:
        payload["details"] = details
    return payload


__all__ = [
    "DIGEST_PREFIX",
    "MAX_ARTIFACT_BYTES",
    "MAX_DIAGNOSTICS_BYTES",
    "MAX_DIAGNOSTICS_CHARS",
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "REQUEST_TO_RESPONSE",
    "Frame",
    "FrameKind",
    "ProtocolTransport",
    "SequenceTracker",
    "descriptor_from_payload",
    "digests_from_payload",
    "execution_from_payload",
    "frame_error_payload",
    "ok_from_payload",
    "parse_frame",
    "sanitize_diagnostics",
]
