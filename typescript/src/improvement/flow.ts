/**
 * ImprovementFlow — the application-service layer over the controller:
 * baseline freeze, propose/seal, paired evaluation through configured
 * runners (fixture loopback by default; the real Choose/Visibility runners
 * through explicit, recorded commands), final acceptance, approval and
 * rollback. Task-only workspaces refuse everything here — owner identities
 * are never invented.
 */

import { ContractError, newId, utcNowIso } from "../contracts/common.ts";
import { digestOf } from "../contracts/canonical.ts";
import { ProcessAdapterClient } from "../adapters/process_adapter.ts";
import { loadFixturePack } from "../adapters/fixture_adapter.ts";
import type { ProjectWorkspace } from "../appservices/workspace.ts";
import {
  type AttemptRecord,
  type CaseSplit,
  type EvaluationRun,
  ImprovementController,
} from "./controller.ts";
import {
  agentVersion,
  type Candidate,
  candidateContentDigest,
  type Rubric,
  rubricDigest,
  THRESHOLD_MIN_MAIN_IMPROVEMENT,
} from "./contracts.ts";
import { isFile } from "../contracts/fsutil.ts";

/** The runner configuration: explicit, user-recorded commands, never guesses. */
export interface RunnerConfig {
  schemaVersion: "1";
  /** Explicit runner locations; cwd lets a runner resolve its own project. */
  choose?: { command: string[]; cwd?: string; note?: string };
  visibility?: { command: string[]; cwd?: string; note?: string };
  fixture?: { fixturesPath: string };
}

export function loadRunnerConfig(workspace: ProjectWorkspace): RunnerConfig | null {
  const path = `${workspace.vowdoDir}/runners.json`;
  if (!isFile(path)) return null;
  const parsed = JSON.parse(Deno.readTextFileSync(path));
  if (!isRunnerConfig(parsed)) {
    throw new ContractError(`${path} is not a valid runner configuration`);
  }
  return parsed;
}

function isRunnerConfig(value: unknown): value is RunnerConfig {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return record["schemaVersion"] === "1";
}

export function saveRunnerConfig(workspace: ProjectWorkspace, config: RunnerConfig): string {
  const path = `${workspace.vowdoDir}/runners.json`;
  Deno.writeTextFileSync(path, JSON.stringify(config, null, 2) + "\n");
  return path;
}

/** Command that runs the in-repo TS fixture adapter (dev mode). */
export function fixtureAdapterCommand(mainModule: string, fixturesPath: string): string[] {
  const exec = Deno.execPath();
  if (exec.endsWith("deno")) {
    return [
      "deno",
      "run",
      "--no-prompt",
      "--allow-read",
      "--allow-write",
      mainModule,
      "__fixture-adapter",
      "--fixtures",
      fixturesPath,
    ];
  }
  return [exec, "__fixture-adapter", "--fixtures", fixturesPath];
}

export class ImprovementFlow {
  readonly controller: ImprovementController;

  constructor(private workspace: ProjectWorkspace, private mainModule: string) {
    this.controller = new ImprovementController(workspace);
  }

  private requireImprovementMode(): void {
    if (this.workspace.spec.mode !== "improvement") {
      throw new ContractError(
        "improvement commands are refused until a workspace is re-initialized in default " +
          "(improvement) mode with workflows and owners — task-only workspaces never invent owner identities",
      );
    }
  }

