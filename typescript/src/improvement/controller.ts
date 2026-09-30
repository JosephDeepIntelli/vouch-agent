/**
 * Improvement controller subset (trusted): baseline/rubric freezing, bounded
 * proposals with seal/invalidate, paired evaluation through the versioned
 * adapter protocol with per-attempt atomic budget reservations, fail-closed
 * comparison + verdict, acceptance decisions bound to digest tuples with
 * approval invalidation on change, releases and rollback records.
 */

import { digestBytes, digestOf } from "../contracts/canonical.ts";
import {
  ApprovalInvalidatedError,
  BudgetExhaustedError,
  ContractError,
  GateDeniedError,
  newId,
  utcNowIso,
} from "../contracts/common.ts";
import { eventRecord } from "../contracts/journal.ts";
import {
  type AcceptanceDecision,
  type AgentVersion,
  agentVersionFromDict,
  type ApprovalBinding,
  bindingDigest,
  type Candidate,
  CANDIDATE_TRANSITIONS,
  candidateContentDigest,
  candidateFromDict,
  candidateSealable,
  type CandidateState,
  candidateWithTransition,
  type ComparisonSummary,
  type MetricDelta,
  type ReleaseRecord,
  type Rubric,
  rubricDigest,
  rubricFromDict,
  UNRECORDED_ENVIRONMENT_DIGEST,
  type Verdict,
  verifyBinding,
} from "./contracts.ts";
import { verdictFromSummary } from "./verdict.ts";
import type { ProcessAdapterClient } from "../adapters/process_adapter.ts";
import type { ProjectWorkspace } from "../appservices/workspace.ts";

export const KIND_BASELINE = "baseline";
export const KIND_RUBRIC = "rubric";
export const KIND_CANDIDATE = "candidate";
export const KIND_EVALUATION = "evaluation";
export const KIND_ATTEMPT = "attempt";
export const KIND_DECISION = "decision";
export const KIND_RELEASE = "release";
export const KIND_WORKFLOW_BASELINE = "workflow-baseline";
export const KIND_WORKFLOW_RUBRIC = "workflow-rubric";
export const KIND_RUN_EXPORT = "run-export";

export const CASE_SET_KIND = "task-case";

export type CaseSplit = "development" | "selection-validation" | "final-acceptance";

export interface AttemptRecord {
  schemaVersion: string;
  attemptId: string;
  runId: string;
  caseId: string;
  repeat: number;
  side: "baseline" | "candidate";
  ok: boolean;
  metric: number | null;
  guardrails: Record<string, boolean>;
  usage: Record<string, unknown> | null;
  error: string | null;
  reservationId: string | null;
}

export interface EvaluationRun {
  schemaVersion: string;
  runId: string;
  workflowId: string;
  candidateId: string;
  candidateDigest: string;
  baselineVersionId: string;
  rubricDigest: string;
  caseSetDigest: string;
  environmentDigest: string | null;
  split: CaseSplit;
  mode: string;
  repeats: number;
  attempts: AttemptRecord[];
  comparison: ComparisonSummary | null;
  verdict: Verdict | null;
  createdAt: string;
  completedAt: string | null;
}

// --- gate policy (R1 scratch scope for evaluation attempts) ------------------------

const GATE_MAX_RESERVATION_USD = 0.5;

function gateScope(workspace: ProjectWorkspace): Set<string> {
  return new Set(workspace.store.listIds(CASE_SET_KIND));
}

/**
 * Run-adapter-attempt gate: R1 risk, resources restricted to imported case
 * ids or eval_-prefixed scratch runs, atomic reservation before dispatch.
 */
