"""Durable ingestion of Choose's retained browser/regression evidence exports.

M4 §D promised the bridge that never existed: Choose's browser and regression
exporters write their OWN formats (``choose-vouch-browser-evidence`` record
version 2 with top-level provenance and nested journey runs;
``choose-vouch-regression-records`` with aggregate suites/overall), while
Vouch's coverage authority resolves per-workflow durable records under the
``browser-evidence`` / ``control-regression`` kinds. A retained export file
sitting somewhere on disk proved nothing — this module is the public importer
that validates an export and persists records the resolver accepts.

What validation means here (fail closed, every refusal names its reason):

* **Exporter version** — the exact record version this importer understands;
  a future/foreign format refuses instead of being half-read.
* **Tested source identity** — the export's provenance (git commit, dirty
  flag, uncommitted content) must name a CLEAN tested source, and the
  persisted record binds that REAL identity as ``sourceRevision``. It is
  never massaged into the runner's static version label, so historical
  browser evidence cannot silently attest a later current build (the
  resolver's revision equality check makes that mismatch refuse).
* **Workflow/case-pack mapping** — every workflow in the export must map to a
  pack this project holds; the record binds that pack's digest, and any
  journey→case mapping must name cases that actually exist in it.
* **Journey identities and matrix** — unique non-empty journey ids; the full
  locale x viewport matrix per journey; honest per-journey pass/fail from the
  runs' own ``ok`` flags and unexpected-console-error counts.
* **Artifact bytes** — every referenced screenshot must exist, hash to its
  declared digest, match its declared byte count, and be persisted
  content-addressed in the project store; the export file itself is stored
  and cited too.
* **Actual outcomes** — failed/partial/zero-test evidence imports as FAILED
  records (observed execution, never passed coverage) or refuses outright
  (zero tests prove nothing at all).

W-C9 stays regression-only: the control importer writes W-C9 records and the
manifest structurally excludes W-C9 from optimization targets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vouch_agent.contracts.cases import TaskPack
from vouch_agent.contracts.common import Role, RunMode, digest_bytes
from vouch_agent.contracts.journal import EventKind, EventRecord
from vouch_agent.errors import ContractError
from vouch_agent.evaluation.attestation_evidence import (
    KIND_BROWSER_EVIDENCE,
    KIND_CONTROL_REGRESSION,
    REQUIRED_LOCALES,
    REQUIRED_VIEWPORTS,
)
from vouch_agent.storage import TASK_PACK_KIND

#: The browser exporter format this importer understands (exact).
BROWSER_EXPORT_KIND = "choose-vouch-browser-evidence"
BROWSER_EXPORT_RECORD_VERSION = 2
#: The regression exporter format this importer understands (exact).
CONTROL_EXPORT_KIND = "choose-vouch-regression-records"
CONTROL_EXPORT_SCHEMA_VERSION = 1
#: Control regression evidence belongs to the regression-only workflow.
CONTROL_WORKFLOW_ID = "W-C9"

_JOURNEY_STATUS_PASSED = "passed"
_JOURNEY_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class ImportedLayerRecord:
    """What one import persisted, and where."""

    kind: str
    record_id: str
    workflow_id: str
    status: str
    source_revision: str
    journeys: int
    artifacts_cited: int


@dataclass(frozen=True)
class BrowserImportResult:
    records: tuple[ImportedLayerRecord, ...]
    tested_commit: str
    journeys: int
    screenshots_verified: int


@dataclass(frozen=True)
class ControlImportResult:
    record: ImportedLayerRecord
    tested_commit: str
    suites: int


def _load_export(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"unreadable evidence export {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError(f"evidence export {path} must be a JSON object")
    return raw


def _clean_provenance(raw: dict[str, Any], *, path: Path, commit_field: str) -> str:
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        raise ContractError(
            f"{path.name}: no provenance; evidence without a tested source "
            "identity is refused"
        )
    commit = provenance.get(commit_field)
    if not isinstance(commit, str) or not commit:
        raise ContractError(f"{path.name}: provenance.{commit_field} must name the tested commit")
    if provenance.get("dirty") is True:
        raise ContractError(
            f"{path.name}: provenance marks the tested tree DIRTY; evidence from a "
            "dirty tree has no clean source identity and is refused"
        )
    uncommitted = provenance.get("uncommitted")
    if isinstance(uncommitted, dict) and (
        uncommitted.get("changedTrackedFiles", 0) or uncommitted.get("untrackedFiles", 0)
    ):
        raise ContractError(
            f"{path.name}: provenance records uncommitted changes in the tested tree; "
            "refused (no clean source identity)"
        )
    return commit


def _pack_for(workspace: Any, pack_ref: str) -> TaskPack:
    data = workspace.store.load(TASK_PACK_KIND, pack_ref)
    if data is None:
        raise ContractError(
            f"pack {pack_ref!r} is not imported in this project; the evidence record "
            "must bind a pack the project holds (import it first)"
        )
    return TaskPack.from_dict(data)


def _validate_journey_cases(
    journey_cases: dict[str, tuple[str, ...]] | None, pack: TaskPack
) -> dict[str, tuple[str, ...]]:
    if not journey_cases:
        return {}
    known = {case.case_id for case in pack.cases}
    validated: dict[str, tuple[str, ...]] = {}
    for journey_id, case_ids in journey_cases.items():
        unknown = [case_id for case_id in case_ids if case_id not in known]
        if unknown:
            raise ContractError(
                f"journey {journey_id!r} maps to case ids {unknown} that are not in the "
                "bound pack; a journey may only verify cases the pack declares"
            )
        validated[journey_id] = tuple(case_ids)
    return validated


def _audit(workspace: Any, subject: str, data: dict[str, Any]) -> None:
    from vouch_agent.contracts.common import new_id

    workspace.journal.append(
        EventRecord(
            event_id=new_id("evt"),
            kind=EventKind.AUDIT_NOTE,
            subject=subject,
            data=data,
            mode=RunMode.FIXTURE,
        )
    )


def import_browser_evidence(
    workspace: Any,
    *,
    export_path: Path,
    pack_refs: dict[str, str],
    journey_cases: dict[str, tuple[str, ...]] | None = None,
    screenshots_dir: Path | None = None,
    expected_commit: str | None = None,
    provider_mode: str = "fixture",
    role: Role = Role.EVALUATOR,
) -> BrowserImportResult:
    """Validate and durably import a Choose browser-evidence export.

    Persists one ``browser-evidence`` record per workflow in the export,
    binding the mapped pack's digest, the REAL tested source commit, the full
    journey matrix and every screenshot's verified bytes. Failed journeys
    import as an honest FAILED record (observed execution, not coverage).
    """
    del role  # evidence ingestion is read-only project state; kept for symmetry
    path = Path(export_path)
    raw = _load_export(path)
    if raw.get("kind") != BROWSER_EXPORT_KIND:
        raise ContractError(
            f"{path.name}: kind must be {BROWSER_EXPORT_KIND!r}, got {raw.get('kind')!r}"
        )
    if raw.get("recordVersion") != BROWSER_EXPORT_RECORD_VERSION:
        raise ContractError(
            f"{path.name}: recordVersion {raw.get('recordVersion')!r} is not "
            f"{BROWSER_EXPORT_RECORD_VERSION}; this importer refuses formats it has "
            "not been validated against"
        )
    commit = _clean_provenance(raw, path=path, commit_field="commit")
    if expected_commit is not None and commit != expected_commit:
        raise ContractError(
            f"{path.name}: tested commit {commit!r} does not match the expected "
            f"{expected_commit!r}; refusing to import evidence for a different source"
        )
    journeys_raw = raw.get("journeys")
    if not isinstance(journeys_raw, list) or not journeys_raw:
        raise ContractError(f"{path.name}: no journeys recorded")
    shots_dir = Path(screenshots_dir) if screenshots_dir else path.parent / "screenshots"
    export_digest = workspace.artifacts.put(path.read_bytes())

    seen_journeys: set[str] = set()
    per_workflow: dict[str, list[dict[str, Any]]] = {}
    screenshots_verified = 0
    # One case-map validation per workflow, against THAT workflow's bound
    # pack and only for journeys that belong to it.
    journey_workflow: dict[str, str] = {}
    for journey in journeys_raw:
        if isinstance(journey, dict):
            jid = str(journey.get("journeyId", ""))
            if jid:
                journey_workflow[jid] = str(journey.get("workflowId", ""))
    journey_case_map: dict[str, tuple[str, ...]] = {}
    for workflow_id, pack_ref in pack_refs.items():
        pack = _pack_for(workspace, pack_ref)
        own = {
            jid: cases
            for jid, cases in (journey_cases or {}).items()
            if journey_workflow.get(jid) == workflow_id
        }
        validated = _validate_journey_cases(own, pack)
        journey_case_map.update(validated)
    orphaned = sorted(set(journey_cases or {}) - set(journey_workflow))
    if orphaned:
        raise ContractError(
            f"journey-case mapping names journeys absent from the export: "
            f"{orphaned}; a mapping may only reference exported journeys"
        )
    for journey in journeys_raw:
        if not isinstance(journey, dict):
            raise ContractError(f"{path.name}: journey entry is not an object")
        journey_id = str(journey.get("journeyId", ""))
        if not journey_id:
            raise ContractError(f"{path.name}: journey entry has no journeyId")
        if journey_id in seen_journeys:
            raise ContractError(f"{path.name}: duplicate journeyId {journey_id!r}")
        seen_journeys.add(journey_id)
        workflow_id = str(journey.get("workflowId", ""))
        if workflow_id not in pack_refs:
            raise ContractError(
                f"journey {journey_id!r} belongs to workflow {workflow_id!r} which has "
                f"no pack mapping (mapped: {sorted(pack_refs)}); every workflow in the "
                "export must bind a pack this project holds"
            )
        runs = journey.get("runs")
        if not isinstance(runs, list) or not runs:
            raise ContractError(f"journey {journey_id!r} recorded no runs")
        cells: dict[tuple[str, str], dict[str, Any]] = {}
        for run in runs:
            if not isinstance(run, dict):
                raise ContractError(f"journey {journey_id!r} has a non-object run")
            locale = str(run.get("locale", ""))
            viewport = str(run.get("viewport", ""))
            if locale not in REQUIRED_LOCALES:
                raise ContractError(
                    f"journey {journey_id!r} run locale {locale!r} not in {list(REQUIRED_LOCALES)}"
                )
            if viewport not in REQUIRED_VIEWPORTS:
                raise ContractError(
                    f"journey {journey_id!r} run viewport {viewport!r} not in "
                    f"{list(REQUIRED_VIEWPORTS)}"
                )
            if (locale, viewport) in cells:
                raise ContractError(
                    f"journey {journey_id!r} repeats matrix cell {locale}/{viewport}"
                )
            cells[(locale, viewport)] = run
        missing = [
            (locale, viewport)
            for locale in REQUIRED_LOCALES
            for viewport in REQUIRED_VIEWPORTS
            if (locale, viewport) not in cells
        ]
        if missing:
            raise ContractError(
                f"journey {journey_id!r} matrix incomplete: missing {missing}; the "
                "importer refuses partial-matrix evidence (M4-D requires every "
                "journey in every locale x viewport)"
            )
        journey_case_ids = list(journey_case_map.get(journey_id, ()))
        runs_ok = True
        journey_cells: list[dict[str, Any]] = []
        for (locale, viewport), run in sorted(cells.items()):
            run_ok = run.get("ok") is True
            console = run.get("console")
            unexpected = (
                console.get("unexpectedErrors", []) if isinstance(console, dict) else []
            )
            if unexpected:
                run_ok = False
            screenshot = run.get("screenshot")
            if not isinstance(screenshot, dict):
                raise ContractError(
                    f"journey {journey_id!r} {locale}/{viewport} records no screenshot"
                )
            declared = str(screenshot.get("sha256", ""))
            digest = _store_screenshot(
                workspace, shots_dir, screenshot, declared, journey_id, locale, viewport
            )
            screenshots_verified += 1
            runs_ok = runs_ok and run_ok
            journey_cells.append(
                {
                    "journeyId": journey_id,
                    "locale": locale,
                    "viewport": viewport,
                    "status": _JOURNEY_STATUS_PASSED if run_ok else _JOURNEY_STATUS_FAILED,
                    "caseIds": journey_case_ids,
                    "screenshotDigests": [digest],
                }
            )
        per_workflow.setdefault(workflow_id, []).extend(journey_cells)

    records: list[ImportedLayerRecord] = []
    for workflow_id in sorted(per_workflow):
        workflow_cells = per_workflow[workflow_id]
        pack = _pack_for(workspace, pack_refs[workflow_id])
        status = (
            _JOURNEY_STATUS_PASSED
            if all(cell["status"] == _JOURNEY_STATUS_PASSED for cell in workflow_cells)
            else _JOURNEY_STATUS_FAILED
        )
        record_id = f"browser-{commit[:12]}-{workflow_id}"
        record = {
            "schemaVersion": "1",
            "workflowId": workflow_id,
            "casePackDigest": pack.digest(),
            "providerMode": provider_mode,
            "sourceRevision": commit,
            "status": status,
            "artifactDigests": [export_digest],
            "journeys": workflow_cells,
            "importedFrom": {
                "exportPath": str(path),
                "exportDigest": export_digest,
                "exporter": str(raw.get("provenance", {}).get("exporter", "")),
                "generatedAt": str(raw.get("generatedAt", "")),
                "recordVersion": BROWSER_EXPORT_RECORD_VERSION,
                "journeyCaseMap": {
                    journey_id: list(case_ids)
                    for journey_id, case_ids in (journey_cases or {}).items()
                },
            },
        }
        workspace.store.save(KIND_BROWSER_EVIDENCE, record_id, record)
        records.append(
            ImportedLayerRecord(
                kind=KIND_BROWSER_EVIDENCE,
                record_id=record_id,
                workflow_id=workflow_id,
                status=status,
                source_revision=commit,
                journeys=len({cell["journeyId"] for cell in workflow_cells}),
                artifacts_cited=1 + len(workflow_cells),
            )
        )
    _audit(
        workspace,
        KIND_BROWSER_EVIDENCE,
        {
            "event": "browser-evidence-imported",
            "testedCommit": commit,
            "records": [record.record_id for record in records],
            "screenshotsVerified": screenshots_verified,
            "exportDigest": export_digest,
        },
    )
    return BrowserImportResult(
        records=tuple(records),
        tested_commit=commit,
        journeys=len(seen_journeys),
        screenshots_verified=screenshots_verified,
    )


def _store_screenshot(
    workspace: Any,
    shots_dir: Path,
    screenshot: dict[str, Any],
    declared: str,
    journey_id: str,
    locale: str,
    viewport: str,
) -> str:
    file_name = Path(str(screenshot.get("file", ""))).name
    shot_path = shots_dir / file_name
    if not shot_path.is_file():
        raise ContractError(
            f"journey {journey_id!r} {locale}/{viewport}: screenshot {file_name!r} "
            f"not found under {shots_dir}; cited artifacts must be imported with the export"
        )
    payload = shot_path.read_bytes()
    actual = digest_bytes(payload)
    if declared and actual != declared:
        raise ContractError(
            f"journey {journey_id!r} {locale}/{viewport}: screenshot {file_name!r} "
            f"hashes to {actual}, the export declares {declared}; tampered or "
            "mismatched evidence is refused"
        )
    declared_bytes = screenshot.get("bytes")
    if isinstance(declared_bytes, int) and declared_bytes != len(payload):
        raise ContractError(
            f"journey {journey_id!r} {locale}/{viewport}: screenshot {file_name!r} "
            f"is {len(payload)} bytes, the export declares {declared_bytes}"
        )
    stored = workspace.artifacts.put(payload)
    if stored != actual:  # pragma: no cover - content addressing is deterministic
        raise ContractError("artifact store re-addressed screenshot bytes; refusing")
    return actual


def import_control_evidence(
    workspace: Any,
    *,
    export_path: Path,
    pack_ref: str,
    suite_cases: dict[str, tuple[str, ...]] | None = None,
    workflow_id: str = CONTROL_WORKFLOW_ID,
    expected_commit: str | None = None,
    provider_mode: str = "fixture",
    role: Role = Role.EVALUATOR,
) -> ControlImportResult:
    """Validate and durably import a Choose product-control regression export.

    Persists one ``control-regression`` record for the regression-only W-C9
    workflow: per-family journeys with honest pass/fail from the suites' own
    counts and exit codes. Any failure, skip, cancellation or todo anywhere
    imports the record as FAILED (observed execution, never passed coverage);
    a zero-test export refuses outright.
    """
    del role
    if workflow_id != CONTROL_WORKFLOW_ID:
        raise ContractError(
            f"control regression evidence maps only to {CONTROL_WORKFLOW_ID!r} "
            f"(regression-only); refusing workflow {workflow_id!r}"
        )
    path = Path(export_path)
    raw = _load_export(path)
    if raw.get("kind") != CONTROL_EXPORT_KIND:
        raise ContractError(
            f"{path.name}: kind must be {CONTROL_EXPORT_KIND!r}, got {raw.get('kind')!r}"
        )
    if raw.get("schemaVersion") != CONTROL_EXPORT_SCHEMA_VERSION:
        raise ContractError(
            f"{path.name}: schemaVersion {raw.get('schemaVersion')!r} is not "
            f"{CONTROL_EXPORT_SCHEMA_VERSION}"
        )
    commit = _clean_provenance(raw, path=path, commit_field="baseCommit")
    if expected_commit is not None and commit != expected_commit:
        raise ContractError(
            f"{path.name}: tested commit {commit!r} does not match the expected "
            f"{expected_commit!r}"
        )
    overall = raw.get("overall")
    if not isinstance(overall, dict):
        raise ContractError(f"{path.name}: no overall result")
    total_tests = int(overall.get("tests", 0) or 0)
    if total_tests <= 0:
        raise ContractError(
            f"{path.name}: zero tests recorded; a zero-test record proves nothing and "
            "cannot import"
        )
    suites = raw.get("suites")
    if not isinstance(suites, list) or not suites:
        raise ContractError(f"{path.name}: no suites recorded")
    pack = _pack_for(workspace, pack_ref)
    case_map = _validate_journey_cases(suite_cases, pack)
    export_digest = workspace.artifacts.put(path.read_bytes())

    families: dict[str, list[dict[str, Any]]] = {}
    for suite in suites:
        if not isinstance(suite, dict):
            raise ContractError(f"{path.name}: suite entry is not an object")
        family = str(suite.get("family", ""))
        if not family:
            raise ContractError(f"{path.name}: suite {suite.get('suite')!r} has no family")
        failed = (
            int(suite.get("fail", 0) or 0) > 0
            or int(suite.get("skipped", 0) or 0) > 0
            or int(suite.get("cancelled", 0) or 0) > 0
            or int(suite.get("todo", 0) or 0) > 0
            or int(suite.get("exitCode", 1) or 0) != 0
            or int(suite.get("pass", 0) or 0) <= 0
        )
        families.setdefault(family, []).append(
            {"suite": str(suite.get("suite", "")), "failed": failed}
        )

    journeys = []
    for family in sorted(families):
        entries = families[family]
        family_passed = not any(entry["failed"] for entry in entries)
        journeys.append(
            {
                "journeyId": f"family:{family}",
                "locale": "",
                "viewport": "",
                "status": _JOURNEY_STATUS_PASSED if family_passed else _JOURNEY_STATUS_FAILED,
                "caseIds": list(case_map.get(family, ())),
                "screenshotDigests": [],
            }
        )
    overall_failed = (
        int(overall.get("fail", 0) or 0) > 0
        or int(overall.get("skipped", 0) or 0) > 0
        or any(journey["status"] != _JOURNEY_STATUS_PASSED for journey in journeys)
    )
    status = _JOURNEY_STATUS_FAILED if overall_failed else _JOURNEY_STATUS_PASSED
    record_id = f"control-{commit[:12]}"
    record = {
        "schemaVersion": "1",
        "workflowId": workflow_id,
        "casePackDigest": pack.digest(),
        "providerMode": provider_mode,
        "sourceRevision": commit,
        "status": status,
        "artifactDigests": [export_digest],
        "journeys": journeys,
        "importedFrom": {
            "exportPath": str(path),
            "exportDigest": export_digest,
            "generator": str(raw.get("generator", {}).get("script", "")),
            "recordedAt": str(raw.get("recordedAt", "")),
            "overall": {
                "tests": total_tests,
                **{k: overall.get(k, 0) for k in ("pass", "fail", "skipped")},
            },
            "suiteCaseMap": {
                family: list(case_ids) for family, case_ids in (suite_cases or {}).items()
            },
        },
    }
    workspace.store.save(KIND_CONTROL_REGRESSION, record_id, record)
    _audit(
        workspace,
        KIND_CONTROL_REGRESSION,
        {
            "event": "control-evidence-imported",
            "testedCommit": commit,
            "record": record_id,
            "status": status,
            "suites": len(suites),
            "exportDigest": export_digest,
        },
    )
    return ControlImportResult(
        record=ImportedLayerRecord(
            kind=KIND_CONTROL_REGRESSION,
            record_id=record_id,
            workflow_id=workflow_id,
            status=status,
            source_revision=commit,
            journeys=len(journeys),
            artifacts_cited=1,
        ),
        tested_commit=commit,
        suites=len(suites),
    )


__all__ = [
    "BROWSER_EXPORT_KIND",
    "BROWSER_EXPORT_RECORD_VERSION",
    "CONTROL_EXPORT_KIND",
    "CONTROL_EXPORT_SCHEMA_VERSION",
    "CONTROL_WORKFLOW_ID",
    "BrowserImportResult",
    "ControlImportResult",
    "ImportedLayerRecord",
    "import_browser_evidence",
    "import_control_evidence",
]
