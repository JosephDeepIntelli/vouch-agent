/**
 * Improvement contracts: AgentVersion, Candidate (bounded digest-bound
 * change proposals), Rubric, ApprovalBinding, decisions and releases.
 * Ported from the Python contracts (schema v1 shapes).
 */

import { digestOf, isPlainObject } from "../contracts/canonical.ts";
import {
  checkVersion,
  ContractError,
  InvalidStateTransitionError,
  newId,
  requireDigest,
  requireStr,
  utcNowIso,
} from "../contracts/common.ts";

export const CHANGE_TYPES = [
  "prompt-delta",
  "clarification-strategy",
  "retrieval-params",
  "tool-selection",
  "submission-strategy",
] as const;
export type ChangeType = (typeof CHANGE_TYPES)[number];

export const CANDIDATE_STATES = [
  "proposed",
  "sealed",
  "evaluated",
  "accepted",
  "rejected",
  "inconclusive",
  "approved",
  "released",
  "observed",
  "rolled-back",
  "invalidated",
] as const;
export type CandidateState = (typeof CANDIDATE_STATES)[number];

export const CANDIDATE_TRANSITIONS: Record<CandidateState, readonly CandidateState[]> = {
  "proposed": ["sealed"],
  "sealed": ["evaluated", "invalidated"],
  "evaluated": ["accepted", "rejected", "inconclusive", "invalidated"],
  "accepted": ["approved", "invalidated"],
  "rejected": [],
  "inconclusive": ["invalidated"],
  "approved": ["released", "invalidated"],
  "released": ["observed", "rolled-back"],
  "observed": ["rolled-back"],
  "rolled-back": [],
  // An invalidated candidate re-enters as a NEW proposal; the record stays
  // invalidated for the audit trail.
  "invalidated": [],
};

/** States from which any bound-digest change forces invalidation. */
export const DIGEST_BINDING_STATES: readonly CandidateState[] = [
  "sealed",
  "evaluated",
  "accepted",
  "approved",
  "released",
  "observed",
];

export function transitionCandidateState(current: CandidateState, target: CandidateState): void {
  if (!CANDIDATE_TRANSITIONS[current].includes(target)) {
    throw new InvalidStateTransitionError(`candidate cannot transition ${current} -> ${target}`);
  }
}

export interface AgentVersion {
  schemaVersion: string;
  versionId: string;
  sourceRef: string;
  promptDeltaDigest: string | null;
  modelId: string;
  toolSchemaVersion: string;
  dependencyLockDigest: string | null;
  environmentDigest: string | null;
}

export function agentVersion(
  input: { versionId: string; sourceRef: string; environmentDigest?: string | null },
): AgentVersion {
  return {
    schemaVersion: "1",
    versionId: input.versionId,
    sourceRef: input.sourceRef,
    promptDeltaDigest: null,
    modelId: "",
    toolSchemaVersion: "1",
    dependencyLockDigest: null,
    environmentDigest: input.environmentDigest ?? null,
  };
}

export function agentVersionFromDict(data: Record<string, unknown>): AgentVersion {
  checkVersion(data);
  return {
    schemaVersion: "1",
    versionId: requireStr(data["versionId"], "versionId"),
    sourceRef: requireStr(data["sourceRef"], "sourceRef"),
    promptDeltaDigest: (data["promptDeltaDigest"] as string | null) ?? null,
    modelId: String(data["modelId"] ?? ""),
    toolSchemaVersion: String(data["toolSchemaVersion"] ?? "1"),
    dependencyLockDigest: (data["dependencyLockDigest"] as string | null) ?? null,
    environmentDigest: (data["environmentDigest"] as string | null) ?? null,
  };
}

export interface Candidate {
  schemaVersion: string;
  candidateId: string;
  parentVersion: AgentVersion;
  changeType: ChangeType;
  delta: string;
  rationale: string;
  expectedImpact: string;
  proposer: string;
  devCaseRefs: string[];
  state: CandidateState;
  stateReason: string;
  createdAt: string;
}

