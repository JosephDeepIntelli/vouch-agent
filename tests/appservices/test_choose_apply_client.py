"""ChooseApplicationClient behavior (stubbed transport, real client logic).

The REAL runner integration (actual tsx subprocess, actual CLI journey,
fresh-process reload) lives in tests/cli/test_choose_adapter.py. These tests
prove the client's own trust rules: receipt validation on every field that
matters, the exact application-scope refusal, honest usage projection, and
the collect completeness gate that makes a clean exit worthless without the
bound artifacts.
"""

from __future__ import annotations

import pytest

from vouch_agent.adapters.base import AdapterDescriptor, AdapterExecution
from vouch_agent.adapters.choose_bundle import (
    ChooseBaselineConfig,
    build_change_bundle,
)
from vouch_agent.appservices.choose_apply import (
    ChooseApplicationClient,
    ChooseEvaluationCase,
    application_scope_refusal,
    evaluation_cases_from_describe,
)
from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import AdapterExecutionError, ContractError

BASELINE = ChooseBaselineConfig(
    config_version="baseline-001",
    digest="sha256:" + "a" * 64,
)
APPLICATION_CASE = "case_apply_config_kettle_en"
OTHER_CASE = "case_find_kettle_en"

CASES = {
    APPLICATION_CASE: ChooseEvaluationCase(
        case_id=APPLICATION_CASE, workflow_id="W-C2", application=True
    ),
    OTHER_CASE: ChooseEvaluationCase(
        case_id=OTHER_CASE, workflow_id="W-C2", application=False
    ),
}


def _digest_of_bytes(payload: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()


class StubInner:
    """Minimal ProcessAdapterClient stand-in returning canned frames.

    The collected artifact bytes genuinely hash to their declared digests, so
    the client's re-addressing check exercises the real path.
    """

    REPORT = b"report-bytes"
    EVIDENCE = b"evidence-bytes"
    USAGE = b"usage-bytes"

    def __init__(self, receipt_overrides: dict | None = None, fail: str | None = None) -> None:
        self.receipt_overrides = receipt_overrides or {}
        self.fail = fail
        self.prepared: list[str] = []
        self.cleaned: list[str] = []
        self.closed = False
        self.sent_bundles: list[dict] = []
        self.bundle = build_change_bundle(
            bundle_id="cand", baseline=BASELINE, delta={"conclusionStyle": "terse"}
        )
        self.collected: list[tuple[str, bytes, str]] = [
            (_digest_of_bytes(self.REPORT), self.REPORT, "report"),
            (_digest_of_bytes(self.EVIDENCE), self.EVIDENCE, "evidence"),
            (_digest_of_bytes(self.USAGE), self.USAGE, "usage"),
        ]
        self.receipt = {
            "receiptVersion": 1,
            "kind": "choose-config-application-receipt",
            "synthetic": True,
            # Replaced per-execute with the actually-sent bundle's digest.
            "requestedDeltaDigest": self.bundle.delta_digest,
            "appliedConfigDigest": "sha256:" + "2" * 64,
            "baselineConfigDigest": BASELINE.digest,
            "baselineConfigVersion": BASELINE.config_version,
            "noOp": False,
            "changedFields": ["conclusionStyle"],
            "transportObserved": True,
            "observedConfigDigest": "sha256:" + "2" * 64,
            "runnerVersion": "runner-x",
            "adapterId": "adapter-x",
            "runId": "eval-1",
            "attemptId": "att-1",
            "caseId": APPLICATION_CASE,
            "workflowId": "W-C2",
            "mode": "fixture",
            "chooseRunId": "run-1",
            "outcome": "complete",
            "supportedClaimCount": 3,
            "reportArtifactDigest": _digest_of_bytes(self.REPORT),
            "evidenceArtifactDigests": [_digest_of_bytes(self.EVIDENCE)],
            "usageArtifactDigest": _digest_of_bytes(self.USAGE),
        }

    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(adapter_id="choose-website-vouch-runner")

    def prepare(self, run_id: str, mode: RunMode) -> None:
        self.prepared.append(run_id)

    def apply_config(
        self, *, run_id, attempt_id, workflow_id, case_input, mode, change_bundle,
        provider_transport=None,
    ):
        if self.fail == "protocol":
            raise AdapterExecutionError("adapter error (vouch/x): simulated")
        self.sent_bundles.append(change_bundle)
        receipt = dict(self.receipt)
        receipt["requestedDeltaDigest"] = change_bundle["deltaDigest"]
        receipt["runId"] = run_id
        receipt["attemptId"] = attempt_id
        # Overrides land AFTER the per-execute binding so a deliberately
        # unfaithful receipt stays unfaithful.
        receipt.update(self.receipt_overrides)
        execution = AdapterExecution(
            ok=True,
            outputs={
                "caseId": case_input.get("caseId"),
                "claimCount": 3,
                "application": {"noOp": receipt["noOp"]},
            },
            evidence_refs=(
                receipt["reportArtifactDigest"],
                *receipt["evidenceArtifactDigests"],
            ),
            usage={"tokensScripted": 1000, "elapsedMsMeasured": 5},
            mode=mode,
            runner_version="runner-x",
        )
        return execution, receipt

    def collect_artifacts(self, run_id: str):
        if self.fail == "incomplete":
            # Drop one artifact the attempts cited.
            return [
                entry
                for entry in self.collected
                if entry[0] != _digest_of_bytes(self.EVIDENCE)
            ]
        return self.collected

    def cleanup(self, run_id: str) -> None:
        self.cleaned.append(run_id)

    def close(self) -> None:
        self.closed = True


class MemoryArtifacts:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, payload: bytes) -> str:
        digest = _digest_of_bytes(payload)
        self.blobs[digest] = payload
        return digest


