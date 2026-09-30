/**
 * The Supervisor — bounded business task execution (design §4.4, §10).
 * Faithful port of the historical Python reference:
 *
 * - budget reserved at submit, BEFORE any step runs; every underlying query
 *   carves an atomic child reservation before the call (nested included);
 * - failure keeps its metering; unmeasurable cost settles conservatively,
 *   never as zero;
 * - persist intent, act, persist result; checkpoint after each step;
 * - ownership lease + revision-fenced run writes (two-worker fencing);
 * - write-ahead finalization intent (crash between terminal writes is
 *   finished exactly once, no work replayed);
 * - a model saying "done" never completes a run: only the spec's
 *   success_criteria checked by trusted code can;
 * - fail-closed bounds (max steps, wall clock, budget) and honest
 *   needs-reconciliation handling for unknown side-effect outcomes.
 */

import { canonicalJson, digestOf, isPlainObject, pythonPlainJson } from "../contracts/canonical.ts";
import {
  BudgetError,
  BudgetExhaustedError,
  ContractError,
  DigestMismatchError,
  InvalidStateTransitionError,
  newId,
  ReconciliationRequiredError,
  ReservationError,
  StepFailureError,
  utcNowIso,
  VowdoError,
} from "../contracts/common.ts";
import {
  type BudgetReservationData,
  costEntry,
  type EventKindValue,
  eventRecord,
  reservationFromDict,
} from "../contracts/journal.ts";
import {
  isTerminal,
  newRun,
  newStep,
  runFromDict,
  type RunStepData,
  specFromDict,
  type TaskRunData,
  type TaskSpecData,
  type TaskStatusValue,
  withStep,
  withTransition,
} from "../contracts/tasks.ts";
import {
  allPassed,
  checksMap,
  type Condition,
  CONDITION_ARTIFACT_SCHEMA,
  type ConditionEvaluation,
  evaluateConditions,
  humanMarkers,
  machineChecksPassed,
  normalizeCriteria,
} from "./conditions.ts";
import {
  CHECKPOINT_KIND,
  recover,
  SIDE_EFFECTING_STEP_KINDS,
  STEP_STATUS_FAILED,
  STEP_STATUS_OK,
  STEP_STATUS_RUNNING,
  STEP_STATUS_SKIPPED,
  STEP_STATUS_UNKNOWN,
  writeCheckpoint,
} from "./checkpoints.ts";
import {
  finalizationFromDict,
  type FinalizationIntentData,
  intentComplete,
  intentWithFlag,
  KIND_RUN_CLOCK,
  KIND_RUN_FINALIZATION,
  KIND_RUN_OWNERSHIP,
  leaseLive,
  type NativeOperationResult,
  type OwnershipRecord,
  pendingWrites,
  readFinalization,
  readOwnership,
  RunOwnershipError,
} from "./records.ts";
import type { MetadataStore } from "../storage/store.ts";
import type { ArtifactStore } from "../storage/artifacts.ts";
import type { Journal } from "../storage/journal.ts";
import type { BudgetLedger } from "../storage/budget.ts";
import type { Runtime, RuntimeSession, WorkerSessionConfig } from "../runtime/ports.ts";

// --- persisted record kinds ------------------------------------------------------

export const KIND_TASK_SPEC = "task-spec";
export const KIND_TASK_RUN = "task-run";
export const KIND_INVOCATION = "invocation";
export const KIND_RESULT_PACKAGE = "result-package";
export const KIND_RUN_INDEX = "run-index";
export const KIND_RUN_RESERVATION = "run-reservation";
export const KIND_BUDGET_SLICE = "budget-slice";
export const KIND_CANCEL_REQUEST = "cancel-request";
export const KIND_PAUSE_REQUEST = "pause-request";
export const KIND_RECONCILIATION_NOTE = "reconciliation-note";
export const KIND_RUN_QUERY_CURSOR = "run-query-cursor";

const CONSUMED_WORK_STATUSES = new Set([STEP_STATUS_OK, STEP_STATUS_UNKNOWN]);
const EPS = 1e-9;

function nowEpoch(): number {
  return Date.now() / 1000;
}

type StopReason =
  | "conditions-met"
  | "pending-acceptance"
  | "max-steps"
  | "wall-clock"
  | "budget-exhausted"
  | "model-failed"
  | "prepare-failed"
  | "no-success-criteria"
  | "cancelled"
  | "needs-reconciliation"
  | "operation-incomplete";

interface Stop {
  reason: StopReason;
  error?: string | null;
}

export interface SupervisorPolicy {
  defaultBudgetUsd: number;
  defaultMaxSteps: number;
  defaultWallClockS: number;
  stepBudgetUsd: number;
  ownershipLeaseS: number;
  pausedRunExpiryS: number;
  runCapabilities: string[];
  modelStepCapabilities: string[];
  contextStepCapabilities: string[];
}

export function defaultPolicy(): SupervisorPolicy {
  return {
    defaultBudgetUsd: 1.0,
    defaultMaxSteps: 8,
    defaultWallClockS: 300.0,
    stepBudgetUsd: 0.25,
    ownershipLeaseS: 900.0,
    pausedRunExpiryS: 86400.0,
    runCapabilities: ["model.invoke", "artifact.read", "artifact.write"],
    modelStepCapabilities: ["model.invoke"],
    contextStepCapabilities: ["artifact.read"],
  };
}

export function policyDigest(policy: SupervisorPolicy): string {
  return digestOf({
    policyVersion: 2,
    defaultBudgetUsd: policy.defaultBudgetUsd,
    defaultMaxSteps: policy.defaultMaxSteps,
    defaultWallClockS: policy.defaultWallClockS,
    stepBudgetUsd: policy.stepBudgetUsd,
    ownershipLeaseS: policy.ownershipLeaseS,
    pausedRunExpiryS: policy.pausedRunExpiryS,
    runCapabilities: policy.runCapabilities,
    modelStepCapabilities: policy.modelStepCapabilities,
    contextStepCapabilities: policy.contextStepCapabilities,
  });
}

// --- per-query budget authority (controller side) ---------------------------------

export class StepQueryBudget {
  private open = new Map<string, number>();
  private stepIds: string[] = [];
  private querySeq = 0;
  private stepMeasuredUsd = 0;
  private stepAssumedUsd = 0;
  private refused = new Map<string, number>();
  private stepHolder = "";
  private allowanceUsd = 0;

  constructor(
    private ledger: BudgetLedger,
    private parentReservationId: string,
    private journal: Journal,
    private runId: string,
    private mode: TaskRunData["mode"],
  ) {}

  beginStep(holder: string, allowanceUsd: number): void {
    this.stepHolder = holder;
    this.allowanceUsd = allowanceUsd;
    this.stepIds = [];
    this.stepMeasuredUsd = 0;
    this.stepAssumedUsd = 0;
  }

  /** Reservation ids created by this step whose query never settled. */
  endStep(): string[] {
    const orphans = this.stepIds.filter((rid) => this.open.has(rid));
    this.stepIds = [];
    return orphans;
  }

  stepCreatedAny(): boolean {
    return this.stepIds.length > 0;
  }

  stepMeasured(): number {
    return round6(this.stepMeasuredUsd);
  }

  stepAssumed(): number {
    return round6(this.stepAssumedUsd);
  }

  reserveQuery(estimateUsd: number | null): string {
    // An unknown price reserves the whole remaining step allowance: the
    // conservative reading. While it is open NO further query is dispatched.
    const amount = estimateUsd === null ? this.allowanceUsd : Number(estimateUsd);
    if (!(amount > EPS)) {
      throw new BudgetExhaustedError(
        `no budget left to reserve for a model query in ${this.stepHolder || "run"}`,
      );
    }
    this.querySeq += 1;
    const child = this.ledger.reserveChild(
      this.parentReservationId,
      `${this.stepHolder}#q${this.querySeq}`,
      round6(amount),
    );
    this.open.set(child.reservationId, child.amountUsd);
    this.stepIds.push(child.reservationId);
    return child.reservationId;
  }

  settleQuery(reservationId: string, actualUsd: number | null): void {
    const amount = this.open.get(reservationId);
    if (amount === undefined) return;
    this.open.delete(reservationId);
    if (actualUsd === null) {
      // Unmeasurable: book the full reservation conservatively, never zero.
      this.ledger.settle(reservationId, amount);
      this.stepAssumedUsd += amount;
      this.book(null, false, reservationId);
      return;
    }
    const actual = Number(actualUsd);
    if (!Number.isFinite(actual)) {
      this.open.set(reservationId, amount);
      throw new ContractError(
        `non-finite cost ${JSON.stringify(actualUsd)} reported for query ${
          JSON.stringify(reservationId)
        }`,
      );
    }
    try {
      this.ledger.settle(reservationId, round6(actual));
    } catch (exc) {
      if (exc instanceof BudgetExhaustedError) {
        // The provider exceeded its declared bound beyond what the parent can
        // absorb: the child stays OPEN, the overrun is surfaced, never clipped.
        this.open.set(reservationId, amount);
        this.refused.set(reservationId, actual);
        this.journal.append(
          eventRecord("audit-note", this.runId, {
            action: "settlement-refused-overrun",
            reservationId,
            reportedUsd: round6(actual),
            reservedUsd: round6(amount),
          }, { actor: "supervisor", mode: this.mode }),
        );
      }
      throw exc;
    }
    this.stepMeasuredUsd += round6(actual);
    this.book(round6(actual), true, reservationId);
  }

  releaseQuery(reservationId: string): void {
    const amount = this.open.get(reservationId);
    if (amount === undefined) return;
    this.open.delete(reservationId);
    this.ledger.release(reservationId);
  }

  /** Conservatively settle a reservation whose query never reported. */
  adoptOrphan(reservationId: string): number {
    const amount = this.open.get(reservationId) ?? 0;
    this.open.delete(reservationId);
    if (amount > 0) {
      this.ledger.settle(reservationId, amount);
      this.stepAssumedUsd += amount;
      this.book(null, false, reservationId);
    }
    return amount;
  }