  recordBaseline(options: {
    versionId: string;
    sourceRef: string;
    workflowId: string;
    mainMetric?: string;
    minImprovement?: number;
    direction?: "increase" | "decrease";
    repeats?: number;
    frozenBy?: string;
  }): { rubricDigest: string; baselineRecordId: string } {
    this.requireImprovementMode();
    const spec = this.workspace.spec;
    const workflow = spec.workflows.find((w) => w.workflowId === options.workflowId);
    if (workflow === undefined) {
      throw new ContractError(
        `unknown workflow ${JSON.stringify(options.workflowId)}; declared: ${
          JSON.stringify(spec.workflows.map((w) => w.workflowId))
        }`,
      );
    }
    const owner = options.frozenBy ?? spec.owners["acceptance-owner"] ?? "";
    if (owner.trim().length === 0) {
      throw new ContractError(
        "rubric freezing requires a non-empty identity (--frozen-by); the project also declares no " +
          "acceptance owner to default to",
      );
    }
    const version = agentVersion({
      versionId: options.versionId,
      sourceRef: options.sourceRef,
      environmentDigest: digestOf({ tool: "vowdo-agent-ts", env: "local-fixture" }),
    });
    const baselineRecordId = this.controller.recordBaseline(version, options.workflowId);
    const thresholds: Record<string, number> = {
      [THRESHOLD_MIN_MAIN_IMPROVEMENT]: options.minImprovement ?? 0,
    };
    const rubric: Rubric = {
      schemaVersion: "1",
      mainMetric: options.mainMetric ?? "main",
      direction: options.direction ?? "increase",
      thresholds,
      hardGuardrails: workflow.guardrails,
      repeats: options.repeats ?? 1,
      frozenBy: null,
      frozenAt: null,
    };
    const frozen = this.controller.freezeRubric(rubric, owner);
    this.workspace.store.save("workflow-rubric", options.workflowId, {
      rubricDigest: frozen.digest,
    });
    return { rubricDigest: frozen.digest, baselineRecordId };
  }

  propose(options: {
    delta: string;
    changeType: string;
    rationale: string;
    workflowId: string;
    expectedImpact?: string;
    proposer?: string;
    seal?: boolean;
  }): { candidate: Candidate; contentDigest: string; sealed: boolean } {
    this.requireImprovementMode();
    const baseline = this.controller.baselineFor(options.workflowId);
    const candidate: Candidate = {
      schemaVersion: "1",
      candidateId: newId("cand"),
      parentVersion: baseline,
      changeType: options.changeType as Candidate["changeType"],
      delta: options.delta,
      rationale: options.rationale,
      expectedImpact: options.expectedImpact ?? "",
      proposer: options.proposer ?? "proposer-agent",
      devCaseRefs: [],
      state: "proposed",
      stateReason: "",
      createdAt: utcNowIso(),
    };
    this.controller.propose(candidate);
    const sealed = options.seal === true ? this.controller.seal(candidate.candidateId) : candidate;
    return {
      candidate: sealed,
      contentDigest: candidateContentDigest(sealed),
      sealed: options.seal === true,
    };
  }

  /**
   * Build the adapter for an evaluation. The fixture loopback is the default;
   * `runner:<name>` requires an explicitly configured runner command and
   * refuses (honestly) when the configured command does not exist.
   */
  buildAdapter(adapterKind: string): ProcessAdapterClient {
    if (adapterKind === "scripted" || adapterKind === "fixture") {
      const config = loadRunnerConfig(this.workspace);
      const fixturesPath = config?.fixture?.fixturesPath ?? defaultFixturesPath(this.workspace);
      if (!isFile(fixturesPath)) {
        throw new ContractError(
          `fixture pack ${JSON.stringify(fixturesPath)} does not exist; create it with ` +
            `'vowdo improve fixtures --out <path>' or configure a runner (runners.json)`,
        );
      }
      return new ProcessAdapterClient(
        fixtureAdapterCommand(this.mainModule, fixturesPath),
        { label: "fixture-adapter" },
      );
    }
    if (adapterKind.startsWith("runner:")) {
      const name = adapterKind.slice("runner:".length);
      const config = loadRunnerConfig(this.workspace);
      const runner = config?.[name as "choose" | "visibility"];
      if (runner === undefined) {
        throw new ContractError(
          `runner ${JSON.stringify(name)} is not configured; record its command in ` +
            `${this.workspace.vowdoDir}/runners.json (explicit locations only, never guessed)`,
        );
      }
      if (!isFile(runner.command[0])) {
        throw new ContractError(
          `runner ${JSON.stringify(name)} command ${
            JSON.stringify(runner.command[0])
          } does not exist ` +
            `on this host; refusing to execute an unconfigured runner (honest skip, not a failure verdict)`,
        );
      }
      return new ProcessAdapterClient(runner.command, { label: `runner:${name}`, cwd: runner.cwd });
    }
    throw new ContractError(`unknown adapter kind ${JSON.stringify(adapterKind)}`);
  }

