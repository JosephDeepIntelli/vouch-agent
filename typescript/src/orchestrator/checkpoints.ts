/**
 * Durable run checkpoints and restart classification. Recovery rule:
 * persist intent, do the side-effecting work, persist the result. After
 * every persisted step the supervisor writes a recovery point.
 * `recover()` classifies a persisted run WITHOUT executing anything.
 */

import { digestOf } from "../contracts/canonical.ts";
import { ContractError } from "../contracts/common.ts";
import { eventRecord } from "../contracts/journal.ts";
import { isTerminal, runFromDict, type RunStepData, type TaskRunData } from "../contracts/tasks.ts";
import { pendingWrites, readFinalization } from "./records.ts";
import type { MetadataStore } from "../storage/store.ts";
import type { Journal } from "../storage/journal.ts";

export const CHECKPOINT_KIND = "checkpoint";
export const TASK_RUN_KIND = "task-run";

/** Step kinds whose interrupted outcome cannot be assumed known. */
export const SIDE_EFFECTING_STEP_KINDS = new Set(["model-call", "tool-call"]);

export const STEP_STATUS_RUNNING = "running";
export const STEP_STATUS_OK = "ok";
export const STEP_STATUS_FAILED = "failed";
export const STEP_STATUS_SKIPPED = "skipped";
export const STEP_STATUS_UNKNOWN = "unknown";

export const RECOVERY_CLASSIFICATIONS = [
  "clean-resume",
  "needs-reconciliation",
  "finalization-pending",
  "terminal",
] as const;

export type RecoveryClassification = (typeof RECOVERY_CLASSIFICATIONS)[number];

export interface RecoveryReport {
  runId: string;
  classification: RecoveryClassification;
  run: TaskRunData;
  detail: string;
  unknownSteps: RunStepData[];
  inFlightSteps: RunStepData[];
}

export function writeCheckpoint(
  store: MetadataStore,
  journal: Journal,
  run: TaskRunData,
  options: { sequence: number; budget?: Record<string, unknown> },
): string {
  const record: Record<string, unknown> = {
    schemaVersion: "1",
    runId: run.runId,
    sequence: options.sequence,
    status: run.status,
    stepCount: run.steps.length,
    run,
    budget: { ...(options.budget ?? {}) },
  };
  store.save(CHECKPOINT_KIND, run.runId, record);
  journal.append(
    eventRecord("recovery-point", run.runId, {
      sequence: options.sequence,
      status: run.status,
      stepCount: run.steps.length,
    }, { actor: "supervisor", mode: run.mode }),
  );
  return digestOf(record);
}

export function loadCheckpoint(
  store: MetadataStore,
  runId: string,
): Record<string, unknown> | null {
  return store.load(CHECKPOINT_KIND, runId);
}

function loadRun(store: MetadataStore, runId: string): TaskRunData {
  const data = store.load(TASK_RUN_KIND, runId);
  if (data !== null) return runFromDict(data);
  const record = loadCheckpoint(store, runId);
  const embedded = record?.["run"];
  if (record !== null && typeof embedded === "object" && embedded !== null) {
    return runFromDict(embedded as Record<string, unknown>);
  }
  throw new ContractError(
    `unknown run ${JSON.stringify(runId)}: no task-run record or checkpoint`,
  );
}

/** Classify a run for restart. Reads persisted state only; executes nothing. */
export function recover(store: MetadataStore, runId: string): RecoveryReport {
  const run = loadRun(store, runId);
  const unknown = run.steps.filter((s) => s.sideEffectUnknown || s.status === STEP_STATUS_UNKNOWN);
  const inFlight = run.steps.filter((s) => s.status === STEP_STATUS_RUNNING);
  const intent = readFinalization(store, runId);
  const pending = intent === null ? [] : pendingWrites(intent);

  if (isTerminal(run.status)) {
    if (pending.length > 0) {
      return {
        runId,
        classification: "finalization-pending",
        run,
        detail:
          `run is ${run.status} but its terminal writes are incomplete (${pending.join(", ")}); ` +
          `finish them exactly once — a terminal status alone does not prove the bookkeeping completed`,
        unknownSteps: unknown,
        inFlightSteps: inFlight,
      };
    }
    return {
      runId,
      classification: "terminal",
      run,
      detail: `run is terminal (${run.status}) with ${run.steps.length} steps`,
      unknownSteps: unknown,
      inFlightSteps: inFlight,
    };
  }
  if (pending.length > 0) {
    return {
      runId,
      classification: "finalization-pending",
      run,
      detail: `run is ${run.status} with an unfinished finalization intent; finish its terminal ` +
        `writes exactly once — no work may be replayed`,
      unknownSteps: unknown,
      inFlightSteps: inFlight,
    };
  }
  if (unknown.length > 0) {
    const ids = unknown.map((s) => s.stepId);
    return {
      runId,
      classification: "needs-reconciliation",
      run,
      detail:
        `${unknown.length} step(s) with unknown side-effect outcome: ${JSON.stringify(ids)}; ` +
        `verified reconciliation note required before resume, never replayed`,
      unknownSteps: unknown,
      inFlightSteps: inFlight,
    };
  }
  const sideEffectingInFlight = inFlight.filter((s) => SIDE_EFFECTING_STEP_KINDS.has(s.kind));
  if (sideEffectingInFlight.length > 0) {
    const ids = sideEffectingInFlight.map((s) => s.stepId);
    return {
      runId,
      classification: "needs-reconciliation",
      run,
      detail: `crash left side-effecting step(s) in flight with unknown outcome: ${
        JSON.stringify(ids)
      }`,
      unknownSteps: unknown,
      inFlightSteps: inFlight,
    };
  }
  if (inFlight.length > 0) {
    const kinds = [...new Set(inFlight.map((s) => s.kind))].sort();
    return {
      runId,
      classification: "clean-resume",
      run,
      detail: `crash left deterministic step(s) in flight (${JSON.stringify(kinds)}); ` +
        `safe to mark skipped and redo`,
      unknownSteps: unknown,
      inFlightSteps: inFlight,
    };
  }
  return {
    runId,
    classification: "clean-resume",
    run,
    detail: `status ${run.status} with all ${run.steps.length} step outcomes known`,
    unknownSteps: unknown,
    inFlightSteps: inFlight,
  };
}
