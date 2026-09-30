/**
 * Shared contract primitives: run modes, roles, ids, timestamps, digests.
 * Mirrors the historical Python reference (schema v1 records).
 */

import { canonicalJson, DIGEST_PREFIX, digestOf } from "./canonical.ts";

export { canonicalJson, DIGEST_PREFIX, digestOf };

export const TOOL_ID = "vowdo-agent-ts";

export class VowdoError extends Error {
  readonly code: string;
  constructor(message: string, code = "vowdo/error") {
    super(message);
    this.name = new.target.name;
    this.code = code;
  }
}

export class ContractError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/contract");
  }
}

export class DigestMismatchError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/digest-mismatch");
  }
}

export class UnknownVersionError extends ContractError {
  constructor(message: string) {
    super(message);
    this.name = "UnknownVersionError";
  }
}

export class BudgetError extends VowdoError {
  constructor(message: string, code = "vowdo/budget") {
    super(message, code);
  }
}

export class BudgetExhaustedError extends BudgetError {
  constructor(message: string) {
    super(message, "vowdo/budget-exhausted");
  }
}

export class ReservationError extends BudgetError {
  constructor(message: string) {
    super(message, "vowdo/budget-reservation");
  }
}

export class UnmeasurableCostError extends BudgetError {
  constructor(message: string) {
    super(message, "vowdo/budget-unmeasurable");
  }
}

export class GateDeniedError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/gate-denied");
  }
}

export class SplitAccessError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/split-access");
  }
}

export class LiveCallBlockedError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/live-call-blocked");
  }
}

export class ReplayExhaustedError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/replay-exhausted");
  }
}

export class SideEffectBlockedError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/side-effect-blocked");
  }
}

export class UnsupportedIsolationError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/unsupported-isolation");
  }
}

export class ReconciliationRequiredError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/reconciliation-required");
  }
}

export class InvalidStateTransitionError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/invalid-transition");
  }
}

export class ApprovalInvalidatedError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/approval-invalidated");
  }
}

export class ProtocolFrameError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/protocol-frame");
  }
}

export class AdapterExecutionError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/adapter-execution");
  }
}

export class MissingMeteringError extends VowdoError {
  constructor(message: string) {
    super(message, "vowdo/missing-metering");
  }
}

/** A failed runtime step that kept its metering (usage that survived). */
export class StepFailureError extends VowdoError {
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