  /** Import a fixture pack's cases as the split's case set. */
  importFixtureCases(
    fixturesPath: string,
    split: CaseSplit,
  ): Array<{ caseId: string; input: Record<string, unknown> }> {
    const pack = loadFixturePack(fixturesPath);
    const cases: Array<{ caseId: string; input: Record<string, unknown> }> = [];
    for (const scenario of pack.scenarios) {
      if (scenario.split !== undefined && scenario.split !== split) continue;
      const inputBytes = new TextEncoder().encode(
        JSON.stringify({ caseId: scenario.scenarioId, workflowId: scenario.workflowId }),
      );
      this.controller.importCase(scenario.scenarioId, scenario.workflowId, split, inputBytes);
      cases.push({ caseId: scenario.scenarioId, input: { caseId: scenario.scenarioId } });
    }
    if (cases.length === 0) {
      throw new ContractError(
        `fixture pack ${JSON.stringify(fixturesPath)} has no cases for split ${split}`,
      );
    }
    return cases;
  }

  async evaluate(options: {
    candidateId: string;
    workflowId: string;
    adapterKind?: string;
    split?: CaseSplit;
    repeats?: number;
    perAttemptReserveUsd?: number;
  }): Promise<EvaluationRun> {
    this.requireImprovementMode();
    const adapterKind = options.adapterKind ?? "scripted";
    const split = options.split ?? "development";
    if (split === "final-acceptance") {
      throw new ContractError(
        "final acceptance runs through 'vowdo improve accept' (the acceptance side owns that split)",
      );
    }
    const fixturesPath = loadRunnerConfig(this.workspace)?.fixture?.fixturesPath ??
      defaultFixturesPath(this.workspace);
    const cases = this.importFixtureCases(fixturesPath, split);
    const baseline = this.controller.baselineFor(options.workflowId);
    const rubric = this.controller.rubricFor(options.workflowId);
    const adapter = this.buildAdapter(adapterKind);
    try {
      return await this.controller.runPairedEvaluation({
        workflowId: options.workflowId,
        candidateId: options.candidateId,
        baseline,
        adapter,
        split,
        rubricDigest: rubricDigest(rubric),
        repeats: options.repeats ?? rubric.repeats,
        cases,
        perAttemptReserveUsd: options.perAttemptReserveUsd ?? 0.05,
      });
    } finally {
      await adapter.close();
    }
  }

  async finalAcceptance(options: {
    candidateId: string;
    workflowId: string;
    adapterKind?: string;
    owner: string;
    evidenceOut: string;
  }): Promise<
    { run: EvaluationRun; evidenceDigest: string; decisionId: string | null; verdict: string }
  > {
    this.requireImprovementMode();
    if (options.owner === "proposer") {
      throw new ContractError("the proposer cannot be the acceptance owner");
    }
    const adapterKind = options.adapterKind ?? "scripted";
    const fixturesPath = loadRunnerConfig(this.workspace)?.fixture?.fixturesPath ??
      defaultFixturesPath(this.workspace);
    const cases = this.importFixtureCases(fixturesPath, "final-acceptance");
    const baseline = this.controller.baselineFor(options.workflowId);
    const rubric = this.controller.rubricFor(options.workflowId);
    const adapter = this.buildAdapter(adapterKind);
    let run: EvaluationRun;
    try {
      run = await this.controller.runPairedEvaluation({
        workflowId: options.workflowId,
        candidateId: options.candidateId,
        baseline,
        adapter,
        split: "final-acceptance",
        rubricDigest: rubricDigest(rubric),
        repeats: rubric.repeats,
        cases,
        perAttemptReserveUsd: 0.05,
      });
    } finally {
      await adapter.close();
    }
    const { exportEvidencePackage } = await import("./evidence.ts");
    const pkg = exportEvidencePackage(this.workspace, run, options.evidenceOut);
    if (run.verdict === "accepted") {
      const decision = this.controller.decide({
        run,
        rubric,
        owner: options.owner,
        evidenceDigest: pkg.manifestDigest,
      });
      return {
        run,
        evidenceDigest: pkg.manifestDigest,
        decisionId: decision.decisionId,
        verdict: decision.verdict,
      };
    }
    return {
      run,
      evidenceDigest: pkg.manifestDigest,
      decisionId: null,
      verdict: run.verdict ?? "inconclusive",
    };
  }
}

