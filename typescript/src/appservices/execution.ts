/**
 * Direct task execution service — the shared layer behind `vowdo run`,
 * `vowdo reconcile` and the detached worker.
 *
 * Durable execution configuration (Python Gate A2 parity): the FIRST
 * execute of a run persists exactly what will execute (scripts + digest,
 * provider mode, isolation mode, runtime backend identity) BEFORE anything
 * dispatches; resume re-verifies every binding and refuses mismatches.
 * The run's monotonic scripted-response cursor is persisted after every
 * completed model step, so a paused run resumed by a fresh process
 * continues without replaying consumed responses.
 */

import { digestOf } from "../contracts/canonical.ts";
import { ContractError, DigestMismatchError, VowdoError } from "../contracts/common.ts";
import {
  newTaskSpecId,
  runFromDict,
  type TaskRunData,
  taskSpec,
  type TaskSpecData,
} from "../contracts/tasks.ts";
import { defaultPolicy, Supervisor } from "../orchestrator/supervisor.ts";
import { KIND_RUN_QUERY_CURSOR } from "../orchestrator/supervisor.ts";
import { recover, type RecoveryReport } from "../orchestrator/checkpoints.ts";
import type { NativeOperationResult } from "../orchestrator/records.ts";
import { InlineRuntime, SAMPLE_SCRIPTS, sampleProviderNames } from "../runtime/inline_runtime.ts";
import { WorkerProcessRuntime } from "../runtime/worker_runtime.ts";
import type {
  QueryBudgetPort,
  Runtime,
  RuntimeSession,
  WorkerSessionConfig,
} from "../runtime/ports.ts";
import type { ResultPackageShape } from "../orchestrator/supervisor.ts";
import { materialInputRecords, snapshotCsvMaterial, storeMaterialSnapshots } from "./materials.ts";
import { reconcileCsvs, reportBytes } from "./csv_reconcile.ts";
import { digestBytes } from "../contracts/canonical.ts";
import { VERSION } from "../version.ts";
import type { ProjectWorkspace } from "./workspace.ts";

export const KIND_EXECUTION_CONFIG = "execution-config";
export const KIND_TASK_RUN = "task-run";
export const KIND_RESULT_PACKAGE = "result-package";
export const KIND_TASK_SPEC = "task-spec";
export const KIND_RUN_INDEX = "run-index";

export const CSV_OPERATION_ID = "vowdo-csv-reconcile/1";

export interface ExecutionOutcome {
  runId: string;
  status: TaskRunData["status"];
  result: ResultPackageShape | null;
  error: string | null;
}

export interface WorkerRuntimeOptions {
  workerPath?: string;
}

export class ExecutionService {
  constructor(
    private workspace: ProjectWorkspace,
    private runtimeOptions: WorkerRuntimeOptions = {},
  ) {}

  providerNames(): string[] {
    return sampleProviderNames();
  }

  private runtime(_scripts: string[], options: { isolated: boolean }): Runtime {
    if (options.isolated) {
      return new WorkerProcessRuntime(this.runtimeOptions);
    }
    return new InlineRuntime();
  }

  private supervisor(
    scripts: string[],
    options: { isolated: boolean; configIdentity?: string | null; cursor?: () => number },
  ): Supervisor {
    return new Supervisor(
      new PoolRuntime(this.runtime(scripts, options), scripts, options.cursor ?? (() => 0)),
      this.workspace.store,
      this.workspace.artifacts,
      this.workspace.ledger,
      this.workspace.journal,
      defaultPolicy(),
      options.configIdentity ?? null,
    );
  }

  // --- lifecycle ---------------------------------------------------------------

  submit(options: {
    goal: string;
    inputs: Record<string, unknown>;
    title?: string;
    budgetUsd?: number | null;
    maxSteps?: number | null;
    completionConditions?: Array<Record<string, unknown>>;
  }): string {
    const spec = taskSpec({
      specId: newTaskSpecId(),
      title: options.title ?? options.goal.slice(0, 60),
      goal: options.goal,
      mode: "fixture",
      inputs: options.inputs,
      maxCostUsd: options.budgetUsd ?? null,
      maxSteps: options.maxSteps ?? null,
      successCriteria: { conditions: options.completionConditions ?? [] },
    });
    return this.supervisor([], { isolated: true }).submit(spec);
  }

