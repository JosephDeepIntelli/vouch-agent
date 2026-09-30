"""The intended M5b pilot flow, proven locally end to end (no real provider).

public candidate -> actual Choose controller -> configured existing
ProviderTransport -> validated receipt -> durable costs/export, with an
INJECTED loopback fake provider (Choose's fake-provider-server serving the
application case's own script). One ROOT budget spans the paired attempts
(the transport's spend cap), the Vouch ledger books the accounted cost,
failures hold their reservation for explicit reconciliation, and a fresh
process re-verifies the durable record. This is a SIMULATION: fixture-mode
material, fake provider, no network endpoint, no live-model claim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vouch_agent.adapters.choose_bundle import ChooseBaselineConfig, build_change_bundle
from vouch_agent.adapters.process_adapter import DEFAULT_EXECUTE_TIMEOUT_S, ProcessAdapterClient
from vouch_agent.cli.main import app

runner = CliRunner()
REPO_ROOT = Path(__file__).parents[2]
CHOOSE = (
    Path(os.environ["VOUCH_CHOOSE_RUNNER_DIR"])
    if os.environ.get("VOUCH_CHOOSE_RUNNER_DIR")
    else None
)
CREDENTIAL_ENV = "VOUCH_PILOT_CREDENTIAL_PILOT_FAKE"


def _runner_ready() -> bool:
    if CHOOSE is None:
        return False
    tsx = CHOOSE / "node_modules" / ".bin" / "tsx"
    if not tsx.is_file() or not (CHOOSE / "scripts" / "vouch" / "runner.ts").is_file():
        return False
    if not (CHOOSE / "scripts" / "vouch" / "fake-provider-server.ts").is_file():
        return False
    inner = ProcessAdapterClient(
        [str(tsx), "scripts/vouch/runner.ts"],
        cwd=str(CHOOSE),
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


class FakeProvider:
    """The Choose-owned loopback fake provider as a subprocess."""

    def __init__(self, *flags: str) -> None:
        self.process = subprocess.Popen(
            [
                str(CHOOSE / "node_modules" / ".bin" / "tsx"),
                "scripts/vouch/fake-provider-server.ts",
                "--port",
                "0",
                *flags,
            ],
            cwd=str(CHOOSE),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = self.process.stdout.readline() if self.process.stdout else ""
            if line.startswith("{"):
                self.port = int(json.loads(line)["port"])
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("fake provider did not start")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            self.process.kill()


@pytest.fixture(scope="module")
def choose_ready() -> None:
    if not _runner_ready():
        pytest.skip("no prepared Choose runner advertising the M5b scope")


@pytest.fixture()
def fake_provider(choose_ready: None) -> FakeProvider:
    provider = FakeProvider()
    yield provider
    provider.stop()


def _transport_config(port: int, *, cap: float = 1.0, max_retries: int = 0) -> dict:
    return {
        "schemaVersion": 1,
        "kind": "choose-provider-config",
        "providerId": "fake-loopback-provider",
        "endpoint": f"http://127.0.0.1:{port}/v1/chat/completions",
        "modelId": "fake-pilot-model",
        "api": "openai-chat-completions-sse",
        "parameters": {"temperature": 0, "maxOutputTokens": 1024, "topP": 1},
        "endpointPolicy": {
            "allowInsecureLoopback": True,
            "allowedHostSuffix": "",
            "requestTimeoutMs": 10_000,
            "maxRetries": max_retries,
            "retryBackoffMs": 10,
            "maxResponseBytes": 1_048_576,
            "reservePromptTokens": 200_000,
        },
        "priceTable": {
            "priceTableVersion": "fake-price-table-1",
            "inputUsdPerMillionTokens": 1,
            "outputUsdPerMillionTokens": 2,
        },
        "credentialRef": {"name": "pilot-fake"},
        "spendCapUsd": cap,
        "fixtureFallback": False,
    }


def _prepare_project(tmp_path: Path, *, cap: float = 1.0) -> tuple[Path, str]:
    def invoke(*args: str) -> str:
        result = runner.invoke(app, list(args))
        assert result.exit_code == 0, result.output
        return result.output

    invoke(
        "init", "--project", str(tmp_path), "--workflow", "W-C2",
        "--owners", "acceptance-owner=ana", "--owners", "release-owner=roger",
        "--cap", str(cap),
    )
    inner = ProcessAdapterClient(
        [str(CHOOSE / "node_modules" / ".bin" / "tsx"), "scripts/vouch/runner.ts"],
        cwd=str(CHOOSE), execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S,
    )
    try:
        inner.describe()
        baseline = ChooseBaselineConfig.from_describe(inner.describe_payload)
    finally:
        inner.close()
    invoke(
        "baseline", "--project", str(tmp_path), "--version", "v0",
        "--source-ref", "git:choose", "--main-metric", "supportedClaims",
    )
    bundle = build_change_bundle(
        bundle_id="pilot-sim", baseline=baseline, delta={"conclusionStyle": "terse"}
    )
    delta_file = tmp_path / "delta.json"
    delta_file.write_text(json.dumps(bundle.to_dict()), encoding="utf-8")
    out = invoke(
        "propose", "--project", str(tmp_path), "--type", "prompt-delta",
        "--rationale", "pilot simulation", "--delta-file", str(delta_file), "--seal",
    )
    candidate = out.split("candidate: ")[1].split()[0]
    invoke(
        "pack", "--project", str(tmp_path), "--from-choose-application",
        "--application-split", "development",
    )
    return tmp_path, candidate


def _evaluate(project: Path, candidate: str, transport_file: Path):
    return runner.invoke(
        app,
        [
            "evaluate", "--project", str(project), "--candidate", candidate,
            "--pack", "choose-application-development", "--adapter", "choose",
            "--split", "development", "--provider-transport", str(transport_file),
        ],
    )


@pytest.mark.skipif(
    not _runner_ready(),
    reason="explicit VOUCH_CHOOSE_RUNNER_DIR with simulation-capable runner required",
)
class TestPilotSimulation:
    def test_full_flow_with_accounted_cost_and_root_budget(
        self, tmp_path: Path, fake_provider: FakeProvider, monkeypatch
    ) -> None:
        monkeypatch.setenv(CREDENTIAL_ENV, "fake-not-a-secret")
        project, candidate = _prepare_project(tmp_path, cap=1.0)
        transport_file = tmp_path / "transport.json"
        transport_file.write_text(json.dumps(_transport_config(fake_provider.port, cap=1.0)))
        result = _evaluate(project, candidate, transport_file)
        assert result.exit_code == 0, result.output
        assert "pairs: 1/1 complete" in result.output
        assert "cost: baseline $" in result.output and "candidate $" in result.output

        from vouch_agent.appservices.workspace import ProjectWorkspace
        from vouch_agent.controller.service import KIND_EVALUATION

        run_id = result.output.split("run: ")[1].split()[0]
        workspace = ProjectWorkspace.open(project)
        try:
            run = workspace.store.load(KIND_EVALUATION, run_id)
            assert run is not None
            costs = {}
            accounting_views = []
            for attempt in run["attempts"]:
                sealed = json.loads(workspace.artifacts.get(attempt["outputDigest"]))
                usage = attempt["usage"]
                assert usage["costUsd"] > 0, "transport-accounted cost missing"
                costs[attempt["side"]] = usage["costUsd"]
                # The transport-accounting artifact is durable evidence.
                transport_views = [
                    json.loads(workspace.artifacts.get(ref))
                    for ref in sealed["evidenceRefs"]
                    if json.loads(workspace.artifacts.get(ref)).get("kind")
                    == "transport-accounting"
                ]
                assert transport_views, "no transport-accounting artifact sealed"
                accounting_views.append(transport_views[0])
            assert set(costs) == {"baseline", "candidate"}
            # ONE root budget: the later attempt's cumulative accounting saw
            # both sides' requests against the same cap.
            last = max(
                accounting_views,
                key=lambda view: view["accounting"]["attempts"],
            )
            assert last["accounting"]["attempts"] >= 14
            assert last["accounting"]["spendCapUsd"] == 1.0
            assert last["accounting"]["settledUsd"] > 0
            # The Vouch ledger booked the accounted cost.
            total = sum(costs.values())
            assert 0 < total < 1.0
        finally:
            workspace.close()

        # Durable export + fresh-process re-verification.
        export = runner.invoke(
            app,
            [
                "export",
                "--project",
                str(project),
                "--run",
                run_id,
                "--out",
                str(project / "export"),
            ],
        )
        assert export.exit_code == 0, export.output
        code = r"""