  refusedSettlements(): Map<string, number> {
    return new Map(this.refused);
  }

  private book(amount: number | null, measurable: boolean, reservationId: string): void {
    this.journal.appendCost(
      costEntry(this.runId, {
        category: "model",
        amountUsd: amount,
        measurable,
        mode: this.mode,
        note: `query reservation ${reservationId}`,
      }),
    );
  }
}

// --- run state --------------------------------------------------------------------

interface RunState {
  run: TaskRunData;
  spec: TaskSpecData;
  conditions: Condition[];
  reservation: BudgetReservationData;
  rootInvocationId: string;
  policyDigestValue: string;
  steps: RunStepData[];
  budgetUsd: number;
  maxSteps: number;
  wallClockS: number;
  measuredUsd: number;
  assumedUnmeasuredUsd: number;
  childlessAssumedUsd: number;
  childlessMeasuredUsd: number;
  unreservedSpendUsd: number;
  overrunReservations: Map<string, number>;
  unmeasuredCost: boolean;
  artifactKeys: Map<string, string>;
  modelOrdinal: number;
  sequence: number;
  nativeNotDone: string[];
  revision: number;

  conservativeSpentUsd(): number;
  overrunUsd(): number;
}

function newStateBase(): Omit<RunState, "conservativeSpentUsd" | "overrunUsd"> {
  return {
    run: null as never,
    spec: null as never,
    conditions: [],
    reservation: null as never,
    rootInvocationId: "",
    policyDigestValue: "",
    steps: [],
    budgetUsd: 0,
    maxSteps: 0,
    wallClockS: 0,
    measuredUsd: 0,
    assumedUnmeasuredUsd: 0,
    childlessAssumedUsd: 0,
    childlessMeasuredUsd: 0,
    unreservedSpendUsd: 0,
    overrunReservations: new Map(),
    unmeasuredCost: false,
    artifactKeys: new Map(),
    modelOrdinal: 1,
    sequence: 0,
    nativeNotDone: [],
    revision: 0,
  };
}

// --- the supervisor ----------------------------------------------------------------

export class Supervisor {
  private active = new Set<string>();
  private activeSessions = new Map<string, RuntimeSession>();
  private ownerId = newId("sup");

  constructor(
    private runtime: Runtime,
    private store: MetadataStore,
    private artifacts: ArtifactStore,
    private ledger: BudgetLedger,
    private journal: Journal,
    private policy: SupervisorPolicy = defaultPolicy(),
    private configIdentity: string | null = null,
  ) {}

  // --- public API -------------------------------------------------------------

  /** Persist spec + queued run; reserve budget BEFORE any work starts. */
  submit(spec: TaskSpecData): string {
    const conditions = normalizeCriteria(spec.successCriteria); // fail closed early
    validateSpecBounds(spec);
    const budgetUsd = spec.maxCostUsd ?? this.policy.defaultBudgetUsd;
    const runId = newRun(specDigestOf(spec), spec.mode).runId;
    // Atomic ledger reservation first: if the project cap refuses, nothing is
    // persisted and nothing is described as schedulable.
    const reservation = this.ledger.reserve(runId, budgetUsd);
    const policyDigestValue = policyDigest(this.policy);

    this.store.save(KIND_TASK_SPEC, spec.specId, spec);
    const rootInvocation = {
      schemaVersion: "1",
      invocationId: newId("inv"),
      taskRef: spec.specId,
      parentId: null,
      inputRefs: [] as string[],
      outputSchema: {},
      policyDigest: policyDigestValue,
      budgetReservationId: reservation.reservationId,
      grantedCapabilities: this.policy.runCapabilities,
      status: "running",
      startedAt: utcNowIso(),
      endedAt: null,
      resultDigest: null,
      error: null,
      depth: 0,
    };
    this.store.save(KIND_INVOCATION, rootInvocation.invocationId, rootInvocation);
    const run: TaskRunData = {
      ...newRun(specDigestOf(spec), spec.mode),
      runId,
    };
    this.store.save(KIND_TASK_RUN, runId, { ...run, revision: 1 });
    this.store.save(KIND_RUN_INDEX, runId, {
      specId: spec.specId,
      taskDigest: run.taskDigest,
      reservationId: reservation.reservationId,
      rootInvocationId: rootInvocation.invocationId,
      policyDigest: policyDigestValue,
      budgetUsd,
    });
    this.store.save(KIND_RUN_RESERVATION, runId, reservation);
    this.journal.append(
      eventRecord("budget-reserved", runId, {
        level: "run",
        reservationId: reservation.reservationId,
        amountUsd: budgetUsd,
      }, { actor: "supervisor", mode: spec.mode }),
    );
    this.journal.append(
      eventRecord("run-started", runId, {
        taskDigest: run.taskDigest,
        specId: spec.specId,
        budgetReservationId: reservation.reservationId,
        budgetUsd,
        conditionCount: conditions.length,
      }, { actor: "supervisor", mode: spec.mode }),
    );
    const state = this.loadState(runId);
    this.checkpoint(state);
    return runId;
  }

  /** Run the §4.4 loop to an explicit stop. Returns the final run. */
  async execute(runId: string): Promise<TaskRunData> {
    this.guardNotActive(runId);
    const pending = this.pendingFinalization(runId);
    if (pending !== null) return this.finishFinalization(pending);
    this.acquireOwnership(runId, "execute");
    const state = this.loadState(runId);
    this.prepareStart(state);
    return await this.runLoop(state);
  }

  /**
   * Resume after reconciliation (or any non-terminal stop). A run with
   * unknown side-effect state requires a non-empty verified note. The
   * unknown step is never replayed. A run whose terminal writes are
   * incomplete is FINISHED, not resumed.
   */
  async resume(runId: string, reconciliationNote = ""): Promise<TaskRunData> {
    this.guardNotActive(runId);
    const pending = this.pendingFinalization(runId);
    if (pending !== null) return this.finishFinalization(pending);
    this.acquireOwnership(runId, "resume");
    const state = this.loadState(runId);
    if (isTerminal(state.run.status)) {
      throw new InvalidStateTransitionError(
        `run ${runId} is terminal (${state.run.status}); nothing to resume`,
      );
    }
    if (
      state.run.status === "running" &&
      state.steps.some((s) =>
        s.status === STEP_STATUS_RUNNING && SIDE_EFFECTING_STEP_KINDS.has(s.kind)
      )
    ) {
      this.enterReconciliation(state, "crash left side-effecting step in flight");
    }
    if (state.run.status === "needs-reconciliation") {
      this.reconcile(state, reconciliationNote);
    }
    this.prepareStart(state);
    return await this.runLoop(state);
  }

  /**
   * Cooperative cancellation, authoritative at the next step boundary. Only
   * an unowned (or expired-lease) run is finalized here; an actively owned
   * run keeps its request for the owner.
   */
  cancel(runId: string, reason = ""): TaskRunData {
    const state = this.loadState(runId);
    if (isTerminal(state.run.status)) {
      throw new InvalidStateTransitionError(
        `run ${runId} is terminal (${state.run.status}); cannot cancel`,
      );
    }
    this.store.save(KIND_CANCEL_REQUEST, runId, { requestedAt: utcNowIso(), reason });
    if (this.active.has(runId)) return state.run;
    if (!this.mayFinalize(runId)) {
      this.journalEvent(state, "audit-note", {
        action: "cancel-requested",
        owner: "another-supervisor",
        reason,
      });
      return state.run;
    }
    this.acquireOwnership(runId, "cancel");
    return this.finalize(state, { reason: "cancelled", error: reason || null }, null);
  }

  pause(runId: string): TaskRunData {
    const state = this.loadState(runId);
    if (isTerminal(state.run.status)) {
      throw new InvalidStateTransitionError(
        `run ${runId} is terminal (${state.run.status}); cannot pause`,
      );
    }
    if (state.run.status !== "running") {
      throw new InvalidStateTransitionError(
        `cannot pause a run in status ${state.run.status}`,
      );
    }
    this.store.save(KIND_PAUSE_REQUEST, runId, { requestedAt: utcNowIso() });
    if (this.active.has(runId)) return state.run;
    if (!this.mayFinalize(runId)) {
      this.journalEvent(state, "audit-note", {
        action: "pause-requested",
        owner: "another-supervisor",
      });
      return state.run;
    }
    this.acquireOwnership(runId, "pause");
    return this.doPause(state);
  }

  /** Stop the run's in-flight owned execution session (external cancel). */
  stopActiveSession(runId: string, reason = ""): boolean {
    const session = this.activeSessions.get(runId);
    if (session === undefined) return false;
    try {
      session.cancel(reason || "cancelled by operator request");
    } catch {
      // the durable request stands; stopping is best effort
    }
    try {
      session.close();
    } catch {
      // close must not mask the cancel
    }
    return true;
  }

  getRun(runId: string): TaskRunData | null {
    const data = this.store.load(KIND_TASK_RUN, runId);
    return data === null ? null : runFromDict(data);
  }

  getResult(runId: string): Record<string, unknown> | null {
    return this.store.load(KIND_RESULT_PACKAGE, runId);
  }

  get ownerIdValue(): string {
    return this.ownerId;
  }

  // --- native deterministic operations ------------------------------------------

