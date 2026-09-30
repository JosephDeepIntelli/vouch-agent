"""Adapter protocol v1 frame-validation tests (attack-shaped inputs).

Design §4.2: unknown version, out-of-order seq, oversized payload and
malformed JSON must fail explicitly (ProtocolFrameError); missing metering
on execute results must fail (MissingMeteringError); nothing may degrade
into a success-shaped result.
"""

from __future__ import annotations

import json

import pytest

from vouch_agent.adapters.protocol import (
    MAX_DIAGNOSTICS_CHARS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    Frame,
    FrameKind,
    SequenceTracker,
    descriptor_from_payload,
    digests_from_payload,
    execution_from_payload,
    ok_from_payload,
    parse_frame,
    sanitize_diagnostics,
)
from vouch_agent.errors import (
    AdapterExecutionError,
    MissingMeteringError,
    ProtocolFrameError,
)


def make_frame(**overrides: object) -> dict:
    frame: dict = {
        "protocolVersion": PROTOCOL_VERSION,
        "seq": 0,
        "kind": "describe-request",
        "runId": None,
        "payload": {},
    }
    frame.update(overrides)
    return frame


class TestFrameParsing:
    def test_round_trip(self) -> None:
        frame = Frame(
            seq=3,
            kind=FrameKind.EXECUTE_REQUEST,
            run_id="run_1",
            payload={"attemptId": "a1"},
        )
        parsed = parse_frame(frame.to_json_line())
        assert parsed.seq == 3
        assert parsed.kind is FrameKind.EXECUTE_REQUEST
        assert parsed.run_id == "run_1"
        assert parsed.payload == {"attemptId": "a1"}

    def test_malformed_json_is_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="malformed JSON"):
            parse_frame(b"{not json")

    def test_non_object_frame_is_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="JSON object"):
            parse_frame(b"[1, 2, 3]")

    def test_empty_line_is_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="empty frame"):
            parse_frame(b"   \n")

    def test_unknown_version_is_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="unknown protocolVersion"):
            parse_frame(json.dumps(make_frame(protocolVersion="2")))

    def test_missing_version_is_rejected(self) -> None:
        payload = make_frame()
        del payload["protocolVersion"]
        with pytest.raises(ProtocolFrameError, match="unknown protocolVersion"):
            parse_frame(json.dumps(payload))

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="unknown frame kind"):
            parse_frame(json.dumps(make_frame(kind="deploy-request")))

    def test_oversized_payload_is_rejected(self) -> None:
        huge = make_frame(payload={"blob": "x" * (MAX_FRAME_BYTES)})
        with pytest.raises(ProtocolFrameError, match="exceeds MAX_FRAME_BYTES"):
            parse_frame(json.dumps(huge))

    def test_oversized_encode_is_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="exceeds MAX_FRAME_BYTES"):
            Frame(
                seq=0, kind=FrameKind.DESCRIBE_REQUEST, payload={"blob": "y" * MAX_FRAME_BYTES}
            ).to_json_line()

    def test_run_lifecycle_kinds_require_run_id(self) -> None:
        with pytest.raises(ProtocolFrameError, match="requires a non-empty runId"):
            parse_frame(json.dumps(make_frame(kind="execute-request", runId=None)))

    def test_describe_must_not_carry_run_id(self) -> None:
        with pytest.raises(ProtocolFrameError, match="must not carry a runId"):
            parse_frame(json.dumps(make_frame(runId="run_1")))

    def test_bad_seq_types_are_rejected(self) -> None:
        for bad in (-1, 1.5, "0", True, None):
            with pytest.raises(ProtocolFrameError, match="seq"):
                parse_frame(json.dumps(make_frame(seq=bad)))

    def test_non_object_payload_is_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="payload must be a JSON object"):
            parse_frame(json.dumps(make_frame(payload=[1])))


class TestSequenceValidation:
    def test_in_order_frames_pass(self) -> None:
        tracker = SequenceTracker()
        for seq in (0, 1, 2):
            tracker.observe(parse_frame(json.dumps(make_frame(seq=seq))))

    def test_seq_gap_is_rejected(self) -> None:
        tracker = SequenceTracker()
        tracker.observe(parse_frame(json.dumps(make_frame(seq=0))))
        with pytest.raises(ProtocolFrameError, match="out-of-order"):
            tracker.observe(parse_frame(json.dumps(make_frame(seq=2))))

    def test_seq_regression_is_rejected(self) -> None:
        tracker = SequenceTracker()
        tracker.observe(parse_frame(json.dumps(make_frame(seq=0))))
        tracker.observe(parse_frame(json.dumps(make_frame(seq=1))))
        with pytest.raises(ProtocolFrameError, match="out-of-order"):
            tracker.observe(parse_frame(json.dumps(make_frame(seq=1))))

    def test_first_frame_must_be_zero(self) -> None:
        tracker = SequenceTracker()
        with pytest.raises(ProtocolFrameError, match="out-of-order"):
            tracker.observe(parse_frame(json.dumps(make_frame(seq=1))))


