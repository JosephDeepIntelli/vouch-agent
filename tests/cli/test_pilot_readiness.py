"""Pilot readiness: the dry-run must be a reliable gate (2026-09-30 review).

Three reproduced defects drive these regressions, translated from the
reviewer's diagnostics (retained in the 2026-09-30 review bundle):

1. planned requests/worst-case counted ONE model request per side while the
   retained usage artifacts report 7 model calls per side — a $0.025 cap was
   advertised READY against a $0.02048 "worst case". The envelope must come
   from the runner's VERSIONED, ENFORCED work budget (max model requests per
   attempt), retries included; undefined bounds refuse.
2. replacing the baseline (v0 -> v1) left the original candidate's plan READY
   — lineage, frozen applied config, rubric, pack and tested-source identity
   must be revalidated before READY.
3. deleting the four real application-receipt artifact files left the plan
   READY because the planner trusted an output SUMMARY. Candidate-side
   receipts must resolve as bytes, validate against this candidate's own
   bundle and the advertised baseline, close their artifact chain, and be
   persisted in the plan for fresh-process review.

Plus: the owner's actual cap must be pinned into the exact provider preflight
config/digest (never a synthesized $1,000,000), a missing cap stays
blocked/unknown, numeric inputs are validated, synthetic material scope stays
labeled and blocking, and the not-implemented authorized-live mode is
advertised BLOCKED.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import _pilot_project
import pytest
from typer.testing import CliRunner

from vouch_agent.adapters.process_adapter import DEFAULT_EXECUTE_TIMEOUT_S, ProcessAdapterClient
from vouch_agent.cli.main import app

runner = CliRunner()
REPO_ROOT = Path(__file__).parents[2]

_BUILT: Path | None = None


def _reference_project() -> Path:
    """A built-on-demand evaluated project (no retained artifacts)."""
    global _BUILT
    if _BUILT is None:
        import tempfile

        repo = _pilot_project.configured_repo()
        if repo is None or not _pilot_project.runner_ready(repo):
            pytest.skip("no configured Choose runner advertising the M5b scope")
        _BUILT = _pilot_project.build_evaluated_project(
            Path(tempfile.mkdtemp(prefix="vouch-pilot-reference-")) / "handoff"
        )
    return _BUILT


def _runner_scope() -> dict | None:
    """Describe the configured runner; None unless it advertises the M5b scope."""
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
        inner.describe()
        payload = inner.describe_payload
        if not isinstance(payload.get("evaluationCases"), list):
            return None
        return payload
    except Exception:
        return None
    finally:
        inner.close()


def _envelope_of(payload: dict) -> dict | None:
    budget = payload.get("workBudget")
    if isinstance(budget, dict) and isinstance(budget.get("envelopes"), dict):
        return budget
    return None


@pytest.fixture(scope="module")
def scope() -> dict:
    payload = _runner_scope()
    if payload is None:
        pytest.skip("no prepared Choose runner advertising the application scope")
    return payload


@pytest.fixture()
def handoff(tmp_path: Path) -> Path:
    """A private copy of the built evaluated reference project."""
    project = tmp_path / "handoff"
    shutil.copytree(_reference_project(), project)
    return project


_COMPLETE_PROVIDER = {
    "provider_id": "owner-named-provider",
    "endpoint": "https://api.owner-named.example/v1/chat/completions",
    "model_id": "owner-named-model",
    "credential_name": "owner-named-credential",
    "price_table_version": "owner-price-table-2026-09",
    "input_usd_per_million_tokens": 1.0,
    "output_usd_per_million_tokens": 2.0,
    "max_retries": 0,
}


def _candidate_of(project: Path) -> str:
    from vouch_agent.appservices.workspace import ProjectWorkspace
    from vouch_agent.controller.service import KIND_CANDIDATE

    workspace = ProjectWorkspace.open(project)
    try:
        ids = workspace.store.list_ids(KIND_CANDIDATE)
        assert ids, "no candidate"
        return sorted(ids)[0]
    finally:
        workspace.close()


def _plan(project: Path, *, provider: dict, cap: float | None, repeats: int = 1) -> dict:
    from vouch_agent.appservices.workspace import ProjectWorkspace

    provider_file = project / "provider-plan.json"
    provider_file.write_text(json.dumps(provider))
    args = [
        "pilot-dryrun",
        "--project",
        str(project),
        "--candidate",
        _candidate_of(project),
        "--pack",
        "choose-application-development",
        "--provider-plan",
        str(provider_file),
        "--repeats",
        str(repeats),
    ]
    if cap is not None:
        args.extend(["--owner-cap", str(cap)])
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    workspace = ProjectWorkspace.open(project)
    try:
        record = workspace.store.load("pilot-dryrun", f"pilot-dryrun-{_candidate_of(project)}")
    finally:
        workspace.close()
    assert record is not None, "no persisted plan"
    return record


class TestEnvelope:
    def test_planned_requests_use_the_enforced_work_budget(
        self, handoff: Path, scope: dict
    ) -> None:
        budget = _envelope_of(scope)
        if budget is None:
            pytest.skip("runner does not advertise a work budget yet (pre-fix runner)")
        find = budget["envelopes"]["find"]
        record = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=5.0)
        planned = record["requests"]["planned"]
        # 1 case x 2 sides x 1 repeat x the ENFORCED max model requests per
        # attempt x (1 + 0 retries) — not 2.
        assert planned == 2 * find["maxModelRequestsPerAttempt"]
        assert record["requests"]["envelope"]["version"] == budget["version"]
        assert record["requests"]["envelope"]["maxModelRequestsPerAttempt"] == (
            find["maxModelRequestsPerAttempt"]
        )
        # The advertised worst case must EXCEED the false $0.02048: it is
        # derived from the enforced per-step token bounds.
        assert record["budget"]["worstCaseUsd"] > 0.025
        # And a $0.025 cap BLOCKS: the plan is beyond the cap.
        tight = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=0.025)
        assert tight["status"] == "blocked"
        assert any("exceeds the owner cap" in reason for reason in tight["blockedReasons"])

    def test_undefined_envelope_refuses(self, handoff: Path, monkeypatch) -> None:
        # A runner that advertises no versioned work budget yields no honest
        # worst case: the plan REFUSES instead of guessing one request/side.
        # A runner that advertises cases but no versioned work budget.
        monkeypatch.setattr(
            "vouch_agent.appservices.pilot_dryrun._describe_scope",
            lambda repo: {
                "evaluationCases": [],
                "baselineConfig": {"configVersion": "baseline-001", "digest": "sha256:" + "a" * 64},
            },
        )
        provider_file = handoff / "provider-plan.json"
        provider_file.write_text(json.dumps(_COMPLETE_PROVIDER))
        result = runner.invoke(
            app,
            [
                "pilot-dryrun",
                "--project",
                str(handoff),
                "--candidate",
                _candidate_of(handoff),
                "--pack",
                "choose-application-development",
                "--provider-plan",
                str(provider_file),
                "--owner-cap",
                "5.0",
            ],
        )
        assert result.exit_code != 0
        assert "work budget" in result.output


class TestLineageDrift:
    def test_replaced_baseline_blocks_the_plan(self, handoff: Path, scope: dict) -> None:
        record = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=50.0)
        assert record["status"] in ("ready", "blocked")
        changed = runner.invoke(
            app,
            [
                "baseline",
                "--project",
                str(handoff),
                "--version",
                "v1",
                "--source-ref",
                "git:different-baseline",
                "--main-metric",
                "supportedClaims",
            ],
        )
        assert changed.exit_code == 0, changed.output
        drifted = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=50.0)
        assert drifted["status"] == "blocked"
        assert any("baseline" in reason for reason in drifted["blockedReasons"])

    def test_rubric_and_pack_identity_are_recorded(self, handoff: Path, scope: dict) -> None:
        record = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=50.0)
        assert record["materialScope"]["casePackDigest"].startswith("sha256:")
        assert record["rubricDigest"].startswith("sha256:")
        assert record["verifiedEvidence"]["runId"]


class TestReceiptVerification:
    def test_deleted_receipt_bytes_block_the_plan(self, handoff: Path, scope: dict) -> None:
        from vouch_agent.appservices.workspace import ProjectWorkspace
        from vouch_agent.controller.service import KIND_EVALUATION

        workspace = ProjectWorkspace.open(handoff)
        deleted = 0
        try:
            for run_id in workspace.store.list_ids(KIND_EVALUATION):
                run = workspace.store.load(KIND_EVALUATION, run_id)
                for attempt in run.get("attempts", []):
                    digest = attempt.get("outputDigest")
                    if not digest:
                        continue
                    output = json.loads(workspace.artifacts.get(str(digest)))
                    for ref in output.get("evidenceRefs", []):
                        try:
                            obj = json.loads(workspace.artifacts.get(str(ref)))
                        except Exception:
                            continue
                        if obj.get("artifactRole") == "application-receipt":
                            for f in handoff.rglob(ref.split(":", 1)[1]):
                                if f.is_file():
                                    f.unlink()
                                    deleted += 1
        finally:
            workspace.close()
        assert deleted == 4, f"expected the 4 retained receipts, deleted {deleted}"
        record = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=50.0)
        assert record["status"] == "blocked"
        assert any("receipt" in reason for reason in record["blockedReasons"])

    def test_verified_candidate_receipts_are_persisted_for_fresh_review(
        self, handoff: Path, scope: dict
    ) -> None:
        record = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=50.0)
        evidence = record["verifiedEvidence"]
        candidate_attempts = evidence["candidateAttempts"]
        assert candidate_attempts, "plan persists no verified candidate receipts"
        for entry in candidate_attempts:
            assert entry["attemptId"].startswith("att")
            assert entry["receiptDigest"].startswith("sha256:")
            assert entry["requestedDeltaDigest"].startswith("sha256:")
        # Fresh process: the persisted receipt digests resolve and validate.
        code = r"""