  async executeNativeOperation(
    runId: string,
    compute: () => NativeOperationResult,
  ): Promise<TaskRunData> {
    this.guardNotActive(runId);
    const pending = this.pendingFinalization(runId);
    if (pending !== null) return this.finishFinalization(pending);
    this.acquireOwnership(runId, "execute-native-operation");
    const state = this.loadState(runId);
    this.prepareStart(state);
    this.ensureClock(state);
    this.active.add(runId);
    try {
      if (this.pauseRequested(runId)) return this.doPause(state);
      if (this.cancelRequested(runId)) return this.finalize(state, { reason: "cancelled" }, null);
      const step = this.beginStep(state, "tool-call", state.run.taskDigest);
      let outcome: NativeOperationResult;
      try {
        outcome = compute();
      } catch (exc) {
        this.endStep(state, step, STEP_STATUS_FAILED, { error: `${errorName(exc)}: ${exc}` });
        return this.finalize(state, { reason: "prepare-failed", error: String(exc) }, null);
      }
      const digest = this.artifacts.put(outcome.payload);
      for (const [key, artifactDigest] of Object.entries(outcome.artifacts ?? {})) {
        state.artifactKeys.set(key, artifactDigest);
      }
      state.artifactKeys.set("final", digest);
      state.nativeNotDone = outcome.notDone ?? [];
      this.endStep(state, step, STEP_STATUS_OK, {
        outputDigest: digest,
        usage: outcome.usage ?? {},
      });
      if (this.cancelRequested(runId)) {
        // Cancellation is authoritative even over a produced artifact.
        return this.finalize(state, { reason: "cancelled" }, null);
      }
      const evaluation = this.evaluate(state);
      const stop: Stop = allPassed(evaluation)
        ? { reason: "conditions-met" }
        : { reason: "operation-incomplete" };
      return this.finalize(state, stop, null);
    } finally {
      this.active.delete(runId);
      try {
        this.accumulateElapsed(state);
      } catch {
        // clock accumulation must never mask results
      }
    }
  }

  // --- run ownership ------------------------------------------------------------

  private mayFinalize(runId: string): boolean {
    const record = readOwnership(this.store, runId);
    if (record === null || !leaseLive(record, nowEpoch())) return true;
    return record.ownerId === this.ownerId;
  }

  private acquireOwnership(runId: string, purpose: string): OwnershipRecord {
    const now = nowEpoch();
    return this.store.transaction(() => {
      const record = readOwnership(this.store, runId);
      if (record !== null && leaseLive(record, now) && record.ownerId !== this.ownerId) {
        throw new RunOwnershipError(
          `run ${runId} is owned by supervisor ${record.ownerId} (lease live for another ` +
            `${Math.round(record.leaseExpiresAtEpochS - now)}s); a second client may request ` +
            `cancel/pause but cannot ${purpose} it`,
        );
      }
      const acquired: OwnershipRecord = {
        schemaVersion: "1",
        runId,
        ownerId: this.ownerId,
        acquiredAt: utcNowIso(),
        leaseExpiresAtEpochS: now + this.policy.ownershipLeaseS,
        heartbeatAt: utcNowIso(),
        revision: (record?.revision ?? 0) + 1,
        releasedAt: null,
      };
      this.store.save(KIND_RUN_OWNERSHIP, runId, acquired);
      return acquired;
    });
  }

  private heartbeatOwnership(runId: string): void {
    const record = readOwnership(this.store, runId);
    if (record === null || record.releasedAt !== null) return;
    if (record.ownerId !== this.ownerId) {
      throw new RunOwnershipError(
        `run ${runId} ownership was taken over by ${record.ownerId}; ` +
          `this supervisor's snapshot is stale and must stop writing`,
      );
    }
    this.store.save(KIND_RUN_OWNERSHIP, runId, {
      ...record,
      leaseExpiresAtEpochS: nowEpoch() + this.policy.ownershipLeaseS,
      heartbeatAt: utcNowIso(),
    });
  }

  private releaseOwnership(runId: string): void {
    const record = readOwnership(this.store, runId);
    if (record === null || record.ownerId !== this.ownerId) return;
    this.store.save(KIND_RUN_OWNERSHIP, runId, { ...record, releasedAt: utcNowIso() });
  }

  // --- run clock -----------------------------------------------------------------

  private ensureClock(state: RunState): Record<string, unknown> {
    let clock = this.store.load(KIND_RUN_CLOCK, state.run.runId);
    if (clock !== null) return clock;
    const now = nowEpoch();
    clock = {
      schemaVersion: "1",
      runId: state.run.runId,
      wallClockS: state.wallClockS,
      startedAtEpochS: now,
      deadlineEpochS: now + state.wallClockS,
      accumulatedElapsedS: 0.0,
      lastResumedAtEpochS: now,
    };
    this.store.save(KIND_RUN_CLOCK, state.run.runId, clock);
    return clock;
  }

  private saveClock(clock: Record<string, unknown>): void {
    this.store.save(KIND_RUN_CLOCK, String(clock["runId"]), clock);
  }

  private pauseExpiryEpoch(state: RunState): number | null {
    const clock = this.store.load(KIND_RUN_CLOCK, state.run.runId);
    if (clock === null) return null;
    const expiry = clock["pauseExpiresAtEpochS"];
    return typeof expiry === "number" ? expiry : null;
  }

  private accumulateElapsed(state: RunState): void {
    const clock = this.store.load(KIND_RUN_CLOCK, state.run.runId);
    if (clock === null) return;
    const resumed = Number(clock["lastResumedAtEpochS"] ?? 0);
    if (resumed <= 0) return;
    clock["accumulatedElapsedS"] = round3(
      Number(clock["accumulatedElapsedS"] ?? 0) + Math.max(0, nowEpoch() - resumed),
    );
    clock["lastResumedAtEpochS"] = 0.0;
    this.saveClock(clock);
  }

  // --- start preparation -----------------------------------------------------------

  private guardNotActive(runId: string): void {
    if (this.active.has(runId)) {
      throw new ContractError(`run ${runId} is already executing in this supervisor`);
    }
  }

  private prepareStart(state: RunState): void {
    const run = state.run;
    if (isTerminal(run.status)) {
      throw new InvalidStateTransitionError(`run ${run.runId} is terminal (${run.status})`);
    }
    if (run.status === "paused") {
      const expiry = this.pauseExpiryEpoch(state);
      if (expiry !== null && nowEpoch() > expiry) {
        throw new InvalidStateTransitionError(
          `run ${run.runId} has been paused past its expiry ` +
            `(${this.policy.pausedRunExpiryS.toFixed(0)}s); its reservation stays open until it ` +
            `is cancelled — cancel() closes it with honest accounting`,
        );
      }
    }
    if (run.status === "needs-reconciliation") {
      throw new ReconciliationRequiredError(
        `run ${run.runId} needs reconciliation; call resume(runId, reconciliationNote) with a verified note`,
      );
    }
    const inFlight = state.steps.map((s, i) => ({ s, i })).filter(({ s }) =>
      s.status === STEP_STATUS_RUNNING
    );
    const sideEffecting = inFlight.filter(({ s }) => SIDE_EFFECTING_STEP_KINDS.has(s.kind));
    if (sideEffecting.length > 0) {
      const ids = sideEffecting.map(({ s }) => s.stepId);
      throw new ReconciliationRequiredError(
        `crash left side-effecting step(s) ${JSON.stringify(ids)} with unknown outcome; ` +
          `recover() then resume(runId, note) is required`,
      );
    }
    if (inFlight.length > 0) this.skipInFlightSteps(state, inFlight.map(({ i }) => i));
    if (run.status === "queued" || run.status === "paused") {
      this.transition(state, "running");
    }
    this.saveRootInvocation(state, "running");
  }

  private skipInFlightSteps(state: RunState, indexes: number[]): void {
    for (const i of indexes) {
      state.steps[i] = { ...state.steps[i], status: STEP_STATUS_SKIPPED, endedAt: utcNowIso() };
    }
    state.run = { ...state.run, steps: [...state.steps], updatedAt: utcNowIso() };
    this.persistRun(state);
    this.journalEvent(state, "audit-note", {
      action: "skip-deterministic-in-flight-steps",
      stepIds: indexes.map((i) => state.steps[i].stepId),
    });
    this.checkpoint(state);
  }

  private enterReconciliation(state: RunState, reason: string): void {
    const inFlight = state.steps.map((s, i) => ({ s, i })).filter(({ s }) =>
      s.status === STEP_STATUS_RUNNING
    );
    for (const { s, i } of inFlight) {
      if (SIDE_EFFECTING_STEP_KINDS.has(s.kind)) {
        state.steps[i] = {
          ...s,
          status: STEP_STATUS_UNKNOWN,
          endedAt: utcNowIso(),
          sideEffectUnknown: true,
          error: s.error ?? "in flight at crash; outcome unknown",
        };
        this.settleSliceForUnknownStep(state, state.steps[i]);
        state.unmeasuredCost = true;
      } else {
        state.steps[i] = { ...s, status: STEP_STATUS_SKIPPED, endedAt: utcNowIso() };
      }
    }
    state.run = { ...state.run, steps: [...state.steps], updatedAt: utcNowIso() };
    this.persistRun(state);
    if (state.run.status === "running") this.transition(state, "needs-reconciliation");
    this.journalEvent(state, "run-reconciliation", { action: "marked-unknown", reason });
    this.checkpoint(state);
  }

  private reconcile(state: RunState, note: string): void {
    const inFlight = state.steps.filter((s) => s.status === STEP_STATUS_RUNNING);
    if (inFlight.length > 0) this.enterReconciliation(state, "in-flight steps found at resume");
    let requiresNote = state.steps.some(
      (s) => s.sideEffectUnknown || s.status === STEP_STATUS_UNKNOWN,
    );
    if (state.run.mode !== "fixture") requiresNote = true; // only fixture runs auto-continue
    if (requiresNote && note.trim().length === 0) {
      throw new ReconciliationRequiredError(
        `run ${state.run.runId} has unknown side-effect state; a non-empty verified ` +
          `reconciliation note is required before resume`,
      );
    }
    state.sequence += 1;
    this.store.save(KIND_RECONCILIATION_NOTE, `${state.run.runId}:${state.sequence}`, {
      note,
      recordedAt: utcNowIso(),
      noteRequired: requiresNote,
    });
    this.journalEvent(state, "run-reconciliation", {
      action: "resume",
      note,
      noteRequired: requiresNote,
    });
    this.transition(state, "running");
  }