export function authorizeAdapterAttempt(
  workspace: ProjectWorkspace,
  options: { runId: string; caseIds: string[]; reserveUsd: number },
): string {
  const scope = gateScope(workspace);
  // (single allowed action: run-adapter-attempt, risk class R1)
  if (!options.runId.startsWith("eval_")) {
    throw new GateDeniedError(
      `gate refused: adapter attempts run under eval_ scratch runs (got ${
        JSON.stringify(options.runId)
      })`,
    );
  }
  const unknown = options.caseIds.filter((id) => !scope.has(id));
  if (unknown.length > 0) {
    throw new GateDeniedError(
      `gate refused: cases ${JSON.stringify(unknown)} were never imported into this workspace; ` +
        `a case that was not imported cannot be named by an attempt`,
    );
  }
  if (options.reserveUsd > GATE_MAX_RESERVATION_USD + 1e-9) {
    throw new GateDeniedError(
      `gate refused: reservation $${options.reserveUsd} exceeds the R1 per-attempt cap ` +
        `$${GATE_MAX_RESERVATION_USD}`,
    );
  }
  const reservation = workspace.ledger.reserve(
    `attempt:${options.runId}`,
    options.reserveUsd,
  );
  workspace.journal.append(
    eventRecord("gate-decision", options.runId, {
      action: "run-adapter-attempt",
      risk: "R1",
      caseCount: options.caseIds.length,
      reservationId: reservation.reservationId,
      reserveUsd: options.reserveUsd,
    }, { actor: "gate" }),
  );
  return reservation.reservationId;
}

// --- controller -----------------------------------------------------------------

export class ImprovementController {
  constructor(private workspace: ProjectWorkspace) {}

  private event(subject: string, kind: string, data: Record<string, unknown>): void {
    this.workspace.journal.append(
      eventRecord(kind as never, subject, data, { actor: "controller" }),
    );
  }

  candidate(candidateId: string): Candidate {
    const data = this.workspace.store.load(KIND_CANDIDATE, candidateId);
    if (data === null) throw new ContractError(`unknown candidate ${JSON.stringify(candidateId)}`);
    return candidateFromDict(data);
  }

  rubric(digest: string): Rubric {
    const data = this.workspace.store.load(KIND_RUBRIC, digest);
    if (data === null) throw new ContractError(`unknown rubric ${JSON.stringify(digest)}`);
    return rubricFromDict(data);
  }

  /** Freeze a rubric ONCE, recording who froze it. Re-freezing is refused. */
  freezeRubric(rubricIn: Rubric, frozenBy: string): { digest: string; rubric: Rubric } {
    if (rubricIn.frozenBy !== null || rubricIn.frozenAt !== null) {
      throw new ContractError(
        "rubric is already frozen; create a new rubric version instead of re-freezing",
      );
    }
    if (frozenBy.trim().length === 0) {
      throw new ContractError("frozen_by must be a non-empty identity string");
    }
    const frozen: Rubric = { ...rubricIn, frozenBy, frozenAt: utcNowIso() };
    const digest = rubricDigest(frozen);
    this.workspace.store.save(KIND_RUBRIC, digest, frozen);
    return { digest, rubric: frozen };
  }

  recordBaseline(version: AgentVersion, workflowId: string): string {
    const recordId = newId("base");
    this.workspace.store.save(KIND_BASELINE, recordId, version);
    this.workspace.store.save(KIND_WORKFLOW_BASELINE, workflowId, {
      versionId: version.versionId,
      baselineRecordId: recordId,
    });
    this.event(workflowId, "audit-note", {
      action: "baseline-recorded",
      versionId: version.versionId,
    });
    return recordId;
  }

  baselineFor(workflowId: string): AgentVersion {
    const mapping = this.workspace.store.load(KIND_WORKFLOW_BASELINE, workflowId);
    if (mapping === null) {
      throw new ContractError(
        `workflow ${
          JSON.stringify(workflowId)
        } has no recorded baseline; run 'vouch improve baseline' first`,
      );
    }
    const record = this.workspace.store.load(KIND_BASELINE, String(mapping["baselineRecordId"]));
    if (record === null) throw new ContractError("baseline record missing");
    return agentVersionFromDict(record);
  }