import json, sys
sys.path.insert(0, "__SRC__")
from vouch_agent.appservices.workspace import ProjectWorkspace
from vouch_agent.controller.service import KIND_EVALUATION
w = ProjectWorkspace.open("__PROJECT__")
run = w.store.load(KIND_EVALUATION, "__RUN__")
saw_cost = 0
saw_accounting = 0
for attempt in run["attempts"]:
    assert attempt["usage"]["costUsd"] > 0
    saw_cost += 1
    sealed = json.loads(w.artifacts.get(attempt["outputDigest"]))
    for ref in sealed["evidenceRefs"]:
        obj = json.loads(w.artifacts.get(ref))
        if obj.get("kind") == "transport-accounting":
            saw_accounting += 1
            assert obj["accounting"]["spendCapUsd"] == 1.0
assert saw_accounting >= 2, "transport accounting not durable"
print("SIM_OK", saw_cost, saw_accounting)
"""
        code = (
            code.replace("__SRC__", str(REPO_ROOT / "src"))
            .replace("__PROJECT__", str(project))
            .replace("__RUN__", run_id)
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("SIM_OK 2"), proc.stdout

    def test_injected_stream_failure_retains_unknown_spend_in_accounting(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A destroyed stream mid-run leaves its reservation HELD in the
        transport accounting — unknown spend is never erased even when the
        controller's own retry completes the run."""
        monkeypatch.setenv(CREDENTIAL_ENV, "fake-not-a-secret")
        provider = FakeProvider("--fail-after-headers", "3")
        try:
            project, candidate = _prepare_project(tmp_path, cap=1.0)
            transport_file = tmp_path / "transport.json"
            transport_file.write_text(json.dumps(_transport_config(provider.port)))
            result = _evaluate(project, candidate, transport_file)
            assert result.exit_code == 0, result.output
            from vouch_agent.appservices.workspace import ProjectWorkspace
            from vouch_agent.controller.service import KIND_EVALUATION

            run_id = result.output.split("run: ")[1].split()[0]
            workspace = ProjectWorkspace.open(project)
            try:
                run = workspace.store.load(KIND_EVALUATION, run_id)
                assert run is not None
                held_views = []
                for attempt in run["attempts"]:
                    sealed = json.loads(workspace.artifacts.get(attempt["outputDigest"]))
                    for ref in sealed["evidenceRefs"]:
                        obj = json.loads(workspace.artifacts.get(ref))
                        if (
                            obj.get("kind") == "transport-accounting"
                            and obj["accounting"]["heldForReconciliationUsd"] > 0
                        ):
                            held_views.append(obj["accounting"])
                assert held_views, (
                    "the destroyed stream's unknown spend must stay held in the "
                    "durable transport accounting, never erased"
                )
            finally:
                workspace.close()
        finally:
            provider.stop()

    def test_cap_exhaustion_fails_the_attempt_and_holds_the_vouch_reservation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A root cap smaller than one request refuses dispatch: the attempt
        fails honestly, the Vouch reservation stays HELD for explicit
        reconciliation, and the operator release path works."""
        monkeypatch.setenv(CREDENTIAL_ENV, "fake-not-a-secret")
        provider = FakeProvider()
        try:
            project, candidate = _prepare_project(tmp_path, cap=1.0)
            transport_file = tmp_path / "transport.json"
            transport_file.write_text(json.dumps(_transport_config(provider.port, cap=0.001)))
            result = _evaluate(project, candidate, transport_file)
            assert result.exit_code == 0, result.output
            assert "verdict: rejected" in result.output or "inconclusive" in result.output

            from vouch_agent.appservices.workspace import ProjectWorkspace
            from vouch_agent.controller.service import KIND_EVALUATION

            run_id = result.output.split("run: ")[1].split()[0]
            workspace = ProjectWorkspace.open(project)
            try:
                run = workspace.store.load(KIND_EVALUATION, run_id)
                assert run is not None
                assert any(a["status"] != "ok" for a in run["attempts"])
                reservations = [
                    r
                    for r in workspace.ledger.reservations()
                    if r.status.value == "open"
                ]
                assert reservations, "no held reservation for the failed attempt"
            finally:
                workspace.close()

            subject = reservations[0].reservation_id
            reconciled = runner.invoke(
                app,
                [
                    "resume",
                    "--project",
                    str(project),
                    "--reconcile",
                    subject,
                    "--verified-note",
                    "fake provider accounting inspected: the cap refused "
                    "dispatch before any request was sent; nothing can be billed",
                    "--release",
                ],
            )
            assert reconciled.exit_code == 0, reconciled.output
        finally:
            provider.stop()
