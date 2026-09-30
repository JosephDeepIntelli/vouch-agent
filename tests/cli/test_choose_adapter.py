"""The Choose application path through the PUBLIC CLI (pilot path, strict).

Replaces the earlier exit-0/substring test: the real journey must show the
sealed candidate's change bundle actually applied by the REAL Choose runner
subprocess (apply-config, protocol v1.2) — changed configuration reaches the
transport and changes the semantic report, an equivalent configuration is a
verified no-op, a harmful paraphrase degrades the real validator outcome, and
every attempt binds a validated application receipt whose artifacts are
durably held. Foreign packs and unsupported scopes refuse BEFORE dispatch, no
runner subprocess outlives the command, and a FRESH PROCESS re-verifies the
exported receipt/report/source closure.

Skips (explicitly, never silently green) when no prepared Choose checkout
advertises the application scope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import _pilot_project
import pytest
from typer.testing import CliRunner

from vouch_agent.adapters.choose_bundle import (
    ChooseBaselineConfig,
    build_change_bundle,
)
from vouch_agent.adapters.process_adapter import DEFAULT_EXECUTE_TIMEOUT_S, ProcessAdapterClient
from vouch_agent.cli.main import app

runner = CliRunner()

REPO_ROOT = Path(__file__).parents[2]


def _probe_runner() -> tuple[ChooseBaselineConfig, dict] | None:
    """Describe the configured runner; None unless it advertises apply-config
    with an evaluation-case vocabulary (the contract this flow needs)."""
    repo = _pilot_project.configured_repo()
    if repo is None:
        return None
    tsx = repo / "node_modules" / ".bin" / "tsx"
    if not tsx.is_file() or not (repo / "scripts" / "vouch" / "runner.ts").is_file():
        return None
    inner = ProcessAdapterClient(
        [str(tsx), "scripts/vouch/runner.ts"],
        cwd=str(repo),
        execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S,
    )
    try:
        descriptor = inner.describe()
        if "apply-config" not in descriptor.actions:
            return None
        payload = inner.describe_payload
        if not isinstance(payload.get("evaluationCases"), list):
            return None
        return ChooseBaselineConfig.from_describe(payload), payload
    except Exception:
        return None
    finally:
        inner.close()


@pytest.fixture(scope="module")
def runner_scope() -> tuple[ChooseBaselineConfig, dict]:
    scope = _probe_runner()
    if scope is None:
        pytest.skip(
            "no prepared Choose runner advertising the application scope "
            "(VOUCH_CHOOSE_RUNNER_DIR)"
        )
    return scope


@pytest.fixture()
def baseline_config(runner_scope: tuple[ChooseBaselineConfig, dict]) -> ChooseBaselineConfig:
    return runner_scope[0]


def _init_project(directory: Path) -> Path:
    result = runner.invoke(
        app,
        [
            "init",
            "--project",
            str(directory),
            "--workflow",
            "W-C2",
            "--owners",
            "acceptance-owner=ana",
            "--owners",
            "release-owner=roger",
            "--cap",
            "2.0",
        ],
    )
    assert result.exit_code == 0, result.output
    return directory


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return _init_project(tmp_path)


def _write_bundle(path: Path, delta: dict, baseline: ChooseBaselineConfig) -> Path:
    bundle = build_change_bundle(bundle_id=f"bundle-{path.stem}", baseline=baseline, delta=delta)
    path.write_text(json.dumps(bundle.to_dict(), indent=2), encoding="utf-8")
    return path


def _prepare_candidate(
    project: Path,
    baseline: ChooseBaselineConfig,
    delta: dict,
    *,
    main_metric: str = "supportedClaims",
) -> str:
    assert (
        runner.invoke(
            app,
            [
                "baseline",
                "--project",
                str(project),
                "--version",
                "v0",
                "--source-ref",
                "git:choose",
                "--main-metric",
                main_metric,
            ],
        ).exit_code
        == 0
    )
    delta_file = _write_bundle(project / "delta.json", delta, baseline)
    propose = runner.invoke(
        app,
        [
            "propose",
            "--project",
            str(project),
            "--type",
            "prompt-delta",
            "--rationale",
            "bounded evaluation-config change",
            "--delta-file",
            str(delta_file),
            "--seal",
        ],
    )
    assert propose.exit_code == 0, propose.output
    return propose.output.split("candidate: ")[1].split()[0]


def _import_application_pack(project: Path, split: str = "development") -> str:
    pack = runner.invoke(
        app,
        [
            "pack",
            "--project",
            str(project),
            "--from-choose-application",
            "--application-split",
            split,
        ],
    )
    assert pack.exit_code == 0, pack.output
    return pack.output.split("pack: ")[1].split()[0]


def _evaluate(project: Path, candidate: str, pack_id: str, split: str = "development"):
    return runner.invoke(
        app,
        [
            "evaluate",
            "--project",
            str(project),
            "--candidate",
            candidate,
            "--pack",
            pack_id,
            "--adapter",
            "choose",
            "--split",
            split,
        ],
    )


def _load_run(project: Path, run_id: str) -> dict:
    from vouch_agent.appservices.workspace import ProjectWorkspace
    from vouch_agent.controller.service import KIND_EVALUATION

    workspace = ProjectWorkspace.open(project)
    try:
        data = workspace.store.load(KIND_EVALUATION, run_id)
        assert data is not None, f"run {run_id} missing"
        return data
    finally:
        workspace.close()


def _attempt_outputs(project: Path, run_id: str) -> dict[str, dict]:
    """attempt_id -> the sealed output artifact's parsed payload."""
    from vouch_agent.appservices.workspace import ProjectWorkspace

    workspace = ProjectWorkspace.open(project)
    try:
        run = _load_run(project, run_id)
        out: dict[str, dict] = {}
        for attempt in run["attempts"]:
            digest = attempt["outputDigest"]
            assert digest, f"attempt {attempt['attemptId']} sealed no output"
            payload = json.loads(workspace.artifacts.get(str(digest)))
            out[str(attempt["side"])] = payload
        return out
    finally:
        workspace.close()


