"""Direct task execution service (Stage C → M3 Gate A2) — the shared layer
behind ``vouch run`` and the TUI task screen.

Runs a business task through the Supervisor on the pinned JAZ runtime with
deterministic scripted providers (fixture mode; the sample scripts live in
``docs/quickstart.md``). The service — not the UI — owns the lifecycle:
closing the TUI or the terminal never cancels or duplicates work; every
state transition is durable in the workspace stores.

Durable execution configuration (Gate A2): the FIRST ``execute()`` of a run
persists exactly what will execute — the provider name or script bytes and
their digest, the provider mode, the isolation mode and the runtime backend
identity — BEFORE anything dispatches. ``resume()`` (and any later
``execute()`` of the same run) loads and VERIFIES that configuration: a
provider whose bytes changed, a missing provider, a tampered script record
or a different runtime backend is refused with a specific mismatch error
before dispatch. A run is never silently switched to a replacement script,
and the historical ``('placeholder',)`` provider is gone.

Response cursor: fixture providers draw from an ordered scripted pool; the
run's monotonic position in that pool (the number of underlying queries
consumed by completed model steps) is persisted on every model step, so a
paused run resumed by a FRESH service (or process) continues with the
original provider without replaying consumed responses and without
duplicated effects or cost.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vouch_agent
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.common import RunMode, digest_of, utc_now_iso
from vouch_agent.contracts.tasks import (
    ResultPackage,
    StepKind,
    TaskRun,
    TaskSpec,
    TaskStatus,
    new_task_spec_id,
)
from vouch_agent.errors import ContractError, DigestMismatchError, VouchError
from vouch_agent.orchestrator import Supervisor, SupervisorPolicy, recover
from vouch_agent.orchestrator.supervisor import KIND_RUN_QUERY_CURSOR, KIND_TASK_RUN
from vouch_agent.runtime.ports import Runtime, WorkerSessionConfig

#: Bounded wait for the test-only native-operation gate file (see
#: ``_native_compute``); never set outside deterministic tests.
_OPERATION_GATE_TIMEOUT_S = 60.0

#: Durable execution configuration record kind (keyed by run id).
KIND_EXECUTION_CONFIG = "execution-config"

#: Fixture-mode provider scripts shipped for the documented sample flow.
#: They read task materials from the session scope (never the prompt), so
#: outputs track the supplied data. Users pass their own scripts via
#: ``--script-file``; nothing here pretends to be a live model.
SAMPLE_SCRIPTS: dict[str, tuple[str, ...]] = {
    "extract-fact": (
        'value = materials["fact"]["value"]\n'
        'return {"finding": value, "source": materials["fact"]["source"]}',
    ),
    "summarize": (
        "items = list(materials.get('items', {}).values())\n"
        'return {"count": len(items), "names": sorted(str(i) for i in items)}',
    ),
}


@dataclass(frozen=True)
class ExecutionOutcome:
    run_id: str
    status: TaskStatus
    result: ResultPackage | None
    error: str | None


class _ScriptedPoolRuntime:
    """Trusted-side wrapper injecting the scripted provider pool (fixture).

    The pool is sealed at execute time (from the durable configuration on
    resume) and the session opens at the run's CURRENT monotonic cursor —
    the number of provider responses completed model steps already consumed
    — so a resumed session never replays consumed responses (Gate A2).
    """

    def __init__(
        self,
        inner: Runtime,
        responses: tuple[str, ...],
        cursor_provider: Callable[[], int] | None = None,
    ) -> None:
        self._inner = inner
        self._responses = responses
        self._cursor_provider = cursor_provider

    def open_session(self, config: WorkerSessionConfig) -> Any:
        if config.mode is not RunMode.FIXTURE:
            raise ContractError(
                "direct execution ships with fixture-mode providers only; "
                "authorized-live wiring is a separate, reviewed milestone"
            )
        from dataclasses import replace

        cursor = int(self._cursor_provider()) if self._cursor_provider is not None else 0
        return self._inner.open_session(
            replace(
                config,
                scripted_responses=self._responses,
                scripted_cursor=cursor,
            )
        )

    def backend_id(self) -> str:
        return self._inner.backend_id()


class ExecutionService:
    def __init__(self, workspace: ProjectWorkspace) -> None:
        self.workspace = workspace
        #: Supervisors currently executing a run through THIS service
        #: instance, so an external stop (a worker's cancel poller) can reach
        #: the in-flight session. Keyed by run id.
        self._active_supervisors: dict[str, Supervisor] = {}

    def stop_active_run(self, run_id: str, reason: str = "") -> bool:
        """Stop the run's in-flight owned execution session (if any).

        Called from outside the executing thread (the detached worker's
        cancel poller): the durable cancel request stands, and this
        accelerates it past the blocking model call by stopping the run's
        guarded worker process group within the bounded group-stop grace.
        The supervisor then finalizes CANCELLED with reservations reconciled.
        """
        supervisor = self._active_supervisors.get(run_id)
        if supervisor is None:
            return False
        return supervisor.stop_active_session(run_id, reason)

    def _run_with_registration(
        self, run_id: str, supervisor: Supervisor, drive: Callable[[], Any]
    ) -> Any:
        try:
            self._active_supervisors[run_id] = supervisor
            return drive()
        finally:
            self._active_supervisors.pop(run_id, None)

    # --- providers ----------------------------------------------------------

    @staticmethod
    def provider_names() -> tuple[str, ...]:
        return tuple(sorted(SAMPLE_SCRIPTS))

    def _runtime(
        self,
        scripts: tuple[str, ...],
        *,
        isolated: bool,
        cursor_provider: Callable[[], int] | None = None,
    ) -> Runtime:
        if isolated:
            from vouch_agent.runtime.isolated_runtime import IsolatedStepRuntime

            inner: Runtime = IsolatedStepRuntime()
        else:
            from vouch_agent.runtime.jaz_engine import JazRuntime

            inner = JazRuntime()
        return _ScriptedPoolRuntime(inner, scripts, cursor_provider)

    # --- lifecycle -------------------------------------------------------------

    def submit(
        self,
        *,
        goal: str,
        inputs: dict[str, Any],
        title: str = "",
        budget_usd: float | None = None,
        max_steps: int | None = None,
        completion_conditions: list[dict[str, Any]] | None = None,
    ) -> str:
        spec = TaskSpec(
            spec_id=new_task_spec_id(),
            title=title or goal[:60],
            goal=goal,
            mode=RunMode.FIXTURE,
            inputs=inputs,
            max_cost_usd=budget_usd,
            max_steps=max_steps,
            success_criteria={"conditions": completion_conditions or []},
        )
        supervisor = self._supervisor(("placeholder",))
        return supervisor.submit(spec)

    def execute(
        self,
        run_id: str,
        *,
        provider: str = "extract-fact",
        script_files: tuple[Path, ...] = (),
        isolated: bool = True,
    ) -> ExecutionOutcome:
        """Execute a submitted run to a terminal state (or a recoverable stop).

        ``isolated=True`` (default) runs the session in the guarded worker
        process — the boundary required before generated-code execution is a
        general capability. Pass ``isolated=False`` only for the shipped
        trusted sample scripts.

        The provider/script/isolation/backend configuration is sealed
        durably on the FIRST execute and verified on every later one: a run
        is never silently switched to a different provider mid-life.
        """
        scripts = self._scripts(provider, script_files)
        try:
            self._verify_or_seal_config(run_id, scripts, provider, script_files, isolated)
        except VouchError as exc:
            # config mismatch is a refused dispatch, not a crash: the run
            # keeps its sealed configuration for a legitimate resume
            return self._error_outcome(run_id, exc)
        supervisor = self._supervisor(
            scripts,
            isolated=isolated,
            config_identity=self._config_identity(run_id),
            cursor_provider=lambda: self._durable_cursor(run_id),
        )
        try:
            run = self._run_with_registration(
                run_id, supervisor, lambda: supervisor.execute(run_id)
            )
        except Exception as exc:  # surface honestly, state stays durable
            return self._error_outcome(run_id, exc)
        return ExecutionOutcome(run_id, run.status, supervisor.get_result(run_id), run.error)

    def run(self, **submit_kwargs: Any) -> ExecutionOutcome:
        """Convenience: submit + execute in one call."""
        provider = submit_kwargs.pop("provider", "extract-fact")
        script_files = submit_kwargs.pop("script_files", ())
        isolated = submit_kwargs.pop("isolated", True)
        run_id = self.submit(**submit_kwargs)
        return self.execute(run_id, provider=provider, script_files=script_files, isolated=isolated)

    def status(self, run_id: str) -> TaskRun | None:
        return self._supervisor(("placeholder",)).get_run(run_id)

    def result(self, run_id: str) -> ResultPackage | None:
        return self._supervisor(("placeholder",)).get_result(run_id)

    def cancel(self, run_id: str, reason: str = "") -> TaskRun | None:
        supervisor = self._supervisor(("placeholder",))
        try:
            return supervisor.cancel(run_id, reason)
        except Exception as exc:
            raise ContractError(f"cancel failed: {exc}") from exc

    def resume(self, run_id: str, reconciliation_note: str = "") -> ExecutionOutcome:
        """Continue a paused/stopped run with its ORIGINAL provider.

        Loads the durable execution configuration recorded at first execute
        and verifies it still holds: provider bytes unchanged, script record
        intact, same runtime backend and isolation mode. Any mismatch — or a
        run that never executed — refuses with a specific error BEFORE any
        dispatch (the historical placeholder-provider behavior is gone).
        """
        try:
            config = self._load_verified_config(run_id)
        except VouchError as exc:
            return self._error_outcome(run_id, exc)
        supervisor = self._supervisor(
            tuple(config["scripts"]),
            isolated=bool(config["isolated"]),
            config_identity=self._config_identity(run_id),
            cursor_provider=lambda: self._durable_cursor(run_id),
        )
        try:
            run = self._run_with_registration(
                run_id, supervisor, lambda: supervisor.resume(run_id, reconciliation_note)
            )
        except Exception as exc:
            return self._error_outcome(run_id, exc)
        return ExecutionOutcome(run_id, run.status, supervisor.get_result(run_id), run.error)

    def recovery_report(self, run_id: str) -> Any:
        return recover(self.workspace.store, run_id)

    def runs(self) -> list[TaskRun]:
        runs = []
        for record_id in self.workspace.store.list_ids(KIND_TASK_RUN):
            data = self.workspace.store.load(KIND_TASK_RUN, record_id)
            if data is not None:
                runs.append(TaskRun.from_dict(data))
        return sorted(runs, key=lambda r: r.created_at)

    # --- durable execution configuration (Gate A2) ------------------------------

    def _config_record(
        self,
        run_id: str,
        scripts: tuple[str, ...],
        provider: str,
        script_files: tuple[Path, ...],
        isolated: bool,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": "1",
            "runId": run_id,
            "providerName": None if script_files else provider,
            "scripts": list(scripts),
            "scriptsDigest": digest_of(list(scripts)),
            "mode": RunMode.FIXTURE.value,
            "isolated": bool(isolated),
            "runtimeId": self._runtime(scripts, isolated=isolated).backend_id(),
            "toolVersion": vouch_agent.__version__,
        }

    def _verify_or_seal_config(
        self,
        run_id: str,
        scripts: tuple[str, ...],
        provider: str,
        script_files: tuple[Path, ...],
        isolated: bool,
    ) -> None:
        """Seal the execution configuration on first execute; verify after."""
        store = self.workspace.store
        existing = store.load(KIND_EXECUTION_CONFIG, run_id)
        if existing is None:
            record = self._config_record(run_id, scripts, provider, script_files, isolated)
            store.save(KIND_EXECUTION_CONFIG, run_id, record)
            # Seed the durable query-cursor record for the sealed config: a
            # run executed at least once ALWAYS carries an absolute cursor
            # record, so resume never has to guess a position (M4 A1 §2).
            store.save(
                KIND_RUN_QUERY_CURSOR,
                run_id,
                {
                    "runId": run_id,
                    "cursor": 0,
                    "configIdentity": record["scriptsDigest"],
                    "modelOrdinal": 0,
                    "updatedAt": utc_now_iso(),
                },
            )
            return
        record = self._config_record(run_id, scripts, provider, script_files, isolated)
        if existing.get("scriptsDigest") != record["scriptsDigest"]:
            raise DigestMismatchError(
                f"run {run_id} was started with a different provider script pool "
                f"(persisted digest {existing.get('scriptsDigest')}, requested "
                f"{record['scriptsDigest']}); a run keeps its original provider — "
                "resume it as-is or cancel and submit a new run"
            )
        if bool(existing.get("isolated")) != isolated:
            raise ContractError(
                f"run {run_id} was started with isolated={existing.get('isolated')}; "
                "isolation mode is part of the sealed execution configuration"
            )
        if existing.get("runtimeId") != record["runtimeId"]:
            raise ContractError(
                f"run {run_id} was started on runtime {existing.get('runtimeId')!r} "
                f"but the current runtime is {record['runtimeId']!r}; refusing to "
                "resume on a different backend (bind deliberate changes to a new run)"
            )

    def _load_verified_config(self, run_id: str) -> dict[str, Any]:
        """Load the sealed configuration and re-verify every binding."""
        from vouch_agent.contracts.common import utc_now_iso

        config = self.workspace.store.load(KIND_EXECUTION_CONFIG, run_id)
        if config is None:
            raise ContractError(
                f"run {run_id} has no persisted execution configuration; it has "
                "never been executed. Execute it with a provider first — resume "
                "never invents placeholder work"
            )
        scripts = tuple(str(s) for s in (config.get("scripts") or ()))
        if not scripts:
            raise ContractError(f"run {run_id} execution config carries no scripts")
        if digest_of(list(scripts)) != config.get("scriptsDigest"):
            raise DigestMismatchError(
                f"run {run_id} execution config is internally inconsistent: the "
                "stored script bytes do not match their recorded digest; refusing "
                "to dispatch (tampered or corrupted record)"
            )
        provider_name = config.get("providerName")
        if provider_name is not None:
            current = SAMPLE_SCRIPTS.get(str(provider_name))
            if current is None:
                raise ContractError(
                    f"run {run_id} used fixture provider {provider_name!r}, which "
                    "no longer exists in this build; refusing to resume — cancel "
                    "the run and submit a new one"
                )
            if digest_of(list(current)) != config.get("scriptsDigest"):
                raise DigestMismatchError(
                    f"fixture provider {provider_name!r} changed since run {run_id} "
                    "started (bytes digest mismatch); refusing to resume a run on "
                    "modified script bytes — cancel it and submit a new run"
                )
        runtime_id = self._runtime(scripts, isolated=bool(config.get("isolated"))).backend_id()
        if runtime_id != config.get("runtimeId"):
            raise ContractError(
                f"run {run_id} was sealed on runtime {config.get('runtimeId')!r} "
                f"but this build provides {runtime_id!r}; refusing to resume on a "
                "different backend (bind deliberate changes to a new run)"
            )
        config["scripts"] = list(scripts)
        config["verifiedAt"] = utc_now_iso()
        return config

    def _config_identity(self, run_id: str) -> str | None:
        """The sealed execution-config digest for the run, if any."""
        config = self.workspace.store.load(KIND_EXECUTION_CONFIG, run_id)
        if config is None:
            return None
        identity = config.get("scriptsDigest")
        return str(identity) if identity is not None else None

    def _durable_cursor(self, run_id: str) -> int:
        """The run's MONOTONIC scripted-response cursor from durable state.

        Reads the ABSOLUTE cursor the supervisor persists after every
        completed model step (never a sum of per-step counters — M4 A1
        review §2: cumulative session counters are not per-step usage, and
        summing them skipped responses on resume). The record is bound to
        the sealed execution configuration: a cursor recorded under a
        different config is meaningless and refuses. A run whose completed
        model steps predate the cursor record cannot be positioned
        verifiably — that fails closed rather than guessing a position that
        could replay a consumed response or skip an unconsumed one.
        """
        record = self.workspace.store.load(KIND_RUN_QUERY_CURSOR, run_id)
        if record is not None:
            identity = self._config_identity(run_id)
            recorded = record.get("configIdentity")
            if identity is not None and recorded is not None and recorded != identity:
                raise ContractError(
                    f"run {run_id} query cursor was recorded under execution config "
                    f"{recorded!r} but the sealed config is {identity!r}; the cursor "
                    "is meaningless across pools — cancel the run and submit a new one"
                )
            cursor = record.get("cursor")
            if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
                raise ContractError(
                    f"run {run_id} query-cursor record carries no reliable position; "
                    "cancel the run and submit a new one"
                )
            return int(cursor)
        run = self.status(run_id)
        if run is None:
            return 0
        completed = [s for s in run.steps if s.kind is StepKind.MODEL_CALL and s.status == "ok"]
        if not completed:
            return 0
        # Every step the current code writes carries its absolute queryCursor;
        # a run with completed model steps but no cursor record and no
        # per-step absolute counts predates the record and cannot be
        # positioned safely.
        absolutes = [
            value
            for s in completed
            for value in (s.usage.get("queryCursor"),)
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        if len(absolutes) != len(completed):
            raise ContractError(
                f"run {run_id} completed model steps before any durable query-cursor "
                "record existed; its consumed provider responses cannot be verified — "
                "cancel it and submit a new run"
            )
        absolute_cursor: int = max(absolutes)
        self.workspace.store.save(
            KIND_RUN_QUERY_CURSOR,
            run_id,
            {
                "runId": run_id,
                "cursor": absolute_cursor,
                "configIdentity": self._config_identity(run_id),
                "modelOrdinal": len(completed),
                "updatedAt": utc_now_iso(),
            },
        )
        return absolute_cursor

    # --- internals ------------------------------------------------------------

    def _error_outcome(self, run_id: str, exc: Exception) -> ExecutionOutcome:
        """Report a failure WITHOUT fabricating a run status.

        The persisted run status is the truth; an error that happened before
        or around dispatch (config mismatch, refused transition) must not be
        recorded as the run having failed.
        """
        current = self.status(run_id)
        status = current.status if current is not None else TaskStatus.FAILED
        return ExecutionOutcome(run_id, status, None, f"{type(exc).__name__}: {exc}")

    def _scripts(self, provider: str, script_files: tuple[Path, ...]) -> tuple[str, ...]:
        if script_files:
            scripts = tuple(Path(f).read_text(encoding="utf-8") for f in script_files)
        else:
            if provider not in SAMPLE_SCRIPTS:
                raise ContractError(
                    f"unknown fixture provider {provider!r}; available: "
                    f"{sorted(SAMPLE_SCRIPTS)} (or pass script files)"
                )
            scripts = SAMPLE_SCRIPTS[provider]
        return scripts

    def _supervisor(
        self,
        scripts: tuple[str, ...],
        *,
        isolated: bool = True,
        config_identity: str | None = None,
        cursor_provider: Callable[[], int] | None = None,
    ) -> Supervisor:
        return Supervisor(
            self._runtime(scripts, isolated=isolated, cursor_provider=cursor_provider),
            self.workspace.store,
            self.workspace.artifacts,
            self.workspace.ledger,
            self.workspace.journal,
            SupervisorPolicy(),
            config_identity=config_identity,
        )

    # --- native deterministic operation: CSV reconciliation (M3-B4) -------------

    # --- native deterministic operation: CSV reconciliation (M3-B4, M4 A1) ------

    #: Operation id of the shipped deterministic CSV reconciliation.
    CSV_OPERATION_ID = "vouch-csv-reconcile/1"

    def submit_csv_reconciliation(
        self,
        *,
        goal: str,
        left_csv: bytes,
        right_csv: bytes,
        join_key: str,
        left_name: str = "left.csv",
        right_name: str = "right.csv",
        delimiter: str = ",",
        ignore_columns: tuple[str, ...] = (),
        title: str = "",
    ) -> str:
        """DURABLY SUBMIT the CSV reconciliation as a native operation run.

        The materials are snapshotted immediately under their OWN content
        digests (immutable), the operation configuration (id + parameters +
        material digests) is sealed in the execution-config record, and the
        run is left QUEUED: the deterministic computation has NOT run yet.
        Executing it is a separate claim — ``execute_native_operation`` in
        this process, or the shared detached worker via its sealed-operation
        dispatcher — so an in-flight run genuinely survives client exit
        instead of reading an already-completed result (M4 A1 test-gap
        repair).
        """
        from vouch_agent.appservices.materials import (
            material_input_records,
            snapshot_csv_material,
            store_material_snapshots,
        )

        # Per-material snapshots through the shared helpers (M4 A3 review §1):
        # the durable attachment carries a SAFE GENERATED internal id (the
        # contract restricts TaskAttachment.name to [A-Za-z0-9._-]), while the
        # USER-VISIBLE filename — spaces, Chinese characters and all — travels
        # beside it as displayName. Operator filenames are therefore never
        # mangled into validity, and each material's bytes land in the store
        # under their OWN digest (the worker later reads them back
        # digest-verified; a concatenated blob cannot satisfy references to
        # two separate materials).
        snapshots = (
            snapshot_csv_material(
                left_csv, display_name=left_name, ordinal=0, delimiter=delimiter
            ),
            snapshot_csv_material(
                right_csv, display_name=right_name, ordinal=1, delimiter=delimiter
            ),
        )
        left_digest, right_digest = store_material_snapshots(
            self.workspace.artifacts, snapshots
        )

        supervisor = self._supervisor(())
        spec = TaskSpec(
            spec_id=new_task_spec_id(),
            title=title or f"Reconcile {left_name} vs {right_name} on {join_key}",
            goal=goal,
            mode=RunMode.FIXTURE,
            inputs={
                "materials": material_input_records(snapshots),
                "operation": self.CSV_OPERATION_ID,
                "joinKey": join_key,
                "delimiter": delimiter,
                "ignoreColumns": list(ignore_columns),
            },
            max_cost_usd=None,  # deterministic operation: no model spend
            max_steps=1,
            success_criteria={
                "conditions": [
                    {
                        "type": "artifact_schema",
                        "schema": {
                            "type": "object",
                            "required": ["joinKey", "rowCounts", "clean"],
                            "properties": {
                                "joinKey": {"type": "string"},
                                "rowCounts": {"type": "object"},
                                "clean": {"type": "boolean"},
                            },
                        },
                    }
                ]
            },
        )
        run_id = supervisor.submit(spec)
        self._seal_operation_config(
            run_id,
            operation=self.CSV_OPERATION_ID,
            parameters={
                "joinKey": join_key,
                "delimiter": delimiter,
                "ignoreColumns": list(ignore_columns),
                "leftName": left_name,
                "rightName": right_name,
                "leftDigest": left_digest,
                "rightDigest": right_digest,
            },
        )
        return run_id

    def _seal_operation_config(
        self, run_id: str, *, operation: str, parameters: dict[str, Any]
    ) -> None:
        """Seal the native-operation execution configuration (first write wins)."""
        store = self.workspace.store
        if store.load(KIND_EXECUTION_CONFIG, run_id) is not None:
            raise ContractError(
                f"run {run_id} already carries a sealed execution configuration"
            )
        store.save(
            KIND_EXECUTION_CONFIG,
            run_id,
            {
                "schemaVersion": "1",
                "runId": run_id,
                "providerName": None,
                "operation": operation,
                "parameters": dict(parameters),
                "operationDigest": digest_of({"operation": operation, "parameters": parameters}),
                "scripts": [],
                "scriptsDigest": digest_of({"operation": operation, "parameters": parameters}),
                "mode": RunMode.FIXTURE.value,
                "isolated": False,  # trusted deterministic code, no model child
                "runtimeId": f"vouch-native-operation/{operation}",
                "toolVersion": vouch_agent.__version__,
            },
        )
        store.save(
            KIND_RUN_QUERY_CURSOR,
            run_id,
            {
                "runId": run_id,
                "cursor": 0,
                "configIdentity": digest_of({"operation": operation, "parameters": parameters}),
                "modelOrdinal": 0,
                "updatedAt": utc_now_iso(),
            },
        )

    def _load_verified_operation_config(self, run_id: str) -> dict[str, Any]:
        """Load and re-verify the sealed native-operation configuration."""
        config = self.workspace.store.load(KIND_EXECUTION_CONFIG, run_id)
        if config is None:
            raise ContractError(
                f"run {run_id} has no persisted execution configuration; it has "
                "never been submitted for execution"
            )
        operation = config.get("operation")
        if operation != self.CSV_OPERATION_ID:
            raise ContractError(
                f"run {run_id} was sealed for unknown native operation {operation!r}"
            )
        parameters = dict(config.get("parameters") or {})
        if digest_of({"operation": operation, "parameters": parameters}) != config.get(
            "operationDigest"
        ):
            raise DigestMismatchError(
                f"run {run_id} operation configuration is internally inconsistent "
                "(parameters do not match their recorded digest); refusing to dispatch"
            )
        return config

    def execute_native_operation(
        self, run_id: str, report_sink: dict[str, Any] | None = None
    ) -> ExecutionOutcome:
        """Claim the run and execute its sealed deterministic operation HERE.

        This is the executor side of ``submit_csv_reconciliation``: the
        computation runs after the durable submit, under the supervisor's
        ownership lease, with persisted steps/checkpoints and the standard
        pause/cancel semantics — whether called in-process or by the shared
        detached worker. ``report_sink`` (optional, same-process callers)
        receives the live report object and digest.
        """
        try:
            config = self._load_verified_operation_config(run_id)
        except VouchError as exc:
            return self._error_outcome(run_id, exc)
        compute = self._native_compute(config, report_sink)
        supervisor = self._supervisor(())
        try:
            run = self._run_with_registration(
                run_id, supervisor, lambda: supervisor.execute_native_operation(run_id, compute)
            )
        except Exception as exc:
            return self._error_outcome(run_id, exc)
        return ExecutionOutcome(run_id, run.status, supervisor.get_result(run_id), run.error)

    def _native_compute(
        self, config: dict[str, Any], report_sink: dict[str, Any] | None
    ) -> Callable[[], Any]:
        """Build the trusted deterministic compute closure for a sealed operation."""
        from vouch_agent.appservices.csv_reconcile import reconcile_csvs, report_bytes
        from vouch_agent.contracts.common import digest_bytes
        from vouch_agent.orchestrator.records import NativeOperationResult

        parameters = dict(config.get("parameters") or {})
        operation = str(config.get("operation"))
        workspace = self.workspace

        def _compute() -> Any:
            # Test-only deterministic barrier (M4 plan §B: "deterministic test
            # barriers"): when set, hold BEFORE computing until the gate file
            # appears, so an in-flight native run can be observed mid-work.
            # Never set outside tests.
            gate = os.environ.get("VOUCH_TEST_OPERATION_GATE_FILE")
            if gate:
                deadline = time.monotonic() + _OPERATION_GATE_TIMEOUT_S
                while not Path(gate).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
            left = workspace.artifacts.get(str(parameters["leftDigest"]))
            right = workspace.artifacts.get(str(parameters["rightDigest"]))
            report = reconcile_csvs(
                left,
                right,
                join_key=str(parameters["joinKey"]),
                left_name=str(parameters["leftName"]),
                right_name=str(parameters["rightName"]),
                delimiter=str(parameters["delimiter"]),
                ignore_columns=tuple(parameters.get("ignoreColumns") or ()),
            )
            payload = report_bytes(report)
            report_digest = digest_bytes(payload)
            if report_sink is not None:
                report_sink["report"] = report
                report_sink["digest"] = report_digest
            return NativeOperationResult(
                payload=payload,
                artifacts={
                    "material:left": str(parameters["leftDigest"]),
                    "material:right": str(parameters["rightDigest"]),
                },
                usage={
                    "operation": operation,
                    "joinKey": str(parameters["joinKey"]),
                    "leftRows": report.left_rows,
                    "rightRows": report.right_rows,
                    "matched": report.matched,
                    "changed": len(report.changed),
                    "missingLeft": len(report.missing_left),
                    "missingRight": len(report.missing_right),
                    "duplicateKeys": len(report.duplicate_keys),
                },
                not_done=() if report.clean else ("discrepancies found — review the report",),
            )

        return _compute

    def run_csv_reconciliation(
        self,
        *,
        goal: str,
        left_csv: bytes,
        right_csv: bytes,
        join_key: str,
        left_name: str = "left.csv",
        right_name: str = "right.csv",
        delimiter: str = ",",
        ignore_columns: tuple[str, ...] = (),
        title: str = "",
    ) -> tuple[str, Any, str]:
        """Run the shipped deterministic CSV reconciliation as a NATIVE task.

        Durable submit FIRST (materials snapshotted by digest, operation
        configuration sealed), then execution through the same claimed-run
        path a detached worker uses — the computation is never precomputed in
        the client. Returns (run_id, report, report_digest).
        """
        run_id = self.submit_csv_reconciliation(
            goal=goal,
            left_csv=left_csv,
            right_csv=right_csv,
            join_key=join_key,
            left_name=left_name,
            right_name=right_name,
            delimiter=delimiter,
            ignore_columns=ignore_columns,
            title=title,
        )
        sink: dict[str, Any] = {}
        outcome = self.execute_native_operation(run_id, report_sink=sink)
        if outcome.status is not TaskStatus.COMPLETED or "report" not in sink:
            raise ContractError(
                f"csv reconciliation run {run_id} did not complete: "
                f"{outcome.status.value} {outcome.error or ''}"
            )
        return run_id, sink["report"], sink["digest"]


__all__ = [
    "KIND_EXECUTION_CONFIG",
    "SAMPLE_SCRIPTS",
    "ExecutionOutcome",
    "ExecutionService",
]
