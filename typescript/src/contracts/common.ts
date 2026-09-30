/**
 * Shared contract primitives: run modes, roles, ids, timestamps, digests.
 * Mirrors `vouch_agent/contracts/common.py` (schema v1 records).
 */

import { canonicalJson, DIGEST_PREFIX, digestOf } from "./canonical.ts";

export { canonicalJson, DIGEST_PREFIX, digestOf };

export const TOOL_ID = "vouch-agent-ts";

export class VouchError extends Error {
  readonly code: string;
  constructor(message: string, code = "vouch/error") {
    super(message);
    this.name = new.target.name;
    this.code = code;
  }
}

export class ContractError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/contract");
  }
}

export class DigestMismatchError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/digest-mismatch");
  }
}

export class UnknownVersionError extends ContractError {
  constructor(message: string) {
    super(message);
    this.name = "UnknownVersionError";
  }
}

export class BudgetError extends VouchError {
  constructor(message: string, code = "vouch/budget") {
    super(message, code);
  }
}

export class BudgetExhaustedError extends BudgetError {
  constructor(message: string) {
    super(message, "vouch/budget-exhausted");
  }
}

export class ReservationError extends BudgetError {
  constructor(message: string) {
    super(message, "vouch/budget-reservation");
  }
}

export class UnmeasurableCostError extends BudgetError {
  constructor(message: string) {
    super(message, "vouch/budget-unmeasurable");
  }
}

export class GateDeniedError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/gate-denied");
  }
}

export class SplitAccessError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/split-access");
  }
}

export class LiveCallBlockedError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/live-call-blocked");
  }
}

export class ReplayExhaustedError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/replay-exhausted");
  }
}

export class SideEffectBlockedError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/side-effect-blocked");
  }
}

export class UnsupportedIsolationError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/unsupported-isolation");
  }
}

export class ReconciliationRequiredError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/reconciliation-required");
  }
}

export class InvalidStateTransitionError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/invalid-transition");
  }
}

export class ApprovalInvalidatedError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/approval-invalidated");
  }
}

export class ProtocolFrameError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/protocol-frame");
  }
}

export class AdapterExecutionError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/adapter-execution");
  }
}

export class MissingMeteringError extends VouchError {
  constructor(message: string) {
    super(message, "vouch/missing-metering");
  }
}

/** A failed runtime step that kept its metering (usage that survived). */
export class StepFailureError extends VouchError {
  readonly usage: Record<string, unknown> | null;
  constructor(code: string, message: string, usage: Record<string, unknown> | null = null) {
    super(message, code);
    this.name = "StepFailureError";
    this.usage = usage;
  }
}

/**
 * UTC now as ISO-8601 with millisecond precision, in the same shape Python's
 * `datetime.now(UTC).isoformat(timespec="milliseconds")` produces:
 * `2026-09-30T12:34:56.789+00:00`. Only this timestamp form appears in records.
 */
export function utcNowIso(): string {
  return new Date().toISOString().replace(/Z$/, "+00:00");
}

/** New opaque id like ``cand_ab12...`` — ids are references, never secrets. */
export function newId(prefix: string): string {
  const uuid = crypto.randomUUID().replace(/-/g, "").slice(0, 16);
  const token = [...crypto.getRandomValues(new Uint8Array(4))]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `${prefix}_${uuid}${token}`;
}

export type RunModeValue = "fixture" | "offline-evaluation" | "authorized-live";

export class RunMode {
  static readonly FIXTURE = new RunMode("fixture");
  static readonly OFFLINE_EVALUATION = new RunMode("offline-evaluation");
  static readonly AUTHORIZED_LIVE = new RunMode("authorized-live");
  static readonly ALL = [RunMode.FIXTURE, RunMode.OFFLINE_EVALUATION, RunMode.AUTHORIZED_LIVE];

  private constructor(readonly value: RunModeValue) {}

  static from(value: string): RunMode {
    const found = RunMode.ALL.find((m) => m.value === value);
    if (!found) throw new ContractError(`unknown run mode ${JSON.stringify(value)}`);
    return found;
  }

  allowsLiveCalls(): boolean {
    return this === RunMode.AUTHORIZED_LIVE;
  }

  allowsSideEffects(): boolean {
    return this === RunMode.AUTHORIZED_LIVE;
  }
}

export const ROLES = [
  "engineer",
  "proposer",
  "evaluator",
  "acceptance-owner",
  "release-owner",
  "budget-owner",
] as const;

export type Role = (typeof ROLES)[number];

export function requireStr(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ContractError(
      `field ${JSON.stringify(field)} must be a non-empty string, got ${JSON.stringify(value)}`,
    );
  }
  return value;
}

export function requireDigest(value: unknown, field: string): string {
  if (
    typeof value !== "string" || !value.startsWith(DIGEST_PREFIX) ||
    value.length !== 71
  ) {
    throw new ContractError(
      `field ${JSON.stringify(field)} must be a sha256 digest string, got ${JSON.stringify(value)}`,
    );
  }
  return value;
}

export function checkVersion(data: Record<string, unknown>, expected = "1"): void {
  const version = data["schemaVersion"];
  if (version !== expected) {
    throw new UnknownVersionError(
      `unsupported schemaVersion ${JSON.stringify(version)} (expected ${JSON.stringify(expected)})`,
    );
  }
}
