"""Fixture adapter (in-repo synthetic Choose adapter) tests.

Covers: synthetic-marker enforcement at load time, pack consistency checks,
deterministic metering/billing, and in-process server dispatch including
failure paths — the subprocess end-to-end cycle lives in
test_process_adapter.py.
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from vouch_agent.adapters.fixture_adapter import (
    DEFAULT_CREDIT_PRICE,
    FIXTURE_ADAPTER_ID,
    FixtureAdapterServer,
    FixturePack,
    FixturePackError,
    deterministic_metering,
)
from vouch_agent.adapters.protocol import Frame, FrameKind, SequenceTracker, parse_frame
from vouch_agent.contracts.common import digest_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "choose"


@pytest.fixture()
def pack() -> FixturePack:
    return FixturePack.load(FIXTURES)


class TestPackLoading:
    def test_loads_all_scenarios(self, pack: FixturePack) -> None:
        assert len(pack.scenarios) == 22
        assert pack.workflow_ids == tuple(f"W-C{i}" for i in range(1, 10))

    def test_missing_synthetic_marker_fails_closed(self, tmp_path: Path) -> None:
        source = json.loads((FIXTURES / "scenarios" / "find-normal-en-us.json").read_text())
        del source["synthetic"]
        target = tmp_path / "scenarios"
        target.mkdir()
        (target / "find-normal-en-us.json").write_text(json.dumps(source))
        shutil.copy(FIXTURES / "pack.json", tmp_path / "pack.json")
        with pytest.raises(FixturePackError, match="synthetic"):
            FixturePack.load(tmp_path)

    def test_false_synthetic_marker_fails_closed(self, tmp_path: Path) -> None:
        source = json.loads((FIXTURES / "pack.json").read_text())
        source["synthetic"] = False
        (tmp_path / "pack.json").write_text(json.dumps(source))
        (tmp_path / "scenarios").mkdir()
        with pytest.raises(FixturePackError, match="synthetic"):
            FixturePack.load(tmp_path)

    def test_every_fixture_json_is_marked_synthetic(self) -> None:
        for path in FIXTURES.rglob("*.json"):
            data = json.loads(path.read_text())
            assert data.get("synthetic") is True, f"{path} missing synthetic marker"

    def test_coverage_referencing_unknown_fixture_fails(self, tmp_path: Path) -> None:
        shutil.copytree(FIXTURES, tmp_path, dirs_exist_ok=True)
        pack_data = json.loads((tmp_path / "pack.json").read_text())
        pack_data["workflowCoverage"]["W-C2"]["fixtureIds"].append("ghost-scenario")
        (tmp_path / "pack.json").write_text(json.dumps(pack_data))
        with pytest.raises(FixturePackError, match="unknown fixture"):
            FixturePack.load(tmp_path)

    def test_fixture_under_wrong_workflow_fails(self, tmp_path: Path) -> None:
        shutil.copytree(FIXTURES, tmp_path, dirs_exist_ok=True)
        scenario_path = tmp_path / "scenarios" / "find-normal-en-us.json"
        scenario = json.loads(scenario_path.read_text())
        scenario["workflowId"] = "W-C3"
        scenario_path.write_text(json.dumps(scenario))
        with pytest.raises(FixturePackError, match="listed under"):
            FixturePack.load(tmp_path)

    def test_scenario_with_result_and_steps_fails(self, tmp_path: Path) -> None:
        shutil.copytree(FIXTURES, tmp_path, dirs_exist_ok=True)
        scenario_path = tmp_path / "scenarios" / "find-normal-en-us.json"
        scenario = json.loads(scenario_path.read_text())
        scenario["steps"] = {"x": {"outcome": "complete"}}
        scenario_path.write_text(json.dumps(scenario))
        with pytest.raises(FixturePackError, match="exactly one of 'result' or 'steps'"):
            FixturePack.load(tmp_path)

    def test_missing_pack_fails(self, tmp_path: Path) -> None:
        with pytest.raises(FixturePackError, match="fixture pack not found"):
            FixturePack.load(tmp_path)


class TestDeterministicMetering:
    def test_same_input_same_metering(self) -> None:
        case = {"scenario": "find-normal-en-us"}
        outputs = {"outcome": "complete", "sources": [{"s": 1}], "claims": [{"c": 1}]}
        assert deterministic_metering(case, outputs) == deterministic_metering(case, outputs)

    def test_billed_outcomes_debit_price(self) -> None:
        usage = deterministic_metering({}, {"outcome": "complete", "creditsPrice": 7})
        assert usage["creditsDebited"] == 7
        assert usage["creditsCurrency"] == "credits"

    def test_unbilled_outcomes_debit_zero(self) -> None:
        for outcome in ("insufficient_sources", "partial", "exhausted", ""):
            usage = deterministic_metering({}, {"outcome": outcome})
            assert usage["creditsDebited"] == 0, outcome

    def test_explicitly_unbilled_and_default_price(self) -> None:
        assert (
            deterministic_metering({}, {"outcome": "complete", "billed": False})["creditsDebited"]
            == 0
        )
        assert (
            deterministic_metering({}, {"outcome": "complete"})["creditsDebited"]
            == DEFAULT_CREDIT_PRICE
        )


class PipeBuffer:
    """A bytes buffer with independent read/write cursors (stdin stand-in).

    ``io.BytesIO`` has one shared cursor, so appending after the reader hit
    EOF either skips or overwrites data. Tests need pipe semantics.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        return len(data)

    def readline(self) -> bytes:
        if self._pos >= len(self._buf):
            return b""
        index = self._buf.find(b"\n", self._pos)
        if index == -1:
            raise ValueError("incomplete frame line pending (tests must write whole lines)")
        line = bytes(self._buf[self._pos : index + 1])
        self._pos = index + 1
        return line


