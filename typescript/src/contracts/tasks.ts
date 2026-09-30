/** Task contracts: TaskSpec / TaskRun lifecycle / RunStep / ResultPackage. */

import { digestOf } from "./canonical.ts";
import {
  checkVersion,
  ContractError,
  InvalidStateTransitionError,
  newId,
  requireStr,
  RunMode,
  type RunModeValue,
  utcNowIso,
} from "./common.ts";

export const TASK_STATUSES = [
  "queued",
  "running",
  "paused",
  "completed",
  "failed",
  "cancelled",
  "needs-reconciliation",
] as const;

export type TaskStatusValue = (typeof TASK_STATUSES)[number];

export const TERMINAL_STATUSES: readonly TaskStatusValue[] = ["completed", "failed", "cancelled"];

export function isTerminal(status: TaskStatusValue): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/** v1 transition table (frozen): unknown side-effect state only leaves NEEDS_RECONCILIATION explicitly. */
export const TASK_TRANSITIONS: Record<TaskStatusValue, readonly TaskStatusValue[]> = {
  "queued": ["running", "cancelled"],
  "running": ["paused", "completed", "failed", "cancelled", "needs-reconciliation"],
  "paused": ["running", "cancelled", "needs-reconciliation"],
  "needs-reconciliation": ["running", "failed", "cancelled"],
  "completed": [],
  "failed": [],
  "cancelled": [],
};

export function transitionTaskStatus(current: TaskStatusValue, target: TaskStatusValue): void {
  if (!TASK_TRANSITIONS[current].includes(target)) {
    throw new InvalidStateTransitionError(
      `task run cannot transition ${current} -> ${target}`,
    );
  }
}

export const STEP_KINDS = [
  "plan",
  "model-call",
  "tool-call",
  "checkpoint",
  "gate-check",
  "finalize",
] as const;
export type StepKindValue = (typeof STEP_KINDS)[number];

export interface RunStepData {
  schemaVersion: string;
  stepId: string;
  kind: StepKindValue;
  status: string;
  startedAt: string;
  endedAt: string | null;
  inputDigest: string | null;
  outputDigest: string | null;
  usage: Record<string, unknown>;
  error: string | null;
  sideEffectUnknown: boolean;
}

export interface TaskSpecData {
  schemaVersion: string;
  specId: string;
  title: string;
  goal: string;
  mode: RunModeValue;
  workflowId: string | null;
  inputs: Record<string, unknown>;
  maxCostUsd: number | null;
  maxWallClockS: number | null;
  maxSteps: number | null;
  locale: string;
  market: string | null;
  successCriteria: Record<string, unknown>;
  createdBy: string;
  createdAt: string;
}

export interface TaskRunData {
  schemaVersion: string;
  runId: string;
  taskDigest: string;
  specId: string | null;
  reservationId: string | null;
  status: TaskStatusValue;
  steps: RunStepData[];
  resultDigest: string | null;
  error: string | null;
  mode: RunModeValue;
  createdAt: string;
  updatedAt: string;
}

export interface ResultPackageData {
  schemaVersion: string;
  runId: string;
  conclusion: string;
  artifactRefs: string[];
  doneItems: string[];
  notDoneItems: string[];
  uncertainties: string[];
  externalActions: Record<string, string>;
  totalCostUsd: number | null;
  completedConditionsCheck: Record<string, boolean>;
  createdAt: string;
}

// --- RunStep ------------------------------------------------------------------

export function newStep(
  kind: StepKindValue,
  usage: Record<string, unknown> = {},
): RunStepData {
  return {
    schemaVersion: "1",
    stepId: newId("step"),
    kind,
    status: "running",
    startedAt: utcNowIso(),
    endedAt: null,
    inputDigest: null,
    outputDigest: null,
    usage,
    error: null,
    sideEffectUnknown: false,
  };
}

export function stepFromDict(data: Record<string, unknown>): RunStepData {
  checkVersion(data);
  const kind = requireStr(data["kind"], "kind") as StepKindValue;
  if (!STEP_KINDS.includes(kind)) {
    throw new ContractError(`unknown step kind ${JSON.stringify(kind)}`);
  }
  return {
    schemaVersion: "1",
    stepId: requireStr(data["stepId"], "stepId"),
    kind,
    status: requireStr(data["status"], "status"),
    startedAt: requireStr(data["startedAt"], "startedAt"),
    endedAt: (data["endedAt"] as string | null) ?? null,
    inputDigest: (data["inputDigest"] as string | null) ?? null,
    outputDigest: (data["outputDigest"] as string | null) ?? null,
    usage: (data["usage"] as Record<string, unknown>) ?? {},
    error: (data["error"] as string | null) ?? null,
    sideEffectUnknown: Boolean(data["sideEffectUnknown"] ?? false),
  };
}

// --- TaskSpec -----------------------------------------------------------------

export function taskSpec(spec: {
  specId: string;
  title: string;
  goal: string;
  mode?: RunModeValue;
  inputs?: Record<string, unknown>;
  maxCostUsd?: number | null;
  maxSteps?: number | null;
  successCriteria?: Record<string, unknown>;
}): TaskSpecData {
  return {
    schemaVersion: "1",
    specId: spec.specId,
    title: spec.title,
    goal: spec.goal,
    mode: spec.mode ?? "fixture",
    workflowId: null,
    inputs: spec.inputs ?? {},
    maxCostUsd: spec.maxCostUsd ?? null,
    maxWallClockS: null,
    maxSteps: spec.maxSteps ?? null,
    locale: "en",
    market: null,
    successCriteria: spec.successCriteria ?? {},
    createdBy: "local",
    createdAt: utcNowIso(),
  };
}

