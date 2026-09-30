/**
 * The scripted fixture engine — the deterministic, never-networked fake
 * model, carrying the JAZ invoke/scope/structured-return/nested-budget
 * semantics through an executor interface (ADR: semantics, not internals).
 *
 * Runs ONLY inside the worker process (or, for the shipped trusted sample
 * scripts, in-process — see inline_runtime.ts). Scripts are JS function
 * bodies receiving (materials, prior_artifacts, invoke); `invoke` is
 * awaitable and draws the next scripted response from the shared ordered
 * pool, enforces the nested depth limit, and routes its budget through the
 * controller's atomic reservations BEFORE the draw (fail closed on
 * exhaustion — no live fallback ever).
 */

export const SCRIPT_CALL_COST_USD = 0.01;
export const SCRIPT_MODEL_ID = "vowdo-scripted/fixture-1";
export const MAX_INVOCATION_DEPTH = 8;
const TOKENS_PER_CHAR = 4;

export class ReplayExhausted extends Error {
  readonly code = "vowdo/replay-exhausted";
}

export interface InvokePort {
  /** Reserve before the query runs; resolves with the controller reservation id. */
  reserve(estimateUsd: number | null): Promise<string>;
  /** Settle at the actual (or null → conservative at the reservation). */
  settle(reservationId: string, actualUsd: number | null): void;
  release(reservationId: string): void;
}

export interface StepEngineResult {
  content: string;
  returnValue: unknown | null;
  promptTokens: number;
  completionTokens: number;
  costUsd: number;
  llmCalls: number;
  error: null | { code: string; message: string };
}

// deno-lint-ignore no-explicit-any
const AsyncFunction = (async function () {} as any).constructor as new (
  ...args: string[]
) => (
  materials: unknown,
  prior: unknown,
  invoke: (i: unknown) => Promise<unknown>,
) => Promise<unknown>;

/**
 * Execute one model step: evaluate `scripts[cursor]` (nested invokes draw
 * subsequent responses) with the given scope. Every draw reserves through
 * `budget` before it runs and settles at the simulated call cost; a failed
 * draw releases its still-open reservation.
 */
export async function executeScriptedStep(options: {
  scripts: string[];
  cursor: number;
  instruction: string;
  materials: unknown;
  priorArtifacts: unknown;
  budget: InvokePort;
}): Promise<StepEngineResult> {
  const state = { cursor: options.cursor, llmCalls: 0, costUsd: 0 };
  const openReservations: string[] = [];

  const drawAndRun = async (_input: unknown, depth: number): Promise<unknown> => {
    if (depth > MAX_INVOCATION_DEPTH) {
      throw new Error(
        `nested invocation depth exceeds the limit (${MAX_INVOCATION_DEPTH}); refusing to go deeper`,
      );
    }
    const reservationId = await options.budget.reserve(SCRIPT_CALL_COST_USD);
    openReservations.push(reservationId);
    if (state.cursor >= options.scripts.length) {
      throw new ReplayExhausted(
        `scripted responses exhausted after ${state.llmCalls} call(s) (model=${SCRIPT_MODEL_ID}); ` +
          `offline/fixture sessions fail closed here instead of falling back to a live call`,
      );
    }
    const script = options.scripts[state.cursor];
    state.cursor += 1;
    state.llmCalls += 1;
    const fn = new AsyncFunction(
      "materials",
      "prior_artifacts",
      "invoke",
      `"use strict";\n${script}`,
    );
    // The nested script sees the same materials scope (JAZ scope semantics:
    // children inherit the caller's scope; capabilities narrow only).
    const value = await fn(
      options.materials,
      options.priorArtifacts,
      (nestedInput: unknown) => drawAndRun(nestedInput, depth + 1),
    );
    await options.budget.settle(reservationId, SCRIPT_CALL_COST_USD);
    const idx = openReservations.indexOf(reservationId);
    if (idx >= 0) openReservations.splice(idx, 1);
    state.costUsd += SCRIPT_CALL_COST_USD;
    return value;
  };

  try {
    const returnValue = await drawAndRun(options.instruction, 0);
    const content = serializeScriptResult(returnValue);
    return {
      content,
      returnValue: returnValue === undefined ? null : returnValue,
      promptTokens: Math.floor(options.instruction.length / TOKENS_PER_CHAR),
      completionTokens: Math.floor(content.length / TOKENS_PER_CHAR),
      costUsd: round6(state.costUsd),
      llmCalls: state.llmCalls,
      error: null,
    };
  } catch (exc) {
    // Failure keeps its metering: completed draws settled at their cost; the
    // failing draw's still-open reservation is released (it never ran).
    for (const rid of [...openReservations]) {
      try {
        options.budget.release(rid);
      } catch {
        // release must not mask the original failure
      }
    }
    return {
      content: "",
      returnValue: null,
      promptTokens: Math.floor(options.instruction.length / TOKENS_PER_CHAR),
      completionTokens: 0,
      costUsd: round6(state.costUsd),
      llmCalls: state.llmCalls,
      error: {
        code: exc instanceof ReplayExhausted ? "vowdo/replay-exhausted" : "vowdo/script-error",
        message: exc instanceof Error ? exc.message : String(exc),
      },
    };
  }
}

function serializeScriptResult(value: unknown): string {
  if (value === undefined) return "null";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function round6(value: number): number {
  return Math.round((value + Number.EPSILON * Math.sign(value)) * 1e6) / 1e6;
}
