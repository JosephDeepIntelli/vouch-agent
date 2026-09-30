/**
 * Durable control-path records: run ownership lease, finalization intent,
 * run clock. Terminal status alone never proves finalization completed.
 */

import { checkVersion, ContractError, utcNowIso, VowdoError } from "../contracts/common.ts";
import type { TaskRunData } from "../contracts/tasks.ts";
import type { MetadataStore } from "../storage/store.ts";

export const KIND_RUN_OWNERSHIP = "run-ownership";
export const KIND_RUN_FINALIZATION = "run-finalization";
export const KIND_RUN_CLOCK = "run-clock";

export const FINALIZATION_WRITES = [
  "packageSaved",
  "ledgerSettled",
  "metadataFinalized",
  "ownershipReleased",
] as const;
export type FinalizationWrite = (typeof FINALIZATION_WRITES)[number];

export class RunOwnershipError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/run-ownership");
  }
}

export interface OwnershipRecord {
  schemaVersion: string;
  runId: string;
  ownerId: string;
  acquiredAt: string;
  leaseExpiresAtEpochS: number;
  heartbeatAt: string;
  revision: number;
  releasedAt: string | null;
}

export function ownershipFromDict(data: Record<string, unknown>): OwnershipRecord {
  return {
    schemaVersion: "1",
    runId: String(data["runId"]),
    ownerId: String(data["ownerId"]),
    acquiredAt: String(data["acquiredAt"] ?? ""),
    leaseExpiresAtEpochS: Number(data["leaseExpiresAtEpochS"] ?? 0),
    heartbeatAt: String(data["heartbeatAt"] ?? ""),
    revision: Math.trunc(Number(data["revision"] ?? 0)),
    releasedAt: (data["releasedAt"] as string | null) ?? null,
  };
}

export function leaseLive(record: OwnershipRecord, nowEpochS: number): boolean {
  return record.releasedAt === null && record.leaseExpiresAtEpochS > nowEpochS;
}

export function readOwnership(store: MetadataStore, runId: string): OwnershipRecord | null {
  const data = store.load(KIND_RUN_OWNERSHIP, runId);
  return data === null ? null : ownershipFromDict(data);
}

/** Write-ahead intent for one run's terminal writes; each completed write flips a flag. */
export interface FinalizationIntentData {
  schemaVersion: string;
  runId: string;
  targetStatus: string;
  resultDigest: string | null;
  package: Record<string, unknown>;
  conservativeUsd: number;
  childlessOverageUsd: number;
  reservationId: string | null;
  ledgerAction: "close" | "keep-open";
  unmeasured: boolean;
  packageSaved: boolean;
  ledgerSettled: boolean;
  metadataFinalized: boolean;
  ownershipReleased: boolean;
  createdAt: string;
  updatedAt: string;
}

export function finalizationFromDict(data: Record<string, unknown>): FinalizationIntentData {
  const pkg = data["package"];
  return {
    schemaVersion: String(data["schemaVersion"] ?? "1"),
    runId: String(data["runId"]),
    targetStatus: String(data["targetStatus"]),
    resultDigest: (data["resultDigest"] as string | null) ?? null,
    package: typeof pkg === "object" && pkg !== null && !Array.isArray(pkg)
      ? { ...(pkg as Record<string, unknown>) }
      : {},
    conservativeUsd: Number(data["conservativeUsd"] ?? 0),
    childlessOverageUsd: Number(data["childlessOverageUsd"] ?? 0),
    reservationId: (data["reservationId"] as string | null) ?? null,
    ledgerAction: String(data["ledgerAction"] ?? "close") as "close" | "keep-open",
    unmeasured: Boolean(data["unmeasured"] ?? false),
    packageSaved: Boolean(data["packageSaved"] ?? false),
    ledgerSettled: Boolean(data["ledgerSettled"] ?? false),
    metadataFinalized: Boolean(data["metadataFinalized"] ?? false),
    ownershipReleased: Boolean(data["ownershipReleased"] ?? false),
    createdAt: String(data["createdAt"] ?? ""),
    updatedAt: String(data["updatedAt"] ?? ""),
  };
}

export function pendingWrites(intent: FinalizationIntentData): FinalizationWrite[] {
  const flags: Record<FinalizationWrite, boolean> = {
    packageSaved: intent.packageSaved,
    ledgerSettled: intent.ledgerSettled,
    metadataFinalized: intent.metadataFinalized,
    ownershipReleased: intent.ownershipReleased,
  };
  return FINALIZATION_WRITES.filter((name) => !flags[name]);
}

export function intentComplete(intent: FinalizationIntentData): boolean {
  return pendingWrites(intent).length === 0;
}

export function intentWithFlag(
  intent: FinalizationIntentData,
  name: FinalizationWrite,
): FinalizationIntentData {
  return {
    ...intent,
    [name]: true,
    updatedAt: utcNowIso(),
  } as FinalizationIntentData;
}

export function readFinalization(
  store: MetadataStore,
  runId: string,
): FinalizationIntentData | null {
  const data = store.load(KIND_RUN_FINALIZATION, runId);
  return data === null ? null : finalizationFromDict(data);
}

export function readClock(store: MetadataStore, runId: string): Record<string, unknown> | null {
  return store.load(KIND_RUN_CLOCK, runId);
}

/** Outcome of one trusted deterministic operation. */
export interface NativeOperationResult {
  payload: Uint8Array;
  artifacts: Record<string, string>;
  usage: Record<string, unknown>;
  notDone: string[];
}

export function checkClockVersion(clock: Record<string, unknown>): void {
  checkVersion(clock);
  if (typeof clock["runId"] !== "string") {
    throw new ContractError("run-clock record carries no runId");
  }
}

export function clockOf(run: TaskRunData): string {
  return run.runId;
}