class ServerScript:
    """Drives FixtureAdapterServer through in-memory byte buffers."""

    def __init__(self, pack: FixturePack, workspace: Path) -> None:
        self._stdin = PipeBuffer()
        self._stdout = io.BytesIO()
        self._stderr = io.BytesIO()
        self._send = SequenceTracker()
        self._recv = SequenceTracker()
        self._workspace = workspace
        self._out_pos = 0
        self._server = FixtureAdapterServer(
            pack,
            workspace,
            stdin=self._stdin,
            stdout=self._stdout,
            stderr=self._stderr,
        )
        self.responses: list[Frame] = []

    def send(self, kind: FrameKind, payload: dict, run_id: str | None = None) -> None:
        frame = Frame(seq=self._send.next(), kind=kind, payload=payload, run_id=run_id)
        self._stdin.write(frame.to_json_line().encode() + b"\n")

    def send_raw(self, line: bytes) -> None:
        self._stdin.write(line + b"\n")

    def serve(self) -> int:
        """Pump the server until it hits EOF; call again after more sends."""
        code = self._server.serve()
        self._stdout.seek(self._out_pos)
        for raw in self._stdout.read().splitlines():
            if raw:
                frame = parse_frame(raw)
                self._recv.observe(frame)
                self.responses.append(frame)
        self._out_pos = self._stdout.tell()
        return code

    @property
    def stderr(self) -> str:
        return self._stderr.getvalue().decode()

    def artifact(self, run_id: str, name: str) -> Path:
        path = self._workspace / "runs" / run_id / "artifacts" / name
        assert path.is_file(), f"missing artifact {path}"
        return path


@pytest.fixture()
def script(pack: FixturePack, tmp_path: Path) -> ServerScript:
    return ServerScript(pack, tmp_path / "workspace")


