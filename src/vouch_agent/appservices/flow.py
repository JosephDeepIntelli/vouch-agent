"""ImprovementFlow — the improvement lifecycle as application services.

Wraps :class:`vouch_agent.controller.VouchController` for CLI/TUI use. Every
method takes the project workspace plus the acting role and passes the role
through honestly: when the controller refuses because a role may not do
something, the error names the role that was required.

Lifecycle exposed here (design §7.2, §11):

    baseline (+ rubric freeze) -> propose -> seal -> paired evaluation
    (development / selection-validation) -> [advance to evaluated] ->
    final acceptance on the final split -> evidence export -> decision ->
    approval -> release record -> rollback export.

Every step persists through the workspace; nothing lives in the caller. All
runs are offline fixture-mode in v1 (no network, no live model calls).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vouch_agent.adapters.base import AdapterClient
from vouch_agent.adapters.choose_bundle import (
    ChooseBaselineConfig,
    parse_candidate_delta,
)
from vouch_agent.adapters.fixture_adapter import FixturePack
from vouch_agent.adapters.process_adapter import (
    DEFAULT_EXECUTE_TIMEOUT_S,
    ProcessAdapterClient,
    fixture_adapter_command,
)
from vouch_agent.appservices.adapters import FixtureScenarioClient, ScriptedOfflineAdapter
from vouch_agent.appservices.choose_apply import (
    ChooseApplicationClient,
    application_scope_refusal,
    evaluation_cases_from_describe,
)
from vouch_agent.appservices.kinds import (
    COMPARISON_KIND,
    RUN_EXPORT_KIND,
    WORKFLOW_BASELINE_KIND,
    WORKFLOW_RUBRIC_KIND,
)
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.contracts.candidate import (
    AgentVersion,
    Candidate,
    CandidateState,
    ChangeType,
)
from vouch_agent.contracts.cases import CaseSplit, TaskPack
from vouch_agent.contracts.common import Role, RunMode, new_id, utc_now_iso
from vouch_agent.contracts.decision import AcceptanceDecision, ReleaseRecord, Verdict
from vouch_agent.contracts.evaluation import EvaluationRun, Rubric
from vouch_agent.contracts.journal import EventKind, EventRecord
from vouch_agent.contracts.project import WorkflowDeclaration
from vouch_agent.controller.service import (
    KIND_BASELINE,
    KIND_CANDIDATE,
    KIND_DECISION,
    KIND_EVALUATION,
    KIND_RESERVATION,
)
from vouch_agent.errors import ContractError, ReconciliationRequiredError, SplitAccessError
from vouch_agent.evaluation import (
    ComparisonSummary,
    compare_run,
    decide,
    run_with_outcomes,
    verdict_from_summary,
)
from vouch_agent.export import ExportedPackage
from vouch_agent.storage import TASK_PACK_KIND, load_task_pack, save_task_pack

#: The adapter kinds the flow can drive.
ADAPTER_SCRIPTED = "scripted"
ADAPTER_FIXTURE = "fixture"
#: The REAL Choose-owned runner (tsx subprocess, protocol v1 + v1.1/v1.2
#: extensions), driven through its apply-config application path: the sealed
#: candidate's change bundle is actually applied against the runner's frozen
#: baseline and every attempt returns a validated application receipt.
ADAPTER_CHOOSE = "choose"


def configured_choose_repo() -> Path:
    """The EXPLICITLY configured Choose checkout (``VOUCH_CHOOSE_RUNNER_DIR``).

    There is deliberately no implicit default: a workstation path baked into
    the package would be configuration by guesswork. Unset means "not
    configured" and every consumer refuses with developer guidance.
    """
    import os

    value = os.environ.get("VOUCH_CHOOSE_RUNNER_DIR", "").strip()
    if not value:
        raise ContractError(
            "VOUCH_CHOOSE_RUNNER_DIR is not set: the Choose runner location is "
            "explicit configuration, and no default path is assumed. To use "
            "the experimental choose adapter, application packs, evidence "
            "importers or pilot commands, point VOUCH_CHOOSE_RUNNER_DIR at a "
            "prepared Choose checkout (node_modules installed, "
            "scripts/vouch/runner.ts present)."
        )
    return Path(value)

_SCRIPTED_METRIC_DEFAULT = "recordedClaims"


@dataclass(frozen=True)
class BaselineFreeze:
    workflow_id: str
    version: AgentVersion
    baseline_id: str
    rubric_digest: str
    rubric: Rubric


@dataclass(frozen=True)
class ProposalResult:
    candidate: Candidate
    sealed: bool
    content_digest: str


@dataclass(frozen=True)
class EvaluationOutcome:
    run: EvaluationRun
    summary: ComparisonSummary
    verdict: Verdict
    candidate: Candidate
    advanced: bool
    adapter_id: str


@dataclass(frozen=True)
class DecisionOutcome:
    decision: AcceptanceDecision
    package: ExportedPackage
    candidate: Candidate


class ImprovementFlow:
    """Stateless-per-call service over one opened workspace."""

    def __init__(self, workspace: ProjectWorkspace) -> None:
        self.workspace = workspace

    # -- pack resolution --------------------------------------------------------

    def import_pack_file(self, path: Path, *, role: Role = Role.EVALUATOR) -> TaskPack:
        """Import a TaskPack JSON file into the project.

        Integrity gate: every case's input artifact must be held AND still
        digest to its name — the BYTES are fetched and verified, not just
        looked up (review A6: a stored artifact that was corrupted after
        import must fail here, not at execution time). A pack naming digests
        the project cannot verify is refused, fail closed.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"unreadable task pack file {path}: {exc}") from exc
        pack = TaskPack.from_dict(data)
        missing: list[str] = []
        for case in pack.cases:
            if not self.workspace.artifacts.exists(case.input_digest):
                missing.append(case.case_id)
                continue
            self.workspace.artifacts.get(case.input_digest)  # verifies the bytes
        if missing:
            raise ContractError(
                f"pack {pack.pack_id!r} names case inputs this project does not hold "
                f"(missing artifact for cases: {missing}); a pack whose inputs cannot "
                "be fetched by digest is refused (fail closed)"
            )
        controller = self.workspace.controller()
        save_task_pack(self.workspace.store, pack)
        controller.import_pack(pack, role)
        return pack

    def load_pack(self, pack_ref: Path | str, *, role: Role = Role.EVALUATOR) -> TaskPack:
        """Resolve ``--pack``: a JSON file path imports it; anything else is a
        stored pack id loaded through the role's split visibility."""
        path = Path(pack_ref)
        if path.is_file():
            return self.import_pack_file(path, role=role)
        pack_id = str(pack_ref)
        if self.workspace.store.load(TASK_PACK_KIND, pack_id) is None:
            raise ContractError(
                f"pack {pack_id!r} is neither an existing file nor an imported pack id "
                f"(imported: {self.workspace.store.list_ids(TASK_PACK_KIND)})"
            )
        return load_task_pack(self.workspace.store, pack_id, role)

    # -- baseline + rubric ---------------------------------------------------------

    def record_baseline(
        self,
        *,
        version_id: str,
        source_ref: str,
        workflow_id: str | None = None,
        main_metric: str | None = None,
        direction: str = "increase",
        min_improvement: float = 0.0,
        min_complete_pairs: int | None = None,
        hard_guardrails: tuple[str, ...] | None = None,
        repeats: int = 1,
        frozen_by: str | None = None,
    ) -> BaselineFreeze:
        """Record the baseline AgentVersion and freeze the workflow rubric.

        Thresholds freeze after baseline measurement and before candidate
        search (design §3.1); this method is where that happens for CLI/TUI
        users. ``frozen_by`` defaults to the project's acceptance owner — the
        identity that owns the standard — and must never be empty.
        """
        spec = self.workspace.spec
        workflow_id = workflow_id or self._single_workflow()
        workflow = spec.workflow(workflow_id)  # unknown workflow fails closed
        owner = frozen_by or spec.owners.get(Role.ACCEPTANCE_OWNER, "")
        if not owner.strip():
            raise ContractError(
                "rubric freezing requires a non-empty identity (--frozen-by); the "
                "project also declares no acceptance owner to default to"
            )
        version = AgentVersion(version_id=version_id, source_ref=source_ref)
        controller = self.workspace.controller()
        baseline_id = controller.record_baseline(version, workflow_id)

        thresholds: dict[str, float] = {"min-main-improvement": float(min_improvement)}
        if min_complete_pairs is not None:
            thresholds["min-complete-pairs"] = float(min_complete_pairs)
        guardrails = hard_guardrails if hard_guardrails is not None else workflow.guardrails
        rubric = Rubric(
            main_metric=main_metric or workflow.main_objective or _SCRIPTED_METRIC_DEFAULT,
            direction=direction,
            thresholds=thresholds,
            hard_guardrails=tuple(guardrails),
            repeats=repeats,
        )
        rubric_digest = controller.freeze_rubric(rubric, frozen_by=owner)
        frozen = controller.rubric(rubric_digest)

        self.workspace.store.save(
            WORKFLOW_BASELINE_KIND,
            workflow_id,
            {"versionId": version_id, "baselineRecordId": baseline_id},
        )
        self.workspace.store.save(
            WORKFLOW_RUBRIC_KIND, workflow_id, {"rubricDigest": rubric_digest}
        )
        return BaselineFreeze(
            workflow_id=workflow_id,
            version=version,
            baseline_id=baseline_id,
            rubric_digest=rubric_digest,
            rubric=frozen,
        )

    def baseline_for(self, workflow_id: str) -> AgentVersion:
        mapping = self.workspace.store.load(WORKFLOW_BASELINE_KIND, workflow_id)
        if mapping is None:
            raise ContractError(
                f"workflow {workflow_id!r} has no recorded baseline; run 'vouch baseline' first"
            )
        record = self.workspace.store.load(KIND_BASELINE, str(mapping["baselineRecordId"]))
        if record is None:  # pragma: no cover - mapping written with the record
            raise ContractError(f"baseline record {mapping['baselineRecordId']!r} missing")
        return AgentVersion.from_dict(record)

    def rubric_for(self, workflow_id: str, rubric_digest: str | None = None) -> Rubric:
        if rubric_digest is not None:
            return self.workspace.controller().rubric(rubric_digest)
        mapping = self.workspace.store.load(WORKFLOW_RUBRIC_KIND, workflow_id)
        if mapping is None:
            raise ContractError(
                f"workflow {workflow_id!r} has no frozen rubric; run 'vouch baseline' "
                "first (thresholds freeze after baseline, before candidate search)"
            )
        return self.workspace.controller().rubric(str(mapping["rubricDigest"]))

    # -- propose / seal ---------------------------------------------------------------

    def propose(
        self,
        *,
        delta: str,
        change_type: str,
        rationale: str,
        expected_impact: str = "",
        proposer: str = "proposer-agent",
        candidate_id: str | None = None,
        workflow_id: str | None = None,
        dev_case_refs: tuple[str, ...] = (),
        seal: bool = False,
    ) -> ProposalResult:
        """Validate and persist a bounded change proposal (scope-checked).

        Returns the candidate with its content digest — the seal preview. With
        ``seal=True`` the candidate is sealed immediately (digest fixed); a
        later byte-level change to anything the digest covers invalidates it.
        """
        spec = self.workspace.spec
        workflow_id = workflow_id or self._single_workflow()
        spec.workflow(workflow_id)
        parent = self.baseline_for(workflow_id)
        candidate = Candidate(
            candidate_id=candidate_id or new_id("cand"),
            parent_version=parent,
            change_type=ChangeType(change_type),
            delta=delta,
            rationale=rationale,
            expected_impact=expected_impact,
            proposer=proposer,
            dev_case_refs=dev_case_refs,
        )
        controller = self.workspace.controller()
        controller.propose(candidate)  # validates scope (allowed change types)
        sealed = controller.seal(candidate.candidate_id) if seal else candidate
        return ProposalResult(
            candidate=sealed,
            sealed=seal,
            content_digest=candidate.content_digest(),
        )

    def seal(self, candidate_id: str) -> Candidate:
        return self.workspace.controller().seal(candidate_id)

    def invalidate(self, candidate_id: str, reason: str) -> Candidate:
        return self.workspace.controller().invalidate(candidate_id, reason)

    # -- evaluation --------------------------------------------------------------------

    def evaluate(
        self,
        *,
        candidate_id: str,
        pack_ref: Path | str,
        split: CaseSplit,
        adapter_kind: str = ADAPTER_SCRIPTED,
        workflow_id: str | None = None,
        rubric_digest: str | None = None,
        repeats: int | None = None,
        role: Role = Role.EVALUATOR,
        fixtures_dir: Path | None = None,
        per_attempt_reserve_usd: float = 0.05,
        provider_transport: dict[str, Any] | None = None,
    ) -> EvaluationOutcome:
        """Run the paired evaluation on a development/selection split.

        ``provider_transport`` (SIMULATION ONLY) hands the Choose runner a
        loopback provider-transport configuration so the pilot path can be
        proven against a local fake provider; it is refused by the runner for
        any non-loopback endpoint and never implies live-model authorization.
        """
        if split not in (CaseSplit.DEVELOPMENT, CaseSplit.SELECTION_VALIDATION):
            raise ContractError(
                f"evaluate runs on development/selection-validation splits; "
                f"{split.value!r} requires the final-acceptance flow"
            )
        return self._run_evaluation(
            candidate_id=candidate_id,
            pack_ref=pack_ref,
            split=split,
            adapter_kind=adapter_kind,
            workflow_id=workflow_id,
            rubric_digest=rubric_digest,
            repeats=repeats,
            role=role,
            fixtures_dir=fixtures_dir,
            per_attempt_reserve_usd=per_attempt_reserve_usd,
            provider_transport=provider_transport,
        )

    def final_acceptance(
        self,
        *,
        candidate_id: str,
        pack_ref: Path | str,
        adapter_kind: str = ADAPTER_SCRIPTED,
        workflow_id: str | None = None,
        rubric_digest: str | None = None,
        role: Role = Role.ACCEPTANCE_OWNER,
        fixtures_dir: Path | None = None,
        per_attempt_reserve_usd: float = 0.05,
        provider_transport: dict[str, Any] | None = None,
    ) -> EvaluationOutcome:
        """Run the independent acceptance evaluation on the final split."""
        if role not in (Role.ACCEPTANCE_OWNER, Role.EVALUATOR):
            raise SplitAccessError(
                f"role {role.value!r} may not run final acceptance; the acceptance "
                "side (acceptance-owner or evaluator role) is required"
            )
        # Final acceptance executes the frozen rubric's actual repeats (A5):
        # a rubric demanding two repeats must not pass on a single pass.
        rubric = self.rubric_for(workflow_id or self._single_workflow(), rubric_digest)
        return self._run_evaluation(
            candidate_id=candidate_id,
            pack_ref=pack_ref,
            split=CaseSplit.FINAL_ACCEPTANCE,
            adapter_kind=adapter_kind,
            workflow_id=workflow_id,
            rubric_digest=rubric_digest,
            repeats=rubric.repeats,
            role=role,
            fixtures_dir=fixtures_dir,
            per_attempt_reserve_usd=per_attempt_reserve_usd,
            provider_transport=provider_transport,
        )

    def _run_evaluation(
        self,
        *,
        candidate_id: str,
        pack_ref: Path | str,
        split: CaseSplit,
        adapter_kind: str,
        workflow_id: str | None,
        rubric_digest: str | None,
        repeats: int | None,
        role: Role,
        fixtures_dir: Path | None,
        per_attempt_reserve_usd: float,
        provider_transport: dict[str, Any] | None = None,
    ) -> EvaluationOutcome:
        workflow_id = workflow_id or self._single_workflow()
        workflow = self.workspace.spec.workflow(workflow_id)
        controller = self.workspace.controller()
        candidate = controller.candidate(candidate_id)  # existence/state check
        pack = self.load_pack(pack_ref, role=role)
        if pack.workflow_id != workflow_id:
            raise ContractError(
                f"pack {pack.pack_id!r} belongs to workflow {pack.workflow_id!r}, "
                f"not {workflow_id!r}"
            )
        if not pack.cases_in(split):
            raise ContractError(
                f"pack {pack.pack_id!r} has no cases in split {split.value} "
                f"(visible to role {role.value})"
            )
        rubric = self.rubric_for(workflow_id, rubric_digest)
        baseline = self.baseline_for(workflow_id)
        adapter, lifecycle_client = self._build_adapter(
            adapter_kind,
            workflow,
            pack,
            fixtures_dir,
            candidate=candidate,
            provider_transport=provider_transport,
        )
        run: EvaluationRun | None = None
        try:
            if split is CaseSplit.FINAL_ACCEPTANCE:
                run = controller.run_final_acceptance(
                    workflow_id=workflow_id,
                    candidate_id=candidate_id,
                    baseline=baseline,
                    pack=pack,
                    rubric_digest=rubric.digest(),
                    adapter=adapter,
                    role=role,
                    mode=RunMode.FIXTURE,
                    per_attempt_reserve_usd=per_attempt_reserve_usd,
                )
            else:
                run = controller.run_paired_evaluation(
                    workflow_id=workflow_id,
                    candidate_id=candidate_id,
                    baseline=baseline,
                    pack=pack,
                    split=split,
                    rubric_digest=rubric.digest(),
                    adapter=adapter,
                    mode=RunMode.FIXTURE,
                    repeats=repeats if repeats is not None else rubric.repeats,
                    per_attempt_reserve_usd=per_attempt_reserve_usd,
                )
            summary = compare_run(
                run,
                workflow,
                rubric,
                expected_case_ids=tuple(c.case_id for c in pack.cases_in(split)),
            )
            run = run_with_outcomes(run, summary)
            # Persist the annotated run + comparison so later commands
            # (export, review, TUI) see exactly what was computed here.
            self.workspace.store.save(KIND_EVALUATION, run.run_id, run.to_dict())
            self.workspace.store.save(COMPARISON_KIND, run.run_id, summary.to_dict())
            if lifecycle_client is not None:
                self._seal_adapter_evidence(lifecycle_client, run.run_id)
        finally:
            # Generic subprocess lifecycle: collect already ran on the success
            # path; cleanup + close must apply on EVERY exit (success, error,
            # timeout, comparison failure, budget refusal) so no runner
            # subprocess outlives the evaluation that started it.
            if lifecycle_client is not None:
                if run is not None:
                    try:
                        lifecycle_client.cleanup(run.run_id)
                    except Exception as exc:  # cleanup is best effort, evidence kept
                        self._audit(
                            run.run_id, {"event": "adapter-cleanup-failed", "error": str(exc)}
                        )
                lifecycle_client.close()

        verdict = verdict_from_summary(summary, rubric)
        advanced = False
        if split is CaseSplit.SELECTION_VALIDATION and verdict is Verdict.ACCEPTED:
            # Only a qualifying selection-validation run promotes a sealed
            # candidate to acceptance-eligible (A5); the controller re-checks
            # the durable run before moving state.
            fresh = controller.candidate(candidate_id)
            advanced = fresh.state is CandidateState.SEALED
            if advanced:
                controller.mark_evaluated(candidate_id)
        final_candidate = controller.candidate(candidate_id)
        return EvaluationOutcome(
            run=run,
            summary=summary,
            verdict=verdict,
            candidate=final_candidate,
            advanced=advanced,
            adapter_id=adapter.describe().adapter_id,
        )

    def _build_adapter(
        self,
        adapter_kind: str,
        workflow: WorkflowDeclaration,
        pack: TaskPack,
        fixtures_dir: Path | None,
        *,
        candidate: Candidate | None = None,
        provider_transport: dict[str, Any] | None = None,
    ) -> tuple[AdapterClient, Any]:
        if adapter_kind == ADAPTER_SCRIPTED:
            return ScriptedOfflineAdapter(workflow), None
        if adapter_kind == ADAPTER_CHOOSE:
            return self._build_choose_adapter(pack, candidate, provider_transport)
        if adapter_kind != ADAPTER_FIXTURE:
            raise ContractError(
                f"unknown adapter {adapter_kind!r}; use '{ADAPTER_SCRIPTED}', "
                f"'{ADAPTER_FIXTURE}' or '{ADAPTER_CHOOSE}'"
            )
        directory = fixtures_dir or default_fixtures_dir()
        if not (directory / "pack.json").is_file():
            raise ContractError(
                f"fixture pack not found at {directory}; pass --fixtures with the "
                "fixture pack directory (repo: fixtures/choose)"
            )
        fixture_pack = FixturePack.load(directory)
        scenarios: dict[str, str] = {}
        for case in pack.cases:
            scenario = fixture_pack.scenarios.get(case.case_id)
            if scenario is None:
                raise ContractError(
                    f"case {case.case_id!r} has no scenario in fixture pack "
                    f"{fixture_pack.pack_id!r}; the fixture adapter can only replay "
                    "declared synthetic scenarios"
                )
            if scenario.workflow_id != pack.workflow_id:
                raise ContractError(
                    f"scenario {scenario.scenario_id!r} belongs to workflow "
                    f"{scenario.workflow_id!r}, pack says {pack.workflow_id!r}"
                )
            scenarios[case.case_id] = scenario.scenario_id
        workspace_dir = self.workspace.adapter_workspace
        workspace_dir.mkdir(parents=True, exist_ok=True)
        inner = ProcessAdapterClient(
            fixture_adapter_command(str(directory), str(workspace_dir)),
            execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S,
        )
        client = FixtureScenarioClient(
            inner,
            scenarios=scenarios,
            artifacts=self.workspace.artifacts,
            run_artifacts_dir=workspace_dir,
        )
        return client, client

    def _build_choose_adapter(
        self,
        pack: TaskPack,
        candidate: Candidate | None,
        provider_transport: dict[str, Any] | None = None,
    ) -> tuple[ChooseApplicationClient, ChooseApplicationClient]:
        """The REAL Choose runner driven through its application path.

        Protocol v1.2 ``apply-config``: baseline attempts run the runner's
        advertised application case under the immutable baseline (explicit
        no-op bundle), candidate attempts under the sealed candidate's
        verified change bundle — the actual delta travels, and every attempt
        comes back with a validated application receipt. Improvement evidence
        stays labeled fixture-mode (deterministic providers), never
        live-model quality.

        Everything that can refuse does so HERE, before any evaluation
        dispatch: missing checkout, missing apply-config support, pack cases
        foreign to the runner's advertised vocabulary, improvement cases
        outside the application scope, and candidate deltas that are not the
        runner's change-bundle JSON.
        """
        repo = configured_choose_repo()
        tsx = repo / "node_modules" / ".bin" / "tsx"
        if not tsx.is_file():
            raise ContractError(
                f"the Choose runner is not available at {repo} (no node_modules/.bin/tsx); "
                "set VOUCH_CHOOSE_RUNNER_DIR to a prepared Choose checkout"
            )
        runner = repo / "scripts" / "vouch" / "runner.ts"
        if not runner.is_file():
            raise ContractError(f"no Vouch runner at {runner}; check the checkout")
        inner = ProcessAdapterClient(
            [str(tsx), "scripts/vouch/runner.ts"],
            cwd=str(repo),
            execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S,
        )
        try:
            descriptor = inner.describe()
            if "apply-config" not in descriptor.actions:
                raise ContractError(
                    f"the Choose runner {descriptor.adapter_id!r} does not advertise "
                    "the apply-config action; this flow applies candidate "
                    "configurations, it never executes an unchanged agent as a "
                    "candidate"
                )
            payload = inner.describe_payload
            baseline = ChooseBaselineConfig.from_describe(payload)
            evaluation_cases = evaluation_cases_from_describe(payload)
            # Reconcile imported task packs with runner case identity: a case
            # id the runner never declared refuses before dispatch (an unknown
            # case mid-evaluation would only surface as failed attempts).
            foreign = [
                case.case_id for case in pack.cases if case.case_id not in evaluation_cases
            ]
            if foreign:
                raise ContractError(
                    f"pack case(s) {foreign} are not cases of the Choose runner "
                    f"(declared: {sorted(evaluation_cases)}); the choose adapter "
                    "executes the runner's own synthetic cases — import the "
                    "application pack with 'vouch pack --from-choose-application'"
                )
            applicable = [
                case.case_id for case in pack.cases if evaluation_cases[case.case_id].application
            ]
            if len(applicable) != len(pack.cases):
                raise application_scope_refusal(
                    evaluation_cases, tuple(case.case_id for case in pack.cases)
                )
            if candidate is None:  # pragma: no cover - controller always provides it
                raise ContractError(
                    "the choose adapter requires the sealed candidate; without its "
                    "change bundle there is nothing to apply"
                )
            bundle = parse_candidate_delta(candidate.delta)
            bundle.require_lineage(baseline)
        except Exception:
            inner.close()
            raise
        if provider_transport is not None:
            provider_transport = dict(provider_transport)
        client = ChooseApplicationClient(
            inner,
            artifacts=self.workspace.artifacts,
            baseline=baseline,
            evaluation_cases=evaluation_cases,
            application_case_id=applicable[0],
            candidate_bundle=bundle,
            provider_transport=provider_transport,
        )
        return client, client

    def _seal_adapter_evidence(self, client: Any, run_id: str) -> None:
        """Collect the subprocess's sealed artifacts and journal their digests."""
        digests = client.collect(run_id)
        self._audit(run_id, {"event": "adapter-evidence-sealed", "digests": list(digests)})
        receipts = getattr(client, "receipts_of", None)
        if receipts is not None:
            bound = receipts(run_id)
            if bound:
                self._audit(
                    run_id,
                    {
                        "event": "application-receipts-sealed",
                        "attempts": sorted(bound),
                        "receipts": [
                            {
                                "attemptId": attempt_id,
                                "requestedDeltaDigest": receipt.requested_delta_digest,
                                "appliedConfigDigest": receipt.applied_config_digest,
                                "baselineConfigDigest": receipt.baseline_config_digest,
                                "noOp": receipt.no_op,
                                "transportObserved": receipt.transport_observed,
                            }
                            for attempt_id, receipt in sorted(bound.items())
                        ],
                    },
                )

    def _audit(self, subject: str, data: dict[str, Any]) -> None:
        self.workspace.journal.append(
            EventRecord(
                event_id=new_id("evt"),
                kind=EventKind.AUDIT_NOTE,
                subject=subject,
                data=data,
                mode=RunMode.FIXTURE,
            )
        )

    # -- decision / approval / release -------------------------------------------------

    def check_acceptance_owner(self, candidate_id: str, owner: str) -> None:
        """Validate the acceptance-owner identity BEFORE any final work runs.

        The owner must be the project's configured acceptance owner and must
        differ from the candidate's proposer (any identity string, not just
        the literal "proposer"). Local CLI role selection remains an
        accountability mechanism — this labels identity honestly rather than
        authenticating a principal.
        """
        candidate = self.workspace.controller().candidate(candidate_id)
        if owner == candidate.proposer:
            raise ContractError(
                f"the proposer ({candidate.proposer!r}) cannot be the acceptance owner"
            )
        configured = self.workspace.spec.owners.get(Role.ACCEPTANCE_OWNER, "")
        if configured and owner != configured:
            raise ContractError(
                f"acceptance owner must be the project's configured acceptance owner "
                f"{configured!r}, got {owner!r}"
            )

    def record_decision(
        self,
        outcome: EvaluationOutcome,
        *,
        owner: str,
        evidence_destination: Path,
        note: str = "",
    ) -> DecisionOutcome:
        """Export evidence, then record the acceptance decision anchored to it."""
        if outcome.run.split is not CaseSplit.FINAL_ACCEPTANCE:
            raise ContractError(
                "acceptance decisions require a final-acceptance run; "
                f"{outcome.run.run_id} ran on {outcome.run.split.value}"
            )
        candidate = self.candidate_by_digest(outcome.run.candidate_digest)
        self.check_acceptance_owner(candidate.candidate_id, owner)
        from vouch_agent.appservices.reporting import ReportService  # local: no cycle

        package = ReportService(self.workspace).export_run(
            outcome.run.run_id, destination=evidence_destination
        )
        controller = self.workspace.controller()
        rubric = controller.rubric(outcome.run.rubric_digest)
        decision = decide(
            outcome.summary,
            outcome.run,
            rubric,
            owner=owner,
            evidence_digest=package.manifest_digest,
            note=note,
        )
        controller.record_decision(
            decision, role=Role.ACCEPTANCE_OWNER, final_acceptance_run=outcome.run.run_id
        )
        self.workspace.store.save(
            RUN_EXPORT_KIND,
            outcome.run.run_id,
            {"manifestDigest": package.manifest_digest, "path": str(package.path)},
        )
        return DecisionOutcome(
            decision=decision,
            package=package,
            candidate=self.candidate_by_digest(decision.candidate_digest),
        )

    def latest_decision_for(self, candidate: Candidate) -> AcceptanceDecision | None:
        digest = candidate.content_digest()
        best: AcceptanceDecision | None = None
        for decision_id in self.workspace.store.list_ids(KIND_DECISION):
            data = self.workspace.store.load(KIND_DECISION, decision_id)
            if data is None:  # pragma: no cover - id came from list_ids
                continue
            decision = AcceptanceDecision.from_dict(data)
            if decision.candidate_digest == digest and (
                best is None or decision.decided_at > best.decided_at
            ):
                best = decision
        return best

    def candidate_by_digest(self, digest: str) -> Candidate:
        for candidate_id in self.workspace.store.list_ids(KIND_CANDIDATE):
            candidate = self.workspace.controller().candidate(candidate_id)
            if candidate.content_digest() == digest:
                return candidate
        raise ContractError(f"no candidate matches digest {digest!r}")

    def approve(
        self, candidate_id: str, *, role: Role = Role.RELEASE_OWNER
    ) -> tuple[Candidate, AcceptanceDecision]:
        candidate = self.workspace.controller().candidate(candidate_id)
        decision = self.latest_decision_for(candidate)
        if decision is None or decision.binding is None:
            raise ContractError(
                f"candidate {candidate_id!r} has no accepted decision with an "
                "approval binding; acceptance must come first"
            )
        approved = self.workspace.controller().record_approval(
            candidate_id, role=role, binding=decision.binding
        )
        return approved, decision

    def record_release(
        self,
        candidate_id: str,
        *,
        deployed_version: str,
        deployed_by: str,
        observed_window: str = "",
        role: Role = Role.RELEASE_OWNER,
    ) -> tuple[Candidate, ReleaseRecord]:
        if role is not Role.RELEASE_OWNER:
            raise ContractError(
                f"role {role.value!r} may not record a release; release-owner required"
            )
        candidate = self.workspace.controller().candidate(candidate_id)
        record = ReleaseRecord(
            release_id=new_id("rel"),
            candidate_digest=candidate.content_digest(),
            deployed_version=deployed_version,
            deployed_by=deployed_by,
            observed_window=observed_window,
        )
        self.workspace.controller().record_release(record, role=role)
        released = self.workspace.controller().candidate(candidate_id)
        return released, record

    # -- recovery -----------------------------------------------------------------------

    def mark_needs_reconciliation(self, subject: str, reason: str) -> None:
        self.workspace.controller().mark_needs_reconciliation(subject, reason)

    def reconcile(
        self,
        subject: str,
        verified_note: str,
        *,
        settle_usd: float | None = None,
        release: bool = False,
    ) -> dict[str, Any]:
        """Explicitly reconcile one subject (stale reservation or run).

        Blind replay after unknown side effects is forbidden: the note must
        describe what was actually verified (query or human check). For an open
        budget reservation, ``--settle USD`` books verified spend and
        ``--release`` returns an unspent hold; one of the three (note-only for
        run subjects, settle or release for reservations) is required.
        """
        if not verified_note.strip():
            raise ReconciliationRequiredError(
                "reconciliation requires a description of what was verified "
                "(--verified-note); blind replay after unknown side effects is refused"
            )
        actions: dict[str, Any] = {}
        if settle_usd is not None or release:
            reservation = next(
                (r for r in self.workspace.ledger.reservations() if r.reservation_id == subject),
                None,
            )
            if reservation is None:
                raise ContractError(
                    f"no budget reservation {subject!r}; settle/release apply to reservations only"
                )
            if settle_usd is not None:
                settled = self.workspace.ledger.settle(subject, settle_usd)
                self.workspace.store.save(
                    KIND_RESERVATION, settled.reservation_id, settled.to_dict()
                )
                actions["settledUsd"] = settled.settled_amount_usd
            else:
                released = self.workspace.ledger.release(subject)
                self.workspace.store.save(
                    KIND_RESERVATION, released.reservation_id, released.to_dict()
                )
                actions["released"] = True
        self.workspace.controller().reconcile(subject, verified_note)
        actions["reconciledAt"] = utc_now_iso()
        return actions

    # -- helpers ------------------------------------------------------------------------

    def _single_workflow(self) -> str:
        workflows = self.workspace.spec.workflows
        if len(workflows) == 1:
            return workflows[0].workflow_id
        raise ContractError(
            "project declares multiple workflows; pass --workflow "
            f"({', '.join(w.workflow_id for w in workflows)})"
        )


def default_fixtures_dir() -> Path:
    """The in-repo Choose fixture pack, when running from a source checkout."""
    return Path(__file__).resolve().parents[3] / "fixtures" / "choose"
