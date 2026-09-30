"""The M5b pilot dry-run: one concrete, discoverable, no-network plan.

The owner's gated decision (named endpoint/model, authorized material scope,
explicit total spend cap) is still pending — this module packages everything
the pilot WILL be so the operator reads one honest plan instead of promises,
and it is a RELIABLE readiness gate (2026-09-30 review):

* the request envelope comes from the runner's VERSIONED, ENFORCED work
  budget (max model requests per attempt), never a one-request-per-side
  guess — observed call counts are evidence about behavior, not ceilings;
* READY requires revalidated lineage: the candidate's parent is the CURRENT
  baseline, the runner's frozen baseline configuration still matches the one
  in the verified application receipts, the rubric and pack still match the
  evaluated run, and the candidate is not invalidated;
* candidate-side application receipts are RESOLVED AS BYTES and validated
  against this candidate's own change bundle, the advertised baseline and
  their attempt identity — a baseline-side receipt or an output summary
  dictionary proves nothing — and the exact verified receipt/attempt refs are
  persisted for fresh-process review;
* the owner's actual cap is pinned into the provider preflight configuration
  (its digest binds the cap); a missing cap leaves the budget UNKNOWN and
  BLOCKED, and withinCap is a bound check, never authorization;
* synthetic material scope stays labeled and blocking even when provider
  names are complete, and the not-implemented authorized-live mode is
  advertised BLOCKED.

Nothing here calls a provider, resolves a credential, or touches the network:
the preflight subprocess itself ``callsNothing``.
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vouch_agent.appservices.flow import configured_choose_repo
from vouch_agent.contracts.cases import CaseSplit
from vouch_agent.contracts.common import Role, digest_of
from vouch_agent.errors import ContractError

PILOT_DRYRUN_KIND = "pilot-dryrun"
#: Estimate-only cap used when the owner cap is still unknown; the plan then
#: reports withinCap=None and stays BLOCKED — this number never authorizes.
ESTIMATE_ONLY_CAP_USD = 1_000_000.0

#: The fixed stop/reconciliation policy every plan carries verbatim.
STOP_RULES = (
    "Stop on the first unknown-spend attempt (the reservation stays held; "
    "nothing retries past an unexplained cost), on any hard-guardrail "
    "violation, or when committed spend reaches the cap.",
    "Reconciliation is explicit and verifiable: 'vouch resume --reconcile "
    "<reservation> --verified-note ... [--settle USD | --release]' after the "
    "actual spend was checked; blind replay after unknown side effects is "
    "refused.",
    "A failed or truncated stream attempt keeps its reservation HELD for "
    "reconciliation — overruns are never erased and failure never books zero.",
    "withinCap is a bound check over the enforced envelope, never spend "
    "authorization: authorization is the owner's explicit cap plus the M5b "
    "gate (endpoint/model, authorized material scope).",
)

_GRADER_STATEMENT = (
    "Independent graders: the rubric frozen after baseline measurement "
    "(digest-bound above) and the deterministic comparator score every paired "
    "attempt; acceptance is decided by the configured acceptance owner (never "
    "the proposer) and release is recorded by the release owner. No model "
    "judges its own output and no score is looked up by digest."
)


def _finite(value: float, name: str, minimum: float, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"provider plan field {name!r} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ContractError(f"provider plan field {name!r} must be finite, got {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        top = f"..{maximum}" if maximum is not None else ""
        raise ContractError(
            f"provider plan field {name!r} must be >= {minimum}{top}, got {value!r}"
        )


@dataclass(frozen=True)
class ProviderPlanInput:
    """Owner-supplied provider facts. Placeholders are allowed and BLOCK.

    Credential VALUES never appear here — only the owning service's
    credential NAME travels, and only into the preflight (which resolves
    nothing).
    """

    provider_id: str = "SYNTHETIC-owner-input-required"
    endpoint: str = "https://synthetic-owner-input-required.invalid/v1/chat/completions"
    model_id: str = "SYNTHETIC-owner-input-required"
    credential_name: str = "SYNTHETIC-owner-named-credential"
    price_table_version: str = "SYNTHETIC-price-table-unversioned"
    input_usd_per_million_tokens: float = 0.0
    output_usd_per_million_tokens: float = 0.0
    temperature: float = 0.0
    max_output_tokens: int = 1024
    top_p: float = 1.0
    request_timeout_ms: int = 60_000
    max_retries: int = 1
    retry_backoff_ms: int = 500
    max_response_bytes: int = 1_048_576
    reserve_prompt_tokens: int = 8_192

    def __post_init__(self) -> None:
        for name in ("provider_id", "model_id", "credential_name", "price_table_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractError(f"provider plan field {name!r} must be a non-empty string")
        _finite(self.input_usd_per_million_tokens, "input_usd_per_million_tokens", 0)
        _finite(self.output_usd_per_million_tokens, "output_usd_per_million_tokens", 0)
        _finite(self.temperature, "temperature", 0, 2)
        _finite(self.top_p, "top_p", 0, 1)
        _finite(self.request_timeout_ms, "request_timeout_ms", 1, 600_000)
        _finite(self.retry_backoff_ms, "retry_backoff_ms", 0, 60_000)
        _finite(self.max_response_bytes, "max_response_bytes", 1, 64 * 1024 * 1024)
        _finite(self.reserve_prompt_tokens, "reserve_prompt_tokens", 1, 10_000_000)
        _finite(self.max_retries, "max_retries", 0, 10)
        _finite(self.max_output_tokens, "max_output_tokens", 1, 200_000)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderPlanInput:
        if not isinstance(data, dict):
            raise ContractError("provider plan must be a JSON object")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - known)
        if unknown:
            raise ContractError(f"provider plan carries unknown fields: {unknown}")
        return cls(**{k: v for k, v in data.items() if k in known})

    def placeholders(self) -> list[str]:
        missing = [
            name
            for name in ("provider_id", "endpoint", "model_id", "credential_name")
            if str(getattr(self, name)).startswith("SYNTHETIC")
        ]
        if self.price_table_version.startswith("SYNTHETIC"):
            missing.append("price_table_version")
        return missing


@dataclass(frozen=True)
class WorkBudgetEnvelope:
    """The runner's advertised, enforced per-attempt request envelope."""

    version: int
    task: str
    slices: int
    max_model_requests_per_attempt: int
    input_tokens: int
    output_tokens: int
    per_step_output_tokens: int
    reserve_input_tokens_per_step: int


