"""Builds the evaluated pilot-reference project through the public CLI.

Shared by the pilot dry-run/readiness tests so they need no retained
workstation artifacts: when the Choose runner is explicitly configured
(VOUCH_CHOOSE_RUNNER_DIR) and advertises the application scope, this module
builds the same evaluated project the retained handoff contained — init,
baseline, sealed terse-style candidate, application packs, development +
selection evaluations — once per test session. Not a test module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from vouch_agent.adapters.choose_bundle import ChooseBaselineConfig, build_change_bundle
from vouch_agent.adapters.process_adapter import (
    DEFAULT_EXECUTE_TIMEOUT_S,
    ProcessAdapterClient,
)
from vouch_agent.cli.main import app

runner = CliRunner()


def configured_repo() -> Path | None:
    value = os.environ.get("VOUCH_CHOOSE_RUNNER_DIR", "").strip()
    return Path(value) if value else None


def runner_ready(repo: Path | None) -> bool:
    if repo is None:
        return False
    if not (repo / "node_modules" / ".bin" / "tsx").is_file():
        return False
    if not (repo / "scripts" / "vouch" / "runner.ts").is_file():
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


def advertised_baseline(repo: Path) -> ChooseBaselineConfig:
    inner = ProcessAdapterClient(
        [str(repo / "node_modules" / ".bin" / "tsx"), "scripts/vouch/runner.ts"],
        cwd=str(repo),
        execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S,
    )
    try:
        inner.describe()
        return ChooseBaselineConfig.from_describe(inner.describe_payload)
    finally:
        inner.close()


def build_evaluated_project(destination: Path) -> Path:
    """Create an evaluated W-C2 project at destination (must not exist yet)."""
    repo = configured_repo()
    if repo is None or not runner_ready(repo):
        raise RuntimeError("Choose runner not configured/ready")

    def invoke(*args: str) -> str:
        result = runner.invoke(app, list(args))
        assert result.exit_code == 0, result.output
        return result.output

    invoke(
        "init", "--project", str(destination), "--workflow", "W-C2",
        "--owners", "acceptance-owner=ana", "--owners", "release-owner=roger",
        "--cap", "2.0",
    )
    invoke(
        "baseline", "--project", str(destination), "--version", "v0",
        "--source-ref", "git:choose", "--main-metric", "supportedClaims",
    )
    baseline = advertised_baseline(repo)
    bundle = build_change_bundle(
        bundle_id="pilot-reference", baseline=baseline, delta={"conclusionStyle": "terse"}
    )
    delta_file = destination / "delta.json"
    delta_file.write_text(json.dumps(bundle.to_dict()), encoding="utf-8")
    out = invoke(
        "propose", "--project", str(destination), "--type", "prompt-delta",
        "--rationale", "pilot reference candidate", "--delta-file", str(delta_file),
        "--seal",
    )
    candidate = out.split("candidate: ")[1].split()[0]
    invoke(
        "pack", "--project", str(destination), "--from-choose-application",
        "--application-split", "development",
    )
    invoke(
        "pack", "--project", str(destination), "--from-choose-application",
        "--application-split", "selection-validation",
    )
    dev = invoke(
        "evaluate", "--project", str(destination), "--candidate", candidate,
        "--pack", "choose-application-development", "--adapter", "choose",
        "--split", "development",
    )
    assert "pairs: 1/1 complete" in dev, dev
    selection = invoke(
        "evaluate", "--project", str(destination), "--candidate", candidate,
        "--pack", "choose-application-selection-validation", "--adapter", "choose",
        "--split", "selection-validation",
    )
    assert "pairs: 1/1 complete" in selection, selection
    return destination
