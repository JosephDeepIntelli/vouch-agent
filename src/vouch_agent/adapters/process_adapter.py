"""AdapterClient over any subprocess that speaks adapter protocol v1.

The controller configures a command (Choose's own Node runner, Visibility's
Python runner, the in-repo fixture adapter, a customer agent's runner) plus
per-run timeouts. The client is a thin, trusted driver: it owns the process
lifecycle, validates every inbound frame (version, sequence, size, metering)
and kills the subprocess on wall-clock overrun — an overrun is a terminal
:class:`AdapterExecutionError` for this client, never a silent retry.

After a kill the client refuses further work (fail closed); the controller
creates a fresh client for the next attempt. Vouch never imports the
adapter's source or shares credentials with it — the process boundary and
the framed protocol are the whole contract.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import TracebackType
from typing import Any

from vouch_agent.adapters.base import AdapterDescriptor, AdapterExecution
from vouch_agent.adapters.protocol import (
    MAX_ARTIFACT_BYTES,
    FrameKind,
    ProtocolTransport,
    artifacts_from_payload,
    descriptor_from_payload,
    digests_from_payload,
    execution_from_payload,
    ok_from_payload,
)
from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import AdapterExecutionError, ProtocolFrameError

DEFAULT_REQUEST_TIMEOUT_S = 30.0
DEFAULT_EXECUTE_TIMEOUT_S = 120.0


class ProcessAdapterClient:
    """Drive a protocol-v1 subprocess through describe/prepare/execute/collect/cleanup.

    Implements the ``AdapterClient`` port from ``vouch_agent.adapters.base``.
    """

    def __init__(
        self,
        command: list[str],
        *,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        execute_timeout_s: float = DEFAULT_EXECUTE_TIMEOUT_S,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        label: str = "",
    ) -> None:
        if not command or not all(isinstance(part, str) for part in command):
            raise ValueError("command must be a non-empty list of strings")
        self._command = command
        self._request_timeout_s = request_timeout_s
        self._execute_timeout_s = execute_timeout_s
        self._env = env
        self._cwd = cwd
        self._label = label or command[0]
        self._transport: ProtocolTransport | None = None
        self._descriptor: AdapterDescriptor | None = None
        self._describe_payload: dict[str, Any] | None = None
        # Run ids this client actually PREPARED (A6): collect/cleanup are bound
        # to the exact prepared run — a result or artifact set is never
        # attributed to a run this client never set up.
        self._prepared_runs: set[str] = set()

    # -- lifecycle helpers ----------------------------------------------------

    @property
    def dead(self) -> bool:
        """True once the subprocess was killed/exited and not restarted."""
        return self._transport is not None and self._transport.dead

    def _ensure_process(self) -> ProtocolTransport:
        if self._transport is not None:
            if self._transport.dead:
                raise AdapterExecutionError(
                    f"adapter {self._label!r} process was killed or exited; "
                    "recreate the client for a new attempt (fail closed, no silent respawn)"
                )
            return self._transport
        try:
            process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                cwd=self._cwd,
                env=self._subprocess_env(),
            )
        except OSError as exc:
            raise AdapterExecutionError(f"failed to start adapter {self._label!r}: {exc}") from exc
        self._transport = ProtocolTransport(process)
        return self._transport

    #: Narrow default environment allowlist (v1.1 / review B2): the child gets
    #: what a runtime needs to resolve its interpreter and nothing that could
    #: smuggle credentials or provider keys into a fixture subprocess. NODE_*
    #: is deliberately excluded (NODE_OPTIONS can inject code); PYTHON*
    #: likewise beyond the explicit unbuffered flag.
    DEFAULT_ENV_ALLOWLIST = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "USERPROFILE",
        "APPDATA",
    )

    def _subprocess_env(self) -> dict[str, str] | None:
        """Explicit env wins (callers building a scoped env own their choice);
        the default is a NARROW allowlist, not an inherited environment.

        One documented addition: ``VOUCH_PILOT_CREDENTIAL_*`` values pass
        through to the child. The transport-simulation credential seam is the
        owning trusted service's environment — values never travel in frames,
        configs, task packs or git, and only this explicit prefix moves."""
        if self._env is not None:
            return dict(self._env)
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in self.DEFAULT_ENV_ALLOWLIST
        }
        for key, value in os.environ.items():
            if key.startswith("VOUCH_PILOT_CREDENTIAL_"):
                env[key] = value
        # The adapter speaks only the framed protocol; keep child scripts we
        # own quiet and deterministic.
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def __enter__(self) -> ProcessAdapterClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def diagnostics(self) -> str:
        """Sanitized stderr tail from the adapter subprocess, if any."""
        return self._transport.diagnostics() if self._transport is not None else ""

    # -- AdapterClient port ----------------------------------------------------

    def describe(self) -> AdapterDescriptor:
        if self._descriptor is not None:
            return self._descriptor
        transport = self._ensure_process()
        frame = transport.request(
            FrameKind.DESCRIBE_REQUEST,
            run_id=None,
            payload={},
            timeout_s=self._request_timeout_s,
        )
        descriptor = descriptor_from_payload(frame.payload)
        if descriptor.protocol_version != "1":
            raise AdapterExecutionError(
                f"adapter {descriptor.adapter_id!r} speaks protocol "
                f"{descriptor.protocol_version!r}, not v1"
            )
        self._descriptor = descriptor
        self._describe_payload = dict(frame.payload)
        return descriptor

    @property
    def describe_payload(self) -> dict[str, Any]:
        """The raw describe payload (baseline config, evaluation cases, ...).

        Callers read runner-declared interop facts the typed descriptor does
        not carry; :meth:`describe` must have run first.
        """
        if self._describe_payload is None:
            raise AdapterExecutionError(
                "describe has not run on this adapter client; the raw payload "
                "is only available after the describe handshake"
            )
        return dict(self._describe_payload)

    def prepare(self, run_id: str, mode: RunMode) -> None:
        transport = self._ensure_process()
        frame = transport.request(
            FrameKind.PREPARE_REQUEST,
            run_id=run_id,
            payload={"mode": mode.value},
            timeout_s=self._request_timeout_s,
        )
        ok_from_payload(frame.payload, FrameKind.PREPARE_RESPONSE)
        self._prepared_runs.add(run_id)

    def execute(
        self,
        *,
        run_id: str,
        attempt_id: str,
        workflow_id: str,
        case_input: dict[str, Any],
        mode: RunMode,
        timeout_s: float | None = None,
    ) -> AdapterExecution:
        from vouch_agent.adapters.protocol import build_identity, verify_identity_echo

        transport = self._ensure_process()
        version_digest = case_input.get("versionDigest")
        if version_digest is not None and not isinstance(version_digest, str):
            raise ProtocolFrameError(
                "execute case_input 'versionDigest' must be a digest string when present "
                "(protocol v1.1 §1, A6)"
            )
        identity = build_identity(
            run_id=run_id,
            attempt_id=attempt_id,
            workflow_id=workflow_id,
            case_id=str(case_input.get("caseId", "")),
            mode=mode.value,
            version_digest=version_digest if version_digest else None,
        )
        frame = transport.request(
            FrameKind.EXECUTE_REQUEST,
            run_id=run_id,
            payload={
                "attemptId": attempt_id,
                "workflowId": workflow_id,
                "caseInput": case_input,
                "mode": mode.value,
                "identity": identity,
            },
            timeout_s=timeout_s if timeout_s is not None else self._execute_timeout_s,
        )
        execution = execution_from_payload(frame.payload)
        verify_identity_echo(identity, frame.payload)
        if execution.mode is not mode:
            # identity.mode and the request mode already agree (echo check);
            # the payload's own mode field must agree with BOTH. A child that
            # answers "authorized-live" to a fixture request is misreporting
            # what it did — refuse before the result can be stored or billed.
            transport.kill_and_drain()
            raise ProtocolFrameError(
                "execute response mode disagreement: requested "
                f"{mode.value!r}, identity echoed {identity['mode']!r}, payload reports "
                f"{execution.mode.value!r}; contradictory mode never reaches storage "
                "or acceptance (A6)"
            )
        return execution

    def apply_config(
        self,
        *,
        run_id: str,
        attempt_id: str,
        workflow_id: str,
        case_input: dict[str, Any],
        mode: RunMode,
        change_bundle: dict[str, Any],
        timeout_s: float | None = None,
        provider_transport: dict[str, Any] | None = None,
    ) -> tuple[AdapterExecution, dict[str, Any]]:
        """v1.2: apply a digest-bound change bundle and execute under it.

        Sends ``apply-config-request`` and returns the decoded execution plus
        the raw application-receipt payload. Identity echo, metering and mode
        agreement are enforced exactly as for execute; receipt VALIDATION
        (digests, lineage, attempt binding) belongs to the caller that knows
        what was supposed to be applied.
        """
        from vouch_agent.adapters.protocol import build_identity, verify_identity_echo

        transport = self._ensure_process()
        version_digest = case_input.get("versionDigest")
        if version_digest is not None and not isinstance(version_digest, str):
            raise ProtocolFrameError(
                "apply-config case_input 'versionDigest' must be a digest string "
                "when present (protocol v1.1 §1, A6)"
            )
        if not isinstance(change_bundle, dict):
            raise ProtocolFrameError("apply-config changeBundle must be a JSON object")
        if provider_transport is not None and not isinstance(provider_transport, dict):
            raise ProtocolFrameError("apply-config providerTransport must be a JSON object")
        identity = build_identity(
            run_id=run_id,
            attempt_id=attempt_id,
            workflow_id=workflow_id,
            case_id=str(case_input.get("caseId", "")),
            mode=mode.value,
            version_digest=version_digest if version_digest else None,
        )
        frame = transport.request(
            FrameKind.APPLY_CONFIG_REQUEST,
            run_id=run_id,
            payload={
                "attemptId": attempt_id,
                "workflowId": workflow_id,
                "caseInput": case_input,
                "mode": mode.value,
                "identity": identity,
                "changeBundle": change_bundle,
                **(
                    {"providerTransport": provider_transport}
                    if provider_transport is not None
                    else {}
                ),
            },
            timeout_s=timeout_s if timeout_s is not None else self._execute_timeout_s,
        )
        execution = execution_from_payload(frame.payload)
        verify_identity_echo(identity, frame.payload)
        if execution.mode is not mode:
            transport.kill_and_drain()
            raise ProtocolFrameError(
                "apply-config response mode disagreement: requested "
                f"{mode.value!r}, payload reports {execution.mode.value!r}; "
                "contradictory mode never reaches storage or acceptance (A6)"
            )
        receipt = frame.payload.get("receipt")
        if not isinstance(receipt, dict):
            raise ProtocolFrameError(
                "apply-config response must carry an application receipt object"
            )
        return execution, dict(receipt)

    def collect(self, run_id: str) -> tuple[str, ...]:
        self._require_prepared(run_id)
        transport = self._ensure_process()
        frame = transport.request(
            FrameKind.COLLECT_REQUEST,
            run_id=run_id,
            payload={},
            timeout_s=self._request_timeout_s,
        )
        return digests_from_payload(frame.payload)

    def collect_artifacts(
        self, run_id: str, *, max_bytes: int = MAX_ARTIFACT_BYTES
    ) -> tuple[tuple[str, bytes, str], ...]:
        """v1.1 §3: collect in-frame artifacts with digest + size verification.

        Returns (digest, bytes, kind) triples; a host path is never honored
        as a read capability. Callers store the verified bytes."""
        self._require_prepared(run_id)
        transport = self._ensure_process()
        frame = transport.request(
            FrameKind.COLLECT_REQUEST,
            run_id=run_id,
            payload={"wantArtifacts": True},
            timeout_s=self._request_timeout_s,
        )
        return artifacts_from_payload(frame.payload, max_bytes=max_bytes)

    def cleanup(self, run_id: str) -> None:
        if self._transport is None:
            return  # nothing was ever prepared for this client
        self._require_prepared(run_id)
        transport = self._ensure_process()
        frame = transport.request(
            FrameKind.CLEANUP_REQUEST,
            run_id=run_id,
            payload={},
            timeout_s=self._request_timeout_s,
        )
        ok_from_payload(frame.payload, FrameKind.CLEANUP_RESPONSE)
        self._prepared_runs.discard(run_id)

    def _require_prepared(self, run_id: str) -> None:
        """Collect/cleanup are bound to the exact run this client prepared (A6)."""
        if run_id not in self._prepared_runs:
            raise AdapterExecutionError(
                f"run {run_id!r} was never prepared on this adapter client; collected "
                "evidence is bound to the exact prepared run (A6) — prepare first"
            )


def fixture_adapter_command(fixtures_dir: str, workspace_dir: str | None = None) -> list[str]:
    """Command line for the in-repo Choose fixture adapter (protocol v1 over stdio).

    Runs with the current interpreter so the fixture adapter shares no state
    with the controller beyond the two pipes.
    """
    command = [
        sys.executable,
        "-m",
        "vouch_agent.adapters.fixture_adapter",
        "--fixtures",
        fixtures_dir,
    ]
    if workspace_dir is not None:
        command.extend(["--workspace", workspace_dir])
    return command


__all__ = [
    "DEFAULT_EXECUTE_TIMEOUT_S",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "ProcessAdapterClient",
    "fixture_adapter_command",
]