import json, sys
sys.path.insert(0, "__SRC__")
from vouch_agent.appservices.workspace import ProjectWorkspace
w = ProjectWorkspace.open("__PROJECT__")
rec = w.store.load("pilot-dryrun", "pilot-dryrun-__CAND__")
for entry in rec["verifiedEvidence"]["candidateAttempts"]:
    receipt = json.loads(w.artifacts.get(entry["receiptDigest"]))
    assert receipt["artifactRole"] == "application-receipt"
    assert receipt["attemptId"] == entry["attemptId"]
    assert receipt["transportObserved"] is True
print("RECEIPTS_OK", len(rec["verifiedEvidence"]["candidateAttempts"]))
"""
        code = (
            code.replace("__SRC__", str(REPO_ROOT / "src"))
            .replace("__PROJECT__", str(handoff))
            .replace("__CAND__", _candidate_of(handoff))
        )
        import os
        import subprocess

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("RECEIPTS_OK"), proc.stdout


class TestCapAndInputs:
    def test_owner_cap_is_bound_into_the_preflight_config_digest(
        self, tmp_path: Path, handoff: Path, scope: dict
    ) -> None:
        a = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=5.0)
        shutil.rmtree(handoff)
        shutil.copytree(_reference_project(), handoff)
        b = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=7.0)
        assert a["budget"]["ownerCapUsd"] == 5.0
        assert b["budget"]["ownerCapUsd"] == 7.0
        assert (
            a["provider"]["preflight"]["configDigest"]
            != b["provider"]["preflight"]["configDigest"]
        ), "the preflight config digest must bind the owner's actual cap"
        assert a["provider"]["preflight"]["budget"]["spendCapUsd"] == 5.0

    def test_missing_cap_stays_blocked_and_unknown(self, handoff: Path, scope: dict) -> None:
        record = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=None)
        assert record["status"] == "blocked"
        assert record["budget"]["ownerCapUsd"] is None
        assert record["budget"]["withinCap"] is None
        assert any("spend cap" in reason for reason in record["blockedReasons"])
        assert record["budget"]["estimateOnly"] is True

    @pytest.mark.parametrize(
        "override",
        [
            {"input_usd_per_million_tokens": float("nan")},
            {"output_usd_per_million_tokens": -1},
            {"temperature": 5},
            {"max_output_tokens": 0},
            {"top_p": 1.5},
            {"max_retries": -1},
            {"request_timeout_ms": 0},
        ],
    )
    def test_invalid_numeric_inputs_refuse(
        self, handoff: Path, scope: dict, override: dict
    ) -> None:
        from vouch_agent.appservices.pilot_dryrun import ProviderPlanInput
        from vouch_agent.errors import ContractError

        with pytest.raises(ContractError):
            ProviderPlanInput.from_dict({**_COMPLETE_PROVIDER, **override})


class TestHonestLabels:
    def test_synthetic_material_scope_blocks_even_with_named_providers(
        self, handoff: Path, scope: dict
    ) -> None:
        record = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=50.0)
        assert record["status"] == "blocked"
        assert any("material scope" in reason for reason in record["blockedReasons"])
        assert record["materialScope"]["synthetic"] is True

    def test_not_implemented_authorized_live_mode_blocks(
        self, handoff: Path, scope: dict
    ) -> None:
        record = _plan(handoff, provider=_COMPLETE_PROVIDER, cap=50.0)
        modes = record["providerModes"]
        assert modes["authorizedLive"]["implemented"] is False
        assert any("authorized-live" in reason for reason in record["blockedReasons"])