  private settleSliceForUnknownStep(state: RunState, step: RunStepData): void {
    let ordinal = 0;
    let holder: string | null = null;
    for (const candidate of state.steps) {
      if (candidate.kind === "model-call") ordinal += 1;
      if (candidate.stepId === step.stepId) {
        if (candidate.kind === "model-call") holder = `${state.run.runId}#model-${ordinal}`;
        break;
      }
    }
    if (holder === null) return;
    const sliceData = this.store.load(KIND_BUDGET_SLICE, holder);
    if (sliceData === null || sliceData["status"] !== "open") return;
    const amount = Number(sliceData["amountUsd"]);
    this.settleStepSlice(state, sliceData, amount, {
      unmeasurable: true,
      detail: `step ${step.stepId}: outcome unknown; cost unmeasured`,
      assumedUsd: amount,
      childlessAssumed: amount,
    });
    state.unmeasuredCost = true;
    state.assumedUnmeasuredUsd += amount;
    state.childlessAssumedUsd += amount;
  }

  // --- the execution loop --------------------------------------------------------

  private async runLoop(state: RunState): Promise<TaskRunData> {
    const runId = state.run.runId;
    this.active.add(runId);
    let session: RuntimeSession | null = null;
    let budget: StepQueryBudget | null = null;
    const clock = this.ensureClock(state);
    clock["lastResumedAtEpochS"] = nowEpoch();
    this.saveClock(clock);
    const deadline = Number(clock["deadlineEpochS"]);
    try {
      let stop: Stop | null = null;

      if (!this.hasOkStep(state, "plan")) stop = this.phaseConfirmGoal(state);
      if (stop === null && !this.hasOkStep(state, "tool-call")) {
        stop = this.phaseGatherContext(state);
      }
      if (stop === null && !this.hasOkStep(state, "gate-check")) stop = this.phaseGateCheck(state);

      while (stop === null) {
        if (this.pauseRequested(runId)) return this.doPause(state);
        if (this.cancelRequested(runId)) {
          stop = { reason: "cancelled" };
          break;
        }
        if (this.consumedWorkSteps(state) >= state.maxSteps) {
          stop = { reason: "max-steps" };
          break;
        }
        if (nowEpoch() >= deadline) {
          stop = { reason: "wall-clock" };
          break;
        }
        if (state.conservativeSpentUsd() >= state.budgetUsd - EPS) {
          this.journalEvent(state, "budget-exhausted", {
            budgetUsd: state.budgetUsd,
            measuredUsd: state.measuredUsd,
            assumedUnmeasuredUsd: state.assumedUnmeasuredUsd,
          });
          stop = { reason: "budget-exhausted" };
          break;
        }
        if (session === null) {
          budget = this.queryBudget(state);
          session = await this.runtime.openSession(this.sessionConfig(state, deadline), budget);
          this.activeSessions.set(runId, session);
        }
        stop = await this.phaseModelCall(state, session, budget!);
        if (stop !== null) break;
        if (this.cancelRequested(runId)) {
          // Cancellation is authoritative over "conditions met".
          stop = { reason: "cancelled" };
          break;
        }
        const evaluation = this.evaluate(state);
        if (allPassed(evaluation)) stop = { reason: "conditions-met" };
        else if (machineChecksPassed(evaluation)) stop = { reason: "pending-acceptance" };
      }
      if (stop!.reason !== "cancelled" && this.cancelRequested(runId)) {
        stop = { reason: "cancelled" }; // authoritative at finalize too
      }
      return this.finalize(state, stop!, session);
    } finally {
      this.activeSessions.delete(runId);
      if (session !== null) {
        try {
          session.close();
        } catch {
          // close must not mask results
        }
      }
      this.active.delete(runId);
      try {
        this.accumulateElapsed(state);
      } catch {
        // never fatal
      }
    }
  }

  private pauseRequested(runId: string): boolean {
    return this.requestActive(KIND_PAUSE_REQUEST, runId);
  }

  private cancelRequested(runId: string): boolean {
    return this.requestActive(KIND_CANCEL_REQUEST, runId);
  }

  private requestActive(kind: string, runId: string): boolean {
    const record = this.store.load(kind, runId);
    return record !== null && !("consumedAt" in record);
  }

  private consumeRequest(kind: string, runId: string): void {
    const record = this.store.load(kind, runId);
    if (record !== null) {
      record["consumedAt"] = utcNowIso();
      this.store.save(kind, runId, record);
    }
  }

  private phaseConfirmGoal(state: RunState): Stop | null {
    const step = this.beginStep(state, "plan", state.run.taskDigest);
    if (state.reservation.status !== "open") {
      const error = `run budget reservation is ${state.reservation.status}`;
      this.endStep(state, step, STEP_STATUS_FAILED, { error });
      return { reason: "prepare-failed", error };
    }
    const summary = {
      goal: state.spec.goal,
      mode: state.spec.mode,
      budgetUsd: state.budgetUsd,
      maxSteps: state.maxSteps,
      wallClockS: state.wallClockS,
      conditionCount: state.conditions.length,
      locale: state.spec.locale,
    };
    this.endStep(state, step, STEP_STATUS_OK, {
      outputDigest: digestOf(summary),
      usage: { conditionCount: state.conditions.length },
    });
    if (state.conditions.length === 0) return { reason: "no-success-criteria" };
    return null;
  }

  private phaseGatherContext(state: RunState): Stop | null {
    const step = this.beginStep(state, "tool-call", digestOf({ inputs: state.spec.inputs }));
    const payload = new TextEncoder().encode(canonicalJson(state.spec.inputs));
    const digest = this.artifacts.put(payload);
    state.artifactKeys.set("inputs", digest);
    this.endStep(state, step, STEP_STATUS_OK, {
      outputDigest: digest,
      usage: { inputKeys: Object.keys(state.spec.inputs).sort() },
    });
    return null;
  }

  private phaseGateCheck(state: RunState): Stop | null {
    const backendId = this.runtime.backendId();
    const step = this.beginStep(state, "gate-check", digestOf({ backend: backendId }));
    if (backendId.length === 0) {
      const error = "runtime backend id is empty";
      this.endStep(state, step, STEP_STATUS_FAILED, { error });
      return { reason: "prepare-failed", error };
    }
    const usage = {
      mode: state.spec.mode,
      allowsLiveCalls: state.spec.mode === "authorized-live",
      allowsSideEffects: state.spec.mode === "authorized-live",
      backendId,
      remainingBudgetUsd: round6(state.budgetUsd - state.conservativeSpentUsd()),
    };
    this.endStep(state, step, STEP_STATUS_OK, {
      outputDigest: digestOf(usage),
      usage,
    });
    return null;
  }

  private queryBudget(state: RunState): StepQueryBudget {
    return new StepQueryBudget(
      this.ledger,
      state.reservation.reservationId,
      this.journal,
      state.run.runId,
      state.run.mode,
    );
  }

  private sessionUsage(session: RuntimeSession | null): Record<string, unknown> | null {
    if (session === null) return null;
    try {
      const usage = session.usage();
      return usage === null ? null : { ...usage };
    } catch {
      return null;
    }
  }

  private static stepQueryDelta(
    result: { raw: Record<string, unknown> } | null,
    usageBefore: Record<string, unknown> | null,
    usageAfter: Record<string, unknown> | null,
  ): number | null {
    const rawCalls = result?.raw?.["llm_calls"];
    if (typeof rawCalls === "number" && Number.isInteger(rawCalls) && rawCalls >= 0) {
      return rawCalls;
    }
    const before = usageBefore?.["llm_calls"];
    const after = usageAfter?.["llm_calls"];
    if (
      typeof before === "number" && Number.isInteger(before) && before >= 0 &&
      typeof after === "number" && Number.isInteger(after) && after >= 0
    ) {
      return Math.max(0, after - before);
    }
    return null;
  }

  private advanceQueryCursor(state: RunState, callsDelta: number | null): number | null {
    const runId = state.run.runId;
    const record = this.store.load(KIND_RUN_QUERY_CURSOR, runId);
    const current = record !== null && typeof record["cursor"] === "number" ? record["cursor"] : 0;
    if (callsDelta === null) {
      if (record === null) return null;
      return current;
    }
    const newCursor = current + callsDelta;
    if (record === null || newCursor > current) {
      this.store.save(KIND_RUN_QUERY_CURSOR, runId, {
        runId,
        cursor: newCursor,
        configIdentity: this.configIdentity,
        modelOrdinal: state.modelOrdinal,
        updatedAt: utcNowIso(),
      });
    }
    return newCursor;
  }

