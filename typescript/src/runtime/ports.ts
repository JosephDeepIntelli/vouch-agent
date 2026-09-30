/**
 * Runtime ports — the executor interface carried over from the JAZ
 * reference semantics (invoke, scope, structured return, nested budgets).
 *
 * The controller talks to a `Runtime` only through these interfaces; the
 * worker side never receives provider keys, acceptance labels or any
 * privileged state. `QueryBudget` keeps reservation authority in the
 * controller: every underlying model query (nested invokes included)
 * reserves BEFORE it runs and settles after.
 */

import type { RunModeValue } from "../contracts/common.ts";

export interface WorkerSessionConfig {
  mode: RunModeValue;
  maxSteps: number;
  wallClockS: number;
  maxCostUsd: number;
  scriptedResponses?: string[];
  scriptedCursor?: number;
}

export interface ModelCallResult {
  content: string;
  returnValue: unknown | null;
  promptTokens: number | null;
  completionTokens: number | null;
  costUsd: number | null;
  modelId: string;
  raw: Record<string, unknown>;
}

export type StepOutcome =
  | { ok: true; result: ModelCallResult }
  | { ok: false; code: string; message: string; usage: Record<string, unknown> | null };

export interface RuntimeSession {
  step(
    instruction: string,
    scope: { materials: unknown; priorArtifacts: unknown },
  ): Promise<ModelCallResult>;
  usage(): Record<string, unknown> | null;
  cancel(reason: string): void;
  close(): void;
}

/** Controller-side query-budget authority handed to a session. */
export interface QueryBudgetPort {
  reserveQuery(estimateUsd: number | null): string;
  settleQuery(reservationId: string, actualUsd: number | null): void;
  releaseQuery(reservationId: string): void;
}

export interface Runtime {
  openSession(config: WorkerSessionConfig, budget: QueryBudgetPort | null): Promise<RuntimeSession>;
  backendId(): string;
}
