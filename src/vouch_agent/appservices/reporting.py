"""ReportService — read-side application services plus package export.

Everything a reviewer needs without mutating project state (export writes a
package directory, which is output, not project state):

* :meth:`ReportService.review` — candidates with states, the next expected
  human action per candidate, guardrail findings from persisted comparisons,
  pending decisions, full-burden costs, recovery posture.
* :meth:`ReportService.cost_report` — controller full-cost figures plus
  per-category journal aggregation (measured/unmeasurable kept apart).
* :meth:`ReportService.workflow_coverage` — the WorkflowManifest summary:
  declared vs fixture-covered vs runner-integrated vs unverified-integration
  (EFFECTIVE statuses only, M4 A2), with the standing honesty rule that a
  manifest entry is declaration, not integration.
* :meth:`ReportService.coverage_detail` — per-workflow effective status with
  the separate logic/browser/regression layer and provider-mode dimensions.
* :meth:`ReportService.export_run` / :meth:`ReportService.export_rollback` —
  deterministic evidence / rollback packages; the manifest digest is the
  anchor an acceptance decision binds to.
* :meth:`ReportService.resume_state` — open reservations and
  needs-reconciliation subjects (the recovery surface for ``vouch resume``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vouch_agent.appservices.flow import ImprovementFlow
from vouch_agent.appservices.kinds import COMPARISON_KIND, RUN_EXPORT_KIND
from vouch_agent.appservices.packs import load_manifest
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.candidate import Candidate, CandidateState
from vouch_agent.contracts.common import RunMode
from vouch_agent.contracts.decision import AcceptanceDecision, ReleaseRecord
from vouch_agent.contracts.evaluation import EvaluationRun, GuardrailFinding
from vouch_agent.contracts.journal import (
    BudgetReservation,
    CostEntry,
    EventKind,
    EventRecord,
)
from vouch_agent.controller.service import (
    KIND_CANDIDATE,
    KIND_DECISION,
    KIND_EVALUATION,
    KIND_RELEASE,
)
from vouch_agent.errors import ContractError, VouchError
from vouch_agent.evaluation import ComparisonSummary
from vouch_agent.export import (
    EvidenceArtifact,
    ExportedPackage,
    export_evidence_package,
    export_rollback_package,
    verify_package,
)
from vouch_agent.storage import TASK_PACK_KIND
from vouch_agent.workflows import (
    DECLARED_CHOOSE_WORKFLOWS,
    CoverageStatus,
    WorkflowManifest,
)

#: What each candidate state waits for, by whom (honest pending-work labels).
NEXT_ACTION: dict[CandidateState, str] = {
    CandidateState.PROPOSED: "seal (digest freeze) by proposer",
    CandidateState.SEALED: "paired evaluation (development/selection split)",
    CandidateState.EVALUATED: "independent final acceptance by the acceptance side",
    CandidateState.ACCEPTED: "release-owner approval",
    CandidateState.APPROVED: "release record after the product's own release process",
    CandidateState.RELEASED: "post-release observation",
    CandidateState.OBSERVED: "final observation -> rollback window closes",
    CandidateState.REJECTED: "terminal (rejected by acceptance decision)",
    CandidateState.INCONCLUSIVE: "terminal for this round (evidence inconclusive)",
    CandidateState.INVALIDATED: "terminal (invalidated; re-enter as a new proposal)",
    CandidateState.ROLLED_BACK: "terminal (rolled back)",
}

_PENDING_STATES = frozenset(
    {
        CandidateState.PROPOSED,
        CandidateState.SEALED,
        CandidateState.EVALUATED,
        CandidateState.ACCEPTED,
        CandidateState.APPROVED,
    }
)


@dataclass(frozen=True)
class RunInfo:
    run: EvaluationRun
    comparison: ComparisonSummary | None
    export_digest: str | None = None


@dataclass(frozen=True)
class CostReport:
    measured_usd: float
    outstanding_reserved_usd: float
    remaining_usd: float
    human_minutes: float
    unmeasurable_entries: list[str]
    by_category: dict[str, dict[str, float]] = field(default_factory=dict)
    total_cap_usd: float = 0.0


@dataclass(frozen=True)
class ResumeState:
    open_reservations: list[BudgetReservation]
    needs_reconciliation: list[str]
    journal_events: int

    @property
    def clean(self) -> bool:
        return not self.open_reservations and not self.needs_reconciliation


@dataclass(frozen=True)
class ReviewReport:
    project_id: str
    project_name: str
    candidates: list[Candidate]
    pending: list[tuple[Candidate, str]]
    decisions: list[AcceptanceDecision]
    releases: list[ReleaseRecord]
    runs: list[RunInfo]
    hard_findings: list[tuple[str, GuardrailFinding]]
    soft_findings: list[tuple[str, GuardrailFinding]]
    costs: CostReport
    resume: ResumeState
    coverage: dict[str, list[str]]
    #: Per-workflow effective coverage with the layer/mode dimensions kept
    #: SEPARATE (M4 A2): {"W-C3": {"status": ..., "layer": ..., "mode": ...}}.
    coverage_dimensions: dict[str, dict[str, str | None]] = field(default_factory=dict)


class ReportService:
    def __init__(self, workspace: ProjectWorkspace) -> None:
        self.workspace = workspace

    # -- review ------------------------------------------------------------------

    def review(self) -> ReviewReport:
        candidates = [
            self.workspace.controller().candidate(cid) for cid in self._ids(KIND_CANDIDATE)
        ]
        candidates.sort(key=lambda c: (c.created_at, c.candidate_id))
        pending = [(c, NEXT_ACTION[c.state]) for c in candidates if c.state in _PENDING_STATES]
        return ReviewReport(
            project_id=self.workspace.spec.project_id,
            project_name=self.workspace.spec.name,
            candidates=candidates,
            pending=pending,
            decisions=self.decisions(),
            releases=self.releases(),
            runs=self.runs(),
            hard_findings=self.findings(severity="hard"),
            soft_findings=self.findings(severity="soft"),
            costs=self.cost_report(),
            resume=self.resume_state(),
            coverage=self.workflow_coverage(),
            coverage_dimensions=self.coverage_detail(),
        )

    def runs(self) -> list[RunInfo]:
        infos: list[RunInfo] = []
        for run_id in self._ids(KIND_EVALUATION):
            data = self.workspace.store.load(KIND_EVALUATION, run_id)
            if data is None:  # pragma: no cover - id came from list_ids
                continue
            run = EvaluationRun.from_dict(data)
            comparison = None
            summary_data = self.workspace.store.load(COMPARISON_KIND, run_id)
            if summary_data is not None:
                comparison = ComparisonSummary.from_dict(summary_data)
            export_digest = None
            export_data = self.workspace.store.load(RUN_EXPORT_KIND, run_id)
            if export_data is not None:
                export_digest = str(export_data["manifestDigest"])
            infos.append(RunInfo(run=run, comparison=comparison, export_digest=export_digest))
        infos.sort(key=lambda info: (info.run.created_at, info.run.run_id))
        return infos

    def decisions(self) -> list[AcceptanceDecision]:
        records = []
        for decision_id in self._ids(KIND_DECISION):
            data = self.workspace.store.load(KIND_DECISION, decision_id)
            if data is not None:  # pragma: no cover - id came from list_ids
                records.append(AcceptanceDecision.from_dict(data))
        records.sort(key=lambda d: (d.decided_at, d.decision_id))
        return records

    def releases(self) -> list[ReleaseRecord]:
        records = []
        for release_id in self._ids(KIND_RELEASE):
            data = self.workspace.store.load(KIND_RELEASE, release_id)
            if data is not None:  # pragma: no cover - id came from list_ids
                records.append(ReleaseRecord.from_dict(data))
        records.sort(key=lambda r: (r.released_at, r.release_id))
        return records

    def findings(self, *, severity: str) -> list[tuple[str, GuardrailFinding]]:
        out: list[tuple[str, GuardrailFinding]] = []
        for info in self.runs():
            if info.comparison is None:
                continue
            found = (
                info.comparison.hard_violations
                if severity == "hard"
                else info.comparison.soft_violations
            )
            out.extend((info.run.run_id, f) for f in found)
        return out

    # -- costs ----------------------------------------------------------------------

    def cost_report(self) -> CostReport:
        controller = self.workspace.controller()
        summary = controller.full_cost_usd()
        entries = self.workspace.journal.cost_entries()
        by_category: dict[str, dict[str, float]] = {}
        for entry in entries:
            bucket = by_category.setdefault(
                entry.category.value, {"measuredUsd": 0.0, "unmeasurable": 0.0}
            )
            if entry.measurable and entry.amount_usd is not None:
                bucket["measuredUsd"] += entry.amount_usd
            else:
                bucket["unmeasurable"] += 1
        return CostReport(
            measured_usd=float(summary["measuredUsd"]),
            outstanding_reserved_usd=float(summary["outstandingReservedUsd"]),
            remaining_usd=float(summary["remainingUsd"]),
            human_minutes=float(summary["humanMinutes"]),
            unmeasurable_entries=list(summary["unmeasurableEntries"]),
            by_category=by_category,
            total_cap_usd=self.workspace.ledger.total_cap_usd(),
        )

    # -- workflow coverage -------------------------------------------------------------

    def workflow_coverage(self) -> dict[str, list[str]]:
        """EFFECTIVE coverage summary; a manifest entry is declaration, not
        integration.

        M4 A2: the summary comes from the manifest's EFFECTIVE statuses — a
        persisted ``runner-integrated`` claim that has not been resolved
        against durable evidence in this process reports under
        ``unverified-integration``, never as integrated coverage.
        """
        manifest = load_manifest(self.workspace)
        if manifest is not None:
            return manifest.coverage_summary()
        # No fixture pack imported yet: everything is merely declared.
        summary: dict[str, list[str]] = {status.value: [] for status in CoverageStatus}
        summary["unverified-integration"] = []
        summary[CoverageStatus.DECLARED.value] = [
            spec.workflow_id for spec in DECLARED_CHOOSE_WORKFLOWS
        ]
        return summary

    def coverage_detail(self) -> dict[str, dict[str, str | None]]:
        """Per-workflow effective status with the SEPARATE evidence-layer
        (logic/browser/control) and provider-mode dimensions (M4 A2)."""
        manifest = load_manifest(self.workspace)
        if manifest is not None:
            return manifest.coverage_dimensions()
        return {
            spec.workflow_id: {"status": CoverageStatus.DECLARED.value, "layer": None, "mode": None}
            for spec in DECLARED_CHOOSE_WORKFLOWS
        }

    def manifest(self) -> WorkflowManifest | None:
        return load_manifest(self.workspace)

    # -- export ------------------------------------------------------------------------

    def export_run(self, run_id: str, *, destination: Path) -> ExportedPackage:
        """Write the deterministic evidence package for one evaluated run.

        The package closes the input/output evidence chain (review A6): the
        verified bytes behind each executed case's input digest and the sealed
        output artifacts of every attempt are embedded — or explicitly
        declared unavailable when the store can no longer produce them. The
        journal/cost sections carry THIS run's lineage (run + attempt
        subjects), not every same-mode event in the project.
        """
        data = self.workspace.store.load(KIND_EVALUATION, run_id)
        if data is None:
            raise ContractError(
                f"unknown evaluation run {run_id!r}; runs: {self._ids(KIND_EVALUATION)}"
            )
        run = EvaluationRun.from_dict(data)
        summary_data = self.workspace.store.load(COMPARISON_KIND, run_id)
        if summary_data is None:
            raise ContractError(
                f"run {run_id!r} has no persisted comparison; run 'vouch evaluate' first"
            )
        summary = ComparisonSummary.from_dict(summary_data)
        rubric = self.workspace.controller().rubric(run.rubric_digest)
        flow = ImprovementFlow(self.workspace)
        candidate: Candidate | None
        try:
            candidate = flow.candidate_by_digest(run.candidate_digest)
        except ContractError:
            candidate = None

        # Run-level lineage: the run and its attempts, nothing else.
        subjects = {run_id, *(a.attempt_id for a in run.attempts)}
        events = [
            e
            for e in self.workspace.journal.events()
            if e.mode is run.mode and e.subject in subjects
        ]
        costs = [
            c
            for c in self.workspace.journal.cost_entries()
            if c.mode is run.mode and c.subject in subjects
        ]

        package = export_evidence_package(
            destination=Path(destination),
            run=run,
            summary=summary,
            rubric=rubric,
            candidate=candidate,
            events=events,
            cost_entries=costs,
            case_inputs=self._case_input_artifacts(run),
            attempt_outputs=self._attempt_output_artifacts(run),
            attempt_evidence=self._attempt_evidence_artifacts(run),
        )
        self.workspace.store.save(
            RUN_EXPORT_KIND,
            run_id,
            {"manifestDigest": package.manifest_digest, "path": str(package.path)},
        )
        return package

    def _case_input_artifacts(self, run: EvaluationRun) -> list[EvidenceArtifact]:
        """The verified input bytes of every case the run executed on."""
        artifacts: list[EvidenceArtifact] = []
        seen: set[str] = set()
        for case in self._pack_cases_for_run(run):
            if case.input_digest in seen:
                continue
            seen.add(case.input_digest)
            payload, reason = self._artifact_payload(case.input_digest)
            artifacts.append(
                EvidenceArtifact(
                    subject_id=case.case_id,
                    kind="case-input",
                    digest=case.input_digest if payload is not None else None,
                    payload_b64=payload,
                    unavailable_reason=reason,
                )
            )
        # Any executed case whose pack is gone still gets an honest entry.
        known = {case.case_id for case in self._pack_cases_for_run(run)}
        for case_id in sorted({a.case_id for a in run.attempts} - known):
            artifacts.append(
                EvidenceArtifact(
                    subject_id=case_id,
                    kind="case-input",
                    unavailable_reason=(
                        "the pack bound to this run is no longer stored; the case's "
                        "input digest cannot be resolved to bytes"
                    ),
                )
            )
        return artifacts

    def _attempt_output_artifacts(self, run: EvaluationRun) -> list[EvidenceArtifact]:
        """The sealed output digest of every attempt, bytes included."""
        artifacts: list[EvidenceArtifact] = []
        for attempt in sorted(run.attempts, key=lambda a: (a.case_id, a.side.value, a.attempt_id)):
            if attempt.output_digest is None:
                artifacts.append(
                    EvidenceArtifact(
                        subject_id=attempt.attempt_id,
                        kind="attempt-output",
                        unavailable_reason=(
                            f"attempt sealed no output artifact (status={attempt.status.value})"
                        ),
                    )
                )
                continue
            payload, reason = self._artifact_payload(attempt.output_digest)
            artifacts.append(
                EvidenceArtifact(
                    subject_id=attempt.attempt_id,
                    kind="attempt-output",
                    digest=attempt.output_digest if payload is not None else None,
                    payload_b64=payload,
                    unavailable_reason=reason,
                )
            )
        return artifacts

    def _artifact_payload(self, digest: str) -> tuple[str | None, str]:
        import base64

        try:
            payload = self.workspace.artifacts.get(digest)  # verifies the bytes
        except VouchError as exc:
            return None, f"artifact {digest} unavailable: {exc}"
        return base64.b64encode(payload).decode("ascii"), ""

    def _attempt_evidence_artifacts(self, run: EvaluationRun) -> list[EvidenceArtifact]:
        """The bytes behind every evidence reference the attempts cited.

        The sealed output artifact of each attempt lists the digests the
        adapter bound to it (report/evidence/usage — and, on the application
        path, the application receipt). The export carries those bytes too —
        or an explicit unavailable declaration — so a reviewer re-verifies the
        full receipt/report/source closure after the runner's own ephemeral
        workspace is long gone.
        """
        import json

        artifacts: list[EvidenceArtifact] = []
        seen: set[tuple[str, str]] = set()
        for attempt in sorted(run.attempts, key=lambda a: (a.case_id, a.side.value, a.attempt_id)):
            if attempt.output_digest is None:
                continue  # the attempt-output closure already declares it unavailable
            try:
                sealed = json.loads(self.workspace.artifacts.get(attempt.output_digest))
            except (VouchError, json.JSONDecodeError):
                continue  # ditto: unavailable outputs are declared, not guessed
            refs = sealed.get("evidenceRefs", [])
            if not isinstance(refs, list):
                refs = []
            for ref in refs:
                digest = str(ref)
                key = (attempt.attempt_id, digest)
                if key in seen:
                    continue
                seen.add(key)
                payload, reason = self._artifact_payload(digest)
                artifacts.append(
                    EvidenceArtifact(
                        subject_id=f"{attempt.attempt_id}:{digest}",
                        kind="attempt-evidence",
                        digest=digest if payload is not None else None,
                        payload_b64=payload,
                        unavailable_reason=reason,
                    )
                )
        return artifacts

    def _pack_cases_for_run(self, run: EvaluationRun) -> tuple[Any, ...]:
        from vouch_agent.contracts.cases import TaskPack

        for pack_id in self.workspace.store.list_ids(TASK_PACK_KIND):
            data = self.workspace.store.load(TASK_PACK_KIND, pack_id)
            if data is None:  # pragma: no cover - id came from list_ids
                continue
            try:
                pack = TaskPack.from_dict(data)
            except ContractError:  # pragma: no cover - corrupt record
                continue
            if pack.digest() == run.case_set_digest:
                return pack.cases_in(run.split)
        return ()

    def export_rollback(
        self, candidate_id: str, *, destination: Path, notes: str = ""
    ) -> ExportedPackage:
        """Write the rollback *plan* package (describes the revert, never deploys)."""
        flow = ImprovementFlow(self.workspace)
        candidate = self.workspace.controller().candidate(candidate_id)
        decision = flow.latest_decision_for(candidate)
        package = export_rollback_package(
            destination=Path(destination),
            candidate=candidate,
            mode=run_mode_of(self.workspace),
            decision=decision,
            notes=notes,
        )
        verify_package(package.path)
        return package

    # -- recovery ------------------------------------------------------------------------

    def resume_state(self) -> ResumeState:
        ledger = self.workspace.ledger
        open_reservations = ledger.open_reservations()
        needs: dict[str, str] = {}
        for event in self.workspace.journal.events():
            if event.kind is not EventKind.RUN_RECONCILIATION:
                continue
            state = str(event.data.get("state", ""))
            if state == "needed":
                needs[event.subject] = str(event.data.get("reason", ""))
            elif state == "resolved" and event.subject in needs:
                del needs[event.subject]
        return ResumeState(
            open_reservations=open_reservations,
            needs_reconciliation=sorted(needs),
            journal_events=len(self.workspace.journal.events()),
        )

    def events(self, subject: str | None = None) -> list[EventRecord]:
        return self.workspace.journal.events(subject)

    def cost_entries(self, subject: str | None = None) -> list[CostEntry]:
        return self.workspace.journal.cost_entries(subject)

    # -- helpers ---------------------------------------------------------------------------

    def _ids(self, kind: str) -> list[str]:
        return self.workspace.store.list_ids(kind)


def run_mode_of(workspace: ProjectWorkspace) -> RunMode:
    """The mode a project's records were produced in (v1: fixture, honestly)."""
    modes = {entry.mode for entry in workspace.journal.cost_entries()}
    modes.update(event.mode for event in workspace.journal.events())
    if modes - {RunMode.FIXTURE}:  # pragma: no cover - v1 CLI never produces these
        raise ContractError(
            f"project mixes run modes {sorted(m.value for m in modes)}; "
            "mode-separated reporting is required (§13.2)"
        )
    return RunMode.FIXTURE
