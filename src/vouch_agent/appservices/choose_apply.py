"""The Choose application client: candidate deltas applied, not just executed.

The REAL Choose runner driven through its ``apply-config`` operation (adapter
protocol v1.2). Baseline and candidate attempts BOTH travel the application
path against the same case and the runner's immutable baseline configuration —
they differ only in the digest-bound change bundle:

* baseline side — the explicit empty bundle (a reported no-op; the effective
  configuration equals the frozen baseline byte-for-byte);
* candidate side — the sealed candidate's verified change bundle.

Trusted client-side duties the controller cannot know about (mirroring
:class:`vouch_agent.appservices.adapters.FixtureScenarioClient`):

* *receipt validation* — every application receipt is bound to THIS attempt,
  the frozen baseline and the exact requested delta before the result can
  count as applied evidence;
* *usage projection* — the runner meters scripted tokens without USD (fixture
  runs never invent a price at the runner); this client quotes the declared
  synthetic rate and projects the receipt/summary's honest case metrics into
  usage channels a frozen rubric can name;
* *durable evidence* — collect() persists the runner's verified in-frame
  artifact bytes into the project's content-addressed store and proves every
  evidence reference the attempts cited is actually held, BEFORE cleanup
  destroys the runner's ephemeral workspace.

Supported application scope is EXACTLY what the runner advertises as
application-capable cases (today: W-C2's ``case_apply_config_kettle_en``).
All-workflow fixture execution coverage stays with the fixture adapter; this
client never implies it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from vouch_agent.adapters.base import AdapterDescriptor, AdapterExecution
from vouch_agent.adapters.choose_bundle import (
    ChooseApplicationReceipt,
    ChooseBaselineConfig,
    ChooseChangeBundle,
    baseline_noop_bundle,
    receipt_from_payload,
    validate_receipt,
)
from vouch_agent.adapters.process_adapter import ProcessAdapterClient
from vouch_agent.appservices.adapters import FIXTURE_USD_PER_TOKEN
from vouch_agent.contracts.common import RunMode
from vouch_agent.errors import AdapterExecutionError, ContractError
from vouch_agent.storage.interfaces import ArtifactStore

#: Synthetic USD per scripted token — same declared rate, same label, as the
#: fixture scenario client. Fixture mode proves the pipeline; the price is
#: deterministic and labelled, never a live provider price.
APPLICATION_CLIENT_ID_SUFFIX = "+choose-application@1"

#: The honest scope sentence every describe() carries.
APPLICATION_SCOPE_NOTE = (
    "Candidate application via the runner's apply-config operation against its "
    "immutable baseline evaluation configuration; application scope is exactly "
    "the runner's advertised application-capable cases (W-C2 "
    "case_apply_config_kettle_en today). Deterministic scripted providers "
    "(fixture mode): proves application and decision plumbing, never live-model "
    "quality. All-workflow execution coverage stays with the fixture adapter."
)


@dataclass(frozen=True)
class ChooseEvaluationCase:
    """One case in the runner's advertised evaluation-case vocabulary."""

    case_id: str
    workflow_id: str
    application: bool
    task: str = ""
    locale: str = ""
    market: str = ""


def evaluation_cases_from_describe(
    payload: dict[str, Any],
) -> dict[str, ChooseEvaluationCase]:
    """Decode the runner's advertised case vocabulary (fail closed on shape)."""
    raw = payload.get("evaluationCases")
    if not isinstance(raw, list) or not raw:
        raise ContractError(
            "the Choose runner's describe payload declares no evaluationCases; "
            "without an advertised case vocabulary, imported packs cannot be "
            "reconciled with runner case identity (refused, fail closed)"
        )
    cases: dict[str, ChooseEvaluationCase] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ContractError("evaluationCases entries must be objects")
        case_id = entry.get("caseId")
        workflow_id = entry.get("workflowId")
        if not isinstance(case_id, str) or not case_id:
            raise ContractError("evaluationCases entries require a non-empty caseId")
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ContractError(f"evaluation case {case_id!r} requires a workflowId")
        if case_id in cases:
            raise ContractError(f"evaluation case {case_id!r} advertised twice")
        cases[case_id] = ChooseEvaluationCase(
            case_id=case_id,
            workflow_id=workflow_id,
            application=bool(entry.get("application", False)),
            task=str(entry.get("task", "")),
            locale=str(entry.get("locale", "")),
            market=str(entry.get("market", "")),
        )
    return cases


