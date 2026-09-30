"""Real Visibility-runner integration (M5a transfer): Vouch's
ProcessAdapterClient driving the Visibility-owned
`scripts/vouch_transfer_runner.py` subprocess through the framed protocol.

This is the transfer proof: the SAME controller-side client, the SAME
protocol (v1 + v1.1 identity echo / separated metering / in-frame
artifacts), a SECOND domain. The case materials are digested with Vouch's
own canonical JSON — so a passing execute also proves the runner's
independent canonical-form implementation agrees byte-for-byte with the
controller's digest of the same materials.

The runner lives in the visibility-backend repo; if it is absent (checkout
missing, /tmp purged) the test SKIPS with an explicit message — an
unavailable prerequisite is a stated coverage gap, never a green checkmark.
Location override: VOUCH_VISIBILITY_RUNNER_DIR env var.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from vouch_agent.adapters.process_adapter import ProcessAdapterClient
from vouch_agent.contracts.common import RunMode, digest_of
from vouch_agent.errors import AdapterExecutionError

#: The runner location is EXPLICIT configuration — no workstation default.
#: Additional candidate directories may be supplied via
#: VOUCH_VISIBILITY_RUNNER_DIR as a colon-separated list.
_RUNNER_DIRS = tuple(
    Path(entry)
    for entry in os.environ.get("VOUCH_VISIBILITY_RUNNER_DIR", "").split(":")
    if entry.strip()
)

_LEGACY = "legacy.example.com"


def _runner_spec() -> tuple[Path, list[str]] | None:
    for directory in _RUNNER_DIRS:
        script = directory / "scripts" / "vouch_transfer_runner.py"
        if not script.is_file():
            continue
        # The runner imports the visibility package, whose modules need the
        # backend's own dependencies — prefer the checkout's venv when present.
        venv_python = directory / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.is_file() else sys.executable
        return directory, [python, str(script)]
    return None


def _product(model: str, flow: str = "0.5L/min") -> dict:
    slug = f"{model.lower()}-slug"
    return {
        "slug": slug,
        "model_number": model,
        "name_zh": "净水器",
        "name_en": "Water Filter",
        "category": "personal",
        "specs": {"flow_rate": flow, "model": model},
        "source_url": f"https://{_LEGACY}/products/{slug}.html",
    }


def _site(products: list[dict]) -> dict:
    return {
        "domain": _LEGACY,
        "crawled_at": "2026-04-10",
        "products": products,
        "certifications": [],
        "company": {"name_zh": "示例公司"},
    }


def _evidence_pack_case() -> dict:
    materials = {
        "kind": "evidence-pack",
        "pack": {
            "items": [
                {
                    "subject": "synthetic brand", "key": "filter_flow", "value": "0.5 L/min",
                    "scope": "measurement", "sourceUrl": "https://legacy.example.com/specs",
                    "sample": "lab bench, 3 units", "measuredAt": "2026-04-01",
                },
                # A measurement without sample+date must be dropped by the
                # runner's REAL library call, not echoed back.
                {
                    "subject": "synthetic brand", "key": "claim_rate", "value": "97%",
                    "scope": "measurement", "sourceUrl": "https://legacy.example.com/claims",
                },
            ],
        },
    }
    return {
        "caseId": "case_visibility_evidence_pack_vouch",
        "workflowId": "W-V1",
        "split": "synthetic",
        "locale": "en",
        "market": "carried-verbatim",
        "synthetic": True,
        "materials": materials,
        "inputDigest": digest_of(materials),
    }


@pytest.mark.slow
def test_visibility_runner_end_to_end_over_real_subprocess() -> None:
    spec = _runner_spec()
    if spec is None:
        pytest.skip(
            "Visibility transfer runner not found (visibility-backend checkout "
            "set VOUCH_VISIBILITY_RUNNER_DIR to a prepared checkout; "
            "VOUCH_VISIBILITY_RUNNER_DIR) — transfer integration not exercised here"
        )
    runner_dir, command = spec
    case = _evidence_pack_case()

    client = ProcessAdapterClient(
        command, cwd=str(runner_dir), request_timeout_s=60, execute_timeout_s=120
    )
    try:
        descriptor = client.describe()
        assert descriptor.adapter_id == "visibility-transfer-runner"
        assert descriptor.protocol_version == "1"
        assert descriptor.workflows, "Visibility workflows declared"
        assert RunMode.FIXTURE in descriptor.enforced_modes

        run_id = "vouch-visibility-transfer-1"
        client.prepare(run_id, RunMode.FIXTURE)
        execution = client.execute(
            run_id=run_id,
            attempt_id="attempt-1",
            workflow_id=case["workflowId"],
            case_input=case,
            mode=RunMode.FIXTURE,
        )
        assert execution.ok, execution.error
        outputs = execution.outputs
        # The REAL library outcome: the unqualified measurement is dropped.
        assert outputs["itemsKept"] == 1
        assert outputs["itemsDropped"] == 1
        # v1.1 §2 separation: measured-only metering, nothing priced/simulated.
        assert execution.usage is not None
        assert execution.usage.get("elapsedMsMeasured", -1) >= 0
        assert execution.usage.get("libraryCallsMeasured") == 1
        assert "costUsd" not in execution.usage
        assert "tokensScripted" not in execution.usage
        assert "creditsSimulated" not in execution.usage
        assert execution.runner_version == "visibility-transfer-runner/1"
        # The digest Vouch computed with ITS canonical JSON was accepted by the
        # runner's independent implementation — cross-impl digest agreement.
        assert outputs["materialsDigest"] == case["inputDigest"]

        digests = client.collect(run_id)
        assert digests, "evidence sealed"
        artifacts = client.collect_artifacts(run_id)
        assert artifacts, "v1.1 in-frame artifacts present"
        blob = next(b for _d, b, k in artifacts if k == "evidence")
        sealed = json.loads(blob.decode("utf-8"))
        assert sealed["itemsKept"] == 1, "sealed artifact carries the real outcome"

        client.cleanup(run_id)
    finally:
        client.close()


@pytest.mark.slow
def test_visibility_runner_refuses_tampered_case_materials() -> None:
    spec = _runner_spec()
    if spec is None:
        pytest.skip("Visibility transfer runner not found — see the end-to-end test above")
    runner_dir, command = spec
    case = _evidence_pack_case()
    tampered = json.loads(json.dumps(case))
    tampered["materials"]["pack"]["items"][0]["value"] = "9.9 L/min"  # digest now lies

    client = ProcessAdapterClient(command, cwd=str(runner_dir), request_timeout_s=60)
    try:
        run_id = "vouch-visibility-tamper-1"
        client.prepare(run_id, RunMode.FIXTURE)
        with pytest.raises(AdapterExecutionError) as excinfo:
            client.execute(
                run_id=run_id,
                attempt_id="attempt-1",
                workflow_id=tampered["workflowId"],
                case_input=tampered,
                mode=RunMode.FIXTURE,
            )
        assert "digest mismatch" in str(excinfo.value)
    finally:
        client.close()