def _describe_scope(repo: Path) -> dict[str, Any]:
    """Describe the runner and return the payload facts the plan depends on.

    Refuses (fail closed) when the runner cannot advertise the case
    vocabulary or the VERSIONED work budget — undefined bounds never become a
    guessed envelope.
    """
    from vouch_agent.adapters.process_adapter import DEFAULT_EXECUTE_TIMEOUT_S, ProcessAdapterClient

    tsx = repo / "node_modules" / ".bin" / "tsx"
    if not tsx.is_file() or not (repo / "scripts" / "vouch" / "runner.ts").is_file():
        raise ContractError(
            f"the Choose checkout at {repo} cannot run the runner "
            "(no node_modules/.bin/tsx or no scripts/vouch/runner.ts); set "
            "VOUCH_CHOOSE_RUNNER_DIR to a prepared checkout — a dry-run "
            "without the real runner's advertised facts is refused"
        )
    inner = ProcessAdapterClient(
        [str(tsx), "scripts/vouch/runner.ts"],
        cwd=str(repo),
        execute_timeout_s=DEFAULT_EXECUTE_TIMEOUT_S,
    )
    try:
        inner.describe()
        payload = inner.describe_payload
    finally:
        inner.close()
    if not isinstance(payload.get("evaluationCases"), list):
        raise ContractError(
            "the Choose runner advertises no evaluation-case vocabulary; the "
            "plan cannot reconcile the material scope (refused, fail closed)"
        )
    return payload


def _require_work_budget(scope: dict[str, Any]) -> None:
    """Refuse an undefined model-request envelope (never guess one per side)."""
    budget = scope.get("workBudget")
    if not isinstance(budget, dict) or not isinstance(budget.get("envelopes"), dict):
        raise ContractError(
            "the Choose runner advertises no VERSIONED work budget; the "
            "model-request envelope is undefined and a worst-case pilot "
            "budget would be a guess — refused (one request per side is not "
            "an envelope)"
        )