  rubricFor(workflowId: string, digest?: string): Rubric {
    if (digest !== undefined) return this.rubric(digest);
    const mapping = this.workspace.store.load(KIND_WORKFLOW_RUBRIC, workflowId);
    if (mapping === null) {
      throw new ContractError(
        `workflow ${
          JSON.stringify(workflowId)
        } has no frozen rubric; run 'vouch improve baseline' ` +
          `first (thresholds freeze after baseline, before candidate search)`,
      );
    }
    return this.rubric(String(mapping["rubricDigest"]));
  }

  propose(candidate: Candidate): string {
    candidateSealable(candidate, this.workspace.spec.allowedChangeTypes);
    this.workspace.store.save(KIND_CANDIDATE, candidate.candidateId, candidate);
    this.event(candidate.candidateId, "candidate-state", { state: "proposed" });
    return candidate.candidateId;
  }

  seal(candidateId: string): Candidate {
    const candidate = this.candidate(candidateId);
    const sealed = candidateWithTransition(candidate, "sealed");
    this.workspace.store.save(KIND_CANDIDATE, candidateId, sealed);
    this.event(candidateId, "candidate-state", {
      state: "sealed",
      digest: digestOf(sealed as never),
    });
    return sealed;
  }

  invalidate(candidateId: string, reason: string): Candidate {
    const candidate = this.candidate(candidateId);
    const invalidated = candidateWithTransition(candidate, "invalidated", reason);
    this.workspace.store.save(KIND_CANDIDATE, candidateId, invalidated);
    this.event(candidateId, "approval-invalidated", { reason });
    return invalidated;
  }

  importCase(caseId: string, workflowId: string, split: CaseSplit, inputBytes: Uint8Array): string {
    const inputDigest = this.workspace.artifacts.put(inputBytes);
    this.workspace.store.save(CASE_SET_KIND, caseId, {
      schemaVersion: "1",
      caseId,
      workflowId,
      split,
      inputDigest,
    });
    return inputDigest;
  }

  caseSetDigest(caseIds: string[]): string {
    return digestOf({ cases: [...caseIds].sort() });
  }

