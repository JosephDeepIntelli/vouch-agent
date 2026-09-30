"""Adapter clients the improvement flow drives (appservice level).

Two implementations of the ``AdapterClient`` port:

* :class:`ScriptedOfflineAdapter` — the built-in deterministic, offline,
  in-process adapter (CLI default). It replays a synthetic advantage for the
  candidate side so the whole pipeline can be exercised without any subprocess
  or network. It proves protocol/gating/accounting semantics only — never
  model improvement (RunMode ``fixture``, design §13.2).

* :class:`FixtureScenarioClient` — drives the REAL in-repo fixture adapter
  subprocess (``python -m vouch_agent.adapters.fixture_adapter``) through
  :class:`vouch_agent.adapters.process_adapter.ProcessAdapterClient`, i.e.
  every frame crosses a real process boundary. Two trusted client-side duties
  the controller cannot know about:

  - *scenario mapping*: the controller's case inputs carry ``caseId``; the
    fixture adapter expects ``caseInput.scenario``. The mapping case id ->
    scenario id is validated up front against the loaded fixture pack and
    injected here, and a case with no mapped scenario fails closed.
  - *synthetic pricing*: the fixture adapter meters tokens/steps/credits but
    quotes no USD (it has no price source and must not invent one at the
    runner). This client converts the metered quantities to USD at the
    declared synthetic rate below so attempts carry a measurable price. The
    price is synthetic and is labeled as such in the usage block.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from vouch_agent.adapters.base import AdapterDescriptor, AdapterExecution
from vouch_agent.adapters.process_adapter import ProcessAdapterClient
from vouch_agent.contracts.common import RunMode
from vouch_agent.contracts.project import WorkflowDeclaration
from vouch_agent.errors import AdapterExecutionError
from vouch_agent.storage.interfaces import ArtifactStore

#: Declared SYNTHETIC price used to give fixture metering a measurable USD
#: value: USD per (input+output) token. Fixture mode proves the pipeline, so
#: the absolute number is irrelevant; what matters is that it is deterministic,
#: labeled, and books both sides.
FIXTURE_USD_PER_TOKEN = 0.000_002

_SCRIPTED_ADAPTER_ID = "scripted-offline@1"


class ScriptedOfflineAdapter:
    """Deterministic offline adapter: candidate strictly better on the main
    metric; the baseline trips the workflow's first guardrail channel on one
    dev case (mirrors the lead integration's OfflineCompareAdapter shape)."""

    def __init__(self, workflow: WorkflowDeclaration, *, cost_usd: float = 0.01) -> None:
        self._workflow = workflow
        self._cost_usd = cost_usd
        # The first case this adapter sees on the baseline side is where the
        # synthetic guardrail trip lands — deterministic within and across runs
        # of the same pack (pack case order is preserved by the controller).
        self._guardrail_case: str | None = None

    def describe(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            adapter_id=_SCRIPTED_ADAPTER_ID,
            workflows=(self._workflow.workflow_id,),
            enforced_modes=(RunMode.FIXTURE,),
            notes=(
                "SYNTHETIC built-in scripted adapter: deterministic offline replay; "
                "proves the Vouch pipeline only, never model improvement."
            ),
        )

    def prepare(self, run_id: str, mode: RunMode) -> None:
        return None

    def execute(
        self,
        *,
        run_id: str,
        attempt_id: str,
        workflow_id: str,
        case_input: dict[str, Any],
        mode: RunMode,
    ) -> AdapterExecution:
        del run_id, attempt_id, workflow_id
        case_id = str(case_input.get("caseId", ""))
        is_candidate = case_input.get("side") == "candidate"
        usage: dict[str, Any] = {
            "costUsd": self._cost_usd,
            self._workflow.main_objective: 0.95 if is_candidate else 0.70,
        }
        # Every declared guardrail is EXPLICITLY measured on every attempt:
        # checked-pass (0) by default — a missing required channel is
        # inconclusive under the strict comparator, never an implicit pass.
        for guardrail in self._workflow.guardrails:
            usage[f"guardrail:{guardrail}"] = 0
        if self._workflow.guardrails and not is_candidate and case_id:
            if self._guardrail_case is None:
                self._guardrail_case = case_id
            if case_id == self._guardrail_case:
                # Baseline-only guardrail hit on one case: comparison context
                # for the reviewer, never candidate clearance.
                usage[f"guardrail:{self._workflow.guardrails[0]}"] = 1
        return AdapterExecution(
            ok=True,
            outputs={"caseId": case_id, "side": case_input.get("side"), "synthetic": True},
            usage=usage,
            mode=mode,
            runner_version=_SCRIPTED_ADAPTER_ID,
        )

    def collect(self, run_id: str) -> tuple[str, ...]:
        return ()

    def cleanup(self, run_id: str) -> None:
        return None


class FixtureScenarioClient:
    """Drive the real fixture adapter subprocess, mapping case ids to scenarios.

    Wraps a :class:`ProcessAdapterClient`; every request/response is a
    protocol-v1 frame over the subprocess's stdin/stdout. ``prepare`` is issued
    lazily before the first execute of a run (the controller does not know
    about prepare), and :meth:`collect` ingests the sealed run artifacts into
    the project's content-addressed artifact store so evidence digests are
    available for export.
    """

    def __init__(
        self,
        inner: ProcessAdapterClient,
        *,
        scenarios: dict[str, str],
        artifacts: ArtifactStore,
        run_artifacts_dir: Path | None = None,
    ) -> None:
        self._inner = inner
        self._scenarios = dict(scenarios)
        self._artifacts = artifacts
        self._run_artifacts_dir = run_artifacts_dir
        self._prepared: set[str] = set()

    @property
    def inner(self) -> ProcessAdapterClient:
        return self._inner

    def describe(self) -> AdapterDescriptor:
        base = self._inner.describe()
        return replace(
            base,
            adapter_id=f"{base.adapter_id}+scenario-client@1",
            notes=(
                f"{base.notes} | client maps case ids to synthetic scenarios and "
                f"prices fixture metering at the declared synthetic rate "
                f"${FIXTURE_USD_PER_TOKEN}/token (labelled synthetic, fixture mode)."
            ),
        )

    def prepare(self, run_id: str, mode: RunMode) -> None:
        if run_id not in self._prepared:
            self._inner.prepare(run_id, mode)
            self._prepared.add(run_id)

    def execute(
        self,
        *,
        run_id: str,
        attempt_id: str,
        workflow_id: str,
        case_input: dict[str, Any],
        mode: RunMode,
    ) -> AdapterExecution:
        self.prepare(run_id, mode)
        case_id = str(case_input.get("caseId", ""))
        scenario = self._scenarios.get(case_id)
        if scenario is None:
            raise AdapterExecutionError(
                f"no synthetic scenario mapped for case {case_id!r} "
                f"(known: {sorted(self._scenarios)}); refusing the attempt (fail closed)"
            )
        enriched = {"scenario": scenario, **case_input}
        result = self._inner.execute(
            run_id=run_id,
            attempt_id=attempt_id,
            workflow_id=workflow_id,
            case_input=enriched,
            mode=mode,
        )
        usage = dict(result.usage) if result.usage else None
        if usage is not None and "costUsd" not in usage:
            tokens = int(usage.get("tokensIn", 0) or 0) + int(usage.get("tokensOut", 0) or 0)
            usage["costUsd"] = round(tokens * FIXTURE_USD_PER_TOKEN, 6)
            usage["priceNote"] = f"synthetic ${FIXTURE_USD_PER_TOKEN}/token (fixture mode)"
            result = replace(result, usage=usage)
        return result

    def collect(self, run_id: str) -> tuple[str, ...]:
        digests = self._inner.collect(run_id)
        self._ingest_run_artifacts(run_id)
        return digests

    def cleanup(self, run_id: str) -> None:
        try:
            self._inner.cleanup(run_id)
        finally:
            self._prepared.discard(run_id)

    def close(self) -> None:
        self._inner.close()

    def _ingest_run_artifacts(self, run_id: str) -> None:
        """Copy the subprocess's sealed artifacts into the project store.

        Content addressing makes this idempotent; the digests were computed by
        the adapter over the same bytes, so a mismatch would surface from the
        store's read-time verification.
        """
        if self._run_artifacts_dir is None:
            return
        base = self._run_artifacts_dir / "runs" / run_id / "artifacts"
        if not base.is_dir():
            return
        for path in sorted(base.iterdir()):
            if path.is_file() and path.name != "_sealed.json":
                self._artifacts.put(path.read_bytes())
