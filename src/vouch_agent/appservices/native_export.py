"""Native ResultPackage export (M4; ownership: evidence specialist).

Writes the FULL native run deliverable to a user-selected directory: run/task
identity, terminal/partial status, mode, material refs/digests, operation/
provider/configuration version, completion checks, costs, uncertainties and
the verified artifact bytes. The export is STAGED and the manifest is
published LAST, so an interrupted export never appears complete; import-side
verification (:func:`verify_native_export`) re-checks every artifact byte
against its digest. Separate from improvement evidence/rollback bundles by
construction and by kind marker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vouch_agent.appservices.execution import ExecutionService
from vouch_agent.contracts.common import digest_bytes
from vouch_agent.contracts.tasks import ResultPackage, TaskRun, TaskStatus
from vouch_agent.errors import ContractError, DigestMismatchError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vouch_agent.appservices.workspace import ProjectWorkspace

MANIFEST_NAME = "manifest.json"
#: Written BEFORE any payload and removed only after the manifest is
#: published: its presence marks an interrupted export (never complete).
INCOMPLETE_MARKER = ".vouch-export-incomplete"

_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)

_KIND_TASK_RUN = "task-run"
_KIND_TASK_SPEC = "task-spec"
_KIND_EXECUTION_CONFIG = "execution-config"
_KIND_RESULT_PACKAGE = "result-package"


def export_run(
    workspace: ProjectWorkspace,
    run_id: str,
    destination: Path,
    *,
    result: ResultPackage | None = None,
) -> Path:
    """Export the FULL native ResultPackage to a user directory.

    The manifest carries the ACTUAL run/task identity read back from durable
    state — never a caller's assertion. An injected ``result`` for a
    different run is rejected: a requested run's export may only contain
    that run's package. Atomic writes; digest-verified artifact payloads;
    manifest published LAST behind an incompleteness marker.
    """
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / INCOMPLETE_MARKER
    _atomic_write(marker, f"staged export of run {run_id}".encode())
    try:
        run_data = workspace.store.load(_KIND_TASK_RUN, run_id)
        if run_data is None:
            raise ContractError(
                f"unknown task run {run_id!r}; nothing to export "
                f"(runs: {workspace.store.list_ids(_KIND_TASK_RUN)})"
            )
        run = TaskRun.from_dict(run_data)
        if result is None:
            result = ExecutionService(workspace).result(run_id)
            if result is None:
                stored = workspace.store.load(_KIND_RESULT_PACKAGE, run_id)
                if stored is not None:
                    result = ResultPackage.from_dict(stored)
        if result is None:
            raise ContractError(f"run {run_id!r} has no result package to export")
        if result.run_id != run_id:
            raise ContractError(
                f"refusing to export an unrelated result package: requested run "
                f"{run_id!r} but the package belongs to {result.run_id!r}"
            )

        spec = _task_spec(workspace, run)
        configuration = workspace.store.load(_KIND_EXECUTION_CONFIG, run_id)
        manifest = _build_manifest(workspace, run, spec, configuration, result)

        for artifact in manifest["artifacts"]:
            payload = workspace.artifacts.get(str(artifact["digest"]))  # verifies bytes
            artifact["bytes"] = len(payload)
            _atomic_write(destination / str(artifact["file"]), payload)
        _atomic_write(destination / MANIFEST_NAME, _json_bytes(manifest))
        marker.unlink(missing_ok=True)
    except BaseException:
        # the marker stays: an interrupted export is visibly incomplete
        raise
    return destination / MANIFEST_NAME


def verify_native_export(destination: Path) -> dict[str, Any]:
    """Verify an exported native run directory on import/review.

    Fails closed on: a leftover incompleteness marker (interrupted export),
    a missing/unparseable manifest, artifact files that do not digest to
    their manifest entry, or files present that the manifest does not list.
    Returns the parsed manifest on success.
    """
    destination = Path(destination)
    if (destination / INCOMPLETE_MARKER).exists():
        raise ContractError(
            f"export at {destination} is INCOMPLETE (staging marker present); an "
            "interrupted export never counts as a deliverable — re-export the run"
        )
    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ContractError(f"no {MANIFEST_NAME} in {destination}: not a complete export")
    try:
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"export manifest of {destination} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("kind") != "vouch-native-run-export":
        raise ContractError(f"manifest of {destination} is not a native run export")
    listed = manifest.get("artifacts")
    if not isinstance(listed, list):
        raise ContractError(f"manifest of {destination} lists no artifacts")
    expected = {MANIFEST_NAME}
    for artifact in listed:
        name = str(artifact.get("file", ""))
        if not name or name == MANIFEST_NAME or name.startswith("."):
            raise ContractError(f"manifest of {destination} lists an invalid artifact {name!r}")
        expected.add(name)
        path = destination / name
        if not path.is_file():
            raise ContractError(f"export {destination} is missing artifact {name!r}")
        actual = digest_bytes(path.read_bytes())
        if actual != artifact.get("digest"):
            raise DigestMismatchError(
                f"export artifact {name!r} digests to {actual}, manifest promised "
                f"{artifact.get('digest')}"
            )
        if len(path.read_bytes()) != artifact.get("bytes"):
            raise ContractError(f"export artifact {name!r} size disagrees with the manifest")
    present = {entry.name for entry in destination.iterdir() if entry.is_file()}
    if present != expected:
        raise ContractError(
            f"export {destination} content does not match manifest: present "
            f"{sorted(present)}, expected {sorted(expected)}"
        )
    return manifest


def _indexed_spec_id(workspace: ProjectWorkspace, run_id: str) -> str | None:
    index = workspace.store.load("run-index", run_id)
    if isinstance(index, dict):
        spec_id = index.get("specId")
        return str(spec_id) if spec_id else None
    return None


def _task_spec(workspace: ProjectWorkspace, run: TaskRun) -> Any | None:
    """The run's TaskSpec: via the run's own linkage field, else via the
    durable run-index the supervisor writes at submit time."""
    spec_id = run.spec_id or _indexed_spec_id(workspace, run.run_id)
    if not spec_id:
        return None
    data = workspace.store.load(_KIND_TASK_SPEC, str(spec_id))
    if data is None:
        return None
    from vouch_agent.contracts.tasks import TaskSpec

    return TaskSpec.from_dict(data)


def _material_entries(spec: Any | None) -> list[dict[str, Any]]:
    """Material refs/digests as recorded on the task spec (both record
    shapes: bare attachment dicts, and id/displayName/attachment records)."""
    if spec is None:
        return []
    materials = spec.inputs.get("materials")
    if not isinstance(materials, list):
        return []
    entries: list[dict[str, Any]] = []
    for material in materials:
        if not isinstance(material, dict):
            continue
        if "attachment" in material and isinstance(material["attachment"], dict):
            attachment = material["attachment"]
            entries.append(
                {
                    "attachmentId": material.get("attachmentId", attachment.get("name")),
                    "displayName": material.get("displayName"),
                    "kind": attachment.get("kind"),
                    "digest": attachment.get("contentDigest"),
                    "sizeBytes": attachment.get("sizeBytes"),
                }
            )
        else:
            entries.append(
                {
                    "attachmentId": material.get("name"),
                    "displayName": None,
                    "kind": material.get("kind"),
                    "digest": material.get("contentDigest"),
                    "sizeBytes": material.get("sizeBytes"),
                }
            )
    return entries


def _build_manifest(
    workspace: ProjectWorkspace,
    run: TaskRun,
    spec: Any | None,
    configuration: dict[str, Any] | None,
    result: ResultPackage,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schemaVersion": "2",
        "kind": "vouch-native-run-export",
        # -- ACTUAL identity, read back from durable state ------------------
        "runId": run.run_id,
        "taskSpecId": run.spec_id or _indexed_spec_id(workspace, run.run_id),
        "taskDigest": run.task_digest,
        "runStatus": run.status.value,
        "terminal": run.status in _TERMINAL_STATUSES,
        "mode": run.mode.value,
        "title": spec.title if spec is not None else None,
        "goal": spec.goal if spec is not None else None,
        "materials": _material_entries(spec),
        "operation": spec.inputs.get("operation") if spec is not None else None,
        "operationInputs": (
            {
                key: value
                for key, value in spec.inputs.items()
                if key not in ("materials", "operation")
            }
            if spec is not None
            else {}
        ),
        # operation/provider/configuration version as sealed at execute time
        "configuration": _configuration_entry(configuration),
        # -- the delivered package ------------------------------------------
        "conclusion": result.conclusion,
        "doneItems": list(result.done_items),
        "notDoneItems": list(result.not_done_items),
        "uncertainties": list(result.uncertainties),
        "externalActions": dict(result.external_actions),
        "totalCostUsd": result.total_cost_usd,
        "completedConditionsCheck": dict(result.completed_conditions_check),
        "deliverable": result.deliverable(),
        "resultCreatedAt": result.created_at,
        "runError": run.error,
        # -- costs with failures included, from the journal -----------------
        "costEntries": [
            entry.to_dict() for entry in workspace.journal.cost_entries(run.run_id)
        ],
        "artifacts": [],
    }
    for index, digest in enumerate(result.artifact_refs):
        name = _artifact_filename(index, digest)
        manifest["artifacts"].append({"digest": digest, "file": name, "bytes": 0})
    return manifest


def _configuration_entry(configuration: dict[str, Any] | None) -> dict[str, Any] | None:
    if configuration is None:
        return None
    return {
        "providerName": configuration.get("providerName"),
        "scriptsDigest": configuration.get("scriptsDigest"),
        "runtimeId": configuration.get("runtimeId"),
        "toolVersion": configuration.get("toolVersion"),
        "isolated": configuration.get("isolated"),
        "mode": configuration.get("mode"),
    }


def _artifact_filename(index: int, digest: str) -> str:
    keep = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    safe_digest = "".join(character if character in keep else "_" for character in digest)
    return f"artifact-{index:03d}-{safe_digest[:16]}.bin"


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    import tempfile

    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".vouch-export-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temp_name, path)
    except BaseException:
        with _suppress():
            os.unlink(temp_name)
        raise


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> bool:
        return True


__all__ = [
    "INCOMPLETE_MARKER",
    "MANIFEST_NAME",
    "export_run",
    "verify_native_export",
]