  /**
   * Execute baseline+candidate on every case of `split`, through the
   * versioned adapter protocol. Budget is reserved before each attempt and
   * settled after; failures and timeouts are booked as attempts too. The
   * candidate must be sealed.
   */
  async runPairedEvaluation(options: {
    workflowId: string;
    candidateId: string;
    baseline: AgentVersion;
    adapter: ProcessAdapterClient;
    split: CaseSplit;
    rubricDigest: string;
    repeats: number;
    cases: Array<{ caseId: string; input: Record<string, unknown> }>;
    perAttemptReserveUsd?: number;
    mode?: string;
  }): Promise<EvaluationRun> {
    const candidate = this.candidate(options.candidateId);
    if (candidate.state !== "sealed" && candidate.state !== "evaluated") {
      throw new ContractError(
        `candidate ${options.candidateId} must be sealed before evaluation (state=${candidate.state})`,
      );
    }
    if (candidate.parentVersion.versionId !== options.baseline.versionId) {
      throw new ContractError(
        `candidate parent ${candidate.parentVersion.versionId} is not the workflow baseline ` +
          `${options.baseline.versionId}; proposals must descend from the frozen baseline`,
      );
    }
    const mode = options.mode ?? "fixture";
    const runId = `eval_${newId("run")}`;
    const perAttempt = options.perAttemptReserveUsd ?? 0.05;
    const reservationId = authorizeAdapterAttempt(this.workspace, {
      runId,
      caseIds: options.cases.map((c) => c.caseId),
      reserveUsd: perAttempt,
    });

    const candidateDigest = candidateContentDigest(candidate);
    const run: EvaluationRun = {
      schemaVersion: "1",
      runId,
      workflowId: options.workflowId,
      candidateId: options.candidateId,
      candidateDigest,
      baselineVersionId: options.baseline.versionId,
      rubricDigest: options.rubricDigest,
      caseSetDigest: this.caseSetDigest(options.cases.map((c) => c.caseId)),
      environmentDigest: options.baseline.environmentDigest,
      split: options.split,
      mode,
      repeats: options.repeats,
      attempts: [],
      comparison: null,
      verdict: null,
      createdAt: utcNowIso(),
      completedAt: null,
    };
    this.workspace.store.save(KIND_EVALUATION, runId, run as never);
    this.event(runId, "attempt-started", { split: options.split, cases: options.cases.length });

    await options.adapter.prepare(runId, mode);
    try {
      for (const caseEntry of options.cases) {
        for (let repeat = 1; repeat <= options.repeats; repeat++) {
          for (const side of ["baseline", "candidate"] as const) {
            const attemptId = newId("att");
            const caseInput: Record<string, unknown> = {
              caseId: caseEntry.caseId,
              ...caseEntry.input,
              ...(side === "candidate" ? { versionDigest: candidateDeltaDigestOf(candidate) } : {}),
            };
            const attempt: AttemptRecord = {
              schemaVersion: "1",
              attemptId,
              runId,
              caseId: caseEntry.caseId,
              repeat,
              side,
              ok: false,
              metric: null,
              guardrails: {},
              usage: null,
              error: null,
              reservationId,
            };
            try {
              const execution = await options.adapter.execute({
                runId,
                attemptId,
                workflowId: options.workflowId,
                caseInput,
                mode,
              });
              attempt.ok = execution.ok;
              attempt.usage = execution.usage;
              const metric = execution.outputs["metric"];
              attempt.metric = typeof metric === "number" ? metric : null;
              const guardrails = execution.outputs["guardrails"];
              attempt.guardrails = isRecord(guardrails)
                ? Object.fromEntries(
                  Object.entries(guardrails).map(([k, v]) => [k, Boolean(v)]),
                )
                : {};
              if (!execution.ok) attempt.error = execution.error ?? "adapter reported failure";
            } catch (exc) {
              attempt.error = exc instanceof Error ? exc.message : String(exc);
              attempt.usage = null;
            }
            run.attempts.push(attempt);
            this.workspace.store.save(KIND_ATTEMPT, attemptId, attempt as never);
            // Persist run progress incrementally (crash = incomplete run, not a lie)
            this.workspace.store.save(KIND_EVALUATION, runId, run as never);
          }
        }
      }
    } finally {
      try {
        await options.adapter.cleanup(runId);
      } catch {
        // cleanup is best-effort; the gate reservation settles below
      }
      this.settleAttemptReservation(runId, reservationId, run, perAttempt);
    }

    run.comparison = compareRun(run);
    const rubric = this.rubric(options.rubricDigest);
    run.verdict = verdictFromSummary(run.comparison, rubric);
    run.completedAt = utcNowIso();
    this.workspace.store.save(KIND_EVALUATION, runId, run as never);
    // A completed paired evaluation advances sealed -> evaluated; the final
    // acceptance decision then moves evaluated -> accepted/rejected/inconclusive.
    const candidateAfter = this.candidate(options.candidateId);
    if (candidateAfter.state === "sealed") {
      const evaluated = candidateWithTransition(candidateAfter, "evaluated", runId);
      this.workspace.store.save(KIND_CANDIDATE, candidateAfter.candidateId, evaluated as never);
      this.event(candidateAfter.candidateId, "candidate-state", { state: "evaluated", runId });
    }
    this.event(runId, "attempt-ended", {
      verdict: run.verdict,
      pairs: run.comparison.completePairs,
    });
    return run;
  }

  private settleAttemptReservation(
    runId: string,
    reservationId: string,
    run: EvaluationRun,
    reservedUsd: number,
  ): void {
    // The gate reservation covered dispatch; settle at the sum of measured
    // usage where available, conservatively at the FULL reservation when any
    // attempt went unmeasured (unmeasured is never free).
    const measured = run.attempts.reduce((sum, a) => {
      const cost = a.usage?.["costUsd"];
      return sum + (typeof cost === "number" && Number.isFinite(cost) ? cost : 0);
    }, 0);
    const unmeasured = run.attempts.some((a) => a.usage === null);
    const amount = unmeasured ? reservedUsd : measured;
    try {
      this.workspace.ledger.settle(reservationId, amount);
    } catch (exc) {
      if (!(exc instanceof BudgetExhaustedError)) throw exc;
      // An overrun stays open for reconciliation — never clipped.
    }
    void runId;
  }