def _client(inner: StubInner) -> ChooseApplicationClient:
    return ChooseApplicationClient(
        inner,
        artifacts=MemoryArtifacts(),
        baseline=BASELINE,
        evaluation_cases=CASES,
        application_case_id=APPLICATION_CASE,
        candidate_bundle=inner.bundle,
    )


def _execute(client: ChooseApplicationClient, side: str = "candidate"):
    return client.execute(
        run_id="eval-1",
        attempt_id="att-1",
        workflow_id="W-C2",
        case_input={"caseId": APPLICATION_CASE, "side": side},
        mode=RunMode.FIXTURE,
    )


class TestApplicationPath:
    def test_candidate_attempt_sends_the_sealed_bundle(self) -> None:
        inner = StubInner()
        client = _client(inner)
        result = _execute(client, "candidate")
        assert inner.sent_bundles[0]["deltaDigest"] == inner.bundle.delta_digest
        assert inner.sent_bundles[0]["delta"] == {"conclusionStyle": "terse"}
        assert result.outputs["application"]["noOp"] is False

    def test_baseline_attempt_sends_the_explicit_noop_bundle(self) -> None:
        inner = StubInner()
        client = _client(inner)
        # The stub's receipt must describe the no-op honestly for validation.
        inner.receipt["requestedDeltaDigest"] = client._baseline_bundle.delta_digest
        inner.receipt["noOp"] = True
        inner.receipt["changedFields"] = []
        inner.receipt["appliedConfigDigest"] = BASELINE.digest
        inner.receipt["observedConfigDigest"] = BASELINE.digest
        _execute(client, "baseline")
        assert inner.sent_bundles[0]["delta"] == {}

    def test_usage_projects_synthetic_price_and_honest_metrics(self) -> None:
        inner = StubInner()
        client = _client(inner)
        result = _execute(client, "candidate")
        assert result.usage["costUsd"] == pytest.approx(1000 * 0.000_002)
        assert "synthetic" in result.usage["priceNote"]
        assert result.usage["supportedClaims"] == 3
        assert result.usage["claimCount"] == 3

    def test_missing_side_refuses(self) -> None:
        client = _client(StubInner())
        with pytest.raises(ContractError, match="side"):
            client.execute(
                run_id="eval-1",
                attempt_id="att-1",
                workflow_id="W-C2",
                case_input={"caseId": APPLICATION_CASE},
                mode=RunMode.FIXTURE,
            )

    def test_non_application_case_refuses_even_if_it_reaches_execute(self) -> None:
        client = _client(StubInner())
        with pytest.raises(ContractError, match="outside the Choose application scope"):
            client.execute(
                run_id="eval-1",
                attempt_id="att-1",
                workflow_id="W-C2",
                case_input={"caseId": OTHER_CASE, "side": "candidate"},
                mode=RunMode.FIXTURE,
            )