  async execute(
    runId: string,
    options: {
      provider?: string;
      scriptFiles?: string[];
      isolated?: boolean;
    } = {},
  ): Promise<ExecutionOutcome> {
    const provider = options.provider ?? "extract-fact";
    const isolated = options.isolated ?? true;
    const scripts = this.scripts(provider, options.scriptFiles ?? []);
    try {
      this.verifyOrSealConfig(runId, {
        scripts,
        provider,
        scriptFiles: options.scriptFiles ?? [],
        isolated,
      });
    } catch (exc) {
      // config mismatch is a refused dispatch, not a crash: the run keeps
      // its sealed configuration for a legitimate resume
      if (exc instanceof VowdoError) return this.errorOutcome(runId, exc);
      throw exc;
    }
    const supervisor = this.supervisor(scripts, {
      isolated,
      configIdentity: this.configIdentity(runId),
      cursor: () => this.durableCursor(runId),
    });
    try {
      const run = await supervisor.execute(runId);
      return {
        runId,
        status: run.status,
        result: this.resultOf(runId),
        error: run.error,
      };
    } catch (exc) {
      return this.errorOutcome(runId, exc);
    }
  }

  async run(options: {
    goal: string;
    inputs: Record<string, unknown>;
    budgetUsd?: number | null;
    maxSteps?: number | null;
    provider?: string;
    scriptFiles?: string[];
    isolated?: boolean;
    completionConditions?: Array<Record<string, unknown>>;
  }): Promise<ExecutionOutcome> {
    const { provider, scriptFiles, isolated, ...submit } = options;
    const runId = this.submit(submit);
    return this.execute(runId, { provider, scriptFiles, isolated });
  }

  status(runId: string): TaskRunData | null {
    return this.supervisor([], { isolated: true }).getRun(runId);
  }

  resultOf(runId: string): ResultPackageShape | null {
    const data = this.workspace.store.load(KIND_RESULT_PACKAGE, runId);
    if (data === null) return null;
    return data as unknown as ResultPackageShape;
  }

  cancel(runId: string, reason = ""): TaskRunData | null {
    try {
      return this.supervisor([], { isolated: true }).cancel(runId, reason);
    } catch (exc) {
      throw new ContractError(`cancel failed: ${exc}`);
    }
  }

  async resume(runId: string, reconciliationNote = ""): Promise<ExecutionOutcome> {
    let config: Record<string, unknown>;
    try {
      config = this.loadVerifiedConfig(runId);
    } catch (exc) {
      if (exc instanceof VowdoError) return this.errorOutcome(runId, exc);
      throw exc;
    }
    const scripts = (config["scripts"] as string[]) ?? [];
    const supervisor = this.supervisor(scripts, {
      isolated: Boolean(config["isolated"]),
      configIdentity: this.configIdentity(runId),
      cursor: () => this.durableCursor(runId),
    });
    try {
      const run = await supervisor.resume(runId, reconciliationNote);
      return { runId, status: run.status, result: this.resultOf(runId), error: run.error };
    } catch (exc) {
      return this.errorOutcome(runId, exc);
    }
  }

  async pause(runId: string): Promise<TaskRunData | null> {
    return this.supervisor([], { isolated: true }).pause(runId);
  }

  recoveryReport(runId: string): RecoveryReport {
    return recover(this.workspace.store, runId);
  }