export function candidateFromDict(data: Record<string, unknown>): Candidate {
  checkVersion(data);
  const changeType = requireStr(data["changeType"], "changeType") as ChangeType;
  if (!CHANGE_TYPES.includes(changeType)) {
    throw new ContractError(`unknown change type ${JSON.stringify(changeType)}`);
  }
  const state = String(data["state"] ?? "proposed") as CandidateState;
  if (!CANDIDATE_STATES.includes(state)) {
    throw new ContractError(`unknown candidate state ${JSON.stringify(state)}`);
  }
  return {
    schemaVersion: "1",
    candidateId: requireStr(data["candidateId"], "candidateId"),
    parentVersion: agentVersionFromDict(
      isPlainObject(data["parentVersion"]) ? data["parentVersion"] : (() => {
        throw new ContractError("candidate.parentVersion must be an object");
      })(),
    ),
    changeType,
    delta: requireStr(data["delta"], "delta"),
    rationale: requireStr(data["rationale"], "rationale"),
    expectedImpact: String(data["expectedImpact"] ?? ""),
    proposer: requireStr(data["proposer"], "proposer"),
    devCaseRefs: Array.isArray(data["devCaseRefs"]) ? data["devCaseRefs"].map(String) : [],
    state,
    stateReason: String(data["stateReason"] ?? ""),
    createdAt: requireStr(data["createdAt"] ?? utcNowIso(), "createdAt"),
  };
}

export function candidateDeltaDigest(candidate: Candidate): string {
  return digestOf({ changeType: candidate.changeType, delta: candidate.delta });
}

/** Digest over the change CONTENT only — excludes lifecycle state/timestamps:
 * approving/releasing the same change must not invalidate its own approval. */
export function candidateContentDigest(candidate: Candidate): string {
  return digestOf({
    parentVersion: candidate.parentVersion,
    changeType: candidate.changeType,
    delta: candidate.delta,
    rationale: candidate.rationale,
    expectedImpact: candidate.expectedImpact,
    proposer: candidate.proposer,
    devCaseRefs: candidate.devCaseRefs,
  });
}

export function candidateWithTransition(
  candidate: Candidate,
  target: CandidateState,
  reason = "",
): Candidate {
  transitionCandidateState(candidate.state, target);
  return { ...candidate, state: target, stateReason: reason || candidate.stateReason };
}

export function candidateSealable(
  candidate: Candidate,
  allowedChangeTypes: readonly string[],
): void {
  if (!allowedChangeTypes.includes(candidate.changeType)) {
    throw new ContractError(
      `change type ${JSON.stringify(candidate.changeType)} is not in allowed_change_types`,
    );
  }
  if (candidate.delta.trim().length === 0) {
    throw new ContractError("candidate delta must not be empty");
  }
  if (candidate.rationale.trim().length === 0) {
    throw new ContractError("candidate must carry a rationale");
  }
}

// --- rubric ---------------------------------------------------------------------

export interface Rubric {
  schemaVersion: string;
  mainMetric: string;
  direction: "increase" | "decrease";
  thresholds: Record<string, number>;
  hardGuardrails: string[];
  repeats: number;
  frozenBy: string | null;
  frozenAt: string | null;
}

export const THRESHOLD_MIN_MAIN_IMPROVEMENT = "min-main-improvement";
export const THRESHOLD_MIN_COMPLETE_PAIRS = "min-complete-pairs";

export function rubricDigest(rubric: Rubric): string {
  return digestOf(rubric);
}

export function rubricFromDict(data: Record<string, unknown>): Rubric {
  checkVersion(data);
  return {
    schemaVersion: "1",
    mainMetric: requireStr(data["mainMetric"], "mainMetric"),
    direction: String(data["direction"] ?? "increase") as "increase" | "decrease",
    thresholds: { ...(data["thresholds"] as Record<string, number> ?? {}) },
    hardGuardrails: Array.isArray(data["hardGuardrails"]) ? data["hardGuardrails"].map(String) : [],
    repeats: Number(data["repeats"] ?? 1),
    frozenBy: (data["frozenBy"] as string | null) ?? null,
    frozenAt: (data["frozenAt"] as string | null) ?? null,
  };
}

// --- comparison summary ------------------------------------------------------------

export interface MetricDelta {
  metric: string;
  meanDelta: number;
  nPairs: number;
}