class TestPayloadDecoders:
    def test_descriptor_decodes(self) -> None:
        descriptor = descriptor_from_payload(
            {
                "adapterId": "choose-runner-v1",
                "workflows": ["W-C2"],
                "actions": ["describe", "execute"],
                "enforcedModes": ["fixture", "offline-evaluation"],
                "notes": "n",
            }
        )
        assert descriptor.adapter_id == "choose-runner-v1"
        assert descriptor.protocol_version == "1"
        assert descriptor.enforced_modes[-1].value == "offline-evaluation"

    def test_descriptor_unknown_version_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="unknown protocolVersion"):
            descriptor_from_payload({"adapterId": "x", "protocolVersion": "9"})

    def test_descriptor_missing_id_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="adapterId"):
            descriptor_from_payload({"workflows": []})

    def test_execute_missing_usage_raises_missing_metering(self) -> None:
        with pytest.raises(MissingMeteringError):
            execution_from_payload({"ok": True, "outputs": {}})

    def test_execute_null_usage_raises_missing_metering(self) -> None:
        with pytest.raises(MissingMeteringError):
            execution_from_payload({"ok": True, "outputs": {}, "usage": None})

    def test_execute_empty_usage_raises_missing_metering(self) -> None:
        with pytest.raises(MissingMeteringError):
            execution_from_payload({"ok": True, "outputs": {}, "usage": {}})

    def test_execute_usage_without_measured_quantity_raises(self) -> None:
        with pytest.raises(MissingMeteringError):
            execution_from_payload({"ok": True, "outputs": {}, "usage": {"note": "n/a"}})

    def test_execute_metered_when_allowed_false(self) -> None:
        execution = execution_from_payload(
            {"ok": True, "outputs": {}, "usage": None}, require_metering=False
        )
        assert execution.usage is None  # immeasurable, policy decides — never zero

    def test_execute_decodes(self) -> None:
        execution = execution_from_payload(
            {
                "ok": False,
                "outputs": {"outcome": "insufficient_sources"},
                "evidenceRefs": ["sha256:" + "a" * 64],
                "toolEvents": [{"tool": "searchWeb"}],
                "usage": {"tokensIn": 10, "creditsDebited": 0},
                "error": "choose/cancelled",
                "mode": "fixture",
                "runnerVersion": "choose-runner-v1",
            }
        )
        assert execution.ok is False
        assert execution.error == "choose/cancelled"
        assert execution.usage["tokensIn"] == 10

    def test_execute_bad_ok_type_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="'ok' must be a boolean"):
            execution_from_payload({"ok": "yes", "usage": {"tokensIn": 1}})

    def test_execute_bad_evidence_digest_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="evidenceRefs"):
            execution_from_payload(
                {"ok": True, "evidenceRefs": ["not-a-digest"], "usage": {"tokensIn": 1}}
            )

    def test_execute_bad_mode_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="malformed execute payload"):
            execution_from_payload({"ok": True, "mode": "yolo", "usage": {"tokensIn": 1}})

    def test_digests_bad_shape_rejected(self) -> None:
        with pytest.raises(ProtocolFrameError, match="digests"):
            digests_from_payload({"digests": ["sha256:short"]})

    def test_digests_decode(self) -> None:
        digests = digests_from_payload({"digests": ["sha256:" + "b" * 64]})
        assert digests == ("sha256:" + "b" * 64,)

    def test_ok_payload_failure_raises_adapter_error(self) -> None:
        with pytest.raises(AdapterExecutionError, match="prepare-response failed"):
            ok_from_payload({"ok": False, "code": "x", "message": "no"}, FrameKind.PREPARE_RESPONSE)


class TestDiagnosticsSanitization:
    def test_redacts_key_value_secrets(self) -> None:
        dirty = "api_key=DUMMY authorization: Bearer abc123 token=zzz"
        clean = sanitize_diagnostics(dirty)
        assert "DUMMY" not in clean
        assert "abc123" not in clean
        assert "zzz" not in clean
        assert "[redacted]" in clean

    def test_redacts_hex_and_base64_blobs(self) -> None:
        blob = "deadbeef" * 8
        clean = sanitize_diagnostics(f"digest {blob} done")
        assert blob not in clean
        assert "[redacted-blob]" in clean

    def test_tail_limits_output(self) -> None:
        clean = sanitize_diagnostics("x" * (MAX_DIAGNOSTICS_CHARS * 2))
        assert len(clean) <= MAX_DIAGNOSTICS_CHARS + 1

    def test_empty_stays_empty(self) -> None:
        assert sanitize_diagnostics("") == ""