def _envelope_for(scope: dict[str, Any], case_ids: tuple[str, ...]) -> WorkBudgetEnvelope:
    """Resolve the enforced envelope covering every planned case.

    All cases of an application pack share one task's envelope; a mixed or
    unknown task refuses (undefined bounds).
    """
    cases = {str(entry.get("caseId")): entry for entry in scope["evaluationCases"]}
    budget = scope["workBudget"]
    envelopes: dict[str, WorkBudgetEnvelope] = {}
    for task, raw in budget["envelopes"].items():
        envelopes[str(task)] = WorkBudgetEnvelope(
            version=int(budget.get("version", 0)),
            task=str(task),
            slices=int(raw.get("slices", 0)),
            max_model_requests_per_attempt=int(raw.get("maxModelRequestsPerAttempt", 0)),
            input_tokens=int(raw.get("inputTokens", 0)),
            output_tokens=int(raw.get("outputTokens", 0)),
            per_step_output_tokens=int(raw.get("perStepOutputTokens", 0)),
            reserve_input_tokens_per_step=int(raw.get("reserveInputTokensPerStep", 0)),
        )
    resolved: WorkBudgetEnvelope | None = None
    for case_id in case_ids:
        entry = cases.get(case_id)
        task = str(entry.get("task", "")) if isinstance(entry, dict) else ""
        envelope = envelopes.get(task)
        if envelope is None or envelope.max_model_requests_per_attempt < 1:
            raise ContractError(
                f"case {case_id!r} has no enforced model-request envelope "
                f"(task {task!r}); undefined bounds refuse the plan"
            )
        if resolved is None:
            resolved = envelope
        elif resolved.task != envelope.task:
            raise ContractError(
                "the planned cases span multiple task envelopes "
                f"({resolved.task!r} and {envelope.task!r}); refuse rather "
                "than mix bounds"
            )
    assert resolved is not None  # case_ids is non-empty by contract
    return resolved