class TestServerDispatch:
    def test_describe_declares_synthetic_fixture_only(self, script: ServerScript) -> None:
        script.send(FrameKind.DESCRIBE_REQUEST, {})
        assert script.serve() == 0
        response = script.responses[0]
        assert response.kind is FrameKind.DESCRIBE_RESPONSE
        assert response.payload["adapterId"] == FIXTURE_ADAPTER_ID
        assert response.payload["enforcedModes"] == ["fixture"]
        assert "SYNTHETIC" in response.payload["notes"]
        assert set(response.payload["actions"]) == {
            "describe",
            "prepare",
            "execute",
            "collect",
            "cleanup",
        }

    def test_execute_before_prepare_fails(self, script: ServerScript) -> None:
        script.send(
            FrameKind.EXECUTE_REQUEST,
            {
                "workflowId": "W-C2",
                "caseInput": {"scenario": "find-normal-en-us"},
                "mode": "fixture",
            },
            run_id="run_x",
        )
        script.serve()
        error = script.responses[0]
        assert error.kind is FrameKind.ERROR
        assert error.payload["code"] == "fixture/run-not-prepared"

    def test_non_fixture_mode_fails_closed(self, script: ServerScript) -> None:
        script.send(FrameKind.PREPARE_REQUEST, {"mode": "offline-evaluation"}, run_id="run_m")
        script.serve()
        error = script.responses[0]
        assert error.kind is FrameKind.ERROR
        assert error.payload["code"] == "vouch/mode-not-enforced"
        assert "real Choose-owned runner" in error.payload["message"]

    def test_unknown_scenario_fails(self, script: ServerScript) -> None:
        script.send(FrameKind.PREPARE_REQUEST, {"mode": "fixture"}, run_id="run_u")
        script.send(
            FrameKind.EXECUTE_REQUEST,
            {"workflowId": "W-C2", "caseInput": {"scenario": "nope"}, "mode": "fixture"},
            run_id="run_u",
        )
        script.serve()
        assert script.responses[0].kind is FrameKind.PREPARE_RESPONSE
        error = script.responses[1]
        assert error.kind is FrameKind.ERROR
        assert error.payload["code"] == "fixture/unknown-scenario"

    def test_workflow_mismatch_fails(self, script: ServerScript) -> None:
        script.send(FrameKind.PREPARE_REQUEST, {"mode": "fixture"}, run_id="run_w")
        script.send(
            FrameKind.EXECUTE_REQUEST,
            {
                "workflowId": "W-C3",
                "caseInput": {"scenario": "find-normal-en-us"},
                "mode": "fixture",
            },
            run_id="run_w",
        )
        script.serve()
        assert script.responses[1].payload["code"] == "fixture/workflow-mismatch"

    def test_session_step_without_save_fails(self, script: ServerScript) -> None:
        script.send(FrameKind.PREPARE_REQUEST, {"mode": "fixture"}, run_id="run_s")
        script.send(
            FrameKind.EXECUTE_REQUEST,
            {
                "workflowId": "W-C7",
                "caseInput": {"scenario": "session-save-reopen-resume-zh-cn", "step": "resume"},
                "mode": "fixture",
            },
            run_id="run_s",
        )
        script.serve()
        assert script.responses[1].payload["code"] == "fixture/scenario-shape"

    def test_inbound_garbage_exits_nonzero_with_error_frame(self, script: ServerScript) -> None:
        script.send_raw(b"%%% not json %%%")
        assert script.serve() == 2
        assert script.responses[0].kind is FrameKind.ERROR
        assert "malformed JSON" in script.responses[0].payload["message"]
        assert "inbound protocol violation" in script.stderr

    def test_execute_then_collect_digests_match_artifacts(self, script: ServerScript) -> None:
        script.send(FrameKind.PREPARE_REQUEST, {"mode": "fixture"}, run_id="run_d")
        script.send(
            FrameKind.EXECUTE_REQUEST,
            {
                "workflowId": "W-C2",
                "caseInput": {"scenario": "find-normal-en-us"},
                "mode": "fixture",
            },
            run_id="run_d",
        )
        script.send(FrameKind.COLLECT_REQUEST, {}, run_id="run_d")
        script.serve()
        execute = script.responses[1]
        collect = script.responses[2]
        assert collect.kind is FrameKind.COLLECT_RESPONSE
        assert collect.payload["digests"] == list(execute.payload["evidenceRefs"])
        # The sealed digests are the actual bytes on disk (content addressing).
        assert execute.payload["evidenceRefs"][0] == digest_bytes(
            script.artifact("run_d", "report.json").read_bytes()
        )
        # cleanup (after sealing) removes the run workspace
        script.send(FrameKind.CLEANUP_REQUEST, {}, run_id="run_d")
        script.serve()
        assert script.responses[3].payload == {"ok": True}
        assert not (script._workspace / "runs" / "run_d").exists()