export function specFromDict(data: Record<string, unknown>): TaskSpecData {
  checkVersion(data);
  const inputs = data["inputs"] ?? {};
  if (typeof inputs !== "object" || Array.isArray(inputs)) {
    throw new ContractError("TaskSpec.inputs must be an object");
  }
  RunMode.from(String(data["mode"] ?? "fixture"));
  return {
    schemaVersion: "1",
    specId: requireStr(data["specId"], "specId"),
    title: requireStr(data["title"], "title"),
    goal: requireStr(data["goal"], "goal"),
    mode: (data["mode"] as RunModeValue) ?? "fixture",
    workflowId: (data["workflowId"] as string | null) ?? null,
    inputs: { ...inputs } as Record<string, unknown>,
    maxCostUsd: numOrNull(data["maxCostUsd"]),
    maxWallClockS: numOrNull(data["maxWallClockS"]),
    maxSteps: numOrNull(data["maxSteps"]),
    locale: requireStr(data["locale"] ?? "en", "locale"),
    market: (data["market"] as string | null) ?? null,
    successCriteria: { ...(data["successCriteria"] as Record<string, unknown> ?? {}) },
    createdBy: requireStr(data["createdBy"] ?? "local", "createdBy"),
    createdAt: requireStr(data["createdAt"] ?? utcNowIso(), "createdAt"),
  };
}

export function specDigest(spec: TaskSpecData): string {
  return digestOf(spec);
}

// --- TaskRun ------------------------------------------------------------------

export function newRun(taskDigest: string, mode: RunModeValue): TaskRunData {
  const now = utcNowIso();
  return {
    schemaVersion: "1",
    runId: newId("run"),
    taskDigest,
    specId: null,
    reservationId: null,
    status: "queued",
    steps: [],
    resultDigest: null,
    error: null,
    mode,
    createdAt: now,
    updatedAt: now,
  };
}

export function runFromDict(data: Record<string, unknown>): TaskRunData {
  checkVersion(data);
  const status = requireStr(data["status"], "status") as TaskStatusValue;
  if (!TASK_STATUSES.includes(status)) {
    throw new ContractError(`unknown task status ${JSON.stringify(status)}`);
  }
  const steps = Array.isArray(data["steps"]) ? data["steps"] : [];
  return {
    schemaVersion: "1",
    runId: requireStr(data["runId"], "runId"),
    taskDigest: requireStr(data["taskDigest"], "taskDigest"),
    specId: (data["specId"] as string | null) ?? null,
    reservationId: (data["reservationId"] as string | null) ?? null,
    status,
    steps: steps.map((s) => stepFromDict(s as Record<string, unknown>)),
    resultDigest: (data["resultDigest"] as string | null) ?? null,
    error: (data["error"] as string | null) ?? null,
    mode: (data["mode"] as RunModeValue) ?? "fixture",
    createdAt: requireStr(data["createdAt"] ?? utcNowIso(), "createdAt"),
    updatedAt: requireStr(data["updatedAt"] ?? utcNowIso(), "updatedAt"),
  };
}

export function withTransition(
  run: TaskRunData,
  target: TaskStatusValue,
  error?: string | null,
): TaskRunData {
  transitionTaskStatus(run.status, target);
  return {
    ...run,
    status: target,
    error: error ?? run.error,
    updatedAt: utcNowIso(),
  };
}

export function withStep(run: TaskRunData, step: RunStepData): TaskRunData {
  return {
    ...run,
    steps: [...run.steps, step],
    updatedAt: utcNowIso(),
  };
}

// --- ResultPackage ------------------------------------------------------------

export function packageDeliverable(pkg: ResultPackageData): boolean {
  const checks = pkg.completedConditionsCheck;
  const values = Object.values(checks);
  return (
    values.length > 0 &&
    values.every((v) => v) &&
    !Object.values(pkg.externalActions).some((s) => s === "pending")
  );
}

export function packageFromDict(data: Record<string, unknown>): ResultPackageData {
  checkVersion(data);
  return {
    schemaVersion: "1",
    runId: requireStr(data["runId"], "runId"),
    conclusion: requireStr(data["conclusion"], "conclusion"),
    artifactRefs: strs(data["artifactRefs"]),
    doneItems: strs(data["doneItems"]),
    notDoneItems: strs(data["notDoneItems"]),
    uncertainties: strs(data["uncertainties"]),
    externalActions: { ...(data["externalActions"] as Record<string, string> ?? {}) },
    totalCostUsd: numOrNull(data["totalCostUsd"]),
    completedConditionsCheck: {
      ...(data["completedConditionsCheck"] as Record<string, boolean> ?? {}),
    },
    createdAt: requireStr(data["createdAt"] ?? utcNowIso(), "createdAt"),
  };
}

export function newTaskSpecId(): string {
  return newId("task");
}

function strs(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((v) => String(v));
}

function numOrNull(value: unknown): number | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ContractError(`expected a finite number, got ${JSON.stringify(value)}`);
  }
  return value;
}
