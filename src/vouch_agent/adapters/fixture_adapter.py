"""Choose journey fixture adapter — protocol v1 over stdio. SYNTHETIC ONLY.

An in-repo adapter that deterministically replays SYNTHETIC Choose-journey
scenarios declared under ``fixtures/choose/``. It has no network, no Node,
no sibling-repo imports and no model: it proves the *pipeline* (framed
protocol, lifecycle, metering, evidence sealing, failure semantics) — it can
never prove anything about real model quality or product improvement
(design §3.1, §13.2).

Run as a subprocess::

    python -m vouch_agent.adapters.fixture_adapter --fixtures fixtures/choose \
        [--workspace DIR]

It answers describe/prepare/execute/collect/cleanup requests from stdin and
writes sanitized diagnostics to stderr. Every scenario file must carry
``"synthetic": true``; anything else fails closed at load time.
"""

from __future__ import annotations

import argparse
import atexit
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from vouch_agent.adapters.protocol import Frame, FrameKind, SequenceTracker, parse_frame
from vouch_agent.contracts.common import RunMode, canonical_json, digest_bytes
from vouch_agent.errors import VouchError

FIXTURE_ADAPTER_ID = "fixture-choose-v1"
FIXTURE_ACTIONS: tuple[str, ...] = ("describe", "prepare", "execute", "collect", "cleanup")
ENFORCED_MODES: tuple[RunMode, ...] = (RunMode.FIXTURE,)
DEFAULT_CREDIT_PRICE = 10
#: Outcomes Choose bills (a supported no-fit answer is a billable deliverable).
_BILLED_OUTCOMES = frozenset({"complete", "no_fit"})


class FixturePackError(VouchError):
    """A fixture pack violated the synthetic-marker/consistency contract."""

    code = "vouch/fixture-pack"


@dataclass(frozen=True)
class Scenario:
    """One declarative synthetic journey step-set."""

    scenario_id: str
    workflow_id: str
    journey: str
    locale: str
    market: str
    currency: str
    case: dict[str, Any]
    result: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    behavior: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None


@dataclass(frozen=True)
class GuardrailFamily:
    """Regression-only guardrail family (never an optimization target)."""

    family_id: str
    name: str
    fixture_ids: tuple[str, ...]
    notes: str = ""


@dataclass(frozen=True)
class FixturePack:
    """The loaded, validated synthetic fixture pack."""

    pack_id: str
    notes: str
    scenarios: dict[str, Scenario]
    workflow_coverage: dict[str, dict[str, Any]]
    guardrail_families: tuple[GuardrailFamily, ...]

    @property
    def workflow_ids(self) -> tuple[str, ...]:
        seen = {s.workflow_id for s in self.scenarios.values()}
        seen.update(self.workflow_coverage)
        return tuple(sorted(seen))

    @classmethod
    def load(cls, fixtures_dir: str | Path) -> FixturePack:
        root = Path(fixtures_dir)
        pack_path = root / "pack.json"
        if not pack_path.is_file():
            raise FixturePackError(f"fixture pack not found at {pack_path}")
        pack_data = _load_json_marked_synthetic(pack_path)
        if pack_data.get("schemaVersion") != "1":
            raise FixturePackError(f"{pack_path}: unsupported pack schemaVersion")

        scenarios: dict[str, Scenario] = {}
        scenarios_dir = root / "scenarios"
        if not scenarios_dir.is_dir():
            raise FixturePackError(f"{scenarios_dir}: scenarios directory is missing")
        for path in sorted(scenarios_dir.glob("*.json")):
            scenario = _load_scenario(path)
            if scenario.scenario_id in scenarios:
                raise FixturePackError(f"duplicate scenarioId {scenario.scenario_id!r}")
            scenarios[scenario.scenario_id] = scenario

        coverage = _load_coverage(pack_data, scenarios)
        guardrails = _load_guardrails(pack_data, scenarios)
        return cls(
            pack_id=str(pack_data.get("packId", "choose-synthetic")),
            notes=str(pack_data.get("notes", "")),
            scenarios=scenarios,
            workflow_coverage=coverage,
            guardrail_families=guardrails,
        )