  private async phaseModelCall(
    state: RunState,
    session: RuntimeSession,
    budget: StepQueryBudget,
  ): Promise<Stop | null> {
    const n = state.modelOrdinal;
    const remaining = state.budgetUsd - state.conservativeSpentUsd();
    const sliceAmount = round6(Math.min(this.policy.stepBudgetUsd, remaining));
    if (sliceAmount <= EPS) {
      this.journalEvent(state, "budget-exhausted", {
        budgetUsd: state.budgetUsd,
        atModelStep: n,
      });
      return { reason: "budget-exhausted" };
    }

    // Budget slice reservation, persisted before the call (record id is the
    // holder string, so recovery can find it without extra indexes).
    const sliceHolder = `${state.run.runId}#model-${n}`;
    const sliceReservation: BudgetReservationData = {
      schemaVersion: "1",
      reservationId: newId("rsv"),
      holder: sliceHolder,
      amountUsd: sliceAmount,
      status: "open",
      settledAmountUsd: null,
      createdAt: utcNowIso(),
      closedAt: null,
      parentReservationId: null,
    };
    this.store.save(KIND_BUDGET_SLICE, sliceHolder, sliceReservation);
    this.journalEvent(state, "budget-reserved", {
      level: "step",
      holder: sliceHolder,
      reservationId: sliceReservation.reservationId,
      amountUsd: sliceAmount,
      modelStep: n,
    });

    const invocation = {
      schemaVersion: "1",
      invocationId: newId("inv"),
      taskRef: state.spec.specId,
      parentId: state.rootInvocationId,
      inputRefs: state.artifactKeys.has("inputs") ? [state.artifactKeys.get("inputs")!] : [],
      outputSchema: this.modelOutputSchema(state),
      policyDigest: state.policyDigestValue,
      budgetReservationId: sliceReservation.reservationId,
      grantedCapabilities: this.policy.modelStepCapabilities,
      status: "running",
      startedAt: utcNowIso(),
      endedAt: null,
      resultDigest: null,
      error: null,
      depth: 1,
    };
    this.store.save(KIND_INVOCATION, invocation.invocationId, invocation);

    const instruction = this.instruction(state, n);
    const step = this.beginStep(
      state,
      "model-call",
      digestOf({
        modelStep: n,
        instruction,
        context: state.artifactKeys.get("inputs") ?? null,
        priorArtifacts: [...state.artifactKeys.values()],
      }),
      sliceReservation.reservationId,
    );

    budget.beginStep(sliceHolder, sliceAmount);
    const usageBefore = this.sessionUsage(session);
    // Scoped verified inputs (review Stage C): the step receives the
    // digest-verified bytes as in-scope data, not opaque digests; later steps
    // additionally receive earlier steps' verified artifacts.
    const materials = this.materials(state);
    const priorArtifacts = this.priorArtifacts(state);
    let result;
    try {
      result = await session.step(instruction, { materials, priorArtifacts: priorArtifacts });
    } catch (exc) {
      if (exc instanceof VowdoError) {
        // Known protocol failure — but never an excuse to book zero: the
        // measured usage that survived is recovered from the error itself or
        // the session's metering snapshots.
        const failure = exc instanceof StepFailureError ? exc : null;
        const usageAfter = failure?.usage ?? this.sessionUsage(session);
        const closed = this.closeStepBudget(state, budget, sliceReservation, sliceAmount, {
          reportedCost: null,
          usageBefore,
          usageAfter,
          detail: `model step ${n}: failed with ${exc.code}`,
        });
        if (exc instanceof BudgetError) {
          this.journalEvent(state, "budget-exhausted", {
            budgetUsd: state.budgetUsd,
            measuredUsd: round6(state.measuredUsd),
            assumedUnmeasuredUsd: round6(state.assumedUnmeasuredUsd),
            reason: `query refused before it ran: ${exc.code}`,
          });
        }
        this.endStep(state, step, STEP_STATUS_FAILED, {
          error: `${exc.code}: ${exc}`,
          usage: {
            failedCostUsd: closed.unmeasurable ? null : closed.measured,
            assumedUnmeasuredUsd: closed.assumed,
          },
          invocation: { ...invocation, status: "failed", endedAt: utcNowIso(), error: String(exc) },
        });
        return { reason: "model-failed", error: `${exc.code}: ${exc}` };
      }
      // Unexpected failure during the side-effecting call: the outcome is
      // UNKNOWN. Mark the step, settle everything it reserved conservatively,
      // and require reconciliation — never replayed.
      this.closeStepBudget(state, budget, sliceReservation, sliceAmount, {
        reportedCost: null,
        usageBefore,
        usageAfter: null, // metering is unusable after a crash
        detail: `model step ${n}: outcome unknown after crash`,
        forceConservative: true,
      });
      state.modelOrdinal += 1; // consumed: never replayed
      this.endStep(state, step, STEP_STATUS_UNKNOWN, {
        error: `outcome unknown: ${errorName(exc)}: ${exc}`,
        sideEffectUnknown: true,
        invocation: {
          ...invocation,
          status: "failed",
          endedAt: utcNowIso(),
          error: `side-effect outcome unknown: ${exc}`,
        },
      });
      this.journalEvent(state, "run-reconciliation", {
        action: "marked-unknown",
        stepId: step.stepId,
        error: `${errorName(exc)}: ${exc}`,
      });
      return { reason: "needs-reconciliation", error: `${errorName(exc)}: ${exc}` };
    }

    const usageAfter = this.sessionUsage(session);
    const closed = this.closeStepBudget(state, budget, sliceReservation, sliceAmount, {
      reportedCost: result.costUsd,
      usageBefore,
      usageAfter,
      detail: `model step ${n} (${step.stepId})`,
    });
    // The step artifact is the EVALUATED result when the runtime provides one
    // (trusted serialization here — the model never certifies its artifact).
    let payloadText: string;
    if (result.returnValue !== null && result.returnValue !== undefined) {
      payloadText = pythonPlainJson(result.returnValue);
    } else {
      payloadText = result.content;
    }
    const digest = this.artifacts.put(new TextEncoder().encode(payloadText));
    state.artifactKeys.set(`step:${n}`, digest);
    state.artifactKeys.set("final", digest);
    if (n === 1) state.artifactKeys.set("draft", digest);
    const callsDelta = Supervisor.stepQueryDelta(
      { raw: result.raw },
      usageBefore,
      usageAfter,
    );
    const queryCursor = this.advanceQueryCursor(state, callsDelta);
    const usage: Record<string, unknown> = {
      promptTokens: result.promptTokens,
      completionTokens: result.completionTokens,
      costUsd: result.costUsd,
      measuredUsd: closed.measured,
      assumedUnmeasuredUsd: closed.assumed,
      modelId: result.modelId,
      backendId: this.runtime.backendId(),
      llmCalls: callsDelta,
      sessionLlmCalls: typeof usageAfter?.["llm_calls"] === "number"
        ? usageAfter["llm_calls"]
        : null,
      queryCursor,
    };
    this.endStep(state, step, STEP_STATUS_OK, {
      outputDigest: digest,
      usage,
      invocation: {
        ...invocation,
        status: "completed",
        endedAt: utcNowIso(),
        resultDigest: digest,
      },
    });
    state.modelOrdinal += 1;
    return null;
  }

  private closeStepBudget(
    state: RunState,
    budget: StepQueryBudget,
    sliceReservation: BudgetReservationData,
    sliceAmount: number,
    options: {
      reportedCost: number | null;
      usageBefore: Record<string, unknown> | null;
      usageAfter: Record<string, unknown> | null;
      detail: string;
      forceConservative?: boolean;
    },
  ): { measured: number; assumed: number; unmeasurable: boolean } {
    const refused = budget.refusedSettlements();
    for (const reservationId of budget.endStep()) {
      if (refused.has(reservationId)) continue; // stays OPEN for reconciliation
      budget.adoptOrphan(reservationId);
    }
    for (const [rid, actual] of refused) state.overrunReservations.set(rid, actual);
    let measured = budget.stepMeasured();
    let assumed = budget.stepAssumed();
    const [cost, unmeasured] = this.stepCost(
      options.reportedCost,
      options.usageBefore,
      options.usageAfter,
    );
    let childlessAssumed = 0;
    let childlessMeasured = 0;
    if (cost !== null && !unmeasured && cost > measured + EPS) {
      // Measured spend no child reservation covers: booked in full — never
      // clipped — and flagged as a port-contract violation.
      const gap = round6(cost - measured);
      measured += gap;
      childlessMeasured = gap;
      state.unreservedSpendUsd += gap;
      this.journalEvent(state, "audit-note", {
        action: "spend-without-query-reservation",
        usd: gap,
        backendId: this.runtime.backendId(),
      });
      this.appendCost(
        state,
        gap,
        true,
        `measured spend with no query reservation ($${gap.toFixed(6)})`,
      );
    }
    if (measured <= EPS && assumed <= EPS && (unmeasured || cost === null)) {
      assumed = sliceAmount;
      childlessAssumed = sliceAmount;
    }
    if (options.forceConservative && assumed <= EPS && measured <= EPS) {
      assumed = sliceAmount;
      childlessAssumed = sliceAmount;
    }
    state.measuredUsd += measured;
    state.assumedUnmeasuredUsd += assumed;
    state.childlessAssumedUsd += childlessAssumed;
    state.childlessMeasuredUsd += childlessMeasured;
    const unmeasurable = assumed > EPS;
    if (unmeasurable) state.unmeasuredCost = true;
    this.settleStepSlice(state, sliceReservation, round6(measured + assumed), {
      unmeasurable,
      detail: options.detail,
      assumedUsd: assumed,
      childlessAssumed,
      childlessMeasured,
    });
    return { measured, assumed, unmeasurable };
  }

  private stepCost(
    reported: number | null,
    before: Record<string, unknown> | null,
    after: Record<string, unknown> | null,
  ): [number | null, boolean] {
    const [beforeCost, beforeUnknown] = usageCost(before);
    const [afterCost, afterUnknown] = usageCost(after);
    if (before !== null && after !== null && !beforeUnknown) {
      if (afterUnknown || afterCost === null || beforeCost === null) return [null, true];
      return [round6(afterCost - beforeCost), false];
    }
    if (reported === null) return [null, true];
    return [Number(reported), false];
  }

  // --- finalize (recoverable terminal writes) --------------------------------------

