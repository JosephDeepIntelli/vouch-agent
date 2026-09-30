"""Deterministic evidence / diff / rollback export packages (design §5, §7.2).

An evidence package is a directory bundle:

* ``manifest.json`` — canonical-JSON manifest carrying the run mode, run id,
  workflow, decision verdict and a digest for **every** other file. The
  digest of the manifest bytes is the package digest.
* ``run.json``, ``attempts.json`` (all attempts, failures included),
  ``outcomes.json``, ``comparison.json``, ``rubric.json``, ``decision.json``,
  ``candidate.json`` + ``candidate-delta.txt`` (the diff), ``events.json``,
  ``cost-journal.json``.
* ``evidence-inputs.json`` / ``evidence-outputs.json`` — the input/output
  artifact closure (review A6): the verified bytes each case executed on and
  the sealed digests each attempt produced, with explicitly declared gaps
  when an artifact is unavailable.

Determinism: same inputs -> byte-identical files -> identical digests. The
bundle embeds no timestamps of its own; everything in it comes from the
records it was built from. That is what makes "the evidence behind digest X"
a reproducible claim rather than a promise.

Mode honesty (§13.2): the manifest records the run mode and the export
refuses to mix modes — fixture, offline-evaluation and authorized-live
material never share one report.

A rollback package describes exactly how to revert a candidate delta and
performs no deploy action; the target product's own release process does the
actual revert and records a ReleaseRecord.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vouch_agent.contracts.candidate import AgentVersion, Candidate
from vouch_agent.contracts.common import (
    RunMode,
    canonical_json,
    digest_bytes,
    verify_digest,
)
from vouch_agent.contracts.decision import AcceptanceDecision
from vouch_agent.contracts.evaluation import EvaluationRun, Rubric
from vouch_agent.contracts.journal import CostEntry, EventRecord
from vouch_agent.errors import ContractError, DigestMismatchError
from vouch_agent.evaluation.comparator import ComparisonSummary

MANIFEST_NAME = "manifest.json"

_MANIFEST_SCHEMA = "1"


@dataclass(frozen=True)
class EvidenceArtifact:
    """One verifiable input/output artifact closing into an evidence package.

    ``digest`` plus ``payload_b64`` make the bytes independently checkable in
    a fresh workspace (no sibling files needed). When the artifact cannot be
    produced, ``unavailable_reason`` must say so explicitly — the package then
    DECLARES the gap instead of silently omitting the evidence (review A6).
    """

    subject_id: str  # case id (inputs) or attempt id (outputs)
    kind: str  # "case-input" | "attempt-output"
    digest: str | None = None
    payload_b64: str | None = None
    unavailable_reason: str = ""

    @property
    def available(self) -> bool:
        return self.digest is not None and self.payload_b64 is not None

    def to_entry(self) -> dict[str, Any]:
        return {
            "subjectId": self.subject_id,
            "kind": self.kind,
            "digest": self.digest,
            "available": self.available,
            "payloadB64": self.payload_b64,
            "unavailableReason": self.unavailable_reason or None,
        }


@dataclass(frozen=True)
class ExportedPackage:
    """Result of an export (and of a successful verification)."""

    path: Path
    package_kind: str
    mode: RunMode
    manifest_digest: str
    file_digests: dict[str, str]
    run_id: str | None = None


def _write_files(
    destination: Path, files: dict[str, bytes], manifest: dict[str, Any]
) -> ExportedPackage:
    if destination.exists() and any(destination.iterdir()):
        raise ContractError(f"export destination {destination} is not empty")
    destination.mkdir(parents=True, exist_ok=True)
    file_digests = {name: digest_bytes(payload) for name, payload in files.items()}
    manifest = dict(manifest)
    manifest["files"] = file_digests
    manifest_payload = canonical_json(manifest).encode("utf-8")
    for name, payload in files.items():
        (destination / name).write_bytes(payload)
    (destination / MANIFEST_NAME).write_bytes(manifest_payload)
    return ExportedPackage(
        path=destination,
        package_kind=str(manifest["packageKind"]),
        mode=RunMode(str(manifest["mode"])),
        manifest_digest=digest_bytes(manifest_payload),
        file_digests=file_digests,
        run_id=manifest.get("runId"),
    )


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _closure_bytes(kind: str, note: str, artifacts: Sequence[EvidenceArtifact]) -> bytes:
    return _json_bytes(
        {
            "schemaVersion": "1",
            "kind": kind,
            "note": (
                f"{note}; entries unavailable in the project store are declared "
                "explicitly, never omitted silently (review A6)"
            ),
            "entries": [a.to_entry() for a in artifacts],
        }
    )


def _assert_single_mode(mode: RunMode, *groups: Sequence[Any]) -> None:
    offenders: list[RunMode] = []
    for group in groups:
        for record in group:
            record_mode = getattr(record, "mode", None)
            if isinstance(record_mode, RunMode) and record_mode is not mode:
                offenders.append(record_mode)
    if offenders:
        kinds = sorted({offender.value for offender in offenders})
        raise ContractError(
            f"refusing to mix run modes in one report (§13.2): run is {mode.value}, "
            f"found records in {kinds}"
        )


def export_evidence_package(
    *,
    destination: Path,
    run: EvaluationRun,
    summary: ComparisonSummary,
    rubric: Rubric,
    decision: AcceptanceDecision | None = None,
    candidate: Candidate | None = None,
    events: Sequence[EventRecord] = (),
    cost_entries: Sequence[CostEntry] = (),
    case_inputs: Sequence[EvidenceArtifact] | None = None,
    attempt_outputs: Sequence[EvidenceArtifact] | None = None,
    attempt_evidence: Sequence[EvidenceArtifact] | None = None,
) -> ExportedPackage:
    """Write the deterministic evidence bundle for one comparison + decision.

    ``case_inputs``/``attempt_outputs``/``attempt_evidence`` close the
    input/output/evidence chain (review A6): every entry carries its digest
    and bytes (verifiable in a fresh workspace with no sibling files), or an
    explicit unavailable declaration. ``attempt_evidence`` carries the sealed
    artifacts each attempt CITED (report/evidence/usage/receipt bytes the
    adapter collected). Passing ``None`` (the default for direct callers)
    omits the closure files; the application services always pass them.
    """
    if summary.run_id != run.run_id:
        raise ContractError(
            f"summary is for run {summary.run_id!r}, evidence requested for {run.run_id!r}"
        )
    verify_digest(rubric.to_dict(), run.rubric_digest)  # run and rubric must agree
    if summary.mode is not run.mode or summary.split is not run.split:
        raise ContractError("summary does not match the run's mode/split")
    if decision is not None and decision.candidate_digest != run.candidate_digest:
        raise ContractError(
            "decision binds candidate digest "
            f"{decision.candidate_digest}, run measured {run.candidate_digest}"
        )
    _assert_single_mode(run.mode, events, cost_entries)

    files: dict[str, bytes] = {
        "run.json": _json_bytes(run.to_dict()),
        "attempts.json": _json_bytes(
            {
                "schemaVersion": "1",
                "note": "every attempt is kept, failures included (design §7.2)",
                "attempts": [
                    a.to_dict()
                    for a in sorted(
                        run.attempts, key=lambda a: (a.case_id, a.side.value, a.attempt_id)
                    )
                ],
            }
        ),
        "outcomes.json": _json_bytes(
            {"schemaVersion": "1", "outcomes": [o.to_dict() for o in summary.pairs]}
        ),
        "comparison.json": _json_bytes(summary.to_dict()),
        "rubric.json": _json_bytes(rubric.to_dict()),
        "events.json": _json_bytes({"schemaVersion": "1", "events": [e.to_dict() for e in events]}),
        "cost-journal.json": _json_bytes(
            {
                "schemaVersion": "1",
                "note": "unmeasurable entries carry measurable=false, never zero (design §10)",
                "entries": [c.to_dict() for c in cost_entries],
            }
        ),
    }
    if case_inputs is not None:
        files["evidence-inputs.json"] = _closure_bytes(
            "case-inputs", "the verified bytes each case executed on", case_inputs
        )
    if attempt_outputs is not None:
        files["evidence-outputs.json"] = _closure_bytes(
            "attempt-outputs", "the sealed output artifacts each attempt produced", attempt_outputs
        )
    if attempt_evidence is not None:
        files["evidence-references.json"] = _closure_bytes(
            "attempt-evidence",
            "the sealed artifacts each attempt cited (report/evidence/usage/receipt bytes)",
            attempt_evidence,
        )
    if candidate is not None:
        files["candidate.json"] = _json_bytes(candidate.to_dict())
        files["candidate-delta.txt"] = candidate.delta.encode("utf-8")
    if decision is not None:
        files["decision.json"] = _json_bytes(decision.to_dict())

    manifest = {
        "schemaVersion": _MANIFEST_SCHEMA,
        "packageKind": "evidence",
        "runId": run.run_id,
        "workflowId": run.workflow_id,
        "mode": run.mode.value,  # §13.2: the mode is part of the report, always
        "split": run.split.value,
        "candidateDigest": run.candidate_digest,
        "baselineDigest": run.baseline_digest,
        "caseSetDigest": run.case_set_digest,
        "rubricDigest": run.rubric_digest,
        "verdict": decision.verdict.value if decision is not None else None,
        "syntheticOnly": run.mode is RunMode.FIXTURE,
    }
    return _write_files(destination, files, manifest)


_ROLLBACK_STEPS = (
    "1. Stop routing work to the candidate configuration (changeType: {change_type}).",
    "2. Restore the parent version from its sourceRef: {source_ref}.",
    "3. Run the target product's own release process to deploy that parent version.",
    "4. Record the actual rollback as a ReleaseRecord, including observed side effects "
    "and compensation status.",
)


def export_rollback_package(
    *,
    destination: Path,
    candidate: Candidate,
    mode: RunMode,
    decision: AcceptanceDecision | None = None,
    measured_baseline: AgentVersion | None = None,
    notes: str = "",
) -> ExportedPackage:
    """Write a rollback plan package for a candidate delta.

    The package *describes* the revert; it never performs it. Vouch has no
    deploy capability in v1 — the target product's own release process does,
    and records the event (design §5, §7.2).

    ``measured_baseline`` is the baseline the acceptance evidence actually
    measured (A7). Rollback restores THAT version: reverting to the
    candidate's parent while a different baseline is deployed would silently
    separate the rollback target from the measured state. When the supplied
    measured baseline disagrees with the candidate's parent, the export
    REFUSES rather than guessing which one the operator meant — the candidate
    must be explicitly rebased (new content digest, fresh evidence) or the
    baseline re-measured first.
    """
    revert_target = candidate.parent_version
    if measured_baseline is not None:
        if measured_baseline.digest() != candidate.parent_version.digest():
            raise ContractError(
                "rollback export refuses to guess: candidate "
                f"{candidate.candidate_id!r} is derived from parent "
                f"{candidate.parent_version.version_id} "
                f"({candidate.parent_version.source_ref}) but the measured baseline is "
                f"{measured_baseline.version_id} ({measured_baseline.source_ref}). "
                "Rebase the candidate explicitly (new content digest + fresh "
                "evidence) or re-measure the baseline before exporting a rollback."
            )
        revert_target = measured_baseline
    steps = tuple(
        step.format(
            change_type=candidate.change_type.value,
            source_ref=revert_target.source_ref,
        )
        for step in _ROLLBACK_STEPS
    )
    rollback = {
        "schemaVersion": "1",
        "packageKind": "rollback-plan",
        "mode": mode.value,
        "candidateDigest": candidate.digest(),
        "candidateDeltaDigest": candidate.delta_digest(),
        "changeType": candidate.change_type.value,
        "parentVersion": candidate.parent_version.to_dict(),
        "measuredBaseline": measured_baseline.to_dict() if measured_baseline is not None else None,
        "revert": {
            "action": "redeploy-parent-version",
            "sourceRef": revert_target.source_ref,
            "steps": list(steps),
            "deployAction": "none",
            "deployActionPerformed": False,
        },
        "decision": decision.to_dict() if decision is not None else None,
        "notes": notes,
        "warning": (
            "This package is a plan, not an action: Vouch performs no deployment. "
            "The target product's own release process executes the rollback and "
            "records the ReleaseRecord."
        ),
    }
    files = {"rollback.json": _json_bytes(rollback)}
    manifest = {
        "schemaVersion": _MANIFEST_SCHEMA,
        "packageKind": "rollback-plan",
        "mode": mode.value,
        "candidateDigest": candidate.digest(),
        "verdict": decision.verdict.value if decision is not None else None,
    }
    return _write_files(destination, files, manifest)


def verify_package(path: Path, *, expected_manifest_digest: str | None = None) -> ExportedPackage:
    """Verify a package directory against its manifest.

    Every file listed in the manifest must exist with exactly the promised
    digest; every file present must be listed. Any deviation raises
    (:class:`DigestMismatchError` for content changes, :class:`ContractError`
    for structural damage) — a package that cannot be verified is not
    evidence.

    The manifest itself cannot vouch for its own bytes; pass
    ``expected_manifest_digest`` (e.g. the ``evidence_digest`` recorded on the
    AcceptanceDecision, which anchors the package from outside) to detect a
    forged manifest.
    """
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ContractError(f"no {MANIFEST_NAME} in {path}")
    manifest_payload = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_payload)
    except ValueError as exc:
        raise ContractError(f"manifest of {path} is not valid JSON: {exc}") from exc
    listed = manifest.get("files")
    mode = manifest.get("mode")
    if not isinstance(listed, dict) or not isinstance(mode, str):
        raise ContractError(f"manifest of {path} lacks files/mode")
    manifest_digest = digest_bytes(manifest_payload)
    if expected_manifest_digest is not None and manifest_digest != expected_manifest_digest:
        raise DigestMismatchError(
            f"package manifest digests to {manifest_digest}, expected "
            f"{expected_manifest_digest} (anchored externally, e.g. by the decision)"
        )
    present = sorted(p.name for p in path.iterdir() if p.is_file())
    expected = sorted([*listed.keys(), MANIFEST_NAME])
    if present != expected:
        raise ContractError(
            f"package {path} content does not match manifest: present {present}, "
            f"expected {expected}"
        )
    for name, promised in listed.items():
        actual = digest_bytes((path / name).read_bytes())
        if actual != promised:
            raise DigestMismatchError(
                f"package file {name} digests to {actual}, manifest promised {promised}"
            )
    return ExportedPackage(
        path=path,
        package_kind=str(manifest.get("packageKind", "unknown")),
        mode=RunMode(mode),
        manifest_digest=manifest_digest,
        file_digests=dict(listed),
        run_id=manifest.get("runId"),
    )


def read_package(path: Path, *, expected_manifest_digest: str | None = None) -> dict[str, Any]:
    """Verify a package and return ``{"manifest": ..., "records": {name: obj}}``.

    ``.json`` files are parsed into objects; other files (e.g. the plain-text
    candidate delta) are returned as raw strings.
    """
    verification = verify_package(path, expected_manifest_digest=expected_manifest_digest)
    records: dict[str, Any] = {}
    for name in sorted(verification.file_digests):
        text = (path / name).read_text(encoding="utf-8")
        records[name] = json.loads(text) if name.endswith(".json") else text
    manifest = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
    return {"manifest": manifest, "records": records}
