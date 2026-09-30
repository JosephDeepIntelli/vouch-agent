/** Event & cost journal contracts + budget reservations (schema v1). */

import {
  checkVersion,
  ContractError,
  newId,
  requireStr,
  RunMode,
  type RunModeValue,
  utcNowIso,
} from "./common.ts";

export const EVENT_KINDS = [
  "run-started",
  "run-completed",
  "run-failed",
  "run-cancelled",
  "run-paused",
  "run-reconciliation",
  "attempt-started",
  "attempt-ended",
  "gate-decision",
  "gate-denied",
  "budget-reserved",
  "budget-settled",
  "budget-released",
  "budget-exhausted",
  "candidate-state",
  "approval-recorded",
  "approval-invalidated",
  "release-recorded",
  "skill-state",
  "recovery-point",
  "adapter-frame",
  "audit-note",
] as const;

export type EventKindValue = (typeof EVENT_KINDS)[number];

export interface EventRecordData {
  schemaVersion: string;
  eventId: string;
  kind: EventKindValue;
  occurredAt: string;
  actor: string;
  subject: string;
  data: Record<string, unknown>;
  mode: RunModeValue;
}

export interface CostEntryData {
  schemaVersion: string;
  entryId: string;
  category: CostCategoryValue;
  subject: string;
  amountUsd: number | null;
  humanMinutes: number | null;
  measurable: boolean;
  mode: RunModeValue;
  recordedAt: string;
  note: string;
}

export const COST_CATEGORIES = [
  "model",
  "tool",
  "search",
  "evaluation",
  "human-review",
  "infrastructure",
  "retry",
] as const;

export type CostCategoryValue = (typeof COST_CATEGORIES)[number];

export interface BudgetReservationData {
  schemaVersion: string;
  reservationId: string;
  holder: string;
  amountUsd: number;
  status: ReservationStatusValue;
  settledAmountUsd: number | null;
  createdAt: string;
  closedAt: string | null;
  parentReservationId: string | null;
}

export const RESERVATION_STATUSES = ["open", "settled", "released"] as const;
export type ReservationStatusValue = (typeof RESERVATION_STATUSES)[number];

export function eventRecord(
  kind: EventKindValue,
  subject: string,
  data: Record<string, unknown>,
  options: { actor?: string; mode?: RunModeValue } = {},
): EventRecordData {
  return {
    schemaVersion: "1",
    eventId: newId("evt"),
    kind,
    occurredAt: utcNowIso(),
    actor: options.actor ?? "controller",
    subject,
    data,
    mode: options.mode ?? "fixture",
  };
}

export function eventFromDict(data: Record<string, unknown>): EventRecordData {
  checkVersion(data);
  const kind = requireStr(data["kind"], "kind") as EventKindValue;
  if (!EVENT_KINDS.includes(kind)) {
    throw new ContractError(`unknown event kind ${JSON.stringify(kind)}`);
  }
  return {
    schemaVersion: "1",
    eventId: requireStr(data["eventId"], "eventId"),
    kind,
    occurredAt: requireStr(data["occurredAt"] ?? utcNowIso(), "occurredAt"),
    actor: requireStr(data["actor"] ?? "controller", "actor"),
    subject: String(data["subject"] ?? ""),
    data: asObject(data["data"], "data"),
    mode: (data["mode"] as RunModeValue) ?? "fixture",
  };
}

export function costEntry(
  subject: string,
  options: {
    category?: CostCategoryValue;
    amountUsd?: number | null;
    humanMinutes?: number | null;
    measurable?: boolean;
    mode?: RunModeValue;
    note?: string;
  } = {},
): CostEntryData {
  const measurable = options.measurable ?? true;
  const amountUsd = options.amountUsd ?? null;
  const humanMinutes = options.humanMinutes ?? null;
  if (amountUsd === null && humanMinutes === null && measurable) {
    throw new ContractError(
      "cost entry must carry amountUsd or humanMinutes, or be marked measurable=false",
    );
  }
  if (amountUsd !== null && amountUsd < 0) {
    throw new ContractError("amountUsd must be >= 0");
  }
  return {
    schemaVersion: "1",
    entryId: newId("cost"),
    category: options.category ?? "model",
    subject,
    amountUsd,
    humanMinutes,
    measurable,
    mode: options.mode ?? "fixture",
    recordedAt: utcNowIso(),
    note: options.note ?? "",
  };
}

export function costFromDict(data: Record<string, unknown>): CostEntryData {
  checkVersion(data);
  const category = requireStr(data["category"], "category") as CostCategoryValue;
  if (!COST_CATEGORIES.includes(category)) {
    throw new ContractError(`unknown cost category ${JSON.stringify(category)}`);
  }
  return {
    schemaVersion: "1",
    entryId: requireStr(data["entryId"], "entryId"),
    category,
    subject: requireStr(data["subject"], "subject"),
    amountUsd: asNumberOrNull(data["amountUsd"]),
    humanMinutes: asNumberOrNull(data["humanMinutes"]),
    measurable: Boolean(data["measurable"] ?? true),
    mode: (data["mode"] as RunModeValue) ?? "fixture",
    recordedAt: requireStr(data["recordedAt"] ?? utcNowIso(), "recordedAt"),
    note: String(data["note"] ?? ""),
  };
}

export function reservationFromDict(data: Record<string, unknown>): BudgetReservationData {
  checkVersion(data);
  const status = (requireStr(data["status"] ?? "open", "status")) as ReservationStatusValue;
  if (!RESERVATION_STATUSES.includes(status)) {
    throw new ContractError(`unknown reservation status ${JSON.stringify(status)}`);
  }
  return {
    schemaVersion: "1",
    reservationId: requireStr(data["reservationId"], "reservationId"),
    holder: requireStr(data["holder"], "holder"),
    amountUsd: asNumber(data["amountUsd"], "amountUsd"),
    status,
    settledAmountUsd: asNumberOrNull(data["settledAmountUsd"]),
    createdAt: requireStr(data["createdAt"] ?? utcNowIso(), "createdAt"),
    closedAt: (data["closedAt"] as string | null) ?? null,
    parentReservationId: (data["parentReservationId"] as string | null) ?? null,
  };
}

function asObject(value: unknown, field: string): Record<string, unknown> {
  if (value === undefined || value === null) return {};
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new ContractError(`field ${JSON.stringify(field)} must be an object`);
  }
  return value as Record<string, unknown>;
}

function asNumber(value: unknown, field: string): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) {
    throw new ContractError(`field ${JSON.stringify(field)} must be a finite number`);
  }
  return n;
}

function asNumberOrNull(value: unknown): number | null {
  if (value === undefined || value === null) return null;
  return asNumber(value, "numeric field");
}

/** Deterministic run mode object for journal interop. */
export function modeValue(mode: RunMode): RunModeValue {
  return mode.value;
}