  private finalize(state: RunState, stop: Stop, session: RuntimeSession | null): TaskRunData {
    const sessionUsage = this.sessionUsage(session) ?? {};

    // Conservative bookkeeping first: crashed-open query children settle at
    // their full amount; anything still open after that is a refused overrun.
    this.adoptOpenChildren(state);
    for (const child of this.ledger.children(state.reservation.reservationId)) {
      if (child.status === "open") {
        if (!state.overrunReservations.has(child.reservationId)) {
          state.overrunReservations.set(
            child.reservationId,
            child.settledAmountUsd ?? child.amountUsd,
          );
        }
      }
    }

    const overrun = state.overrunUsd();
    const step = this.beginStep(
      state,
      "finalize",
      digestOf({ stopReason: stop.reason, error: stop.error ?? null }),
    );
    const evaluation = this.evaluate(state);
    let pkg = this.buildPackage(state, stop, evaluation);
    if (state.overrunReservations.size > 0) {
      pkg = {
        ...pkg,
        uncertainties: [
          ...pkg.uncertainties,
          `at least one model query cost more than its reservation could absorb ` +
          `(${JSON.stringify([...state.overrunReservations.keys()].sort())}); the overrun is ` +
          `open pending reconciliation`,
        ],
      };
      stop = {
        reason: "needs-reconciliation",
        error: "budget overrun: a query exceeded its declared bound",
      };
    }
    let target: TaskStatusValue;
    if (stop.reason === "needs-reconciliation") target = "needs-reconciliation";
    else if (stop.reason === "cancelled") target = "cancelled";
    else if (stop.reason === "conditions-met" && packageDeliverable(pkg)) target = "completed";
    else target = "failed";
    if (state.unreservedSpendUsd > EPS && target === "completed") {
      target = "failed";
      const note =
        `runtime reported $${
          state.unreservedSpendUsd.toFixed(6)
        } of spend no per-query reservation ` +
        `covers; the budget port contract was violated`;
      pkg = {
        ...pkg,
        notDoneItems: [...pkg.notDoneItems, note],
        uncertainties: [...pkg.uncertainties, note],
      };
    }
    if (overrun > EPS && target === "completed") {
      // A run that overran its budget is never certified as budget-conformant.
      target = "failed";
      const overrunNote =
        `budget overrun: conservative spend $${state.conservativeSpentUsd().toFixed(6)} exceeds ` +
        `the $${state.budgetUsd.toFixed(6)} task budget by $${overrun.toFixed(6)}`;
      pkg = {
        ...pkg,
        notDoneItems: [...pkg.notDoneItems, overrunNote],
        uncertainties: [
          ...pkg.uncertainties,
          "budget overrun: completion cannot be certified as budget-conformant",
        ],
      };
    }
    this.endStep(state, step, STEP_STATUS_OK, {
      outputDigest: digestOf(pkg),
      usage: {
        stopReason: stop.reason,
        session: sessionUsage,
        measuredUsd: round6(state.measuredUsd),
        assumedUnmeasuredUsd: round6(state.assumedUnmeasuredUsd),
        overrunUsd: round6(Math.max(0, overrun)),
      },
    });

    const intent: FinalizationIntentData = {
      schemaVersion: "1",
      runId: state.run.runId,
      targetStatus: target,
      resultDigest: digestOf(pkg),
      package: pkg as unknown as Record<string, unknown>,
      conservativeUsd: round6(state.conservativeSpentUsd()),
      childlessOverageUsd: round6(state.childlessAssumedUsd + state.childlessMeasuredUsd),
      reservationId: state.reservation.reservationId,
      ledgerAction: target === "needs-reconciliation" ? "keep-open" : "close",
      unmeasured: state.unmeasuredCost,
      packageSaved: false,
      ledgerSettled: false,
      metadataFinalized: false,
      ownershipReleased: false,
      createdAt: utcNowIso(),
      updatedAt: utcNowIso(),
    };
    return this.runFinalizationProtocol(state, intent);
  }

  private runFinalizationProtocol(state: RunState, intent: FinalizationIntentData): TaskRunData {
    this.writeIntent(intent);
    return this.applyFinalizationWrites(state, intent);
  }

  private writeIntent(intent: FinalizationIntentData): void {
    this.store.save(KIND_RUN_FINALIZATION, intent.runId, intent);
  }

  private applyFinalizationWrites(state: RunState, intentIn: FinalizationIntentData): TaskRunData {
    let current = intentIn;
    if (!current.packageSaved) {
      this.store.save(KIND_RESULT_PACKAGE, current.runId, current.package);
      current = intentWithFlag(current, "packageSaved");
      this.writeIntent(current);
    }
    if (!current.ledgerSettled) {
      this.settleRunLedger(state, current);
      current = intentWithFlag(current, "ledgerSettled");
      this.writeIntent(current);
    }
    if (!current.metadataFinalized) {
      this.finalizeMetadata(state, current);
      current = intentWithFlag(current, "metadataFinalized");
      this.writeIntent(current);
    }
    if (!current.ownershipReleased) {
      this.releaseOwnership(current.runId);
      current = intentWithFlag(current, "ownershipReleased");
      this.writeIntent(current);
    }
    this.consumeRequest(KIND_PAUSE_REQUEST, current.runId);
    this.consumeRequest(KIND_CANCEL_REQUEST, current.runId);
    return this.getRun(current.runId) ?? state.run;
  }

  private settleRunLedger(state: RunState, intent: FinalizationIntentData): void {
    if (intent.ledgerAction === "keep-open") {
      // Reconciliation may resume: the reservation keeps counting.
      this.store.save(KIND_RUN_RESERVATION, intent.runId, state.reservation);
      return;
    }
    this.adoptOpenChildren(state);
    if (state.reservation.status !== "open") {
      this.journalEvent(state, "audit-note", {
        action: "run-reservation-already-closed",
        status: state.reservation.status,
        intendedUsd: intent.conservativeUsd,
      });
    } else {
      try {
        const settled = this.ledger.settleParentFromChildren(
          intent.reservationId ?? state.reservation.reservationId,
          intent.childlessOverageUsd,
        );
        this.store.save(KIND_RUN_RESERVATION, intent.runId, settled);
      } catch (exc) {
        if (exc instanceof ReservationError) {
          // Our own earlier attempt committed before the crash (the intent
          // proves one was in progress and we hold the lease): exactly-once.
          this.journalEvent(state, "audit-note", {
            action: "run-settlement-already-committed",
            intendedUsd: intent.conservativeUsd,
          });
        } else {
          throw exc;
        }
      }
    }
    this.journalEvent(state, "budget-settled", {
      level: "run",
      reservationId: intent.reservationId,
      actualUsd: intent.conservativeUsd,
      overageUsd: intent.childlessOverageUsd,
      unmeasured: intent.unmeasured,
    });
  }

  private finalizeMetadata(state: RunState, intent: FinalizationIntentData): void {
    const runId = intent.runId;
    const target = intent.targetStatus as TaskStatusValue;
    const persisted = this.store.load(KIND_TASK_RUN, runId);
    const persistedStatus = persisted !== null
      ? String(persisted["status"]) as TaskStatusValue
      : null;
    if (persistedStatus !== null && isTerminal(persistedStatus) && persistedStatus !== target) {
      // A persisted terminal status is never overwritten (cancellation wins).
      this.journalEvent(state, "audit-note", {
        action: "terminal-status-not-overwritten",
        persistedStatus,
        intendedStatus: target,
      });
    } else if (persistedStatus !== target) {
      const current = persistedStatus === state.run.status ? state.run : this.getRun(runId);
      if (current === null) throw new ContractError(`run ${runId} vanished during finalization`);
      const allowed: readonly TaskStatusValue[] = transitionsOf(current.status);
      if (allowed.includes(target)) {
        this.transition(state, target, {
          error: intentError(intent),
          resultDigest: intent.resultDigest,
        });
      } else {
        this.journalEvent(state, "audit-note", {
          action: "terminal-transition-unavailable",
          fromStatus: current.status,
          intendedStatus: target,
        });
      }
    }
    if (target === "completed") {
      this.journalEvent(state, "run-completed", {
        resultDigest: intent.resultDigest,
        stopReason: "finalization",
      });
    } else if (target === "cancelled") {
      this.journalEvent(state, "run-cancelled", { resultDigest: intent.resultDigest });
    } else {
      this.journalEvent(state, "run-failed", {
        resultDigest: intent.resultDigest,
        stopReason: "finalization",
        error: intentError(intent),
        needsReconciliation: target === "needs-reconciliation",
      });
    }
    const rootStatus = target === "completed"
      ? "completed"
      : target === "cancelled"
      ? "cancelled"
      : "failed";
    this.saveRootInvocation(state, rootStatus, intent.resultDigest ?? undefined);
    this.checkpoint(state);
  }

  private adoptOpenChildren(state: RunState): void {
    for (const child of this.ledger.children(state.reservation.reservationId)) {
      if (child.status !== "open") continue;
      if (state.overrunReservations.has(child.reservationId)) continue; // refused: stays open
      this.ledger.settle(child.reservationId, child.amountUsd);
      this.appendCost(
        state,
        null,
        false,
        `query reservation ${child.reservationId} never settled (owner died); assumed spent conservatively`,
      );
      state.unmeasuredCost = true;
      state.assumedUnmeasuredUsd += child.amountUsd;
    }
  }

  private pendingFinalization(runId: string): FinalizationIntentData | null {
    const intent = readFinalization(this.store, runId);
    if (intent === null || intentComplete(intent)) return null;
    return intent;
  }

  /** Finish a crashed run's terminal writes exactly once (no replay). */
  private finishFinalization(intent: FinalizationIntentData): TaskRunData {
    const runId = intent.runId;
    this.acquireOwnership(runId, "finish-finalization");
    const state = this.loadState(runId);
    this.journalEvent(state, "audit-note", {
      action: "finalization-recovered",
      pendingWrites: pendingWrites(intent),
    });
    return this.applyFinalizationWrites(state, intent);
  }

  private doPause(state: RunState): TaskRunData {
    this.consumeRequest(KIND_PAUSE_REQUEST, state.run.runId);
    const clock = this.store.load(KIND_RUN_CLOCK, state.run.runId);
    if (clock !== null) {
      const now = nowEpoch();
      clock["pausedAtEpochS"] = now;
      clock["pauseExpiresAtEpochS"] = now + this.policy.pausedRunExpiryS;
      this.saveClock(clock);
    }
    this.transition(state, "paused");
    this.journalEvent(state, "run-paused", {
      action: "run-paused",
      pauseExpiresAfterS: this.policy.pausedRunExpiryS,
    });
    this.checkpoint(state);
    this.releaseOwnership(state.run.runId);
    return state.run;
  }