class TestReceiptGate:
    @pytest.mark.parametrize(
        "overrides,message",
        [
            ({"transportObserved": False}, "transportObserved"),
            ({"observedConfigDigest": "sha256:" + "8" * 64}, "observedConfigDigest"),
            ({"baselineConfigDigest": "sha256:" + "7" * 64}, "baselineConfigDigest"),
            ({"attemptId": "att-OTHER"}, "attemptId"),
            ({"mode": "live"}, "mode"),
        ],
    )
    def test_unfaithful_receipts_fail_the_attempt(self, overrides: dict, message: str) -> None:
        client = _client(StubInner(receipt_overrides=overrides))
        with pytest.raises(ContractError, match=message):
            _execute(client, "candidate")

    def test_protocol_level_failure_propagates_unaltered(self) -> None:
        client = _client(StubInner(fail="protocol"))
        with pytest.raises(AdapterExecutionError, match="vouch/x"):
            _execute(client, "candidate")


class TestCollectCompleteness:
    def test_collect_persists_verified_bytes_and_returns_digests(self) -> None:
        inner = StubInner()
        client = _client(inner)
        _execute(client, "candidate")
        digests = client.collect("eval-1")
        assert len(digests) == 3
        artifacts = client._artifacts
        for digest in digests:
            assert digest in artifacts.blobs

    def test_missing_cited_evidence_refuses(self) -> None:
        inner = StubInner(fail="incomplete")
        client = _client(inner)
        _execute(client, "candidate")
        with pytest.raises(AdapterExecutionError, match="absent from the runner's collected"):
            client.collect("eval-1")

    def test_receipt_lookup_after_execute(self) -> None:
        client = _client(StubInner())
        _execute(client, "candidate")
        receipt = client.receipt_for("eval-1", "att-1")
        assert receipt is not None
        assert receipt.no_op is False
        assert client.receipt_for("eval-1", "att-missing") is None


class TestDescribeScope:
    def test_evaluation_cases_decode_and_dedupe(self) -> None:
        payload = {
            "evaluationCases": [
                {"caseId": APPLICATION_CASE, "workflowId": "W-C2", "application": True},
                {"caseId": OTHER_CASE, "workflowId": "W-C2"},
            ]
        }
        cases = evaluation_cases_from_describe(payload)
        assert cases[APPLICATION_CASE].application is True
        assert cases[OTHER_CASE].application is False

    def test_missing_or_empty_vocabulary_fails_closed(self) -> None:
        with pytest.raises(ContractError, match="evaluationCases"):
            evaluation_cases_from_describe({})

    def test_scope_refusal_names_the_supported_cases_exactly(self) -> None:
        error = application_scope_refusal(CASES, (OTHER_CASE,))
        text = str(error)
        assert APPLICATION_CASE in text
        assert OTHER_CASE in text
        assert "W-C2" in text
        assert "--adapter fixture" in text

    def test_describe_carries_the_scope_note(self) -> None:
        inner = StubInner()
        client = _client(inner)
        descriptor = client.describe()
        assert descriptor.adapter_id.endswith("+choose-application@1")
        assert "never live-model quality" in descriptor.notes
        assert "case_apply_config_kettle_en" in descriptor.notes