export function defaultFixturesPath(workspace: ProjectWorkspace): string {
  return `${workspace.vowdoDir}/fixtures-improvement.json`;
}

export const SYNTHETIC_FIXTURE_PACK = {
  schemaVersion: 1,
  synthetic: true,
  packId: "vowdo-improvement-fixtures-v1",
  notes:
    "SYNTHETIC improvement-semantics fixtures: deterministic paired baseline/candidate outcomes " +
    "that prove sealing, comparison, acceptance and invalidation. NOT evidence of model or product " +
    "improvement.",
  guardrailFamilies: [
    {
      familyId: "no-hallucinated-citations",
      fixtureIds: ["improve-normal-en", "improve-guardrail-hit-en"],
    },
  ],
  scenarios: [
    {
      scenarioId: "improve-normal-en",
      workflowId: "W-C3",
      split: "development",
      baseline: {
        ok: true,
        metric: 0.62,
        guardrails: { "no-hallucinated-citations": true },
        usage: { tokensScripted: 180, elapsedMsMeasured: 41, costUsd: 0.01 },
        outputs: { note: "synthetic baseline" },
      },
      candidate: {
        ok: true,
        metric: 0.71,
        guardrails: { "no-hallucinated-citations": true },
        usage: { tokensScripted: 200, elapsedMsMeasured: 43, costUsd: 0.012 },
        outputs: { note: "synthetic candidate" },
      },
    },
    {
      scenarioId: "improve-normal-2-en",
      workflowId: "W-C3",
      split: "development",
      baseline: {
        ok: true,
        metric: 0.55,
        guardrails: { "no-hallucinated-citations": true },
        usage: { tokensScripted: 160, elapsedMsMeasured: 38, costUsd: 0.01 },
      },
      candidate: {
        ok: true,
        metric: 0.68,
        guardrails: { "no-hallucinated-citations": true },
        usage: { tokensScripted: 190, elapsedMsMeasured: 40, costUsd: 0.011 },
      },
    },
    {
      scenarioId: "improve-final-en",
      workflowId: "W-C3",
      split: "final-acceptance",
      baseline: {
        ok: true,
        metric: 0.60,
        guardrails: { "no-hallucinated-citations": true },
        usage: { tokensScripted: 170, elapsedMsMeasured: 40, costUsd: 0.01 },
      },
      candidate: {
        ok: true,
        metric: 0.75,
        guardrails: { "no-hallucinated-citations": true },
        usage: { tokensScripted: 210, elapsedMsMeasured: 45, costUsd: 0.012 },
      },
    },
    {
      scenarioId: "improve-guardrail-hit-en",
      workflowId: "W-C3",
      split: "selection-validation",
      baseline: {
        ok: true,
        metric: 0.6,
        guardrails: { "no-hallucinated-citations": true },
        usage: { tokensScripted: 150, elapsedMsMeasured: 39, costUsd: 0.01 },
      },
      candidate: {
        ok: true,
        metric: 0.9,
        guardrails: { "no-hallucinated-citations": false },
        usage: { tokensScripted: 220, elapsedMsMeasured: 44, costUsd: 0.012 },
      },
    },
  ],
} as const;

export type { AttemptRecord };