  latestDecisionFor(candidate: Candidate): AcceptanceDecision | null {
    const decisions = this.decisions().filter((d) =>
      d.candidateDigest === candidateContentDigest(candidate)
    );
    return decisions.length > 0 ? decisions[decisions.length - 1] : null;
  }

  decisions(): AcceptanceDecision[] {
    return this.workspace.store.listIds(KIND_DECISION).map((id) => {
      const data = this.workspace.store.load(KIND_DECISION, id);
      return data === null ? null : decisionFromDict(data);
    }).filter((d): d is AcceptanceDecision => d !== null)
      .sort((a, b) => (a.decidedAt < b.decidedAt ? -1 : 1));
  }

  /** Apply the frozen rubric and record the acceptance decision. */
  decide(options: {
    run: EvaluationRun;
    rubric: Rubric;
    owner: string;
    evidenceDigest: string;
    allowedReleaseScope?: "none" | "project" | "environment";
    note?: string;
  }): AcceptanceDecision {
    const verdict = verdictFromSummary(runComparison(options.run), options.rubric);
    const binding: ApprovalBinding | null = verdict === "accepted"
      ? {
        schemaVersion: "1",
        candidateDigest: options.run.candidateDigest,
        rubricDigest: options.run.rubricDigest,
        environmentDigest: options.run.environmentDigest ?? UNRECORDED_ENVIRONMENT_DIGEST,
        acceptanceCaseSetDigest: options.run.caseSetDigest,
      }
      : null;
    const decision: AcceptanceDecision = {
      schemaVersion: "1",
      decisionId: newId("dec"),
      verdict,
      candidateDigest: options.run.candidateDigest,
      evidenceDigest: options.evidenceDigest,
      owner: options.owner,
      decidedAt: utcNowIso(),
      allowedReleaseScope: options.allowedReleaseScope ?? "none",
      binding,
      finalAcceptanceRunId: options.run.runId,
      note: options.note ?? "",
    };
    if (verdict === "accepted" && decision.owner === "proposer") {
      throw new ContractError("the proposer cannot be the acceptance owner");
    }
    this.workspace.store.save(KIND_DECISION, decision.decisionId, decision as never);
    this.event(options.run.candidateId, "approval-recorded", {
      decisionId: decision.decisionId,
      verdict,
      binding: binding !== null ? bindingDigest(binding) : null,
    });
    // An accepted decision advances the candidate state machine.
    if (verdict === "accepted" || verdict === "rejected" || verdict === "inconclusive") {
      const candidate = this.candidate(options.run.candidateId);
      const target = verdict === "accepted"
        ? "accepted"
        : verdict === "rejected"
        ? "rejected"
        : "inconclusive";
      if (CANDIDATE_TRANSITIONS[candidate.state].includes(target as CandidateState)) {
        const next = candidateWithTransition(
          candidate,
          target as CandidateState,
          decision.decisionId,
        );
        this.workspace.store.save(KIND_CANDIDATE, candidate.candidateId, next as never);
      }
    }
    return decision;
  }