  private buildPackage(
    state: RunState,
    stop: Stop,
    evaluation: ConditionEvaluation,
  ): ResultPackageShape {
    const checks = checksMap(evaluation);
    const doneItems: string[] = evaluation.results.filter((r) => r.passed).map((r) =>
      `condition ${r.key} passed (${r.detail})`
    );
    doneItems.push(
      ...state.steps
        .filter((s) => s.status === STEP_STATUS_OK && s.kind !== "finalize")
        .map((s) => `${s.kind} step ${s.stepId} completed`),
    );
    const notDone: string[] = evaluation.results.filter((r) => !r.passed).map((r) =>
      `condition ${r.key} NOT met: ${r.detail}`
    );
    const uncertainties: string[] = [];
    const externalActions: Record<string, string> = {};
    for (const marker of humanMarkers(evaluation)) {
      externalActions[`human-acceptance:${marker.key}`] = "pending";
      notDone.push(`condition ${marker.key}: delivered pending human acceptance`);
    }
    if (state.unmeasuredCost) {
      uncertainties.push(
        "at least one model call has unmeasured cost; total_cost_usd is unknown and the budget " +
          "was settled conservatively",
      );
    }
    const unknownSteps = state.steps.filter((s) => s.sideEffectUnknown).map((s) => s.stepId);
    if (unknownSteps.length > 0) {
      uncertainties.push(
        `step outcome(s) unknown pending reconciliation: ${JSON.stringify(unknownSteps)}`,
      );
    }
    const reasonNotes: Record<StopReason, string> = {
      "conditions-met": "",
      "pending-acceptance":
        "machine-checkable conditions met; delivery is incomplete until a human accepts the result",
      "max-steps":
        `stopped at the step bound (${state.maxSteps} work steps) before completion conditions were met`,
      "wall-clock":
        `stopped at the wall-clock bound (${state.wallClockS}s) before completion conditions were met`,
      "budget-exhausted": `stopped: budget exhausted ($${
        state.budgetUsd.toFixed(6)
      } reserved) before completion conditions were met`,
      "model-failed": `model step failed: ${stop.error ?? ""}`,
      "prepare-failed": `preparation failed: ${stop.error ?? ""}`,
      "no-success-criteria":
        "spec declares no success criteria; completion is not machine-checkable, so the run is not completed",
      "cancelled": "cancelled by request; partial steps kept",
      "needs-reconciliation": `stopped with unknown side-effect outcome (${stop.error ?? ""}); ` +
        "reconciliation required and the unknown step will not be replayed",
      "operation-incomplete":
        "the deterministic operation completed but its result did not satisfy the run's completion conditions",
    };
    const note = reasonNotes[stop.reason];
    if (note) notDone.push(note);
    notDone.push(...state.nativeNotDone);
    const checkCount = Object.keys(checks).length;
    const conclusions: Partial<Record<StopReason, string>> = {
      "conditions-met":
        `completed: ${checkCount}/${checkCount} conditions checked true by trusted code`,
      "pending-acceptance":
        "work complete pending human acceptance; delivery is incomplete-but-delivered until accepted",
      "cancelled": "cancelled before completion; partial steps kept",
      "needs-reconciliation":
        "stopped: side-effect outcome unknown; a reconciliation note is required before any replay",
    };
    const conclusion = conclusions[stop.reason] ??
      `stopped without meeting completion conditions: ${stop.reason}`;
    return {
      schemaVersion: "1",
      runId: state.run.runId,
      conclusion,
      artifactRefs: [...state.artifactKeys.values()],
      doneItems,
      notDoneItems: notDone,
      uncertainties,
      externalActions,
      totalCostUsd: state.unmeasuredCost ? null : round6(state.measuredUsd),
      completedConditionsCheck: checks,
      createdAt: utcNowIso(),
    };
  }

  // --- step persistence helpers ------------------------------------------------------

  private beginStep(
    state: RunState,
    kind: RunStepData["kind"],
    inputDigest: string | null,
    sliceReservationId?: string,
  ): RunStepData {
    const step: RunStepData = {
      ...newStep(kind, sliceReservationId ? { sliceReservationId } : {}),
      inputDigest,
    };
    state.steps.push(step);
    state.run = withStep(state.run, step);
    this.persistRun(state);
    this.checkpoint(state);
    return step;
  }

  private endStep(
    state: RunState,
    step: RunStepData,
    status: string,
    options: {
      error?: string | null;
      outputDigest?: string | null;
      usage?: Record<string, unknown>;
      sideEffectUnknown?: boolean;
      invocation?: Record<string, unknown>;
    } = {},
  ): void {
    const index = state.steps.findIndex((s) => s.stepId === step.stepId);
    const mergedUsage = { ...state.steps[index].usage, ...(options.usage ?? {}) };
    state.steps[index] = {
      ...state.steps[index],
      status,
      endedAt: utcNowIso(),
      outputDigest: options.outputDigest ?? null,
      usage: mergedUsage,
      error: options.error ?? state.steps[index].error,
      sideEffectUnknown: options.sideEffectUnknown ?? false,
    };
    state.run = { ...state.run, steps: [...state.steps], updatedAt: utcNowIso() };
    this.persistRun(state);
    if (options.invocation !== undefined) {
      this.store.save(
        KIND_INVOCATION,
        String(options.invocation["invocationId"]),
        options.invocation,
      );
    }
    this.checkpoint(state);
  }

  private settleStepSlice(
    state: RunState,
    sliceData: BudgetReservationData | Record<string, unknown>,
    actualUsd: number,
    options: {
      unmeasurable: boolean;
      detail: string;
      assumedUsd?: number;
      childlessAssumed?: number;
      childlessMeasured?: number;
    },
  ): void {
    const reservation = reservationFromDict(sliceData as Record<string, unknown>);
    const record: Record<string, unknown> = {
      ...reservation,
      status: "settled",
      settledAmountUsd: round6(actualUsd),
      closedAt: utcNowIso(),
      unmeasurable: options.unmeasurable,
      assumedUsd: round6(options.assumedUsd ?? (options.unmeasurable ? actualUsd : 0)),
      childlessAssumedUsd: round6(options.childlessAssumed ?? 0),
      childlessMeasuredUsd: round6(options.childlessMeasured ?? 0),
    };
    this.store.save(KIND_BUDGET_SLICE, reservation.holder, record);
    this.journalEvent(state, "budget-settled", {
      level: "step",
      holder: reservation.holder,
      reservationId: reservation.reservationId,
      reservedUsd: reservation.amountUsd,
      actualUsd: record["settledAmountUsd"],
      unmeasurable: options.unmeasurable,
    });
  }

  private appendCost(
    state: RunState,
    amountUsd: number | null,
    measurable: boolean,
    note: string,
  ): void {
    this.journal.appendCost(
      costEntry(state.run.runId, {
        category: "model",
        amountUsd,
        measurable,
        mode: state.run.mode,
        note,
      }),
    );
  }

  /** Write the run snapshot behind revision fencing. */
  private persistRun(state: RunState): void {
    this.store.transaction(() => {
      const current = this.store.load(KIND_TASK_RUN, state.run.runId);
      if (current !== null && Number(current["revision"] ?? 0) !== state.revision) {
        throw new RunOwnershipError(
          `run ${state.run.runId} was written by another supervisor (persisted revision ` +
            `${current["revision"]}, this snapshot ${state.revision}); refusing to overwrite it ` +
            `with a stale snapshot`,
        );
      }
      state.revision += 1;
      this.store.save(KIND_TASK_RUN, state.run.runId, {
        ...state.run,
        revision: state.revision,
      });
    });
  }

  private transition(
    state: RunState,
    target: TaskStatusValue,
    options: { error?: string | null; resultDigest?: string | null } = {},
  ): void {
    const next = withTransition(state.run, target, options.error);
    state.run = {
      ...next,
      resultDigest: options.resultDigest ?? next.resultDigest,
    };
    this.persistRun(state);
  }

  private journalEvent(state: RunState, kind: EventKindValue, data: Record<string, unknown>): void {
    this.journal.append(
      eventRecord(kind, state.run.runId, data, { actor: "supervisor", mode: state.run.mode }),
    );
  }

  private checkpoint(state: RunState): void {
    state.sequence += 1;
    writeCheckpoint(this.store, this.journal, state.run, {
      sequence: state.sequence,
      budget: {
        reservationId: state.reservation.reservationId,
        reservedUsd: state.reservation.amountUsd,
        measuredUsd: round6(state.measuredUsd),
        assumedUnmeasuredUsd: round6(state.assumedUnmeasuredUsd),
        unmeasurable: state.unmeasuredCost,
      },
    });
    try {
      this.heartbeatOwnership(state.run.runId);
    } catch {
      // best effort; fenced writes surface staleness at persistRun
    }
  }

  private saveRootInvocation(state: RunState, status: string, resultDigest?: string): void {
    const data = this.store.load(KIND_INVOCATION, state.rootInvocationId);
    if (data === null) {
      throw new ContractError(
        `root invocation ${state.rootInvocationId} missing for run ${state.run.runId}`,
      );
    }
    const finished = status === "completed" || status === "failed" || status === "cancelled";
    this.store.save(KIND_INVOCATION, state.rootInvocationId, {
      ...data,
      status,
      startedAt: data["startedAt"] ?? utcNowIso(),
      endedAt: finished ? utcNowIso() : data["endedAt"],
      resultDigest: resultDigest ?? data["resultDigest"],
    });
  }

  // --- evaluation + helpers -----------------------------------------------------------

  private evaluate(state: RunState): ConditionEvaluation {
    return evaluateConditions(state.conditions, {
      artifacts: state.artifactKeys,
      loadArtifact: (digest) => this.artifacts.get(digest),
      measuredCostUsd: state.measuredUsd,
      hasUnmeasuredCost: state.unmeasuredCost,
      costBoundUsd: state.spec.maxCostUsd,
    });
  }

  private modelOutputSchema(state: RunState): Record<string, unknown> {
    for (const condition of state.conditions) {
      if (condition.type === CONDITION_ARTIFACT_SCHEMA && condition.artifact === "final") {
        return condition.schema ?? {};
      }
    }
    return {};
  }

  private instruction(state: RunState, modelStep: number): string {
    return canonicalJson({
      modelStep,
      goal: state.spec.goal,
      mode: state.spec.mode,
      locale: state.spec.locale,
      contextArtifact: state.artifactKeys.get("inputs") ?? null,
      materialsInScope: Object.keys(this.materials(state)).sort(),
      priorArtifacts: [...state.artifactKeys.values()],
      priorArtifactsInScope: Object.keys(this.priorArtifacts(state)).sort(),
      remainingBudgetUsd: round6(state.budgetUsd - state.conservativeSpentUsd()),
      note: "produce the next output artifact; completion is judged only by the trusted " +
        "success_criteria, not by any claim of being done",
    });
  }

