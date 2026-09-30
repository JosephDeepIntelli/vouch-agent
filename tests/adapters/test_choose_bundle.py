"""Change-bundle and application-receipt authority (client side, offline).

These are the pre/post-dispatch trust rules for the Choose application path:
a tampered bundle, an unsupported free-text delta, a stale baseline lineage or
an unfaithful receipt must refuse — never surface as a failed attempt
mid-evaluation or, worse, as applied evidence.
"""

from __future__ import annotations

import pytest

from vouch_agent.adapters.choose_bundle import (
    ChooseBaselineConfig,
    baseline_noop_bundle,
    build_change_bundle,
    parse_candidate_delta,
    parse_change_bundle,
    receipt_from_payload,
    validate_receipt,
)
from vouch_agent.contracts.common import digest_of
from vouch_agent.errors import ContractError

BASELINE = ChooseBaselineConfig(
    config_version="baseline-001",
    digest="sha256:" + "a" * 64,
)


def _bundle_dict(delta: dict, **overrides: object) -> dict:
    bundle = build_change_bundle(
        bundle_id="bundle-test-1", baseline=BASELINE, delta=delta
    )
    raw = bundle.to_dict()
    raw.update(overrides)
    return raw


class TestParseChangeBundle:
    def test_valid_bundle_recomputes_its_own_digest(self) -> None:
        raw = _bundle_dict({"conclusionStyle": "terse"})
        bundle = parse_change_bundle(raw)
        assert bundle.delta == {"conclusionStyle": "terse"}
        assert bundle.delta_digest == raw["deltaDigest"]

    def test_digest_matches_the_runner_canonical_form(self) -> None:
        # The identity projection is exactly what Choose hashes: canonical JSON
        # over schemaVersion/kind/synthetic/bundleId/baseline lineage/delta.
        raw = _bundle_dict({"quotePolicy": "paraphrase"})
        identity = {
            "schemaVersion": 1,
            "kind": "choose-config-change-bundle",
            "synthetic": True,
            "bundleId": "bundle-test-1",
            "baselineConfigVersion": BASELINE.config_version,
            "baselineDigest": BASELINE.digest,
            "delta": {"quotePolicy": "paraphrase"},
        }
        assert raw["deltaDigest"] == digest_of(identity)

    def test_tampered_delta_fails_closed(self) -> None:
        raw = _bundle_dict({"conclusionStyle": "terse"})
        raw["delta"] = {"conclusionStyle": "paraphrase"}
        with pytest.raises(ContractError, match="config-bundle-tampered"):
            parse_change_bundle(raw)

    def test_swapped_baseline_lineage_fails_on_validation(self) -> None:
        other = ChooseBaselineConfig(
            config_version="baseline-002",
            digest="sha256:" + "b" * 64,
        )
        # A bundle genuinely cut against ANOTHER lineage parses (its digest
        # covers that lineage) — and the lineage check against the real
        # runner's advertised baseline refuses it.
        bundle = build_change_bundle(
            bundle_id="bundle-test-1", baseline=other, delta={"quotePolicy": "paraphrase"}
        )
        parsed = parse_change_bundle(bundle.to_dict())
        with pytest.raises(ContractError, match="config-baseline-mismatch"):
            parsed.require_lineage(BASELINE)

    def test_free_text_delta_is_refused_with_guidance(self) -> None:
        with pytest.raises(ContractError, match="free text is never converted"):
            parse_candidate_delta("+ require a complete-cost claim for every option")

    def test_non_bundle_json_is_refused(self) -> None:
        with pytest.raises(ContractError, match="schemaVersion"):
            parse_candidate_delta('{"note": "make it better"}')

    @pytest.mark.parametrize(
        "delta",
        [
            {"configVersion": "baseline-002"},
            {"budget": 100},
            {"apiKey": "sk-test"},
            {"unknownField": True},
            {"quotePolicy": "sometimes"},
            {"systemSupplement": 7},
        ],
    )
    def test_denied_and_unknown_fields_refuse_before_dispatch(self, delta: dict) -> None:
        with pytest.raises(ContractError):
            build_change_bundle(bundle_id="b", baseline=BASELINE, delta=delta)

    def test_missing_fields_refuse(self) -> None:
        with pytest.raises(ContractError, match="bundleId"):
            parse_change_bundle(
                {
                    "schemaVersion": 1,
                    "kind": "choose-config-change-bundle",
                    "synthetic": True,
                    "baselineConfigVersion": "baseline-001",
                    "baselineDigest": BASELINE.digest,
                    "delta": {},
                    "deltaDigest": digest_of({}),
                }
            )


class TestBaselineNoopBundle:
    def test_empty_delta_is_a_declared_noop(self) -> None:
        bundle = baseline_noop_bundle(BASELINE)
        assert bundle.delta == {}
        assert parse_change_bundle(bundle.to_dict()) == bundle