  /**
   * Release approval: re-verify the approval binding against trusted storage
   * — any change to a bound digest invalidates the approval.
   */
  approve(options: { candidateId: string; approver: string }): Record<string, unknown> {
    const candidate = this.candidate(options.candidateId);
    const digest = candidateContentDigest(candidate);
    let decision =
      this.decisions().filter((d) => d.candidateDigest === digest && d.verdict === "accepted")
        .sort((a, b) => (a.decidedAt < b.decidedAt ? 1 : -1))[0];
    if (decision === undefined) {
      // A decision may exist under the ORIGINAL content digest: the bytes
      // changed since acceptance. Resolve it through the evaluation runs this
      // candidate actually produced (trusted storage, not caller digests).
      const runIds = new Set(
        this.workspace.store.listIds(KIND_EVALUATION)
          .map((id) => this.workspace.store.load(KIND_EVALUATION, id))
          .filter((data) => data !== null && data["candidateId"] === candidate.candidateId)
          .map((data) => String(data!["runId"])),
      );
      decision = this.decisions()
        .filter((d) =>
          d.verdict === "accepted" && d.finalAcceptanceRunId !== null &&
          runIds.has(d.finalAcceptanceRunId!)
        )
        .sort((a, b) => (a.decidedAt < b.decidedAt ? 1 : -1))[0];
    }
    if (decision === undefined || decision.binding === null) {
      throw new ContractError(
        `candidate ${candidate.candidateId} has no accepted decision to approve`,
      );
    }
    // Re-resolve the bound final-acceptance run from trusted storage (A5).
    const runData = decision.finalAcceptanceRunId !== null
      ? this.workspace.store.load(KIND_EVALUATION, decision.finalAcceptanceRunId)
      : null;
    if (runData === null) {
      throw new ContractError("the bound final-acceptance run is missing from trusted storage");
    }
    const run = evaluationFromDict(runData);
    try {
      verifyBinding(decision.binding, {
        candidateDigest: candidateContentDigest(candidate),
        rubricDigest: run.rubricDigest,
        environmentDigest: run.environmentDigest ?? UNRECORDED_ENVIRONMENT_DIGEST,
        acceptanceCaseSetDigest: run.caseSetDigest,
      });
    } catch {
      this.invalidate(candidate.candidateId, "approval binding no longer matches bound digests");
      throw new ApprovalInvalidatedError(
        "approval binding no longer matches: one of candidate/rubric/environment/case-set digests changed",
      );
    }
    const next = candidateWithTransition(candidate, "approved", decision.decisionId);
    this.workspace.store.save(KIND_CANDIDATE, candidate.candidateId, next as never);
    this.event(candidate.candidateId, "candidate-state", {
      state: "approved",
      by: options.approver,
    });
    return {
      candidateId: candidate.candidateId,
      decisionId: decision.decisionId,
      state: "approved",
    };
  }

  recordRelease(options: {
    candidateId: string;
    deployedVersion: string;
    deployedBy: string;
    observedWindow?: string;
    sideEffects?: string[];
  }): ReleaseRecord {
    const candidate = this.candidate(options.candidateId);
    if (candidate.state !== "approved") {
      throw new ContractError(
        `candidate ${candidate.candidateId} is ${candidate.state}; only approved candidates release`,
      );
    }
    const record: ReleaseRecord = {
      schemaVersion: "1",
      releaseId: newId("rel"),
      candidateDigest: candidateContentDigest(candidate),
      deployedVersion: options.deployedVersion,
      deployedBy: options.deployedBy,
      observedWindow: options.observedWindow ?? "",
      rollbackTrigger: "",
      sideEffects: options.sideEffects ?? [],
      compensationStatus: "none",
      releasedAt: utcNowIso(),
    };
    this.workspace.store.save(KIND_RELEASE, record.releaseId, record as never);
    const released = candidateWithTransition(candidate, "released", record.releaseId);
    this.workspace.store.save(KIND_CANDIDATE, candidate.candidateId, released as never);
    this.event(record.releaseId, "release-recorded", {
      candidateId: candidate.candidateId,
      deployedVersion: record.deployedVersion,
    });
    return record;
  }

  recordRollback(releaseId: string, trigger: string): ReleaseRecord {
    const data = this.workspace.store.load(KIND_RELEASE, releaseId);
    if (data === null) throw new ContractError(`unknown release ${JSON.stringify(releaseId)}`);
    const record = data as unknown as ReleaseRecord;
    const updated: ReleaseRecord = {
      ...record,
      rollbackTrigger: trigger,
      compensationStatus: "pending",
    };
    this.workspace.store.save(KIND_RELEASE, releaseId, updated as never);
    const candidates = this.workspace.store.listIds(KIND_CANDIDATE)
      .map((id) => candidateFromDict(this.workspace.store.load(KIND_CANDIDATE, id)!))
      .filter((c) =>
        candidateContentDigest(c) === record.candidateDigest && c.state === "released"
      );
    for (const candidate of candidates) {
      const rolled = candidateWithTransition(candidate, "rolled-back", trigger);
      this.workspace.store.save(KIND_CANDIDATE, candidate.candidateId, rolled as never);
      this.event(candidate.candidateId, "candidate-state", { state: "rolled-back", trigger });
    }
    this.event(releaseId, "audit-note", { action: "rollback-recorded", trigger });
    return updated;
  }
}