def application_scope_refusal(
    cases: dict[str, ChooseEvaluationCase], pack_case_ids: tuple[str, ...]
) -> ContractError:
    """The precise refusal for improvement cases outside the application scope."""
    supported = sorted(
        case.case_id for case in cases.values() if case.application
    )
    unsupported = [case_id for case_id in pack_case_ids if case_id not in supported]
    return ContractError(
        f"pack case(s) {unsupported} are outside the Choose application scope: the "
        f"apply-config path currently supports only {supported} "
        f"({', '.join(sorted({cases[c].workflow_id for c in supported if c in cases}))}); "
        "refusing unsupported improvement cases instead of executing the "
        "candidate as an unchanged agent (that would produce identical "
        "baseline/candidate evidence and no application). All-workflow fixture "
        "execution coverage stays with --adapter fixture."
    )


class ChooseApplicationClient:
    """Drives baseline+candidate attempts through the runner's apply-config."""

    def __init__(
        self,
        inner: ProcessAdapterClient,
        *,
        artifacts: ArtifactStore,
        baseline: ChooseBaselineConfig,
        evaluation_cases: dict[str, ChooseEvaluationCase],
        application_case_id: str,
        candidate_bundle: ChooseChangeBundle | None,
        provider_transport: dict[str, Any] | None = None,
    ) -> None:
        self._inner = inner
        self._provider_transport = provider_transport
        self._artifacts = artifacts
        self._baseline = baseline
        self._evaluation_cases = dict(evaluation_cases)
        self._application_case_id = application_case_id
        self._candidate_bundle = candidate_bundle
        self._baseline_bundle = baseline_noop_bundle(baseline)
        self._prepared: set[str] = set()
        # Evidence references each attempt cited (per run), so collect() can
        # prove the durable store actually holds what the attempts bound.
        self._refs_by_run: dict[str, dict[str, tuple[str, ...]]] = {}
        self._receipts_by_run: dict[str, dict[str, ChooseApplicationReceipt]] = {}

    @property
    def inner(self) -> ProcessAdapterClient:
        return self._inner

    @property
    def baseline(self) -> ChooseBaselineConfig:
        return self._baseline

    @property
    def application_case_id(self) -> str:
        return self._application_case_id

    def receipt_for(self, run_id: str, attempt_id: str) -> ChooseApplicationReceipt | None:
        """The validated application receipt of one attempt, if it executed."""
        return self._receipts_by_run.get(run_id, {}).get(attempt_id)

    def describe(self) -> AdapterDescriptor:
        base = self._inner.describe()
        return replace(
            base,
            adapter_id=f"{base.adapter_id}{APPLICATION_CLIENT_ID_SUFFIX}",
            notes=(
                f"{APPLICATION_SCOPE_NOTE} | Client validates every application "
                f"receipt against the frozen baseline {self._baseline.config_version}/"
                f"{self._baseline.digest[:19]}…, applies the sealed candidate's "
                f"digest-bound change bundle on candidate attempts and an explicit "
                "no-op bundle on baseline attempts, and prices fixture metering at "
                f"the declared synthetic rate ${FIXTURE_USD_PER_TOKEN}/token "
                "(labelled synthetic, fixture mode)."
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
        side = str(case_input.get("side", ""))
        if side not in ("baseline", "candidate"):
            raise ContractError(
                f"attempt {attempt_id!r} carries no baseline/candidate side; the "
                "application path pairs sides against the same case"
            )
        case_id = str(case_input.get("caseId", ""))
        entry = self._evaluation_cases.get(case_id)
        if entry is None or not entry.application:
            # The flow already refused this at build time; a case that reaches
            # here anyway never executes silently as an unchanged agent.
            raise application_scope_refusal(self._evaluation_cases, (case_id,))
        bundle = (
            self._baseline_bundle if side == "baseline" else self._candidate_bundle
        )
        if bundle is None:
            raise ContractError(
                f"candidate attempt {attempt_id!r} has no verified change bundle; "
                "the Choose application path requires the sealed candidate's delta "
                "to be the runner's change-bundle JSON"
            )
        result, receipt_payload = self._inner.apply_config(
            run_id=run_id,
            attempt_id=attempt_id,
            workflow_id=workflow_id,
            case_input=case_input,
            mode=mode,
            change_bundle=bundle.to_dict(),
            provider_transport=self._provider_transport,
        )
        receipt = receipt_from_payload({"receipt": receipt_payload})
        validate_receipt(
            receipt,
            bundle=bundle,
            baseline=self._baseline,
            run_id=run_id,
            attempt_id=attempt_id,
            case_id=case_id,
            workflow_id=workflow_id,
            mode=mode.value,
            runner_version=result.runner_version,
        )
        # An explicitly non-empty delta whose effective configuration equals the
        # baseline stays the runner's honest no-op report (receipt.no_op); it is
        # never dressed up as an applied change — the comparison sees exactly
        # what the receipt says.
        self._refs_by_run.setdefault(run_id, {})[attempt_id] = tuple(result.evidence_refs)
        self._receipts_by_run.setdefault(run_id, {})[attempt_id] = receipt
        return self._project_usage(result, receipt)

    def _project_usage(
        self, result: AdapterExecution, receipt: ChooseApplicationReceipt
    ) -> AdapterExecution:
        """Quote the synthetic price and project honest case metrics.

        The runner keeps scripted tokens / measured elapsed ms / simulated
        credits as separate metering fields and never reports costUsd; the
        receipt carries the case's own outcome facts (supported claims). Both
        are projected into usage channels a frozen rubric can name — no
        side-based scores, no digest-to-score shortcuts.
        """
        usage = dict(result.usage) if result.usage else {}
        # Only a SUCCESSFUL attempt may be priced by projection. A failed
        # attempt's spend is UNKNOWN (the transport holds its reservation for
        # reconciliation) — inventing costUsd=0 here would let a failure book
        # zero spend, which the budget rules forbid.
        if result.ok and "costUsd" not in usage:
            tokens = int(usage.get("tokensScripted", 0) or 0)
            usage["costUsd"] = round(tokens * FIXTURE_USD_PER_TOKEN, 6)
            usage["priceNote"] = f"synthetic ${FIXTURE_USD_PER_TOKEN}/token (fixture mode)"
        if receipt.supported_claim_count is not None:
            usage["supportedClaims"] = receipt.supported_claim_count
        claim_count = result.outputs.get("claimCount")
        if isinstance(claim_count, int):
            usage["claimCount"] = claim_count
        return replace(result, usage=usage)

    def collect(self, run_id: str) -> tuple[str, ...]:
        """Persist the runner's verified in-frame artifacts durably.

        Every (digest, bytes) pair returned by the runner is re-hashed by the
        protocol layer and content-addressed into the project store; then the
        completeness gate proves each evidence reference the run's attempts
        cited is actually held. This runs BEFORE cleanup destroys the runner's
        ephemeral workspace — afterwards the durable store is the only copy.
        """
        artifacts = self._inner.collect_artifacts(run_id)
        digests: list[str] = []
        for digest, payload_bytes, _kind in artifacts:
            stored = self._artifacts.put(payload_bytes)
            if stored != digest:
                raise AdapterExecutionError(
                    f"collected artifact re-addressed to {stored}, runner declared "
                    f"{digest}; refusing to bind mismatched evidence"
                )
            digests.append(digest)
        cited = {
            ref
            for attempt_refs in self._refs_by_run.get(run_id, {}).values()
            for ref in attempt_refs
        }
        missing = sorted(cited - set(digests))
        if missing:
            raise AdapterExecutionError(
                f"run {run_id!r}: {len(missing)} evidence reference(s) the attempts "
                f"sealed are absent from the runner's collected artifacts "
                f"({missing[:5]}{'…' if len(missing) > 5 else ''}); a successful "
                "exit never counts as proof without the bound artifacts"
            )
        return tuple(digests)

    def cleanup(self, run_id: str) -> None:
        try:
            self._inner.cleanup(run_id)
        finally:
            self._prepared.discard(run_id)

    def close(self) -> None:
        self._inner.close()

    def receipts_of(self, run_id: str) -> dict[str, ChooseApplicationReceipt]:
        """Validated receipts of one run's attempts (attempt_id -> receipt)."""
        return dict(self._receipts_by_run.get(run_id, {}))


__all__ = [
    "APPLICATION_SCOPE_NOTE",
    "ChooseApplicationClient",
    "ChooseEvaluationCase",
    "application_scope_refusal",
    "evaluation_cases_from_describe",
]