def _receipt_payload(**overrides: object) -> dict:
    receipt = {
        "receiptVersion": 1,
        "kind": "choose-config-application-receipt",
        "synthetic": True,
        "requestedDeltaDigest": "sha256:" + "1" * 64,
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
        "caseId": "case-1",
        "workflowId": "W-C2",
        "mode": "fixture",
        "chooseRunId": "run-1",
        "outcome": "complete",
        "supportedClaimCount": 3,
        "reportArtifactDigest": "sha256:" + "3" * 64,
        "evidenceArtifactDigests": ["sha256:" + "4" * 64],
        "usageArtifactDigest": "sha256:" + "5" * 64,
    }
    receipt.update(overrides)
    return {"receipt": receipt}


class TestReceiptValidation:
    def _valid(self) -> tuple[object, object]:
        bundle = build_change_bundle(
            bundle_id="b", baseline=BASELINE, delta={"conclusionStyle": "terse"}
        )
        # The receipt's requested digest must equal the sent bundle's.
        payload = _receipt_payload(requestedDeltaDigest=bundle.delta_digest)
        return bundle, payload

    def test_valid_receipt_passes(self) -> None:
        bundle, payload = self._valid()
        receipt = receipt_from_payload(payload)
        validate_receipt(
            receipt,
            bundle=bundle,
            baseline=BASELINE,
            run_id="eval-1",
            attempt_id="att-1",
            case_id="case-1",
            workflow_id="W-C2",
            mode="fixture",
            runner_version="runner-x",
        )

    def test_foreign_delta_digest_refuses(self) -> None:
        bundle, payload = self._valid()
        payload["receipt"]["requestedDeltaDigest"] = "sha256:" + "9" * 64
        receipt = receipt_from_payload(payload)
        with pytest.raises(ContractError, match="requestedDeltaDigest"):
            validate_receipt(
                receipt,
                bundle=bundle,
                baseline=BASELINE,
                run_id="eval-1",
                attempt_id="att-1",
                case_id="case-1",
                workflow_id="W-C2",
                mode="fixture",
                runner_version="runner-x",
            )

    def test_stale_baseline_refuses(self) -> None:
        bundle, payload = self._valid()
        payload["receipt"]["baselineConfigDigest"] = "sha256:" + "7" * 64
        receipt = receipt_from_payload(payload)
        with pytest.raises(ContractError, match="baselineConfigDigest"):
            validate_receipt(
                receipt,
                bundle=bundle,
                baseline=BASELINE,
                run_id="eval-1",
                attempt_id="att-1",
                case_id="case-1",
                workflow_id="W-C2",
                mode="fixture",
                runner_version="runner-x",
            )

    def test_unrelated_attempt_refuses(self) -> None:
        bundle, payload = self._valid()
        payload["receipt"]["attemptId"] = "att-OTHER"
        receipt = receipt_from_payload(payload)
        with pytest.raises(ContractError, match="attemptId"):
            validate_receipt(
                receipt,
                bundle=bundle,
                baseline=BASELINE,
                run_id="eval-1",
                attempt_id="att-1",
                case_id="case-1",
                workflow_id="W-C2",
                mode="fixture",
                runner_version="runner-x",
            )

    def test_unobserved_transport_refuses(self) -> None:
        bundle, payload = self._valid()
        payload["receipt"]["transportObserved"] = False
        receipt = receipt_from_payload(payload)
        with pytest.raises(ContractError, match="transportObserved"):
            validate_receipt(
                receipt,
                bundle=bundle,
                baseline=BASELINE,
                run_id="eval-1",
                attempt_id="att-1",
                case_id="case-1",
                workflow_id="W-C2",
                mode="fixture",
                runner_version="runner-x",
            )

    def test_observed_config_disagreeing_with_applied_refuses(self) -> None:
        bundle, payload = self._valid()
        payload["receipt"]["observedConfigDigest"] = "sha256:" + "8" * 64
        receipt = receipt_from_payload(payload)
        with pytest.raises(ContractError, match="observedConfigDigest"):
            validate_receipt(
                receipt,
                bundle=bundle,
                baseline=BASELINE,
                run_id="eval-1",
                attempt_id="att-1",
                case_id="case-1",
                workflow_id="W-C2",
                mode="fixture",
                runner_version="runner-x",
            )

    def test_missing_receipt_object_refuses(self) -> None:
        with pytest.raises(ContractError, match="no application receipt"):
            receipt_from_payload({})

    def test_wrong_receipt_kind_refuses(self) -> None:
        payload = _receipt_payload()
        payload["receipt"]["kind"] = "something-else"
        with pytest.raises(ContractError, match="kind"):
            receipt_from_payload(payload)