export function decisionFromDict(data: Record<string, unknown>): AcceptanceDecision {
  const binding = data["binding"];
  return {
    schemaVersion: String(data["schemaVersion"] ?? "1"),
    decisionId: String(data["decisionId"]),
    verdict: String(data["verdict"]) as Verdict,
    candidateDigest: String(data["candidateDigest"]),
    evidenceDigest: String(data["evidenceDigest"]),
    owner: String(data["owner"]),
    decidedAt: String(data["decidedAt"]),
    allowedReleaseScope: String(data["allowedReleaseScope"] ?? "none") as
      | "none"
      | "project"
      | "environment",
    binding: isRecord(binding)
      ? {
        schemaVersion: "1",
        candidateDigest: String(binding["candidateDigest"]),
        rubricDigest: String(binding["rubricDigest"]),
        environmentDigest: String(binding["environmentDigest"]),
        acceptanceCaseSetDigest: String(binding["acceptanceCaseSetDigest"]),
      }
      : null,
    finalAcceptanceRunId: (data["finalAcceptanceRunId"] as string | null) ?? null,
    note: String(data["note"] ?? ""),
  };
}

export function evaluationFromDict(data: Record<string, unknown>): EvaluationRun {
  return {
    schemaVersion: String(data["schemaVersion"] ?? "1"),
    runId: String(data["runId"]),
    workflowId: String(data["workflowId"]),
    candidateId: String(data["candidateId"]),
    candidateDigest: String(data["candidateDigest"]),
    baselineVersionId: String(data["baselineVersionId"]),
    rubricDigest: String(data["rubricDigest"]),
    caseSetDigest: String(data["caseSetDigest"]),
    environmentDigest: (data["environmentDigest"] as string | null) ?? null,
    split: String(data["split"]) as CaseSplit,
    mode: String(data["mode"] ?? "fixture"),
    repeats: Number(data["repeats"] ?? 1),
    attempts: (data["attempts"] as AttemptRecord[] | undefined) ?? [],
    comparison: (data["comparison"] as ComparisonSummary | null) ?? null,
    verdict: (data["verdict"] as Verdict | null) ?? null,
    createdAt: String(data["createdAt"]),
    completedAt: (data["completedAt"] as string | null) ?? null,
  };
}

function candidateDeltaDigestOf(candidate: Candidate): string {
  return digestOf({ changeType: candidate.changeType, delta: candidate.delta });
}

// --- comparison (fail-closed) ------------------------------------------------------

export function runComparison(run: EvaluationRun): ComparisonSummary {
  if (run.comparison !== null) return run.comparison;
  return compareRun(run);
}

