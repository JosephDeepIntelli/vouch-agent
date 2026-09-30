"""WorkflowManifest tests: W-C1..W-C9 coverage, statuses, and the rule that
fixture data alone can never mark a workflow runner-integrated.

M4 A2 — one promotion authority: every promotion test drives the durable
resolver (``evaluation.attestation_evidence``) against records a REAL
controller produced (through the public controller entry points with the
acceptance port fakes). Two caller-constructed records that merely agree with
each other can no longer promote anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ACCEPTANCE = Path(__file__).resolve().parents[1] / "regression" / "acceptance"
if str(_ACCEPTANCE) not in sys.path:
    sys.path.insert(0, str(_ACCEPTANCE))

from support import (  # noqa: E402
    ChannelAdapter,
    FakeArtifacts,
    FakeJournal,
    FakeLedger,
    FakeStore,
    make_baseline,
    make_candidate,
    rubric,
    seed_inputs,
)

from vouch_agent.adapters.base import AdapterDescriptor  # noqa: E402
from vouch_agent.adapters.fixture_adapter import FixturePack  # noqa: E402
from vouch_agent.contracts.cases import CaseSplit, TaskCase, TaskPack  # noqa: E402
from vouch_agent.contracts.common import Role, RunMode, digest_bytes, digest_of  # noqa: E402
from vouch_agent.contracts.project import ProjectSpec  # noqa: E402
from vouch_agent.controller import VouchController  # noqa: E402
from vouch_agent.errors import ContractError  # noqa: E402
from vouch_agent.evaluation.attestation_evidence import descriptor_digest  # noqa: E402
from vouch_agent.gate import CapabilityBroker, GatePolicy, RiskClass  # noqa: E402
from vouch_agent.workflows.manifest import (  # noqa: E402
    DECLARED_CHOOSE_WORKFLOWS,
    DECLARED_GUARDRAIL_FAMILIES,
    CoverageLayer,
    CoverageStatus,
    RunnerIntegrationRecord,
    WorkflowManifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "choose"

ALL_JOURNEYS = tuple(f"W-C{i}" for i in range(1, 10))
ALL_GUARDRAILS = ("RG-ACCESS", "RG-PAYMENTS-CREDITS", "RG-SECURITY", "RG-STORAGE")


@pytest.fixture()
def manifest() -> WorkflowManifest:
    return WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))


class TestDeclaration:
    def test_declares_exactly_nine_choose_workflows(self) -> None:
        assert tuple(s.workflow_id for s in DECLARED_CHOOSE_WORKFLOWS) == ALL_JOURNEYS

    def test_declares_four_guardrail_families(self) -> None:
        assert tuple(s.workflow_id for s in DECLARED_GUARDRAIL_FAMILIES) == ALL_GUARDRAILS

    def test_every_workflow_declares_required_evidence(self) -> None:
        for spec in (*DECLARED_CHOOSE_WORKFLOWS, *DECLARED_GUARDRAIL_FAMILIES):
            assert spec.required_evidence, spec.workflow_id


class TestFixtureCoverage:
    def test_all_nine_workflows_fixture_covered(self, manifest: WorkflowManifest) -> None:
        for workflow_id in ALL_JOURNEYS:
            entry = manifest.entry(workflow_id)
            assert entry.status is CoverageStatus.FIXTURE_COVERED, workflow_id
            assert entry.fixture_ids, f"{workflow_id} has no fixtures"
            assert set(entry.locales) <= {"en", "zh"}

    def test_guardrail_families_present_with_statuses(self, manifest: WorkflowManifest) -> None:
        covered = {"RG-PAYMENTS-CREDITS", "RG-STORAGE"}
        for family_id in ALL_GUARDRAILS:
            entry = manifest.guardrail(family_id)
            assert entry.family == "guardrail"
            assert entry.regression_only is True
            expected = (
                CoverageStatus.FIXTURE_COVERED if family_id in covered else CoverageStatus.DECLARED
            )
            assert entry.status is expected, family_id

    def test_nothing_is_runner_integrated_from_fixture_data(
        self, manifest: WorkflowManifest
    ) -> None:
        summary = manifest.coverage_summary()
        assert summary["runner-integrated"] == []
        assert summary["unverified-integration"] == []
        assert set(summary["fixture-covered"]) >= set(ALL_JOURNEYS)

    def test_guardrails_never_optimization_targets(self, manifest: WorkflowManifest) -> None:
        targets = manifest.optimization_target_ids()
        assert set(targets) == set(ALL_JOURNEYS) - {"W-C9"}
        assert not (set(targets) & set(ALL_GUARDRAILS))

    def test_assert_declared_complete_passes(self, manifest: WorkflowManifest) -> None:
        manifest.assert_declared_complete()

    def test_assert_declared_complete_catches_missing_workflow(
        self, manifest: WorkflowManifest
    ) -> None:
        trimmed = WorkflowManifest(
            entries=tuple(e for e in manifest.entries if e.workflow_id != "W-C4"),
            guardrails=manifest.guardrails,
        )
        with pytest.raises(ContractError, match="W-C4"):
            trimmed.assert_declared_complete()

    def test_manifest_digest_is_stable_and_content_addressed(
        self, manifest: WorkflowManifest
    ) -> None:
        again = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        assert manifest.digest() == again.digest()
        assert manifest.fixture_pack_digest == again.fixture_pack_digest

    def test_round_trip_through_dict(self, manifest: WorkflowManifest) -> None:
        restored = WorkflowManifest.from_dict(json.loads(manifest.to_canonical_json()))
        assert restored.digest() == manifest.digest()
        restored.assert_declared_complete()


# -- genuine durable evidence (real controller + port fakes) ----------------------


def _make_controller_for(workflow_id: str, cap: float = 1.0):
    """A real controller whose project DECLARES ``workflow_id`` (the shared
    ``make_controller`` fixture only declares ``wf-compare``)."""
    store, artifacts, ledger, journal = FakeStore(), FakeArtifacts(), FakeLedger(cap), FakeJournal()
    project = ProjectSpec.from_dict(
        {
            "schemaVersion": "1",
            "projectId": f"proj-{workflow_id.lower()}",
            "name": "promotion authority regression",
            "workflows": [
                {
                    "schemaVersion": "1",
                    "workflowId": workflow_id,
                    "name": workflow_id,
                    "mainObjective": "score",
                }
            ],
            "owners": {"acceptance-owner": "ana", "release-owner": "roger"},
            "allowedChangeTypes": ["prompt-delta"],
            "budget": {"schemaVersion": "1", "totalUsdCap": cap},
        }
    )
    broker = CapabilityBroker(
        GatePolicy(
            allowed_actions=frozenset({"run-adapter-attempt"}),
            max_risk_class=RiskClass.R1,
            allowed_resources=frozenset(),
            resource_prefixes=("case-", "eval_"),
            max_reservation_usd=0.5,
        ),
        ledger,
        journal,
    )
    return VouchController(project, store, artifacts, ledger, journal, broker), store, artifacts


def _genuine_run(workflow_id: str):
    """A real controller-produced run for ``workflow_id`` + its durable records."""
    controller, store, artifacts = _make_controller_for(workflow_id)
    controller.init()
    selection_case = f"case-{workflow_id.lower()}-sel-1"
    final_case = f"case-{workflow_id.lower()}-fin-1"
    cases = (
        TaskCase(
            case_id=selection_case,
            workflow_id=workflow_id,
            split=CaseSplit.SELECTION_VALIDATION,
            group_id=f"family-{workflow_id}",
            input_digest=digest_of({"case": selection_case}),
        ),
        TaskCase(
            case_id=final_case,
            workflow_id=workflow_id,
            split=CaseSplit.FINAL_ACCEPTANCE,
            group_id=f"family-{workflow_id}",
            input_digest=digest_of({"case": final_case}),
        ),
    )
    pack = TaskPack(
        pack_id=f"pack-{workflow_id.lower()}",
        workflow_id=workflow_id,
        cases=cases,
        mode=RunMode.FIXTURE,
        notes="synthetic promotion-authority pack",
    )
    seed_inputs(artifacts, pack)
    controller.import_pack(pack, Role.EVALUATOR)
    controller.record_baseline(make_baseline(), workflow_id)
    candidate = make_candidate()
    controller.propose(candidate)
    controller.seal(candidate.candidate_id)
    rubric_digest = controller.freeze_rubric(rubric(), "ana")
    adapter = ChannelAdapter()
    run = controller.run_paired_evaluation(
        workflow_id=workflow_id,
        candidate_id="cand-acc",
        baseline=make_baseline(),
        pack=pack,
        split=CaseSplit.SELECTION_VALIDATION,
        rubric_digest=rubric_digest,
        adapter=adapter,
    )
    return store, artifacts, pack, run, adapter


def _record_for(
    store: Any, artifacts: Any, run: Any, adapter: ChannelAdapter
) -> RunnerIntegrationRecord:
    """A record stating EXACTLY what the durable evidence supports."""
    from vouch_agent.contracts.evaluation import AttemptRecord

    attempt_digests = []
    revision = ""
    for attempt in run.attempts:
        stored = AttemptRecord.from_dict(store.load("attempt", attempt.attempt_id))
        attempt_digests.append(stored.digest())
        sealed = json.loads(artifacts.get(stored.output_digest).decode("utf-8"))
        revision = sealed["runnerVersion"]
    return RunnerIntegrationRecord(
        workflow_id=run.workflow_id,
        runner_id=adapter.adapter_id,
        runner_descriptor_digest=descriptor_digest(adapter.describe()),
        verified_by="acceptance-owner",
        statement="controller-observed describe() + verified execution evidence",
        runner_revision=revision,
        case_pack_digest=run.case_set_digest,
        coverage_layer=CoverageLayer.LOGIC,
        provider_mode="fixture",
        verified_run_id=run.run_id,
        verified_attempt_digests=tuple(attempt_digests),
        evidence_digest=digest_bytes(b"placeholder-until-the-authority-stamps-it"),
    )


def _layer_record(
    store: Any,
    artifacts: Any,
    record_id: str,
    *,
    workflow_id: str,
    pack: TaskPack,
    case_ids: tuple[str, ...],
    revision: str,
    browser: bool,
    **overrides: Any,
) -> None:
    """Store a durable browser/control record satisfying the published
    contract (bytes for every cited artifact digest are materialized)."""
    artifact_digest = artifacts.put(b"synthetic-layer-artifact")
    record: dict[str, Any] = {
        "schemaVersion": "1",
        "workflowId": workflow_id,
        "casePackDigest": pack.digest(),
        "providerMode": "fixture",
        "sourceRevision": revision,
        "status": "passed",
        "artifactDigests": [artifact_digest],
        "journeys": [],
    }
    if browser:
        for locale in ("en", "zh"):
            for viewport in ("desktop", "mobile"):
                record["journeys"].append(
                    {
                        "journeyId": "primary",
                        "locale": locale,
                        "viewport": viewport,
                        "status": "passed",
                        "caseIds": list(case_ids),
                        "screenshotDigests": [artifact_digest],
                    }
                )
    else:
        record["journeys"].append(
            {
                "journeyId": "regression-suite",
                "status": "passed",
                "caseIds": list(case_ids),
                "screenshotDigests": [],
            }
        )
    record.update(overrides)
    store.save("browser-evidence" if browser else "control-regression", record_id, record)


class TestRunnerIntegrationIsAttestedOnly:
    def test_fixture_runner_cannot_attest(self) -> None:
        with pytest.raises(ContractError, match="fixture runner"):
            RunnerIntegrationRecord(
                workflow_id="W-C3",
                runner_id="fixture-choose-v1",
                runner_descriptor_digest="sha256:" + "a" * 64,
                verified_by="acceptance-owner",
                statement="s",
                runner_revision="git:dd2345c",
                case_pack_digest="sha256:" + "c" * 64,
                coverage_layer=CoverageLayer.LOGIC,
                verified_run_id="run_x",
                verified_attempt_digests=("sha256:" + "e" * 64,),
                evidence_digest="sha256:" + "d" * 64,
            )

    def test_unknown_workflow_cannot_attest(self) -> None:
        with pytest.raises(ContractError, match="unknown workflow"):
            RunnerIntegrationRecord(
                workflow_id="W-C99",
                runner_id="choose-runner-v1",
                runner_descriptor_digest="sha256:" + "a" * 64,
                verified_by="acceptance-owner",
                statement="s",
                runner_revision="git:dd2345c",
                case_pack_digest="sha256:" + "c" * 64,
                coverage_layer=CoverageLayer.LOGIC,
                verified_run_id="run_x",
                verified_attempt_digests=("sha256:" + "e" * 64,),
                evidence_digest="sha256:" + "d" * 64,
            )

    def test_hand_edited_runner_integrated_manifest_fails_closed(
        self, manifest: WorkflowManifest
    ) -> None:
        data = json.loads(manifest.to_canonical_json())
        for entry in data["entries"]:
            if entry["workflowId"] == "W-C3":
                entry["status"] = "runner-integrated"
        with pytest.raises(ContractError, match="without a valid"):
            WorkflowManifest.from_dict(data)

    def test_attestation_with_fixture_runner_in_json_fails_closed(
        self, manifest: WorkflowManifest
    ) -> None:
        data = json.loads(manifest.to_canonical_json())
        for entry in data["entries"]:
            if entry["workflowId"] == "W-C3":
                entry["status"] = "runner-integrated"
        data["runnerIntegrations"] = [
            {
                "schemaVersion": "1",
                "workflowId": "W-C3",
                "runnerId": "fixture-choose-v1",
                "runnerDescriptorDigest": "sha256:" + "a" * 64,
                "verifiedBy": "acceptance-owner",
                "statement": "pretend the fixture adapter is a real runner",
                "runnerRevision": "git:dd2345c",
                "casePackDigest": "sha256:" + "c" * 64,
                "coverageLayer": "logic",
                "providerMode": "fixture",
                "verifiedRunId": "run_x",
                "verifiedAttemptDigests": ["sha256:" + "e" * 64],
                "evidenceDigest": "sha256:" + "d" * 64,
            }
        ]
        with pytest.raises(ContractError, match="fixture runner"):
            WorkflowManifest.from_dict(data)


class TestOnePromotionAuthority:
    """M4 A2: promotion resolves durable evidence or refuses — an invented
    record (even one whose fields look self-consistent) never promotes."""

    def test_invented_record_cannot_promote_against_empty_store(
        self, manifest: WorkflowManifest
    ) -> None:
        invented = RunnerIntegrationRecord(
            workflow_id="W-C3",
            runner_id="invented-runner",
            runner_descriptor_digest=digest_of({"adapterId": "invented-runner"}),
            verified_by="local-review",
            statement="Synthetic claim: never executed",
            runner_revision="git:never-existed",
            case_pack_digest="sha256:" + "b" * 64,
            coverage_layer=CoverageLayer.LOGIC,
            provider_mode="authorized-live",
            verified_run_id="nonexistent-run",
            verified_attempt_digests=("sha256:" + "c" * 64,),
            evidence_digest="sha256:" + "d" * 64,
        )
        with pytest.raises(ContractError, match="no durable evaluation run"):
            manifest.with_runner_integration(
                invented,
                store=FakeStore(),
                artifacts=FakeArtifacts(),
                observed_descriptor=AdapterDescriptor(
                    adapter_id="invented-runner", workflows=("W-C3",)
                ),
            )
        assert manifest.entry("W-C3").status is CoverageStatus.FIXTURE_COVERED

    def test_regression_promotion_rejects_mismatched_evidence(
        self, manifest: WorkflowManifest
    ) -> None:
        """with_regression_coverage used to ignore its evidence argument
        entirely; contradictory revision/mode records must now refuse."""
        store, artifacts, _pack, run, adapter = _genuine_run("W-C9")
        honest = _record_for(store, artifacts, run, adapter)
        wrong_revision = RunnerIntegrationRecord(
            **{
                **honest.__dict__,
                "workflow_id": "W-C9",
                "coverage_layer": CoverageLayer.CONTROL,
                "runner_revision": "completely-different",
            }
        )
        with pytest.raises(ContractError, match="recorded exchanges"):
            manifest.with_regression_coverage(
                wrong_revision,
                store=store,
                artifacts=artifacts,
                observed_descriptor=adapter.describe(),
            )
        wrong_mode = RunnerIntegrationRecord(
            **{
                **honest.__dict__,
                "workflow_id": "W-C9",
                "coverage_layer": CoverageLayer.CONTROL,
                "provider_mode": "authorized-live",
            }
        )
        with pytest.raises(ContractError, match="never attests a higher mode"):
            manifest.with_regression_coverage(
                wrong_mode,
                store=store,
                artifacts=artifacts,
                observed_descriptor=adapter.describe(),
            )
        assert manifest.entry("W-C9").status is CoverageStatus.FIXTURE_COVERED

    def test_genuine_logic_evidence_promotes_and_binds_its_digest(self) -> None:
        manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        store, artifacts, _pack, run, adapter = _genuine_run("W-C3")
        record = _record_for(store, artifacts, run, adapter)
        promoted = manifest.with_runner_integration(
            record, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
        )
        assert promoted.effective_status("W-C3") == "runner-integrated"
        assert promoted.entry("W-C3").coverage_layer == "logic"
        assert promoted.entry("W-C3").provider_mode == "fixture"
        # the stored record carries the digest of the RESOLVED evidence doc,
        # not the placeholder the caller handed in
        stored = promoted.runner_integrations[0]
        assert stored.evidence_digest != record.evidence_digest
        assert stored.evidence_digest.startswith("sha256:")

    def test_genuine_browser_evidence_promotes_with_layer_dimensions(self) -> None:
        manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        store, artifacts, pack, run, adapter = _genuine_run("W-C3")
        record = _record_for(store, artifacts, run, adapter)
        layered = RunnerIntegrationRecord(
            **{**record.__dict__, "coverage_layer": CoverageLayer.BROWSER}
        )
        _layer_record(
            store,
            artifacts,
            "be-w-c3",
            workflow_id="W-C3",
            pack=pack,
            case_ids=tuple(a.case_id for a in run.attempts),
            revision=record.runner_revision,
            browser=True,
        )
        promoted = manifest.with_runner_integration(
            layered, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
        )
        assert promoted.effective_status("W-C3") == "runner-integrated"
        dimensions = promoted.coverage_dimensions()
        assert dimensions["W-C3"] == {
            "status": "runner-integrated",
            "layer": "browser",
            "mode": "fixture",
        }
        # dimensions are per workflow: untouched entries carry no layer claim
        assert dimensions["W-C2"] == {"status": "fixture-covered", "layer": None, "mode": None}

    def test_genuine_control_evidence_records_regression_only_coverage(self) -> None:
        manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        store, artifacts, pack, run, adapter = _genuine_run("W-C9")
        record = _record_for(store, artifacts, run, adapter)
        guard = RunnerIntegrationRecord(
            **{**record.__dict__, "coverage_layer": CoverageLayer.CONTROL}
        )
        _layer_record(
            store,
            artifacts,
            "cr-w-c9",
            workflow_id="W-C9",
            pack=pack,
            case_ids=tuple(a.case_id for a in run.attempts),
            revision=record.runner_revision,
            browser=False,
        )
        recorded = manifest.with_regression_coverage(
            guard, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
        )
        entry = recorded.entry("W-C9")
        assert entry.status is CoverageStatus.RUNNER_INTEGRATED
        assert entry.regression_only is True  # still permanently excluded
        assert entry.coverage_layer == "control"
        assert "W-C9" not in recorded.optimization_target_ids()

    def test_guardrail_regression_coverage_requires_control_layer(self) -> None:
        manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        store, artifacts, _pack, run, adapter = _genuine_run("RG-SECURITY")
        record = _record_for(store, artifacts, run, adapter)
        with pytest.raises(ContractError, match="CONTROL-layer"):
            manifest.with_regression_coverage(
                RunnerIntegrationRecord(
                    **{**record.__dict__, "coverage_layer": CoverageLayer.LOGIC}
                ),
                store=store,
                artifacts=artifacts,
                observed_descriptor=adapter.describe(),
            )

    def test_regression_only_workflow_cannot_take_runner_integration(self) -> None:
        manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        store, artifacts, _pack, run, adapter = _genuine_run("W-C9")
        record = _record_for(store, artifacts, run, adapter)
        with pytest.raises(ContractError, match="regression-only"):
            manifest.with_runner_integration(
                record, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
            )

    def test_double_attestation_rejected(self) -> None:
        manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        store, artifacts, _pack, run, adapter = _genuine_run("W-C3")
        record = _record_for(store, artifacts, run, adapter)
        promoted = manifest.with_runner_integration(
            record, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
        )
        with pytest.raises(ContractError, match="already has a runner integration"):
            promoted.with_runner_integration(
                record, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
            )

    def test_layer_mode_disagreement_fails_closed_on_reload(self) -> None:
        manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        store, artifacts, _pack, run, adapter = _genuine_run("W-C3")
        record = _record_for(store, artifacts, run, adapter)
        promoted = manifest.with_runner_integration(
            record, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
        )
        data = json.loads(promoted.to_canonical_json())
        for entry in data["entries"]:
            if entry["workflowId"] == "W-C3":
                entry["providerMode"] = "authorized-live"  # forged upgrade
        with pytest.raises(ContractError, match="disagree"):
            WorkflowManifest.from_dict(data)


class TestImportedIntegrationsAreUnverified:
    def _promoted(self) -> tuple[WorkflowManifest, Any, Any, Any]:
        manifest = WorkflowManifest.from_fixture_pack(FixturePack.load(FIXTURES))
        store, artifacts, _pack, run, adapter = _genuine_run("W-C3")
        record = _record_for(store, artifacts, run, adapter)
        promoted = manifest.with_runner_integration(
            record, store=store, artifacts=artifacts, observed_descriptor=adapter.describe()
        )
        return promoted, store, artifacts, adapter.describe()

    def test_deserialized_promotion_reports_unverified_until_resolved(self) -> None:
        promoted, _store, _artifacts, _descriptor = self._promoted()
        assert promoted.effective_status("W-C3") == "runner-integrated"
        # a round-trip through persistence drops the verified marking:
        # imported records are claims until resolved against durable state
        restored = WorkflowManifest.from_dict(json.loads(promoted.to_canonical_json()))
        assert restored.effective_status("W-C3") == "unverified-integration"
        assert restored.entry("W-C3").status.value == "runner-integrated"  # persisted claim intact

    def test_reload_and_public_summary_show_unverified_not_integrated(self) -> None:
        """The M3 reporting defect inverted: a deserialized unverified claim
        must NOT appear under runner-integrated in the summary consumers read."""
        promoted, _store, _artifacts, _descriptor = self._promoted()
        restored = WorkflowManifest.from_dict(json.loads(promoted.to_canonical_json()))
        summary = restored.coverage_summary()
        assert "W-C3" not in summary["runner-integrated"]
        assert summary["runner-integrated"] == []
        assert summary["unverified-integration"] == ["W-C3"]
        # and the in-memory verified manifest reports itself correctly
        assert promoted.coverage_summary()["runner-integrated"] == ["W-C3"]
        assert promoted.coverage_summary()["unverified-integration"] == []

    def test_verify_integrations_refuses_missing_durable_state_after_prior_verification(
        self,
    ) -> None:
        """Verification with missing/corrupted durable state fails EVEN AFTER
        a prior verification — no in-memory flag bypasses revalidation."""
        promoted, store, artifacts, descriptor = self._promoted()
        assert promoted.effective_status("W-C3") == "runner-integrated"
        verified = promoted.verify_integrations(store, artifacts, descriptor)
        assert verified.effective_status("W-C3") == "runner-integrated"
        for kind, record_id in list(store.data):
            if kind == "attempt":
                store.data.pop((kind, record_id))
        with pytest.raises(ContractError, match="no durable attempt record"):
            verified.verify_integrations(store, artifacts, descriptor)

    def test_verify_integrations_detects_edited_record_after_promotion(self) -> None:
        promoted, store, artifacts, descriptor = self._promoted()
        restored = WorkflowManifest.from_dict(json.loads(promoted.to_canonical_json()))
        reverified = restored.verify_integrations(store, artifacts, descriptor)
        assert reverified.effective_status("W-C3") == "runner-integrated"
        # edit the persisted record's evidence binding -> digest disagrees
        record = reverified.runner_integrations[0]
        tampered = RunnerIntegrationRecord(
            **{**record.__dict__, "evidence_digest": "sha256:" + "f" * 64}
        )
        carrying = WorkflowManifest(
            entries=reverified.entries,
            guardrails=reverified.guardrails,
            fixture_pack_digest=reverified.fixture_pack_digest,
            runner_integrations=(tampered,),
        )
        with pytest.raises(ContractError, match="evidence digest"):
            carrying.verify_integrations(store, artifacts, descriptor)

    def test_verify_integrations_against_empty_store_refuses(self) -> None:
        promoted, _store, _artifacts, _descriptor = self._promoted()
        restored = WorkflowManifest.from_dict(json.loads(promoted.to_canonical_json()))
        with pytest.raises(ContractError):
            restored.verify_integrations(
                FakeStore(), FakeArtifacts(), AdapterDescriptor(adapter_id="channel-fixture@1")
            )