export interface ComparisonSummary {
  schemaVersion: string;
  runId: string;
  totalPairs: number;
  completePairs: number;
  completeRepeatPairs: number;
  incompleteReasons: Record<string, string>;
  hardViolations: string[];
  missingGuardrailMeasurements: string[];
  metricDeltas: MetricDelta[];
  cost: { measurable: boolean; baselineUsd: number | null; candidateUsd: number | null } | null;
  pairsBelowRequestedRepeats: number;
}

// --- decisions ---------------------------------------------------------------------

export type Verdict = "accepted" | "rejected" | "inconclusive";

export interface ApprovalBinding {
  schemaVersion: string;
  candidateDigest: string;
  rubricDigest: string;
  environmentDigest: string;
  acceptanceCaseSetDigest: string;
}

export function bindingFromDict(data: Record<string, unknown>): ApprovalBinding {
  checkVersion(data);
  return {
    schemaVersion: "1",
    candidateDigest: requireDigest(data["candidateDigest"], "candidateDigest"),
    rubricDigest: requireDigest(data["rubricDigest"], "rubricDigest"),
    environmentDigest: requireDigest(data["environmentDigest"], "environmentDigest"),
    acceptanceCaseSetDigest: requireDigest(
      data["acceptanceCaseSetDigest"],
      "acceptanceCaseSetDigest",
    ),
  };
}

export function bindingDigest(binding: ApprovalBinding): string {
  return digestOf(binding);
}

/** Recompute each bound digest at approval-consumption time and compare. */
export function verifyBinding(
  binding: ApprovalBinding,
  actual: {
    candidateDigest: string;
    rubricDigest: string;
    environmentDigest: string;
    acceptanceCaseSetDigest: string;
  },
): void {
  if (
    binding.candidateDigest !== actual.candidateDigest ||
    binding.rubricDigest !== actual.rubricDigest ||
    binding.environmentDigest !== actual.environmentDigest ||
    binding.acceptanceCaseSetDigest !== actual.acceptanceCaseSetDigest
  ) {
    throw new ContractError(
      "approval binding no longer matches: one of candidate/rubric/environment/case-set digests changed",
      // code set below by caller to vouch/approval-invalidated
    );
  }
}

export interface AcceptanceDecision {
  schemaVersion: string;
  decisionId: string;
  verdict: Verdict;
  candidateDigest: string;
  evidenceDigest: string;
  owner: string;
  decidedAt: string;
  allowedReleaseScope: "none" | "project" | "environment";
  binding: ApprovalBinding | null;
  finalAcceptanceRunId: string | null;
  note: string;
}

export interface ReleaseRecord {
  schemaVersion: string;
  releaseId: string;
  candidateDigest: string;
  deployedVersion: string;
  deployedBy: string;
  observedWindow: string;
  rollbackTrigger: string;
  sideEffects: string[];
  compensationStatus: "none" | "pending" | "done" | "not-applicable";
  releasedAt: string;
}

export function newDecision(input: {
  verdict: Verdict;
  candidateDigest: string;
  evidenceDigest: string;
  owner: string;
  binding: ApprovalBinding | null;
  finalAcceptanceRunId: string | null;
  allowedReleaseScope?: "none" | "project" | "environment";
  note?: string;
}): AcceptanceDecision {
  if (input.verdict === "accepted" && input.binding === null) {
    throw new ContractError("an accepted verdict requires an explicit approval binding");
  }
  if (input.owner === "proposer") {
    throw new ContractError("the proposer cannot be the acceptance owner");
  }
  return {
    schemaVersion: "1",
    decisionId: newId("dec"),
    verdict: input.verdict,
    candidateDigest: requireDigest(input.candidateDigest, "candidateDigest"),
    evidenceDigest: requireDigest(input.evidenceDigest, "evidenceDigest"),
    owner: requireStr(input.owner, "owner"),
    decidedAt: utcNowIso(),
    allowedReleaseScope: input.allowedReleaseScope ?? "none",
    binding: input.binding,
    finalAcceptanceRunId: input.finalAcceptanceRunId,
    note: input.note ?? "",
  };
}

/** Deterministic digest binding "no environment was recorded" — itself a bound state. */
export const UNRECORDED_ENVIRONMENT_DIGEST = digestOf({ environmentDigest: null });