  runs(): TaskRunData[] {
    const runs: TaskRunData[] = [];
    for (const recordId of this.workspace.store.listIds(KIND_TASK_RUN)) {
      const data = this.workspace.store.load(KIND_TASK_RUN, recordId);
      if (data !== null) {
        const { run } = this.loadRunRecord(data);
        runs.push(run);
      }
    }
    return runs.sort((
      a,
      b,
    ) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0));
  }

  private loadRunRecord(data: Record<string, unknown>): { run: TaskRunData } {
    return { run: runFromDict(data) };
  }

  // --- durable execution configuration --------------------------------------------

  private configRecord(options: {
    scripts: string[];
    provider: string;
    scriptFiles: string[];
    isolated: boolean;
    runId: string;
  }): Record<string, unknown> {
    return {
      schemaVersion: "1",
      runId: options.runId,
      providerName: options.scriptFiles.length > 0 ? null : options.provider,
      scripts: [...options.scripts],
      scriptsDigest: digestOf([...options.scripts]),
      mode: "fixture",
      isolated: options.isolated,
      runtimeId: options.isolated
        ? new WorkerProcessRuntime(this.runtimeOptions).backendId()
        : new InlineRuntime().backendId(),
      toolVersion: VERSION,
    };
  }

  private verifyOrSealConfig(
    runId: string,
    options: { scripts: string[]; provider: string; scriptFiles: string[]; isolated: boolean },
  ): void {
    const store = this.workspace.store;
    const existing = store.load(KIND_EXECUTION_CONFIG, runId);
    const record = this.configRecord({ ...options, runId });
    if (existing === null) {
      store.save(KIND_EXECUTION_CONFIG, runId, record);
      // Seed the durable query-cursor record for the sealed config.
      store.save(KIND_RUN_QUERY_CURSOR, runId, {
        runId,
        cursor: 0,
        configIdentity: record["scriptsDigest"],
        modelOrdinal: 0,
        updatedAt: new Date().toISOString().replace("Z$", "+00:00"),
      });
      return;
    }
    if (existing["scriptsDigest"] !== record["scriptsDigest"]) {
      throw new DigestMismatchError(
        `run ${runId} was started with a different provider script pool (persisted digest ` +
          `${existing["scriptsDigest"]}, requested ${record["scriptsDigest"]}); a run keeps its ` +
          `original provider — resume it as-is or cancel and submit a new run`,
      );
    }
    if (Boolean(existing["isolated"]) !== options.isolated) {
      throw new ContractError(
        `run ${runId} was started with isolated=${existing["isolated"]}; isolation mode is part ` +
          `of the sealed execution configuration`,
      );
    }
    if (existing["runtimeId"] !== record["runtimeId"]) {
      throw new ContractError(
        `run ${runId} was started on runtime ${JSON.stringify(existing["runtimeId"])} but the ` +
          `current runtime is ${JSON.stringify(record["runtimeId"])}; refusing to resume on a ` +
          `different backend (bind deliberate changes to a new run)`,
      );
    }
  }

  private loadVerifiedConfig(runId: string): Record<string, unknown> {
    const config = this.workspace.store.load(KIND_EXECUTION_CONFIG, runId);
    if (config === null) {
      throw new ContractError(
        `run ${runId} has no persisted execution configuration; it has never been executed. ` +
          `Execute it with a provider first — resume never invents placeholder work`,
      );
    }
    const scripts = ((config["scripts"] as string[]) ?? []).map(String);
    if (scripts.length === 0) {
      throw new ContractError(`run ${runId} execution config carries no scripts`);
    }
    if (digestOf(scripts) !== config["scriptsDigest"]) {
      throw new DigestMismatchError(
        `run ${runId} execution config is internally inconsistent: the stored script bytes do ` +
          `not match their recorded digest; refusing to dispatch (tampered or corrupted record)`,
      );
    }
    const providerName = config["providerName"];
    if (providerName !== null && providerName !== undefined) {
      const current = SAMPLE_SCRIPTS[String(providerName)];
      if (current === undefined) {
        throw new ContractError(
          `run ${runId} used fixture provider ${JSON.stringify(providerName)}, which no longer ` +
            `exists in this build; refusing to resume — cancel the run and submit a new one`,
        );
      }
      if (digestOf(current) !== config["scriptsDigest"]) {
        throw new DigestMismatchError(
          `fixture provider ${JSON.stringify(providerName)} changed since run ${runId} started ` +
            `(bytes digest mismatch); refusing to resume a run on modified script bytes — cancel ` +
            `it and submit a new run`,
        );
      }
    }
    const runtimeId = config["isolated"]
      ? new WorkerProcessRuntime(this.runtimeOptions).backendId()
      : new InlineRuntime().backendId();
    if (runtimeId !== config["runtimeId"]) {
      throw new ContractError(
        `run ${runId} was sealed on runtime ${
          JSON.stringify(config["runtimeId"])
        } but this build ` +
          `provides ${JSON.stringify(runtimeId)}; refusing to resume on a different backend ` +
          `(bind deliberate changes to a new run)`,
      );
    }
    return { ...config, scripts };
  }

  private configIdentity(runId: string): string | null {
    const config = this.workspace.store.load(KIND_EXECUTION_CONFIG, runId);
    if (config === null) return null;
    const identity = config["scriptsDigest"];
    return typeof identity === "string" ? identity : null;
  }

  /** The run's MONOTONIC scripted-response cursor from durable state. */
  durableCursor(runId: string): number {
    const record = this.workspace.store.load(KIND_RUN_QUERY_CURSOR, runId);
    if (record !== null) {
      const identity = this.configIdentity(runId);
      const recorded = record["configIdentity"];
      if (
        identity !== null && recorded !== null && recorded !== undefined && recorded !== identity
      ) {
        throw new ContractError(
          `run ${runId} query cursor was recorded under execution config ${
            JSON.stringify(recorded)
          } ` +
            `but the sealed config is ${
              JSON.stringify(identity)
            }; the cursor is meaningless across ` +
            `pools — cancel the run and submit a new one`,
        );
      }
      const cursor = record["cursor"];
      if (typeof cursor !== "number" || !Number.isInteger(cursor) || cursor < 0) {
        throw new ContractError(
          `run ${runId} query-cursor record carries no reliable position; cancel the run and ` +
            `submit a new one`,
        );
      }
      return cursor;
    }
    const run = this.status(runId);
    if (run === null) return 0;
    const completed = run.steps.filter((s) => s.kind === "model-call" && s.status === "ok");
    if (completed.length === 0) return 0;
    const absolutes = completed.map((s) => s.usage["queryCursor"]).filter(
      (v): v is number => typeof v === "number" && Number.isInteger(v),
    );
    if (absolutes.length !== completed.length) {
      throw new ContractError(
        `run ${runId} completed model steps before any durable query-cursor record existed; its ` +
          `consumed provider responses cannot be verified — cancel it and submit a new run`,
      );
    }
    const absoluteCursor = Math.max(...absolutes);
    this.workspace.store.save(KIND_RUN_QUERY_CURSOR, runId, {
      runId,
      cursor: absoluteCursor,
      configIdentity: this.configIdentity(runId),
      modelOrdinal: completed.length,
      updatedAt: new Date().toISOString().replace("Z$", "+00:00"),
    });
    return absoluteCursor;
  }

  private errorOutcome(runId: string, exc: unknown): ExecutionOutcome {
    const current = this.status(runId);
    return {
      runId,
      status: current?.status ?? "failed",
      result: null,
      error: `${exc instanceof Error ? exc.name : String(exc)}: ${
        exc instanceof Error ? exc.message : String(exc)
      }`,
    };
  }

  private scripts(provider: string, scriptFiles: string[]): string[] {
    if (scriptFiles.length > 0) {
      return scriptFiles.map((f) => Deno.readTextFileSync(f));
    }
    const pool = SAMPLE_SCRIPTS[provider];
    if (pool === undefined) {
      throw new ContractError(
        `unknown fixture provider ${JSON.stringify(provider)}; available: ${
          JSON.stringify(sampleProviderNames())
        } (or pass script files)`,
      );
    }
    return [...pool];
  }

  // --- native deterministic operation: CSV reconciliation ---------------------------

  submitCsvReconciliation(options: {
    goal: string;
    leftCsv: Uint8Array;
    rightCsv: Uint8Array;
    joinKey: string;
    leftName?: string;
    rightName?: string;
    delimiter?: string;
    ignoreColumns?: string[];
    title?: string;
  }): string {
    const leftName = options.leftName ?? "left.csv";
    const rightName = options.rightName ?? "right.csv";
    const delimiter = options.delimiter ?? ",";
    const snapshots = [
      snapshotCsvMaterial(options.leftCsv, { displayName: leftName, ordinal: 0, delimiter }),
      snapshotCsvMaterial(options.rightCsv, { displayName: rightName, ordinal: 1, delimiter }),
    ];
    const digests = storeMaterialSnapshots(this.workspace.artifacts, snapshots);
    const joinKey = options.joinKey;
    const ignoreColumns = options.ignoreColumns ?? [];
    const spec = taskSpec({
      specId: newTaskSpecId(),
      title: options.title ?? `Reconcile ${leftName} vs ${rightName} on ${joinKey}`,
      goal: options.goal,
      mode: "fixture",
      inputs: {
        materials: materialInputRecords(snapshots),
        operation: CSV_OPERATION_ID,
        joinKey,
        delimiter,
        ignoreColumns,
      },
      maxCostUsd: null, // deterministic operation: no model spend
      maxSteps: 1,
      successCriteria: {
        conditions: [
          {
            type: "artifact_schema",
            schema: {
              type: "object",
              required: ["joinKey", "rowCounts", "clean"],
              properties: {
                joinKey: { type: "string" },
                rowCounts: { type: "object" },
                clean: { type: "boolean" },
              },
            },
          },
        ],
      },
    });
    const runId = this.supervisor([], { isolated: true }).submit(spec);
    // Seal the native-operation configuration (first write wins).
    const store = this.workspace.store;
    if (store.load(KIND_EXECUTION_CONFIG, runId) !== null) {
      throw new ContractError(`run ${runId} already carries a sealed execution configuration`);
    }
    const parameters = {
      joinKey,
      delimiter,
      ignoreColumns,
      leftName,
      rightName,
      leftDigest: digests[0],
      rightDigest: digests[1],
    };
    const operationDigest = digestOf({ operation: CSV_OPERATION_ID, parameters });
    store.save(KIND_EXECUTION_CONFIG, runId, {
      schemaVersion: "1",
      runId,
      providerName: null,
      operation: CSV_OPERATION_ID,
      parameters,
      operationDigest,
      scripts: [],
      scriptsDigest: operationDigest,
      mode: "fixture",
      isolated: false, // trusted deterministic code, no model child
      runtimeId: `vowdo-native-operation/${CSV_OPERATION_ID}`,
      toolVersion: VERSION,
    });
    store.save(KIND_RUN_QUERY_CURSOR, runId, {
      runId,
      cursor: 0,
      configIdentity: operationDigest,
      modelOrdinal: 0,
      updatedAt: new Date().toISOString().replace("Z$", "+00:00"),
    });
    return runId;
  }

  private loadVerifiedOperationConfig(runId: string): Record<string, unknown> {
    const config = this.workspace.store.load(KIND_EXECUTION_CONFIG, runId);
    if (config === null) {
      throw new ContractError(
        `run ${runId} has no persisted execution configuration; it has never been submitted for execution`,
      );
    }
    const operation = config["operation"];
    if (operation !== CSV_OPERATION_ID) {
      throw new ContractError(
        `run ${runId} was sealed for unknown native operation ${JSON.stringify(operation)}`,
      );
    }
    const parameters = { ...((config["parameters"] as Record<string, unknown>) ?? {}) };
    if (digestOf({ operation, parameters }) !== config["operationDigest"]) {
      throw new DigestMismatchError(
        `run ${runId} operation configuration is internally inconsistent (parameters do not match ` +
          `their recorded digest); refusing to dispatch`,
      );
    }
    return config;
  }

  async executeNativeOperation(
    runId: string,
    reportSink?: { report?: unknown; digest?: string },
  ): Promise<ExecutionOutcome> {
    let config: Record<string, unknown>;
    try {
      config = this.loadVerifiedOperationConfig(runId);
    } catch (exc) {
      if (exc instanceof VowdoError) return this.errorOutcome(runId, exc);
      throw exc;
    }
    const compute = this.nativeCompute(config, reportSink);
    const supervisor = this.supervisor([], { isolated: true });
    try {
      const run = await supervisor.executeNativeOperation(runId, compute);
      return { runId, status: run.status, result: this.resultOf(runId), error: run.error };
    } catch (exc) {
      return this.errorOutcome(runId, exc);
    }
  }

  private nativeCompute(
    config: Record<string, unknown>,
    reportSink?: { report?: unknown; digest?: string },
  ): () => NativeOperationResult {
    const parameters = { ...((config["parameters"] as Record<string, unknown>) ?? {}) };
    const operation = String(config["operation"]);
    const workspace = this.workspace;
    return () => {
      const left = workspace.artifacts.get(String(parameters["leftDigest"]));
      const right = workspace.artifacts.get(String(parameters["rightDigest"]));
      const report = reconcileCsvs(left, right, {
        joinKey: String(parameters["joinKey"]),
        leftName: String(parameters["leftName"]),
        rightName: String(parameters["rightName"]),
        delimiter: String(parameters["delimiter"]),
        ignoreColumns: (parameters["ignoreColumns"] as string[]) ?? [],
      });
      const payload = reportBytes(report);
      const reportDigest = digestBytes(payload);
      if (reportSink !== undefined) {
        reportSink.report = report;
        reportSink.digest = reportDigest;
      }
      return {
        payload,
        artifacts: {
          "material:left": String(parameters["leftDigest"]),
          "material:right": String(parameters["rightDigest"]),
        },
        usage: {
          operation,
          joinKey: String(parameters["joinKey"]),
          leftRows: report.rowCounts.left,
          rightRows: report.rowCounts.right,
          matched: report.rowCounts.matched,
          changed: report.changed.length,
          missingLeft: report.missingInLeft.length,
          missingRight: report.missingInRight.length,
          duplicateKeys: report.duplicateKeys.length,
        },
        notDone: report.clean ? [] : ["discrepancies found — review the report"],
      };
    };
  }

  /** Durable submit FIRST, then execution through the claimed-run path. */
  async runCsvReconciliation(options: {
    goal: string;
    leftCsv: Uint8Array;
    rightCsv: Uint8Array;
    joinKey: string;
    leftName?: string;
    rightName?: string;
    delimiter?: string;
    ignoreColumns?: string[];
    title?: string;
  }): Promise<{ runId: string; report: ReturnType<typeof reconcileCsvs>; digest: string }> {
    const runId = this.submitCsvReconciliation(options);
    const sink: { report?: unknown; digest?: string } = {};
    const outcome = await this.executeNativeOperation(runId, sink);
    if (outcome.status !== "completed" || sink.report === undefined) {
      throw new ContractError(
        `csv reconciliation run ${runId} did not complete: ${outcome.status} ${
          outcome.error ?? ""
        }`,
      );
    }
    return {
      runId,
      report: sink.report as ReturnType<typeof reconcileCsvs>,
      digest: sink.digest as string,
    };
  }
}

/**
 * Trusted-side wrapper injecting the sealed scripted provider pool (fixture)
 * and the run's CURRENT durable cursor — a resumed session never replays
 * consumed responses (Python _ScriptedPoolRuntime parity).
 */
export class PoolRuntime implements Runtime {
  constructor(
    private inner: Runtime,
    private pool: string[],
    private cursorProvider: () => number,
  ) {}

  backendId(): string {
    return this.inner.backendId();
  }

  async openSession(
    config: WorkerSessionConfig,
    budget: QueryBudgetPort | null,
  ): Promise<RuntimeSession> {
    if (config.mode !== "fixture") {
      throw new ContractError(
        "direct execution ships with fixture-mode providers only; authorized-live wiring is " +
          "a separate, reviewed milestone",
      );
    }
    return this.inner.openSession(
      { ...config, scriptedResponses: [...this.pool], scriptedCursor: this.cursorProvider() },
      budget,
    );
  }
}

export type { TaskSpecData };
