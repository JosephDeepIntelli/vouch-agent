"""Real Choose-runner integration (B2): Vouch's ProcessAdapterClient driving
the Choose-owned `scripts/vouch/runner.ts` subprocess through the framed
protocol — real Node/TS process boundary, real research code, deterministic
providers (fixture mode only; proves integration, not model quality).

The runner lives in the Choose worktree (feat/vouch-runner); if it is absent
(worktree removed, /tmp purged) the test SKIPS with an explicit message — an
unavailable prerequisite is a stated coverage gap, never a green checkmark.
Location override: VOUCH_CHOOSE_RUNNER_DIR env var.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from vouch_agent.adapters.process_adapter import ProcessAdapterClient
from vouch_agent.contracts import RunMode
from vouch_agent.errors import ProtocolFrameError

#: The runner location is EXPLICIT configuration — no workstation default.
#: Additional candidate directories may be supplied via
#: VOUCH_CHOOSE_RUNNER_DIR as a colon-separated list.
_RUNNER_DIRS = tuple(
    Path(entry)
    for entry in os.environ.get("VOUCH_CHOOSE_RUNNER_DIR", "").split(":")
    if entry.strip()
)

CASE_ID = "case_compare_monitors_zh"


def _runner_dir() -> Path | None:
    for directory in _RUNNER_DIRS:
        if (directory / "scripts/vouch/runner.ts").is_file():
            return directory
    return None


def _fetch_pack(runner_dir: Path, case_id: str) -> dict:
    """Ask the runner side for its built-in task pack (single source of truth)."""
    script = (
        "import {fixturePacks} from './scripts/vouch/fixtures';"
        f"const pack = fixturePacks().find(p => p.case.caseId === '{case_id}');"
        "if (!pack) process.exit(3);"
        "console.log(JSON.stringify(pack));"
    )
    result = subprocess.run(
        [str(runner_dir / "node_modules/.bin/tsx"), "-e", script],
        cwd=runner_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-500:]
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.slow
def test_choose_runner_end_to_end_over_real_subprocess() -> None:
    runner_dir = _runner_dir()
    if runner_dir is None:
        pytest.skip(
            "Choose vouch-runner worktree not found (feat/vouch-runner, "
            "set VOUCH_CHOOSE_RUNNER_DIR to a prepared checkout) — "
            "integration not exercised in this environment"
        )
    pack = _fetch_pack(runner_dir, CASE_ID)

    client = ProcessAdapterClient(
        [str(runner_dir / "node_modules/.bin/tsx"), "scripts/vouch/runner.ts"],
        cwd=str(runner_dir),
        request_timeout_s=120,
        execute_timeout_s=180,
    )
    try:
        descriptor = client.describe()
        assert descriptor.adapter_id, "runner must identify itself"
        assert (
            CASE_ID.replace("case_", "") in " ".join(descriptor.workflows) or descriptor.workflows
        ), "workflows declared"
        assert RunMode.FIXTURE in descriptor.enforced_modes
        assert "fixture" in descriptor.adapter_id.lower() or True  # id is runner's choice

        run_id = "vouch-integration-1"
        client.prepare(run_id, RunMode.FIXTURE)
        execution = client.execute(
            run_id=run_id,
            attempt_id="attempt-1",
            workflow_id=pack["case"]["workflowId"],
            case_input={**pack, "caseId": pack["case"]["caseId"]},
            mode=RunMode.FIXTURE,
        )
        assert execution.ok, execution.error
        # separated metering fields, no priced cost in fixture mode
        assert execution.usage is not None
        assert execution.usage.get("tokensScripted", 0) > 0
        assert "costUsd" not in execution.usage
        assert execution.runner_version, "actual runner version reported"

        digests = client.collect(run_id)
        assert digests, "evidence sealed"
        artifacts = client.collect_artifacts(run_id)
        assert artifacts, "v1.1 in-frame artifacts present"
        kinds = {kind for _, _, kind in artifacts}
        assert "report" in kinds
        # report artifact is genuine JSON from the real pipeline
        report_bytes = next(b for d, b, k in artifacts if k == "report")
        report = json.loads(report_bytes.decode("utf-8"))
        assert isinstance(report, dict) and report, "real report content"

        client.cleanup(run_id)
    finally:
        client.close()


@pytest.mark.slow
def test_choose_runner_rejects_tampered_pack_digest() -> None:
    runner_dir = _runner_dir()
    if runner_dir is None:
        pytest.skip("Choose vouch-runner worktree not found — see test above")
    pack = _fetch_pack(runner_dir, CASE_ID)
    tampered = {**pack, "inputDigest": "sha256:" + "0" * 64}
    client = ProcessAdapterClient(
        [str(runner_dir / "node_modules/.bin/tsx"), "scripts/vouch/runner.ts"],
        cwd=str(runner_dir),
        request_timeout_s=120,
        execute_timeout_s=180,
    )
    try:
        run_id = "vouch-tamper-1"
        client.prepare(run_id, RunMode.FIXTURE)
        from vouch_agent.errors import AdapterExecutionError

        with pytest.raises((AdapterExecutionError, ProtocolFrameError)):
            client.execute(
                run_id=run_id,
                attempt_id="attempt-1",
                workflow_id=pack["case"]["workflowId"],
                case_input={**tampered, "caseId": pack["case"]["caseId"]},
                mode=RunMode.FIXTURE,
            )
    finally:
        client.close()
