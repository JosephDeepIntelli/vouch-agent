"""The M5b pilot dry-run CLI: packaging, persistence and refusals.

The readiness SEMantics (envelope, lineage, receipts, cap binding) live in
tests/cli/test_pilot_readiness.py; these tests cover the command's packaging
behavior that stays true across both: synthetic placeholders block, the
persisted plan survives a fresh process, and a missing checkout refuses
rather than guessing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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

_SYNTHETIC_PROVIDER = {
    "provider_id": "SYNTHETIC-owner-input-required",
    "model_id": "SYNTHETIC-owner-input-required",
    "price_table_version": "SYNTHETIC-price-table-unversioned",
    "input_usd_per_million_tokens": 1.0,
    "output_usd_per_million_tokens": 2.0,
    "max_retries": 0,
}


def _ready_runner() -> bool:
    repo = _pilot_project.configured_repo()
    if repo is None:
        return False
    if not (repo / "node_modules" / ".bin" / "tsx").is_file():
        return False
    inner = ProcessAdapterClient(
        [str(repo / "node_modules" / ".bin" / "tsx"), "scripts/vouch/runner.ts"],
        cwd=str(repo),
        execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S,
    )
    try:
        inner.describe()
        payload = inner.describe_payload
        return isinstance(payload.get("evaluationCases"), list) and isinstance(
            payload.get("workBudget"), dict
        )
    except Exception:
        return False
    finally:
        inner.close()


@pytest.fixture(scope="module")
def choose_ready() -> None:
    if not _ready_runner():
        pytest.skip("no prepared Choose runner advertising the M5b scope")


@pytest.fixture()
def handoff(tmp_path: Path) -> Path:
    project = tmp_path / "handoff"
    shutil.copytree(_reference_project(), project)
    return project


def _candidate_of(project: Path) -> str:
    from vouch_agent.appservices.workspace import ProjectWorkspace
    from vouch_agent.controller.service import KIND_CANDIDATE

    workspace = ProjectWorkspace.open(project)
    try:
        return sorted(workspace.store.list_ids(KIND_CANDIDATE))[0]
    finally:
        workspace.close()


def _invoke_plan(project: Path, provider: dict, cap: float | None):
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
    ]
    if cap is not None:
        args.extend(["--owner-cap", str(cap)])
    return runner.invoke(app, args)


class TestPackaging:
    def test_synthetic_placeholders_block_and_label(
        self, handoff: Path, choose_ready: None
    ) -> None:
        result = _invoke_plan(handoff, _SYNTHETIC_PROVIDER, None)
        assert result.exit_code == 0, result.output
        assert "pilot dry-run: blocked" in result.output
        assert "provider selection pending owner input" in result.output
        assert "no explicit total spend cap" in result.output
        assert "ESTIMATE ONLY" in result.output
        assert "SYNTHETIC placeholders/materials present" in result.output
        assert "stop rule:" in result.output
        assert "graders:" in result.output

    def test_persisted_plan_survives_a_fresh_process(
        self, handoff: Path, choose_ready: None
    ) -> None:
        result = _invoke_plan(handoff, _SYNTHETIC_PROVIDER, 5.0)
        assert result.exit_code == 0, result.output
        candidate = _candidate_of(handoff)
        code = r"""
import json, sys
sys.path.insert(0, "__SRC__")
from vouch_agent.appservices.workspace import ProjectWorkspace
w = ProjectWorkspace.open("__PROJECT__")
rec = w.store.load("pilot-dryrun", "pilot-dryrun-__CAND__")
assert rec and rec["status"] == "blocked"
assert rec["requests"]["planned"] > 2, "envelope-derived count expected"
assert rec["budget"]["estimateOnly"] is False
assert rec["provider"]["preflight"]["callsNothing"] is True
assert "apiKey" not in json.dumps(rec)
print("PLAN_OK", rec["requests"]["planned"])
"""
        code = (
            code.replace("__SRC__", str(REPO_ROOT / "src"))
            .replace("__PROJECT__", str(handoff))
            .replace("__CAND__", candidate)
        )
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("PLAN_OK"), proc.stdout

    def test_no_checkout_refuses(self, handoff: Path, choose_ready: None, monkeypatch) -> None:
        monkeypatch.setenv("VOUCH_CHOOSE_RUNNER_DIR", "/nonexistent-choose")
        result = _invoke_plan(handoff, _SYNTHETIC_PROVIDER, 1.0)
        assert result.exit_code != 0
        assert "refused" in result.output or "cannot run" in result.output