export function compareRun(run: EvaluationRun): ComparisonSummary {
  const caseIds = [...new Set(run.attempts.map((a) => a.caseId))];
  const incompleteReasons: Record<string, string> = {};
  const hardViolations: string[] = [];
  const missingGuardrailMeasurements: string[] = [];
  const rubricGuardrails = new Set<string>();
  for (const attempt of run.attempts) {
    for (const name of Object.keys(attempt.guardrails)) rubricGuardrails.add(name);
  }
  let completePairs = 0;
  let completeRepeatPairs = 0;
  let pairsBelowRequestedRepeats = 0;
  const metricSamples = new Map<string, number[]>();

  for (const caseId of caseIds) {
    for (let repeat = 1; repeat <= run.repeats; repeat++) {
      const baseline = run.attempts.find((a) =>
        a.caseId === caseId && a.repeat === repeat && a.side === "baseline"
      );
      const candidate = run.attempts.find((a) =>
        a.caseId === caseId && a.repeat === repeat && a.side === "candidate"
      );
      const pairKey = run.repeats > 1 ? `repeat-${repeat}:${caseId}` : caseId;
      if (baseline === undefined || candidate === undefined) {
        const missing = baseline === undefined ? "baseline" : "candidate";
        incompleteReasons[pairKey] = `${pairKey.includes(":") ? "" : ""}${missing}:missing`;
        continue;
      }
      if (baseline.usage === null && candidate.usage === null) {
        incompleteReasons[pairKey] = "both:unmeasured";
        continue;
      }
      if (!baseline.ok && !candidate.ok) {
        incompleteReasons[pairKey] = "both:failed";
        continue;
      }
      if (!candidate.ok && baseline.ok) {
        incompleteReasons[pairKey] = `candidate:failed`;
        continue;
      }
      if (baseline.metric === null || candidate.metric === null) {
        incompleteReasons[pairKey] = "metric:unmeasured";
        continue;
      }
      // hard guardrails on either side
      let violated = false;
      for (const name of rubricGuardrails) {
        const b = baseline.guardrails[name];
        const c = candidate.guardrails[name];
        if (b === undefined || c === undefined) {
          missingGuardrailMeasurements.push(`${caseId}:${name}`);
          continue;
        }
        if (!b || !c) {
          hardViolations.push(`${caseId}:${name}`);
          violated = true;
        }
      }
      if (violated) continue;
      metricSamples.set("__main__", [
        ...(metricSamples.get("__main__") ?? []),
        candidate.metric - baseline.metric,
      ]);
      if (repeat === run.repeats) completeRepeatPairs += 1;
    }
    const caseCompleteRepeats = countCompleteRepeats(run, caseId);
    if (caseCompleteRepeats < run.repeats) pairsBelowRequestedRepeats += 1;
    if (caseCompleteRepeats > 0) completePairs += 1;
  }

  const samples = metricSamples.get("__main__") ?? [];
  const metricDeltas: MetricDelta[] = samples.length > 0
    ? [{
      metric: "main",
      meanDelta: samples.reduce((s, v) => s + v, 0) / samples.length,
      nPairs: samples.length,
    }]
    : [];
  const allAttempts = run.attempts;
  const measurable = allAttempts.every((a) => a.usage !== null);
  const cost = {
    measurable,
    baselineUsd: sumCost(allAttempts.filter((a) => a.side === "baseline")),
    candidateUsd: sumCost(allAttempts.filter((a) => a.side === "candidate")),
  };
  return {
    schemaVersion: "1",
    runId: run.runId,
    totalPairs: caseIds.length * run.repeats,
    completePairs,
    completeRepeatPairs,
    incompleteReasons,
    hardViolations,
    missingGuardrailMeasurements,
    metricDeltas,
    cost,
    pairsBelowRequestedRepeats,
  };
}

function countCompleteRepeats(run: EvaluationRun, caseId: string): number {
  let count = 0;
  for (let repeat = 1; repeat <= run.repeats; repeat++) {
    const baseline = run.attempts.find((a) =>
      a.caseId === caseId && a.repeat === repeat && a.side === "baseline"
    );
    const candidate = run.attempts.find((a) =>
      a.caseId === caseId && a.repeat === repeat && a.side === "candidate"
    );
    if (
      baseline !== undefined && candidate !== undefined && baseline.ok && candidate.ok &&
      baseline.metric !== null && candidate.metric !== null
    ) {
      count += 1;
    }
  }
  return count;
}

function sumCost(attempts: AttemptRecord[]): number | null {
  let sawAny = false;
  let total = 0;
  for (const attempt of attempts) {
    const cost = attempt.usage?.["costUsd"];
    if (typeof cost === "number" && Number.isFinite(cost)) {
      sawAny = true;
      total += cost;
    }
  }
  return sawAny ? total : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export { digestBytes };