def _run_choose_preflight(
    provider: ProviderPlanInput,
    *,
    planned_requests: int,
    reserve_prompt_tokens: int,
    max_output_tokens: int,
    spend_cap_usd: float,
    repo: Path,
) -> dict[str, Any]:
    """Drive the documented preflight CLI (it calls nothing, resolves nothing)."""
    tsx = repo / "node_modules" / ".bin" / "tsx"
    config = {
        "schemaVersion": 1,
        "kind": "choose-provider-config",
        "providerId": provider.provider_id,
        "endpoint": provider.endpoint,
        "modelId": provider.model_id,
        "api": "openai-chat-completions-sse",
        "parameters": {
            "temperature": provider.temperature,
            "maxOutputTokens": max_output_tokens,
            "topP": provider.top_p,
        },
        "endpointPolicy": {
            "allowInsecureLoopback": False,
            "allowedHostSuffix": "",
            "requestTimeoutMs": provider.request_timeout_ms,
            "maxRetries": provider.max_retries,
            "retryBackoffMs": provider.retry_backoff_ms,
            "maxResponseBytes": provider.max_response_bytes,
            "reservePromptTokens": reserve_prompt_tokens,
        },
        "priceTable": {
            "priceTableVersion": provider.price_table_version,
            "inputUsdPerMillionTokens": provider.input_usd_per_million_tokens,
            "outputUsdPerMillionTokens": provider.output_usd_per_million_tokens,
        },
        "credentialRef": {"name": provider.credential_name},
        "spendCapUsd": spend_cap_usd,
        "fixtureFallback": False,
    }
    scope = {
        "plannedRequests": planned_requests,
        "approxPromptTokensPerRequest": reserve_prompt_tokens,
        "approxOutputTokensPerRequest": max_output_tokens,
    }
    with tempfile.TemporaryDirectory(prefix="vouch-pilot-dryrun-") as tmp:
        config_path = Path(tmp) / "provider-config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        command = [
            str(tsx),
            "scripts/vouch/provider-transport.ts",
            "--preflight",
            "--config",
            str(config_path),
            "--scope",
            json.dumps(scope),
        ]
        proc = subprocess.run(
            command,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
            raise ContractError(
                "the Choose provider preflight refused the plan's config: "
                f"{tail[0] if tail else 'no diagnostics'}; a dry-run budget is "
                "computed by the real preflight or not at all"
            )
        lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith("{"):
                plan = json.loads(line)
                if plan.get("kind") == "choose-provider-preflight":
                    return plan
    raise ContractError(
        "the Choose provider preflight produced no parseable plan; refusing to "
        "guess a pilot budget"
    )


def _latest_run_for(workspace: Any, candidate_digest: str) -> dict[str, Any] | None:
    """The most recent completed evaluation run of this candidate digest."""
    from vouch_agent.controller.service import KIND_EVALUATION

    best: dict[str, Any] | None = None
    best_at = ""
    for run_id in workspace.store.list_ids(KIND_EVALUATION):
        data = workspace.store.load(KIND_EVALUATION, run_id)
        if not isinstance(data, dict) or data.get("candidateDigest") != candidate_digest:
            continue
        if data.get("executionStatus") != "completed":
            continue
        ended = max(
            (str(attempt.get("endedAt", "")) for attempt in data.get("attempts", [])),
            default="",
        )
        if best is None or ended >= best_at:
            best, best_at = data, ended
    return best


def _verify_application_evidence(
    workspace: Any,
    *,
    run: dict[str, Any] | None,
    bundle_delta_digest: str,
    advertised_baseline_digest: str,
    advertised_baseline_version: str,
) -> tuple[list[str], dict[str, Any] | None]:
    """Resolve and validate the CANDIDATE-side application receipts as bytes.

    A baseline-side receipt, an output-summary dictionary, or a receipt that
    does not bind THIS candidate's bundle, THIS run's attempts and the
    advertised baseline proves nothing. Every candidate attempt's evidence
    closure must resolve. Returns (blocked reasons, verified evidence record).
    """
    if run is None:
        return [
            "no completed evaluation run exists for this candidate's digest: "
            "the candidate must first travel the public application path "
            "('vouch evaluate --adapter choose') so the pilot's receipts are "
            "the verified path's receipts"
        ], None
    run_id = str(run.get("runId", ""))
    workflow_id = str(run.get("workflowId", ""))
    reasons: list[str] = []
    candidate_entries: list[dict[str, Any]] = []
    baseline_receipts = 0
    runner_versions: set[str] = set()
    saw_candidate_attempt = False

    for attempt in run.get("attempts", []):
        if attempt.get("side") != "candidate":
            # Baseline-side receipts are resolved for closure only — they
            # never establish candidate application.
            digest = attempt.get("outputDigest")
            if not digest:
                continue
            try:
                sealed = json.loads(workspace.artifacts.get(str(digest)))
            except Exception:
                continue
            for ref in sealed.get("evidenceRefs", []):
                try:
                    obj = json.loads(workspace.artifacts.get(str(ref)))
                except Exception:
                    continue
                if obj.get("artifactRole") == "application-receipt":
                    baseline_receipts += 1
            continue
        saw_candidate_attempt = True
        attempt_id = str(attempt.get("attemptId", ""))
        digest = attempt.get("outputDigest")
        if not digest:
            reasons.append(f"candidate attempt {attempt_id!r} sealed no output artifact")
            continue
        try:
            sealed = json.loads(workspace.artifacts.get(str(digest)))
        except Exception as exc:  # unreadable bytes = absent
            reasons.append(
                f"candidate attempt {attempt_id!r} output artifact does not "
                f"resolve ({exc})"
            )
            continue
        refs = sealed.get("evidenceRefs", [])
        receipts: list[dict[str, Any]] = []
        missing: list[str] = []
        for ref in refs:
            try:
                obj = json.loads(workspace.artifacts.get(str(ref)))
            except Exception:
                missing.append(str(ref))
                continue
            if obj.get("artifactRole") == "application-receipt":
                receipts.append(obj)
        if missing:
            reasons.append(
                f"candidate attempt {attempt_id!r} cites evidence that does "
                f"not resolve in the durable store: {missing[:2]}"
            )
        if not receipts:
            reasons.append(
                f"candidate attempt {attempt_id!r} has no application-receipt "
                "artifact (an output summary's 'application' dictionary is "
                "not receipt evidence)"
            )
            continue
        if len(receipts) > 1:
            reasons.append(
                f"candidate attempt {attempt_id!r} cites {len(receipts)} "
                "application receipts; exactly one is expected"
            )
            continue
        receipt = receipts[0]
        problems: list[str] = []
        if receipt.get("kind") != "choose-config-application-receipt":
            problems.append("wrong receipt kind")
        if receipt.get("receiptVersion") != 1:
            problems.append("unsupported receipt version")
        if str(receipt.get("runId", "")) != run_id:
            problems.append("receipt binds a different run")
        if str(receipt.get("attemptId", "")) != attempt_id:
            problems.append("receipt binds a different attempt")
        if str(receipt.get("caseId", "")) != str(attempt.get("caseId", "")):
            problems.append("receipt binds a different case")
        if str(receipt.get("workflowId", "")) != workflow_id:
            problems.append("receipt binds a different workflow")
        if receipt.get("mode") != "fixture":
            problems.append("receipt mode is not fixture")
        if receipt.get("transportObserved") is not True:
            problems.append("transportObserved is not true")
        if receipt.get("observedConfigDigest") != receipt.get("appliedConfigDigest"):
            problems.append("observed configuration disagrees with the applied one")
        if str(receipt.get("requestedDeltaDigest", "")) != bundle_delta_digest:
            problems.append("receipt's requested delta is not THIS candidate's change bundle")
        if str(receipt.get("baselineConfigDigest", "")) != advertised_baseline_digest:
            problems.append(
                "receipt baseline differs from the runner's advertised frozen "
                "baseline (configuration drift)"
            )
        if str(receipt.get("baselineConfigVersion", "")) != advertised_baseline_version:
            problems.append("receipt baseline version differs from the advertised one")
        if receipt.get("noOp") is True and receipt.get("appliedConfigDigest") != receipt.get(
            "baselineConfigDigest"
        ):
            problems.append("no-op receipt with a non-baseline applied config")
        runner_versions.add(str(receipt.get("runnerVersion", "")))
        if problems:
            reasons.append(
                f"candidate attempt {attempt_id!r} receipt failed validation: "
                + "; ".join(problems)
            )
            continue
        candidate_entries.append(
            {
                "attemptId": attempt_id,
                "caseId": str(attempt.get("caseId", "")),
                "receiptDigest": digest_of(receipt),
                "requestedDeltaDigest": str(receipt["requestedDeltaDigest"]),
                "appliedConfigDigest": str(receipt["appliedConfigDigest"]),
                "noOp": bool(receipt["noOp"]),
            }
        )

    if not saw_candidate_attempt:
        reasons.append(
            "the evaluated run has no candidate-side attempts; baseline-side "
            "receipts do not establish candidate application"
        )
    evidence = {
        "runId": run_id,
        "split": str(run.get("split", "")),
        "runnerVersion": (sorted(v for v in runner_versions if v) or [""])[0],
        "baselineConfigDigest": advertised_baseline_digest,
        "candidateAttempts": candidate_entries,
        "baselineReceiptsResolved": baseline_receipts,
    }
    return reasons, evidence


def plan_pilot_dryrun(
    workspace: Any,
    *,
    candidate_id: str,
    pack_ref: str,
    split: CaseSplit,
    repeats: int,
    provider: ProviderPlanInput,
    owner_cap_usd: float | None,
    role: Role = Role.EVALUATOR,
) -> dict[str, Any]:
    """Build (and persist) the operator-ready M5b dry-run plan record.

    Fails closed on invalid input or undefined bounds; BLOCKS (explicit,
    never silently ready) on pending owner input, lineage/config drift,
    unverifiable receipts, synthetic material scope, the not-implemented
    authorized-live mode, or a plan beyond the cap. Returns the persisted
    record dict (status "ready" | "blocked").
    """
    if repeats < 1:
        raise ContractError("repeats must be >= 1; a pilot without paired repeats is refused")
    if owner_cap_usd is not None:
        _finite(owner_cap_usd, "owner_cap_usd", 0.000001, 1_000_000)

    from vouch_agent.adapters.choose_bundle import ChooseBaselineConfig, parse_candidate_delta
    from vouch_agent.appservices.flow import ImprovementFlow

    scope = _describe_scope(configured_choose_repo())
    _require_work_budget(scope)
    baseline_config = ChooseBaselineConfig.from_describe(scope)
    controller = workspace.controller()
    candidate = controller.candidate(candidate_id)

    flow = ImprovementFlow(workspace)
    workflow_id = flow._single_workflow()  # appservice-internal reuse
    rubric = flow.rubric_for(workflow_id)
    current_baseline = flow.baseline_for(workflow_id)
    pack = flow.load_pack(pack_ref, role=role)
    if pack.workflow_id != workflow_id:
        raise ContractError(
            f"pack {pack.pack_id!r} belongs to workflow {pack.workflow_id!r}, "
            f"not {workflow_id!r}"
        )
    cases = pack.cases_in(split)
    if not cases:
        raise ContractError(
            f"pack {pack.pack_id!r} has no cases in split {split.value}; the "
            "material scope is undefined and the pilot is refused"
        )

    # -- the enforced request envelope (never one request per side) ---------
    envelope = _envelope_for(scope, tuple(case.case_id for case in cases))
    planned_requests = (
        len(cases)
        * 2
        * repeats
        * envelope.max_model_requests_per_attempt
        * (1 + provider.max_retries)
    )
    reserve_prompt_tokens = max(
        provider.reserve_prompt_tokens, envelope.reserve_input_tokens_per_step
    )
    max_output_tokens = max(provider.max_output_tokens, envelope.per_step_output_tokens)

    # -- the budget: owner cap pinned into the exact preflight config -------
    estimate_only = owner_cap_usd is None
    spend_cap = owner_cap_usd if owner_cap_usd is not None else ESTIMATE_ONLY_CAP_USD
    preflight = _run_choose_preflight(
        provider,
        planned_requests=planned_requests,
        reserve_prompt_tokens=reserve_prompt_tokens,
        max_output_tokens=max_output_tokens,
        spend_cap_usd=spend_cap,
        repo=configured_choose_repo(),
    )
    worst_case = preflight.get("budget", {}).get("worstCaseUsd")
    if not isinstance(worst_case, (int, float)):
        raise ContractError("the preflight reported no worst-case spend; refusing to guess")
    within_cap: bool | None = None
    if owner_cap_usd is not None:
        within_cap = bool(preflight.get("budget", {}).get("withinCap")) and worst_case <= float(
            owner_cap_usd
        )

    blocked: list[str] = []
    placeholders = provider.placeholders()
    if placeholders:
        blocked.append(
            "provider selection pending owner input (M5b gate): "
            f"{placeholders} are SYNTHETIC placeholders; the owner must name the "
            "endpoint/model/credential/price table before any pilot runs"
        )
    if estimate_only:
        blocked.append(
            "no explicit total spend cap: the owner's named cap is a hard M5b "
            "gate (elapsed time is not consent to paid inference); the "
            "worst-case figure is ESTIMATE-ONLY"
        )

    # -- lineage revalidation (drift blocks until explicit rebase) ----------
    if candidate.parent_version.digest() != current_baseline.digest():
        blocked.append(
            "baseline drift: the candidate's parent baseline "
            f"{candidate.parent_version.digest()} is not the current baseline "
            f"{current_baseline.digest()}; an explicit rebase and fresh "
            "evidence are required before the plan can be ready"
        )
    bundle = parse_candidate_delta(candidate.delta)

    run = _latest_run_for(workspace, candidate.content_digest())
    receipt_reasons, evidence = _verify_application_evidence(
        workspace,
        run=run,
        bundle_delta_digest=bundle.delta_digest,
        advertised_baseline_digest=baseline_config.digest,
        advertised_baseline_version=baseline_config.config_version,
    )
    blocked.extend(receipt_reasons)
    if run is not None:
        if str(run.get("rubricDigest", "")) != rubric.digest():
            blocked.append(
                "rubric drift: the frozen rubric changed since the evaluated "
                "run; re-evaluate before planning the pilot"
            )
        if str(run.get("caseSetDigest", "")) != pack.digest():
            blocked.append(
                "pack drift: the plan's pack digest differs from the one the "
                "evaluated run executed"
            )
    if candidate.state.value == "invalidated":
        blocked.append("the candidate is invalidated")

    # -- honest labels -------------------------------------------------------
    material_synthetic = all(case.synthetic for case in cases)
    if material_synthetic:
        blocked.append(
            "material scope is SYNTHETIC (fixture material): the owner must "
            "authorize the actual pilot material scope (M5b gate) — complete "
            "provider names do not authorize synthetic materials"
        )
    modes = scope.get("providerModes")
    provider_modes = {
        "fixture": {"implemented": True},
        "transportSimulation": {
            "implemented": bool(
                isinstance(modes, dict)
                and isinstance(modes.get("transportSimulation"), dict)
                and modes["transportSimulation"].get("implemented") is True
            ),
            "loopbackOnly": True,
        },
        "authorizedLive": {
            "implemented": bool(
                isinstance(modes, dict)
                and isinstance(modes.get("authorizedLive"), dict)
                and modes["authorizedLive"].get("implemented") is True
            ),
        },
    }
    if not provider_modes["authorizedLive"]["implemented"]:
        blocked.append(
            "authorized-live provider mode is NOT implemented: the pilot can "
            "only be SIMULATED locally (transport simulation, loopback fake "
            "provider) until M5b implements the authorized mode"
        )
    if owner_cap_usd is not None and worst_case > float(owner_cap_usd):
        blocked.append(
            f"worst-case spend ${worst_case:.4f} exceeds the owner cap "
            f"${owner_cap_usd:.4f}; the plan is beyond the cap and stays blocked"
        )

    synthetic = bool(placeholders) or estimate_only or material_synthetic
    record = {
        "schemaVersion": "2",
        "kind": "vouch-pilot-dryrun",
        "status": "blocked" if blocked else "ready",
        "blockedReasons": blocked,
        "synthetic": synthetic,
        "candidate": {
            "candidateId": candidate.candidate_id,
            "candidateDigest": candidate.content_digest(),
            "baselineDigest": current_baseline.digest(),
            "parentBaselineDigest": candidate.parent_version.digest(),
        },
        "rubricDigest": rubric.digest(),
        "workflowId": workflow_id,
        "materialScope": {
            "casePackDigest": pack.digest(),
            "caseIds": [case.case_id for case in cases],
            "split": split.value,
            "repeats": repeats,
            "synthetic": material_synthetic,
            "graderStatement": _GRADER_STATEMENT,
        },
        "requests": {
            "planned": planned_requests,
            "maxRetriesPerRequest": provider.max_retries,
            "envelope": {
                "source": "choose work budget (enforced)",
                "version": envelope.version,
                "task": envelope.task,
                "slices": envelope.slices,
                "maxModelRequestsPerAttempt": envelope.max_model_requests_per_attempt,
                "perStepOutputTokens": envelope.per_step_output_tokens,
                "reserveInputTokensPerStep": envelope.reserve_input_tokens_per_step,
            },
            "note": (
                "planned = cases x 2 sides x repeats x maxModelRequestsPerAttempt "
                "x (1 + maxRetries), from the runner's versioned enforced work "
                "budget — observed call counts are evidence, not ceilings"
            ),
        },
        "provider": {
            "credentialName": provider.credential_name,
            "preflight": preflight,
        },
        "providerModes": provider_modes,
        "budget": {
            "ownerCapUsd": owner_cap_usd,
            "worstCaseUsd": worst_case,
            "withinCap": within_cap,
            "estimateOnly": estimate_only,
        },
        "verifiedEvidence": evidence,
        "stopRules": list(STOP_RULES),
    }
    record["planDigest"] = digest_of({k: v for k, v in record.items() if k != "planDigest"})
    workspace.store.save(PILOT_DRYRUN_KIND, f"pilot-dryrun-{candidate_id}", record)
    return record


__all__ = [
    "ESTIMATE_ONLY_CAP_USD",
    "PILOT_DRYRUN_KIND",
    "STOP_RULES",
    "ProviderPlanInput",
    "WorkBudgetEnvelope",
    "plan_pilot_dryrun",
]