  /** Verified task materials for the model step's scope (digest re-verified). */
  private materials(state: RunState): Record<string, unknown> {
    const digest = state.artifactKeys.get("inputs");
    if (digest === undefined) return {};
    const payload = this.artifacts.get(digest);
    const text = new TextDecoder("utf-8", { fatal: false }).decode(payload);
    try {
      const parsed = JSON.parse(text);
      if (isPlainObject(parsed)) return parsed;
      return { value: parsed };
    } catch {
      return { text };
    }
  }

  /** Earlier model steps' verified artifacts as scoped context. */
  private priorArtifacts(state: RunState): Record<string, unknown> {
    const prior: Record<string, unknown> = {};
    for (let ordinal = 1; ordinal < state.modelOrdinal; ordinal++) {
      const digest = state.artifactKeys.get(`step:${ordinal}`);
      if (digest === undefined) continue;
      const payload = this.artifacts.get(digest);
      const text = new TextDecoder("utf-8", { fatal: false }).decode(payload);
      try {
        prior[`step-${ordinal}`] = JSON.parse(text);
      } catch {
        prior[`step-${ordinal}`] = text;
      }
    }
    return prior;
  }

  private sessionConfig(state: RunState, deadline: number): WorkerSessionConfig {
    const remainingSteps = Math.max(1, state.maxSteps - this.consumedWorkSteps(state));
    return {
      mode: state.spec.mode,
      maxSteps: remainingSteps,
      wallClockS: Math.max(0.1, deadline - nowEpoch()),
      maxCostUsd: round6(Math.max(0, state.budgetUsd - state.conservativeSpentUsd())),
    };
  }

  private hasOkStep(state: RunState, kind: RunStepData["kind"]): boolean {
    return state.steps.some((s) => s.kind === kind && s.status === STEP_STATUS_OK);
  }

  private consumedWorkSteps(state: RunState): number {
    return state.steps.filter(
      (s) => SIDE_EFFECTING_STEP_KINDS.has(s.kind) && CONSUMED_WORK_STATUSES.has(s.status),
    ).length;
  }

  // --- state load/rebuild -------------------------------------------------------------

  loadState(runId: string): RunState {
    const runData = this.store.load(KIND_TASK_RUN, runId);
    if (runData === null) throw new ContractError(`unknown run ${JSON.stringify(runId)}`);
    const run = runFromDict(runData);
    const index = this.store.load(KIND_RUN_INDEX, runId);
    if (index === null) throw new ContractError(`run ${runId} has no run-index record`);
    const specData = this.store.load(KIND_TASK_SPEC, String(index["specId"]));
    if (specData === null) {
      throw new ContractError(
        `task spec ${JSON.stringify(index["specId"])} missing for run ${runId}`,
      );
    }
    const spec = specFromDict(specData);
    if (digestOf(spec) !== run.taskDigest) {
      throw new DigestMismatchError(
        `task spec digest ${digestOf(spec)} does not match run digest ${run.taskDigest}`,
      );
    }
    const reservationData = this.store.load(KIND_RUN_RESERVATION, runId);
    if (reservationData === null) {
      throw new ContractError(`run ${runId} has no run-level budget reservation`);
    }
    const reservation = reservationFromDict(reservationData);
    if (reservation.reservationId !== index["reservationId"]) {
      throw new ContractError(`run ${runId} reservation id mismatch`);
    }

    const state: RunState = {
      ...newStateBase(),
      run,
      spec,
      conditions: normalizeCriteria(spec.successCriteria),
      reservation,
      rootInvocationId: String(index["rootInvocationId"]),
      policyDigestValue: String(index["policyDigest"]),
      steps: [...run.steps],
      budgetUsd: reservation.amountUsd,
      maxSteps: spec.maxSteps ?? this.policy.defaultMaxSteps,
      wallClockS: spec.maxWallClockS ?? this.policy.defaultWallClockS,
      revision: Number(runData["revision"] ?? 0),
      conservativeSpentUsd() {
        return this.measuredUsd + this.assumedUnmeasuredUsd;
      },
      overrunUsd() {
        return this.conservativeSpentUsd() - this.budgetUsd;
      },
    };
    this.rebuildAccounting(state);
    this.rebuildOverruns(state);
    const record = this.store.load(CHECKPOINT_KIND, runId);
    state.sequence = record !== null ? Number(record["sequence"] ?? 0) : 0;
    return state;
  }

  private rebuildOverruns(state: RunState): void {
    for (const event of this.journal.events(state.run.runId)) {
      if (event.kind !== "audit-note") continue;
      const data = event.data ?? {};
      if (data["action"] !== "settlement-refused-overrun") continue;
      const reservationId = data["reservationId"];
      if (typeof reservationId === "string") {
        state.overrunReservations.set(reservationId, Number(data["reportedUsd"] ?? 0));
      }
    }
  }

  private rebuildAccounting(state: RunState): void {
    const entries = this.journal.costEntries(state.run.runId);
    state.measuredUsd = entries.reduce((sum, e) => sum + (e.amountUsd ?? 0), 0);
    state.unmeasuredCost = entries.some((e) => !e.measurable);
    const prefix = `${state.run.runId}#`;
    let assumed = 0;
    let childlessAssumed = 0;
    let childlessMeasured = 0;
    for (const recordId of this.store.listIds(KIND_BUDGET_SLICE)) {
      if (!recordId.startsWith(prefix)) continue;
      const sliceRecord = this.store.load(KIND_BUDGET_SLICE, recordId);
      if (sliceRecord === null) continue;
      const fallback = sliceRecord["unmeasurable"] ? Number(sliceRecord["amountUsd"] ?? 0) : 0;
      const sliceAssumed = Number(sliceRecord["assumedUsd"] ?? fallback);
      assumed += sliceAssumed;
      childlessAssumed += Number(sliceRecord["childlessAssumedUsd"] ?? sliceAssumed);
      childlessMeasured += Number(sliceRecord["childlessMeasuredUsd"] ?? 0);
    }
    state.assumedUnmeasuredUsd = assumed;
    state.childlessAssumedUsd = childlessAssumed;
    state.childlessMeasuredUsd = childlessMeasured;
    let ordinal = 0;
    for (const step of state.steps) {
      if (step.kind === "tool-call" && step.status === STEP_STATUS_OK) {
        if ("operation" in step.usage) {
          if (step.outputDigest !== null) {
            state.artifactKeys.set("final", step.outputDigest);
            const opKey = `operation:${String(step.usage["operation"])}`;
            if (!state.artifactKeys.has(opKey)) state.artifactKeys.set(opKey, step.outputDigest);
          }
        } else if (step.outputDigest !== null) {
          state.artifactKeys.set("inputs", step.outputDigest);
        }
      } else if (step.kind === "model-call") {
        ordinal += 1;
        if (step.status === STEP_STATUS_OK && step.outputDigest !== null) {
          state.artifactKeys.set(`step:${ordinal}`, step.outputDigest);
          state.artifactKeys.set("final", step.outputDigest);
          if (ordinal === 1) state.artifactKeys.set("draft", step.outputDigest);
        }
      }
    }
    state.modelOrdinal = ordinal + 1;
  }
}

// --- helpers -------------------------------------------------------------------------

export interface ResultPackageShape {
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

export function packageDeliverable(pkg: ResultPackageShape): boolean {
  const checks = pkg.completedConditionsCheck;
  const values = Object.values(checks);
  return (
    values.length > 0 &&
    values.every((v) => v) &&
    !Object.values(pkg.externalActions).some((s) => s === "pending")
  );
}

export function intentError(intent: FinalizationIntentData): string | null {
  const pkg = intent.package;
  const notDone = pkg["notDoneItems"];
  if (Array.isArray(notDone) && notDone.length > 0) return String(notDone[notDone.length - 1]);
  return null;
}

function transitionsOf(status: TaskStatusValue): readonly TaskStatusValue[] {
  const table: Record<TaskStatusValue, readonly TaskStatusValue[]> = {
    "queued": ["running", "cancelled"],
    "running": ["paused", "completed", "failed", "cancelled", "needs-reconciliation"],
    "paused": ["running", "cancelled", "needs-reconciliation"],
    "needs-reconciliation": ["running", "failed", "cancelled"],
    "completed": [],
    "failed": [],
    "cancelled": [],
  };
  return table[status];
}

function validateSpecBounds(spec: TaskSpecData): void {
  if (spec.maxCostUsd !== null && spec.maxCostUsd < 0) {
    throw new ContractError("TaskSpec.maxCostUsd must be >= 0");
  }
  if (spec.maxWallClockS !== null && spec.maxWallClockS <= 0) {
    throw new ContractError("TaskSpec.maxWallClockS must be > 0");
  }
  if (spec.maxSteps !== null && spec.maxSteps < 0) {
    throw new ContractError("TaskSpec.maxSteps must be >= 0");
  }
}

function specDigestOf(spec: TaskSpecData): string {
  return digestOf(spec);
}

/** (measured cost, unmeasured flag) from a session usage snapshot. */
function usageCost(usage: Record<string, unknown> | null): [number | null, boolean] {
  if (!isPlainObject(usage)) return [null, true];
  if ("cost_usd" in usage) {
    const cost = usage["cost_usd"];
    const unmeasured = Number(usage["unmeasured_calls"] ?? 0) > 0 || cost === null ||
      cost === undefined;
    return [cost === null || cost === undefined ? null : Number(cost), unmeasured];
  }
  if ("costUsd" in usage) {
    const cost = usage["costUsd"];
    return [
      cost === null || cost === undefined ? null : Number(cost),
      cost === null || cost === undefined,
    ];
  }
  return [null, true];
}

function round6(value: number): number {
  return Math.round((value + Number.EPSILON * Math.sign(value)) * 1e6) / 1e6;
}

function round3(value: number): number {
  return Math.round((value + Number.EPSILON * Math.sign(value)) * 1e3) / 1e3;
}

function errorName(exc: unknown): string {
  if (exc instanceof Error) return exc.name;
  return typeof exc;
}

export { finalizationFromDict, recover };
