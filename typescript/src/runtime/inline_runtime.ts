/**
 * Inline runtime: executes ONLY the shipped trusted sample scripts
 * in-process. User-supplied script files are REFUSED here — generated code
 * never executes in the privileged controller; use the isolated worker for
 * anything not shipped in this build.
 */

import { digestOf } from "../contracts/canonical.ts";
import { ContractError, LiveCallBlockedError, StepFailureError } from "../contracts/common.ts";
import type {
  ModelCallResult,
  QueryBudgetPort,
  Runtime,
  RuntimeSession,
  WorkerSessionConfig,
} from "./ports.ts";
import { executeScriptedStep } from "./scripted_engine.ts";

/** Fixture-mode provider scripts shipped for the documented sample flow
 * (JS bodies; the Python reference ships the equivalent Python bodies). */
export const SAMPLE_SCRIPTS: Record<string, string[]> = {
  "extract-fact": [
    "const fact = materials.fact;\nreturn { finding: fact.value, source: fact.source };",
  ],
  "summarize": [
    "const items = Object.values(materials.items ?? {});\nreturn { count: items.length, names: items.map((i) => String(i)).sort() };",
  ],
};

export function sampleProviderNames(): string[] {
  return Object.keys(SAMPLE_SCRIPTS).sort();
}

export class InlineRuntime implements Runtime {
  private scripts: string[] = [];
  private cursor = 0;
  private usageSnapshot: Record<string, unknown> | null = null;
  private session: InlineSession | null = null;

  constructor(_sealedScriptsDigest: string | null = null) {}

  backendId(): string {
    return "vowdo-inline-scripted/1";
  }

  async openSession(
    config: WorkerSessionConfig,
    budget: QueryBudgetPort | null,
  ): Promise<RuntimeSession> {
    if (config.mode !== "fixture") {
      throw new LiveCallBlockedError("inline execution is fixture-mode only");
    }
    if (config.scriptedResponses === undefined) {
      throw new ContractError("inline runtime requires the sealed script pool");
    }
    // Inline mode executes ONLY shipped trusted scripts: the pool must
    // digest to one of the shipped sample pools (byte-for-byte).
    const digest = digestOf(config.scriptedResponses);
    const shipped = Object.values(SAMPLE_SCRIPTS).some((pool) => digestOf(pool) === digest);
    if (!shipped) {
      throw new ContractError(
        "inline mode executes only the shipped trusted sample scripts; pass user scripts to the " +
          "isolated worker (default) instead",
      );
    }
    this.scripts = [...config.scriptedResponses];
    this.cursor = config.scriptedCursor ?? 0;
    this.session = new InlineSession(
      this.scripts,
      () => this.cursor,
      (c) => {
        this.cursor = c;
      },
      () => this.usageSnapshot,
      (u) => {
        this.usageSnapshot = u;
      },
      budget,
    );
    return this.session;
  }
}

class InlineSession implements RuntimeSession {
  constructor(
    private scripts: string[],
    private cursor: () => number,
    private setCursor: (c: number) => void,
    private getUsage: () => Record<string, unknown> | null,
    private setUsage: (u: Record<string, unknown> | null) => void,
    private budget: QueryBudgetPort | null,
  ) {}

  async step(
    instruction: string,
    scope: { materials: unknown; priorArtifacts: unknown },
  ): Promise<ModelCallResult> {
    const before = this.cursor();
    const result = await executeScriptedStep({
      scripts: this.scripts,
      cursor: before,
      instruction,
      materials: scope.materials,
      priorArtifacts: scope.priorArtifacts,
      budget: {
        reserve: async (estimate) => {
          if (this.budget === null) {
            throw new ContractError("no query budget is attached to this session");
          }
          return this.budget.reserveQuery(estimate);
        },
        settle: (rid, actual) => this.budget?.settleQuery(rid, actual),
        release: (rid) => this.budget?.releaseQuery(rid),
      },
    });
    this.setCursor(before + result.llmCalls);
    const cumulativeCost = Number(this.getUsage()?.["cost_usd"] ?? 0) + result.costUsd;
    const cumulativeCalls = Number(this.getUsage()?.["llm_calls"] ?? 0) + result.llmCalls;
    this.setUsage({ cost_usd: cumulativeCost, llm_calls: cumulativeCalls, unmeasured_calls: 0 });
    if (result.error !== null) {
      throw new StepFailureError(result.error.code, result.error.message, this.getUsage());
    }
    return {
      content: result.content,
      returnValue: result.returnValue,
      promptTokens: result.promptTokens,
      completionTokens: result.completionTokens,
      costUsd: result.costUsd,
      modelId: "vowdo-scripted/fixture-1",
      raw: { return_value: result.returnValue, llm_calls: result.llmCalls },
    };
  }

  usage(): Record<string, unknown> | null {
    return this.getUsage();
  }

  cancel(_reason: string): void {
    // in-process trusted scripts cannot be interrupted mid-evaluation; the
    // durable cancel request still stops the run at the next boundary
  }

  close(): void {
    // nothing to release
  }
}