def _load_json_marked_synthetic(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixturePackError(f"{path}: unreadable JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise FixturePackError(f"{path}: top level must be a JSON object")
    if data.get("synthetic") is not True:
        raise FixturePackError(
            f'{path}: missing "synthetic": true — fixture packs must be explicitly synthetic'
        )
    return data


def _load_scenario(path: Path) -> Scenario:
    data = _load_json_marked_synthetic(path)
    for key in ("scenarioId", "workflowId", "journey", "locale", "market", "currency"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FixturePackError(f"{path}: field {key!r} must be a non-empty string")
    if data["locale"] not in ("en", "zh"):
        raise FixturePackError(f"{path}: locale must be 'en' or 'zh'")
    case = data.get("case")
    if not isinstance(case, dict):
        raise FixturePackError(f"{path}: 'case' must be an object")
    result = data.get("result", {})
    steps = data.get("steps", {})
    if not isinstance(result, dict) or not isinstance(steps, dict):
        raise FixturePackError(f"{path}: 'result' and 'steps' must be objects")
    if bool(result) == bool(steps):
        raise FixturePackError(f"{path}: exactly one of 'result' or 'steps' is required")
    behavior = data.get("behavior", {})
    if not isinstance(behavior, dict):
        raise FixturePackError(f"{path}: 'behavior' must be an object")
    mode = behavior.get("mode", "ok")
    if mode not in ("ok", "fail", "stall"):
        raise FixturePackError(f"{path}: behavior.mode must be ok|fail|stall, got {mode!r}")
    if mode == "fail" and not isinstance(behavior.get("code"), str):
        raise FixturePackError(f"{path}: behavior.mode 'fail' requires a 'code'")
    return Scenario(
        scenario_id=data["scenarioId"],
        workflow_id=data["workflowId"],
        journey=data["journey"],
        locale=data["locale"],
        market=data["market"],
        currency=data["currency"],
        case=case,
        result=result,
        steps=steps,
        behavior=behavior,
        path=path,
    )


def _load_coverage(
    pack_data: dict[str, Any], scenarios: dict[str, Scenario]
) -> dict[str, dict[str, Any]]:
    coverage = pack_data.get("workflowCoverage")
    if not isinstance(coverage, dict) or not coverage:
        raise FixturePackError("pack.json: 'workflowCoverage' must be a non-empty object")
    for workflow_id, entry in coverage.items():
        if not isinstance(entry, dict):
            raise FixturePackError(f"workflowCoverage[{workflow_id!r}] must be an object")
        fixture_ids = entry.get("fixtureIds", [])
        if not isinstance(fixture_ids, list) or not fixture_ids:
            raise FixturePackError(f"workflowCoverage[{workflow_id!r}] needs fixtureIds")
        for fixture_id in fixture_ids:
            scenario = scenarios.get(fixture_id)
            if scenario is None:
                raise FixturePackError(
                    f"workflowCoverage[{workflow_id!r}] references unknown fixture {fixture_id!r}"
                )
            if scenario.workflow_id != workflow_id:
                raise FixturePackError(
                    f"fixture {fixture_id!r} declares workflowId {scenario.workflow_id!r} "
                    f"but is listed under {workflow_id!r}"
                )
    return coverage


def _load_guardrails(
    pack_data: dict[str, Any], scenarios: dict[str, Scenario]
) -> tuple[GuardrailFamily, ...]:
    raw = pack_data.get("guardrailFamilies", [])
    if not isinstance(raw, list):
        raise FixturePackError("pack.json: 'guardrailFamilies' must be a list")
    families: list[GuardrailFamily] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise FixturePackError("guardrailFamilies entries must be objects")
        family_id = entry.get("familyId")
        if not isinstance(family_id, str) or not family_id:
            raise FixturePackError("guardrail family needs a familyId")
        fixture_ids = entry.get("fixtureIds", [])
        if not isinstance(fixture_ids, list):
            raise FixturePackError(f"guardrail family {family_id!r}: fixtureIds must be a list")
        for fixture_id in fixture_ids:
            if fixture_id not in scenarios:
                raise FixturePackError(
                    f"guardrail family {family_id!r} references unknown fixture {fixture_id!r}"
                )
        families.append(
            GuardrailFamily(
                family_id=family_id,
                name=str(entry.get("name", family_id)),
                fixture_ids=tuple(fixture_ids),
                notes=str(entry.get("notes", "")),
            )
        )
    return tuple(families)


# --- run state ----------------------------------------------------------------


@dataclass
class RunState:
    """Per-run workspace state (prepared -> executed -> collected -> cleaned)."""

    run_id: str
    directory: Path
    scenario_states: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def artifacts_dir(self) -> Path:
        return self.directory / "artifacts"

    @property
    def state_path(self) -> Path:
        return self.directory / "state.json"

    def save(self) -> None:
        self.state_path.write_text(
            json.dumps({"runId": self.run_id, "scenarioStates": self.scenario_states}, indent=2),
            encoding="utf-8",
        )

    def write_artifact(self, name: str, payload: dict[str, Any]) -> str:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / name
        content = canonical_json(payload) + "\n"
        path.write_text(content, encoding="utf-8")
        return digest_bytes(content.encode("utf-8"))


# --- deterministic execution ---------------------------------------------------


def deterministic_metering(case_input: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    """Usage derived only from content sizes — same inputs, same metering."""
    tokens_in = len(canonical_json(case_input)) // 4 + 12
    tokens_out = len(canonical_json(outputs)) // 4 + 8
    sources = outputs.get("sources") or []
    claims = outputs.get("claims") or []
    return {
        "tokensIn": tokens_in,
        "tokensOut": tokens_out,
        "modelSteps": max(1, tokens_out // 400),
        "searches": len(sources) if isinstance(sources, list) else 0,
        "pageInspections": max(0, len(sources) // 2) if isinstance(sources, list) else 0,
        "recordedClaims": len(claims) if isinstance(claims, list) else 0,
        "creditsDebited": _credit_debit(outputs),
        "creditsCurrency": "credits",
    }


def _credit_debit(outputs: dict[str, Any]) -> int:
    if outputs.get("billed") is False or not outputs.get("ok", True):
        return 0
    outcome = outputs.get("outcome")
    if outcome is not None and outcome not in _BILLED_OUTCOMES:
        return 0
    price = outputs.get("creditsPrice", DEFAULT_CREDIT_PRICE)
    return int(price) if isinstance(price, int) and price >= 0 else 0


def build_outputs(
    scenario: Scenario, case_input: dict[str, Any], state: RunState
) -> dict[str, Any]:
    """Deterministically derive the journey outputs for one execute step."""
    base = scenario.steps.get(str(case_input.get("step"))) if scenario.steps else None
    if scenario.steps and base is None:
        raise FixturePackError(
            f"scenario {scenario.scenario_id!r} requires one of steps "
            f"{sorted(scenario.steps)}, got {case_input.get('step')!r}"
        )
    result = scenario.result if base is None else base
    outputs: dict[str, Any] = {
        "scenario": scenario.scenario_id,
        "synthetic": True,
        "workflowId": scenario.workflow_id,
        "journey": scenario.journey,
        "locale": scenario.locale,
        "market": scenario.market,
        "currency": scenario.currency,
    }
    outputs.update(result)
    outputs.update(_journey_extensions(scenario, case_input, state))
    return outputs


def _journey_extensions(
    scenario: Scenario, case_input: dict[str, Any], state: RunState
) -> dict[str, Any]:
    """Journey-specific inheritance: revisions, handoffs, session resume."""
    extra: dict[str, Any] = {}
    if scenario.journey == "revision":
        parent = case_input.get("parent")
        if not isinstance(parent, dict):
            raise FixturePackError(f"scenario {scenario.scenario_id!r} requires caseInput.parent")
        extra["parentReportDigest"] = parent.get("reportDigest", "")
        extra["parentOutcome"] = parent.get("outcome", "")
        extra["parentPreserved"] = True
        extra["childVersion"] = int(parent.get("version", 1)) + 1
        change = case_input.get("change")
        extra["changedFields"] = sorted(change) if isinstance(change, dict) else []
    elif scenario.journey == "handoff":
        handoff = case_input.get("handoff")
        if not isinstance(handoff, dict):
            raise FixturePackError(f"scenario {scenario.scenario_id!r} requires caseInput.handoff")
        extra["handoffFrom"] = handoff.get("from", "")
        extra["inheritedSelection"] = list(handoff.get("selected", []))
        extra["credentialsInherited"] = False  # old-result permissions never carry over
        exclusions = case_input.get("exclusions")
        if isinstance(exclusions, list):
            extra["exclusionsApplied"] = list(exclusions)
    elif scenario.journey == "session":
        step = str(case_input.get("step"))
        key = f"session:{scenario.scenario_id}"
        if step == "save":
            state.scenario_states[key] = {
                "savedOutputs": {
                    "outcome": scenario.steps.get("save", {}).get("outcome", ""),
                    "locale": scenario.locale,
                },
                "draft": case_input.get("draft", {}),
            }
        saved = state.scenario_states.get(key)
        if step in ("reopen", "resume", "reopen-locale-switched") and saved is None:
            raise FixturePackError(
                f"scenario {scenario.scenario_id!r}: step {step!r} requires a prior 'save' step"
            )
        assert saved is not None  # narrowed by the guard above
        if step == "reopen":
            # The dynamically restored draft (what was actually saved) wins over
            # any static value declared in the scenario file.
            extra["draftRestored"] = saved.get("draft", {})
            extra["reportLanguagePreserved"] = saved.get("savedOutputs", {}).get("locale")
        if step == "reopen-locale-switched":
            extra["uiLocale"] = case_input.get("uiLocale", scenario.locale)
            extra["reportLanguagePreserved"] = saved.get("savedOutputs", {}).get("locale")
            extra["draftRestored"] = saved.get("draft", {})
        if step == "resume":
            extra["resumedFromOutcome"] = saved.get("savedOutputs", {}).get("outcome")
            extra["resumed"] = True
    return extra


def tool_events_for(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic tool-event trace mirroring the Choose toolset shape."""
    events: list[dict[str, Any]] = [{"tool": "planResearch", "revisions": 1}]
    if outputs.get("sources"):
        events.append({"tool": "searchWeb", "results": len(outputs["sources"])})
        events.append({"tool": "readEvidence", "sources": len(outputs["sources"])})
    if outputs.get("claims"):
        events.append({"tool": "recordClaims", "claims": len(outputs["claims"])})
    if outputs.get("clarification"):
        events.append({"tool": "askBuyer", "kind": outputs["clarification"].get("kind", "")})
    events.append({"tool": "submitDecisionReport", "task": outputs.get("journey", "")})
    return events


# --- stdio server ---------------------------------------------------------------


class FixtureAdapterServer:
    """Serves protocol v1 frames from a loaded fixture pack."""

    def __init__(
        self,
        pack: FixturePack,
        workspace: Path,
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        stderr: BinaryIO | None = None,
    ) -> None:
        self._pack = pack
        self._workspace = workspace
        self._runs: dict[str, RunState] = {}
        self._stdin = stdin or sys.stdin.buffer
        self._stdout = stdout or sys.stdout.buffer
        self._stderr = stderr or sys.stderr.buffer
        self._send_seq = SequenceTracker()
        self._recv_seq = SequenceTracker()

    def serve(self) -> int:
        """Read frames until EOF; returns the process exit code."""
        while True:
            line = self._stdin.readline()
            if not line:
                return 0
            try:
                frame = parse_frame(line)
                self._recv_seq.observe(frame)
            except VouchError as exc:
                self._write_error(None, code=exc.code, message=str(exc))
                self._diag(f"inbound protocol violation: {exc}")
                return 2
            try:
                self._dispatch(frame)
            except _ErrorResponse as failure:
                self._write_error(frame.run_id, code=failure.code, message=failure.message)
            except VouchError as exc:
                self._write_error(frame.run_id, code=exc.code, message=str(exc))

    def _dispatch(self, frame: Frame) -> None:
        handler = {
            FrameKind.DESCRIBE_REQUEST: self._describe,
            FrameKind.PREPARE_REQUEST: self._prepare,
            FrameKind.EXECUTE_REQUEST: self._execute,
            FrameKind.COLLECT_REQUEST: self._collect,
            FrameKind.CLEANUP_REQUEST: self._cleanup,
        }.get(frame.kind)
        if handler is None:
            raise _ErrorResponse(
                "vouch/protocol-frame", f"unexpected frame kind {frame.kind.value}"
            )
        handler(frame)

    def _describe(self, frame: Frame) -> None:
        self._write_response(
            FrameKind.DESCRIBE_RESPONSE,
            run_id=None,
            payload={
                "adapterId": FIXTURE_ADAPTER_ID,
                "protocolVersion": "1",
                "workflows": list(self._pack.workflow_ids),
                "actions": list(FIXTURE_ACTIONS),
                "enforcedModes": [m.value for m in ENFORCED_MODES],
                "notes": (
                    "SYNTHETIC fixture adapter: deterministic journeys only. Proves the "
                    "Vouch pipeline (protocol, gating, metering, sealing); proves nothing "
                    "about model quality or real product improvement."
                ),
            },
        )

    def _prepare(self, frame: Frame) -> None:
        assert frame.run_id is not None
        self._require_fixture_mode(frame.payload.get("mode"))
        if frame.run_id in self._runs:
            raise _ErrorResponse("fixture/run-already-prepared", f"run {frame.run_id} exists")
        directory = self._workspace / "runs" / frame.run_id
        directory.mkdir(parents=True, exist_ok=True)
        run = RunState(run_id=frame.run_id, directory=directory)
        run.save()
        self._runs[frame.run_id] = run
        self._write_response(FrameKind.PREPARE_RESPONSE, run_id=frame.run_id, payload={"ok": True})

    def _execute(self, frame: Frame) -> None:
        assert frame.run_id is not None
        run = self._runs.get(frame.run_id)
        if run is None:
            raise _ErrorResponse("fixture/run-not-prepared", f"run {frame.run_id} was not prepared")
        self._require_fixture_mode(frame.payload.get("mode"))
        payload = frame.payload
        workflow_id = payload.get("workflowId")
        case_input = payload.get("caseInput")
        if not isinstance(case_input, dict):
            raise _ErrorResponse("vouch/protocol-frame", "caseInput must be an object")
        scenario_id = case_input.get("scenario")
        scenario = self._pack.scenarios.get(str(scenario_id)) if scenario_id else None
        if scenario is None:
            raise _ErrorResponse(
                "fixture/unknown-scenario",
                f"no synthetic scenario {scenario_id!r} in pack {self._pack.pack_id!r}",
            )
        if workflow_id != scenario.workflow_id:
            raise _ErrorResponse(
                "fixture/workflow-mismatch",
                f"scenario {scenario.scenario_id!r} belongs to workflow "
                f"{scenario.workflow_id!r}, request said {workflow_id!r}",
            )

        mode = scenario.behavior.get("mode", "ok")
        if mode == "stall":
            seconds = float(scenario.behavior.get("stallSeconds", 30))
            self._diag(
                f"scenario {scenario.scenario_id} stalling {seconds}s "
                "(overrun probe; no response will be sent)"
            )
            time.sleep(seconds)
            return  # killed by the controller before this returns in practice

        try:
            outputs = build_outputs(scenario, case_input, run)
        except FixturePackError as exc:
            raise _ErrorResponse("fixture/scenario-shape", str(exc)) from exc
        if mode == "fail":
            outputs["ok"] = False
        usage = deterministic_metering(case_input, outputs)
        report_digest = run.write_artifact("report.json", outputs)
        sources_digest = run.write_artifact("sources.json", {"sources": outputs.get("sources", [])})
        usage_digest = run.write_artifact("usage.json", usage)
        run.save()
        self._write_response(
            FrameKind.EXECUTE_RESPONSE,
            run_id=frame.run_id,
            payload={
                "ok": mode != "fail",
                "outputs": outputs,
                "evidenceRefs": [report_digest, sources_digest, usage_digest],
                "toolEvents": tool_events_for(outputs),
                "usage": usage,
                "error": scenario.behavior.get("code") if mode == "fail" else None,
                "mode": RunMode.FIXTURE.value,
                "runnerVersion": f"{FIXTURE_ADAPTER_ID}/{self._pack.pack_id}",
                # v1.1 §1: echo the request identity verbatim
                "identity": dict(frame.payload.get("identity") or {}),
            },
        )

    def _collect(self, frame: Frame) -> None:
        assert frame.run_id is not None
        run = self._runs.get(frame.run_id)
        if run is None:
            raise _ErrorResponse("fixture/run-not-prepared", f"run {frame.run_id} was not prepared")
        digests: list[str] = []
        if run.artifacts_dir.is_dir():
            for path in sorted(run.artifacts_dir.iterdir()):
                if path.is_file():
                    digests.append(digest_bytes(path.read_bytes()))
        run.write_artifact("_sealed.json", {"sealed": digests})
        payload: dict[str, Any] = {"digests": digests}
        if frame.payload.get("wantArtifacts"):
            # v1.1 §3: in-frame, content-addressed, size-bounded artifacts
            import base64

            artifacts = []
            for path in sorted(run.artifacts_dir.iterdir()):
                if path.is_file() and path.name != "_sealed.json":
                    data = path.read_bytes()
                    artifacts.append(
                        {
                            "digest": digest_bytes(data),
                            "bytes": base64.b64encode(data).decode("ascii"),
                            "kind": (
                                "report"
                                if path.name == "report.json"
                                else "usage"
                                if path.name == "usage.json"
                                else "evidence"
                            ),
                        }
                    )
            payload["artifacts"] = artifacts
        self._write_response(FrameKind.COLLECT_RESPONSE, run_id=frame.run_id, payload=payload)

    def _cleanup(self, frame: Frame) -> None:
        assert frame.run_id is not None
        run = self._runs.pop(frame.run_id, None)
        if run is not None:
            shutil.rmtree(run.directory, ignore_errors=True)
        self._write_response(FrameKind.CLEANUP_RESPONSE, run_id=frame.run_id, payload={"ok": True})

    def _require_fixture_mode(self, mode: Any) -> None:
        try:
            parsed = RunMode(str(mode))
        except ValueError:
            raise _ErrorResponse("vouch/mode-not-enforced", f"unknown mode {mode!r}") from None
        if parsed not in ENFORCED_MODES:
            raise _ErrorResponse(
                "vouch/mode-not-enforced",
                f"{FIXTURE_ADAPTER_ID} enforces only fixture mode; {parsed.value} would "
                "require a real Choose-owned runner (fail closed, no silent degradation)",
            )

    def _diag(self, message: str) -> None:
        """Write one sanitized diagnostic line to stderr (bytes-safe)."""
        self._stderr.write(f"fixture-adapter: {message}\n".encode())
        self._stderr.flush()

    def _write_response(
        self, kind: FrameKind, *, run_id: str | None, payload: dict[str, Any]
    ) -> None:
        frame = Frame(seq=self._send_seq.next(), kind=kind, payload=payload, run_id=run_id)
        self._stdout.write(frame.to_json_line().encode("utf-8") + b"\n")
        self._stdout.flush()

    def _write_error(self, run_id: str | None, *, code: str, message: str) -> None:
        self._write_response(
            FrameKind.ERROR,
            run_id=run_id,
            payload={"code": code, "message": message},
        )


class _ErrorResponse(Exception):
    """Internal control flow: respond with an error frame, keep serving."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fixture-adapter", description=__doc__)
    parser.add_argument("--fixtures", required=True, help="path to the fixture pack directory")
    parser.add_argument("--workspace", default=None, help="run workspace directory")
    args = parser.parse_args(argv)

    try:
        pack = FixturePack.load(args.fixtures)
    except FixturePackError as exc:
        sys.stderr.write(f"fixture-adapter: refusing to load pack: {exc}\n")
        return 3

    if args.workspace:
        workspace = Path(args.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = Path(tempfile.mkdtemp(prefix="vouch-fixture-choose-"))
        atexit.register(shutil.rmtree, workspace, ignore_errors=True)

    return FixtureAdapterServer(pack, workspace).serve()


if __name__ == "__main__":  # pragma: no cover - subprocess entry
    raise SystemExit(main())