def _no_runner_processes() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "scripts/vouch/runner.ts"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode != 0


def _wait_for_runner_exit(timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _no_runner_processes():
            return True
        time.sleep(0.2)
    return False


class TestPublicApplicationJourney:
    def test_changed_candidate_applies_and_changes_the_report(
        self, project: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        candidate = _prepare_candidate(project, baseline_config, {"conclusionStyle": "terse"})
        pack_id = _import_application_pack(project)
        evaluate = _evaluate(project, candidate, pack_id)
        assert evaluate.exit_code == 0, evaluate.output
        assert "choose-website-vouch-runner+choose-application@1" in evaluate.output
        assert "pairs: 1/1 complete" in evaluate.output
        run_id = evaluate.output.split("run: ")[1].split()[0]

        outputs = _attempt_outputs(project, run_id)
        baseline_out, candidate_out = outputs["baseline"], outputs["candidate"]
        # The ACTUAL delta differed per side: baseline ran as a verified no-op,
        # the candidate applied the terse conclusion style.
        assert baseline_out["outputs"]["application"]["noOp"] is True
        assert candidate_out["outputs"]["application"]["noOp"] is False
        assert candidate_out["outputs"]["application"]["changedFields"] == ["conclusionStyle"]
        assert (
            baseline_out["outputs"]["application"]["appliedConfigDigest"]
            != candidate_out["outputs"]["application"]["appliedConfigDigest"]
        )
        # The semantic report genuinely changed (no side-based shortcut).
        assert (
            baseline_out["outputs"]["reportArtifactDigest"]
            != candidate_out["outputs"]["reportArtifactDigest"]
        )
        # Receipts: every attempt cited the receipt artifact and it is held.
        self._assert_evidence_held(project, outputs)
        assert _wait_for_runner_exit(), "runner subprocess outlived the evaluation"

    def _assert_evidence_held(self, project: Path, outputs: dict[str, dict]) -> None:
        from vouch_agent.appservices.workspace import ProjectWorkspace

        workspace = ProjectWorkspace.open(project)
        try:
            for side, payload in outputs.items():
                receipts = [
                    ref
                    for ref in payload["evidenceRefs"]
                    if self._is_application_receipt(workspace, ref)
                ]
                assert receipts, f"{side} attempt cited no application-receipt artifact"
                for ref in payload["evidenceRefs"]:
                    assert workspace.artifacts.exists(ref), (
                        f"{side} evidence {ref} absent from the durable store"
                    )
        finally:
            workspace.close()

    def _is_application_receipt(self, workspace, digest: str) -> bool:
        raw = workspace.artifacts.get(digest)
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return False
        return record.get("artifactRole") == "application-receipt"

    def test_equivalent_configuration_is_a_verified_noop(
        self, project: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        # quotePolicy=verbatim IS the baseline value: an honest no-op.
        candidate = _prepare_candidate(project, baseline_config, {"quotePolicy": "verbatim"})
        pack_id = _import_application_pack(project)
        evaluate = _evaluate(project, candidate, pack_id)
        assert evaluate.exit_code == 0, evaluate.output
        run_id = evaluate.output.split("run: ")[1].split()[0]
        outputs = _attempt_outputs(project, run_id)
        baseline_out, candidate_out = outputs["baseline"], outputs["candidate"]
        assert candidate_out["outputs"]["application"]["noOp"] is True
        assert candidate_out["outputs"]["application"]["changedFields"] == []
        assert (
            candidate_out["outputs"]["application"]["appliedConfigDigest"]
            == baseline_out["outputs"]["application"]["appliedConfigDigest"]
        )
        # Identical effective configuration: the reports are byte-identical.
        assert (
            candidate_out["outputs"]["reportArtifactDigest"]
            == baseline_out["outputs"]["reportArtifactDigest"]
        )

    def test_harmful_candidate_degrades_through_the_real_validator(
        self, project: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        candidate = _prepare_candidate(project, baseline_config, {"quotePolicy": "paraphrase"})
        pack_id = _import_application_pack(project)
        evaluate = _evaluate(project, candidate, pack_id)
        assert evaluate.exit_code == 0, evaluate.output
        assert "verdict: rejected" in evaluate.output
        run_id = evaluate.output.split("run: ")[1].split()[0]
        outputs = _attempt_outputs(project, run_id)
        baseline_out, candidate_out = outputs["baseline"], outputs["candidate"]
        assert candidate_out["outputs"]["application"]["noOp"] is False
        # The degradation is the product's own code answering: the paraphrased
        # citation spans no longer verify, the REAL validator leaves the claims
        # non-supported, and the receipt records the degraded outcome.
        baseline_receipt = self._receipt(project, baseline_out)
        candidate_receipt = self._receipt(project, candidate_out)
        assert baseline_receipt["outcome"] == "complete"
        assert candidate_receipt["outcome"] == "partial"
        assert candidate_receipt["supportedClaimCount"] < baseline_receipt["supportedClaimCount"]
        baseline_claims = self._evidence_bytes(project, baseline_out)["claims"]
        candidate_claims = self._evidence_bytes(project, candidate_out)["claims"]
        assert all(claim["assessment"] == "supported" for claim in baseline_claims)
        assert any(claim["assessment"] != "supported" for claim in candidate_claims)
        # The honest metric dropped on the candidate side, and cannot qualify.
        assert "metric supportedClaims: baseline 3.0000 -> candidate 1.0000" in evaluate.output

    def _receipt(self, project: Path, payload: dict) -> dict:
        from vouch_agent.appservices.workspace import ProjectWorkspace

        workspace = ProjectWorkspace.open(project)
        try:
            digest = next(
                ref
                for ref in payload["evidenceRefs"]
                if json.loads(workspace.artifacts.get(ref)).get("artifactRole")
                == "application-receipt"
            )
            return json.loads(workspace.artifacts.get(digest))
        finally:
            workspace.close()

    def _evidence_bytes(self, project: Path, payload: dict) -> dict:
        from vouch_agent.appservices.workspace import ProjectWorkspace

        workspace = ProjectWorkspace.open(project)
        try:
            digest = next(
                ref
                for ref in payload["evidenceRefs"]
                if json.loads(workspace.artifacts.get(ref)).get("kind") == "evidence"
                and json.loads(workspace.artifacts.get(ref)).get("artifactRole")
                != "application-receipt"
            )
            return json.loads(workspace.artifacts.get(digest))
        finally:
            workspace.close()

    def _supported_claims(self, project: Path, payload: dict) -> int:
        evidence = self._evidence_bytes(project, payload)
        return sum(1 for claim in evidence["claims"] if claim["assessment"] == "supported")

    def test_selection_validation_and_final_acceptance_run_the_frozen_rubric(
        self, project: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        # A zero-improvement candidate (terse conclusion style) is honestly
        # ACCEPTED as non-inferiority under the frozen rubric (min-improvement
        # 0) — and the degraded paraphrase candidate is honestly rejected with
        # no promotion and no approval path.
        candidate = _prepare_candidate(project, baseline_config, {"conclusionStyle": "terse"})
        dev_pack = _import_application_pack(project, "development")
        selection_pack = _import_application_pack(project, "selection-validation")
        final_pack = _import_application_pack(project, "final-acceptance")

        dev = _evaluate(project, candidate, dev_pack)
        assert dev.exit_code == 0, dev.output
        selection = _evaluate(project, candidate, selection_pack, "selection-validation")
        assert selection.exit_code == 0, selection.output
        assert "candidate advanced to 'evaluated'" in selection.output

        accept = runner.invoke(
            app,
            [
                "accept",
                "--project",
                str(project),
                "--candidate",
                candidate,
                "--pack",
                final_pack,
                "--adapter",
                "choose",
                "--out",
                str(project / "evidence"),
                "--owner",
                "ana",
            ],
        )
        assert accept.exit_code == 0, accept.output
        assert "verdict: accepted" in accept.output
        approve = runner.invoke(
            app, ["approve", "--project", str(project), "--candidate", candidate]
        )
        assert approve.exit_code == 0, approve.output

    def test_degraded_candidate_cannot_reach_approval(
        self, tmp_path: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        # The harmful candidate's journey: selection-validation rejects it (no
        # promotion), final acceptance rejects it, and approval refuses.
        project = _init_project(tmp_path)
        harmful = _prepare_candidate(project, baseline_config, {"quotePolicy": "paraphrase"})
        selection_pack = _import_application_pack(project, "selection-validation")
        selection = _evaluate(project, harmful, selection_pack, "selection-validation")
        assert selection.exit_code == 0, selection.output
        assert "verdict: rejected" in selection.output
        assert "candidate advanced to 'evaluated'" not in selection.output
        final = _import_application_pack(project, "final-acceptance")
        accept = runner.invoke(
            app,
            [
                "accept",
                "--project",
                str(project),
                "--candidate",
                harmful,
                "--pack",
                final,
                "--adapter",
                "choose",
                "--out",
                str(project / "evidence-harmful"),
                "--owner",
                "ana",
            ],
        )
        # A selection-rejected candidate never reaches final acceptance at
        # all — the gate refuses before any acceptance work runs.
        assert accept.exit_code != 0
        assert "completed selection evaluation" in accept.output
        approve = runner.invoke(
            app, ["approve", "--project", str(project), "--candidate", harmful]
        )
        assert approve.exit_code != 0


class TestPreDispatchRefusals:
    def test_free_text_delta_refuses_before_dispatch(
        self, project: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        assert (
            runner.invoke(
                app,
                [
                    "baseline",
                    "--project",
                    str(project),
                    "--version",
                    "v0",
                    "--source-ref",
                    "git:choose",
                    "--main-metric",
                    "supportedClaims",
                ],
            ).exit_code
            == 0
        )
        delta_file = project / "delta.txt"
        delta_file.write_text("+ require a complete-cost claim", encoding="utf-8")
        propose = runner.invoke(
            app,
            [
                "propose",
                "--project",
                str(project),
                "--type",
                "prompt-delta",
                "--rationale",
                "free text",
                "--delta-file",
                str(delta_file),
                "--seal",
            ],
        )
        assert propose.exit_code == 0, propose.output
        candidate = propose.output.split("candidate: ")[1].split()[0]
        pack_id = _import_application_pack(project)
        evaluate = _evaluate(project, candidate, pack_id)
        assert evaluate.exit_code != 0
        assert "free text is never converted" in evaluate.output
        # No run was recorded: nothing executed.
        from vouch_agent.appservices.workspace import ProjectWorkspace
        from vouch_agent.controller.service import KIND_EVALUATION

        workspace = ProjectWorkspace.open(project)
        try:
            assert workspace.store.list_ids(KIND_EVALUATION) == []
        finally:
            workspace.close()
        assert _wait_for_runner_exit(), "runner subprocess leaked by a refused evaluation"

    def test_foreign_pack_case_refuses_before_dispatch(
        self, project: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        # The Vouch fixture-pack vocabulary (scenario ids) is NOT the runner's
        # case vocabulary — the exact trap the earlier test fell into.
        candidate = _prepare_candidate(project, baseline_config, {"conclusionStyle": "terse"})
        fixtures = REPO_ROOT / "fixtures" / "choose"
        pack = runner.invoke(
            app,
            [
                "pack",
                "--project",
                str(project),
                "--from-fixture",
                str(fixtures),
                "--workflow",
                "W-C2",
                "--dev",
                "1",
                "--final",
                "0",
            ],
        )
        assert pack.exit_code == 0, pack.output
        pack_id = pack.output.split("pack: ")[1].split()[0]
        evaluate = _evaluate(project, candidate, pack_id)
        assert evaluate.exit_code != 0
        assert "not cases of the Choose runner" in evaluate.output

    def test_ordinary_runner_case_is_outside_the_application_scope(
        self, project: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        candidate = _prepare_candidate(project, baseline_config, {"conclusionStyle": "terse"})
        # A pack whose case id IS a runner case, but not an APPLICATION case:
        # ordinary execution coverage must not be dressed up as application.
        pack_file = self._handmade_case_pack(project, "case_find_kettle_en")
        evaluate = _evaluate(project, candidate, str(pack_file))
        assert evaluate.exit_code != 0
        assert "outside the Choose application scope" in evaluate.output
        assert "--adapter fixture" in evaluate.output

    def _handmade_case_pack(self, project: Path, case_id: str) -> Path:
        """A pack file naming a runner case id, input pre-stored by digest.

        ``evaluate --pack <file>`` imports it through the ordinary gate (the
        case input must be held and digest-verify), so the refusal exercised
        here is the APPLICATION-scope one, not an import failure.
        """
        from vouch_agent.appservices.workspace import ProjectWorkspace
        from vouch_agent.contracts.common import canonical_json, digest_of

        payload = {
            "schemaVersion": "1",
            "caseId": case_id,
            "workflowId": "W-C2",
            "source": "test-handmade",
        }
        workspace = ProjectWorkspace.open(project)
        try:
            workspace.artifacts.put(canonical_json(payload).encode("utf-8"))
        finally:
            workspace.close()
        pack = {
            "schemaVersion": "1",
            "packId": "handmade-find",
            "workflowId": "W-C2",
            "mode": "fixture",
            "notes": "SYNTHETIC handmade pack for scope refusal testing",
            "cases": [
                {
                    "schemaVersion": "1",
                    "caseId": case_id,
                    "workflowId": "W-C2",
                    "split": "development",
                    "groupId": "handmade",
                    "inputDigest": digest_of(payload),
                    "sourceRefs": [case_id],
                    "locale": "en",
                    "market": "US",
                    "synthetic": True,
                }
            ],
        }
        pack_file = project / "handmade-pack.json"
        pack_file.write_text(json.dumps(pack), encoding="utf-8")
        return pack_file

    def test_refuses_without_checkout(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("VOUCH_CHOOSE_RUNNER_DIR", str(tmp_path / "nowhere"))
        result = runner.invoke(
            app,
            [
                "evaluate",
                "--project",
                str(tmp_path),
                "--candidate",
                "cand-missing",
                "--adapter",
                "choose",
                "--split",
                "development",
            ],
        )
        assert result.exit_code != 0


class TestFreshProcessVerification:
    def test_exported_closure_reverifies_in_a_fresh_process(
        self, project: Path, baseline_config: ChooseBaselineConfig
    ) -> None:
        candidate = _prepare_candidate(project, baseline_config, {"conclusionStyle": "terse"})
        pack_id = _import_application_pack(project)
        evaluate = _evaluate(project, candidate, pack_id)
        assert evaluate.exit_code == 0, evaluate.output
        run_id = evaluate.output.split("run: ")[1].split()[0]

        export = runner.invoke(
            app,
            [
                "export",
                "--project",
                str(project),
                "--run",
                run_id,
                "--out",
                str(project / "export"),
            ],
        )
        assert export.exit_code == 0, export.output

        # A FRESH PROCESS (no shared state with this test) re-verifies: every
        # attempt's evidence references resolve in the project store, the
        # application receipts bind the right digests, and the exported
        # package carries the receipt/report/source bytes.
        script = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, "__SRC__")
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.controller.service import KIND_EVALUATION
from vouch_agent.export import verify_package

project = Path("__PROJECT__")
run_id = "__RUN_ID__"
workspace = ProjectWorkspace.open(project)
run = workspace.store.load(KIND_EVALUATION, run_id)
assert run, "run missing"
receipts = []
for attempt in run["attempts"]:
    payload = json.loads(workspace.artifacts.get(attempt["outputDigest"]))
    application = payload["outputs"].get("application")
    assert application, f"no application block on {{attempt['attemptId']}}"
    for ref in payload["evidenceRefs"]:
        assert workspace.artifacts.exists(ref), f"missing evidence {{ref}}"
    raw = workspace.artifacts.get(payload["evidenceRefs"][0])
    record = json.loads(raw)
    if isinstance(record, dict) and record.get("artifactRole") == "application-receipt":
        receipts.append(record)
evidence_dir = project / "export"
closure = json.loads((evidence_dir / "evidence-references.json").read_text())
cited = [a for a in closure["entries"] if a.get("kind") == "attempt-evidence"]
assert cited, "export cites no attempt evidence"
missing = [a for a in cited if not a.get("available")]
assert not missing, f"unavailable evidence in export: {{missing}}"
with_receipts = [
    a for a in cited
    if b"application-receipt" in __import__("base64").b64decode(a["payloadB64"])
]
assert with_receipts, "no application receipt bytes in the export"
digest_ok = all(
    __import__("hashlib").sha256(__import__("base64").b64decode(a["payloadB64"])).hexdigest()
    == a["digest"].split(":", 1)[1]
    for a in cited
)
assert digest_ok, "exported evidence bytes do not hash to their digests"
verify_package(evidence_dir)
print("FRESH_OK", len(run["attempts"]), len(receipts), len(cited))
"""
        script = (
            script.replace("__SRC__", str(REPO_ROOT / "src"))
            .replace("__PROJECT__", str(project))
            .replace("__RUN_ID__", run_id)
        )
        env = dict(os.environ)
        env.setdefault("PYTHONPATH", str(REPO_ROOT / "src"))
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("FRESH_OK"), proc.stdout
