/**
 * Adapter protocol v1/v1.1 — versioned JSONL frames over stdin/stdout
 * (design §4.2), ported from `vouch_agent/adapters/protocol.py`.
 *
 * Failure semantics (fail closed, never silently success-shaped): unknown
 * protocolVersion, out-of-order seq, oversized frames (checked WHILE
 * accumulating), malformed JSON, missing metering, adapter-reported error
 * frames, and wall-clock overruns (process killed) all raise typed errors.
 */

import { digestBytes, isPlainObject } from "../contracts/canonical.ts";
import {
  AdapterExecutionError,
  ContractError,
  MissingMeteringError,
  ProtocolFrameError,
} from "../contracts/common.ts";

export const PROTOCOL_VERSION = "1";
export const MAX_FRAME_BYTES = 8 * 1024 * 1024;
export const MAX_DIAGNOSTICS_CHARS = 8000;
export const MAX_DIAGNOSTICS_BYTES = 64 * 1024;
export const MAX_ARTIFACT_BYTES = 32 * 1024 * 1024;

export const FRAME_KINDS = [
  "describe-request",
  "describe-response",
  "prepare-request",
  "prepare-response",
  "execute-request",
  "execute-response",
  "apply-config-request",
  "apply-config-response",
  "collect-request",
  "collect-response",
  "cleanup-request",
  "cleanup-response",
  "error",
] as const;

export type FrameKind = (typeof FRAME_KINDS)[number];

const KINDS_REQUIRING_RUN_ID = new Set<FrameKind>([
  "prepare-request",
  "prepare-response",
  "execute-request",
  "execute-response",
  "apply-config-request",
  "apply-config-response",
  "collect-request",
  "collect-response",
  "cleanup-request",
  "cleanup-response",
]);

export const REQUEST_TO_RESPONSE: Record<string, FrameKind> = {
  "describe-request": "describe-response",
  "prepare-request": "prepare-response",
  "execute-request": "execute-response",
  "apply-config-request": "apply-config-response",
  "collect-request": "collect-response",
  "cleanup-request": "cleanup-response",
};

export interface Frame {
  seq: number;
  kind: FrameKind;
  payload: Record<string, unknown>;
  runId: string | null;
  protocolVersion: string;
}

export interface AdapterDescriptor {
  adapterId: string;
  protocolVersion: string;
  workflows: string[];
  actions: string[];
  enforcedModes: string[];
  notes: string;
}

export interface AdapterExecution {
  ok: boolean;
  outputs: Record<string, unknown>;
  evidenceRefs: string[];
  toolEvents: Array<Record<string, unknown>>;
  usage: Record<string, unknown> | null;
  error: string | null;
  mode: string;
  runnerVersion: string;
}

export function frameToJsonLine(frame: Frame): string {
  const line = JSON.stringify({
    protocolVersion: frame.protocolVersion,
    seq: frame.seq,
    kind: frame.kind,
    runId: frame.runId,
    payload: frame.payload,
  });
  checkOutboundSize(line);
  return line;
}

function checkOutboundSize(line: string): void {
  if (new TextEncoder().encode(line).length > MAX_FRAME_BYTES) {
    throw new ProtocolFrameError(
      `frame of ${
        new TextEncoder().encode(line).length
      } bytes exceeds MAX_FRAME_BYTES=${MAX_FRAME_BYTES}`,
    );
  }
}

/** Decode and structurally validate one JSONL frame. */
export function parseFrame(input: Uint8Array | string): Frame {
  let text: string;
  if (typeof input !== "string") {
    if (input.length > MAX_FRAME_BYTES) {
      throw new ProtocolFrameError(
        `inbound frame of ${input.length} bytes exceeds MAX_FRAME_BYTES=${MAX_FRAME_BYTES}`,
      );
    }
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(input);
    } catch (exc) {
      throw new ProtocolFrameError(`frame is not valid UTF-8: ${exc}`);
    }
  } else {
    text = input;
    checkOutboundSize(text);
  }
  text = text.trim();
  if (text.length === 0) throw new ProtocolFrameError("empty frame line");
  let obj: unknown;
  try {
    obj = JSON.parse(text);
  } catch (exc) {
    throw new ProtocolFrameError(`malformed JSON frame: ${exc}`);
  }
  if (!isPlainObject(obj)) throw new ProtocolFrameError("frame must be a JSON object");

  const version = obj["protocolVersion"];
  if (version !== PROTOCOL_VERSION) {
    throw new ProtocolFrameError(
      `unknown protocolVersion ${JSON.stringify(version)} (only ${
        JSON.stringify(PROTOCOL_VERSION)
      } is supported)`,
    );
  }
  const rawKind = String(obj["kind"]);
  if (!FRAME_KINDS.includes(rawKind as FrameKind)) {
    throw new ProtocolFrameError(`unknown frame kind ${JSON.stringify(rawKind)}`);
  }
  const kind = rawKind as FrameKind;
  const seq = obj["seq"];
  if (typeof seq !== "number" || !Number.isInteger(seq) || seq < 0) {
    throw new ProtocolFrameError(`seq must be a non-negative integer, got ${JSON.stringify(seq)}`);
  }
  const runId = obj["runId"];
  if (KINDS_REQUIRING_RUN_ID.has(kind)) {
    if (typeof runId !== "string" || runId.length === 0) {
      throw new ProtocolFrameError(
        `${kind} requires a non-empty runId, got ${JSON.stringify(runId)}`,
      );
    }
  } else if (kind === "error") {
    if (
      runId !== null && runId !== undefined && (typeof runId !== "string" || runId.length === 0)
    ) {
      throw new ProtocolFrameError(
        `error frame runId must be a non-empty string, got ${JSON.stringify(runId)}`,
      );
    }
  } else if (runId !== null && runId !== undefined) {
    throw new ProtocolFrameError(`${kind} must not carry a runId, got ${JSON.stringify(runId)}`);
  }
  const payload = obj["payload"];
  if (!isPlainObject(payload)) throw new ProtocolFrameError("payload must be a JSON object");
  return {
    seq,
    kind,
    payload,
    runId: runId === undefined ? null : (runId as string | null),
    protocolVersion: version,
  };
}

/** Enforces per-direction monotonic sequence numbers starting at 0. */
export class SequenceTracker {
  private expectedValue = 0;

  get expected(): number {
    return this.expectedValue;
  }

  next(): number {
    return this.expectedValue++;
  }

  observe(frame: Frame): void {
    if (frame.seq !== this.expectedValue) {
      throw new ProtocolFrameError(
        `out-of-order frame: expected seq ${this.expectedValue}, got ${frame.seq} (${frame.kind})`,
      );
    }
    this.expectedValue += 1;
  }
}

// --- payload decoders ---------------------------------------------------------

export function descriptorFromPayload(payload: Record<string, unknown>): AdapterDescriptor {
  const version = String(payload["protocolVersion"] ?? PROTOCOL_VERSION);
  if (version !== PROTOCOL_VERSION) {
    throw new ProtocolFrameError(
      `descriptor declares unknown protocolVersion ${JSON.stringify(version)}`,
    );
  }
  return {
    adapterId: requireStrField(payload, "adapterId"),
    protocolVersion: version,
    workflows: strList(payload["workflows"] ?? [], "workflows"),
    actions: strList(payload["actions"] ?? [], "actions"),
    enforcedModes: strList(payload["enforcedModes"] ?? ["fixture"], "enforcedModes"),
    notes: String(payload["notes"] ?? ""),
  };
}

export function executionFromPayload(
  payload: Record<string, unknown>,
  requireMetering = true,
): AdapterExecution {
  const ok = payload["ok"];
  if (typeof ok !== "boolean") {
    throw new ProtocolFrameError(
      `execute payload 'ok' must be a boolean, got ${JSON.stringify(ok)}`,
    );
  }
  const outputs = payload["outputs"] ?? {};
  if (!isPlainObject(outputs)) {
    throw new ProtocolFrameError("execute payload 'outputs' must be an object");
  }
  const toolEvents = payload["toolEvents"] ?? [];
  if (!Array.isArray(toolEvents) || !toolEvents.every(isPlainObject)) {
    throw new ProtocolFrameError("execute payload 'toolEvents' must be a list of objects");
  }
  const evidenceRefs: string[] = [];
  for (const ref of (payload["evidenceRefs"] ?? []) as unknown[]) {
    if (typeof ref !== "string" || !ref.startsWith("sha256:") || ref.length !== 71) {
      throw new ProtocolFrameError(
        `malformed execute payload: evidenceRefs entry ${JSON.stringify(ref)}`,
      );
    }
    evidenceRefs.push(ref);
  }
  const usage = payload["usage"];
  if (requireMetering) {
    requireUsage(usage);
  } else if (usage !== null && usage !== undefined && !isPlainObject(usage)) {
    throw new ProtocolFrameError("execute payload 'usage' must be an object when present");
  }
  const error = payload["error"];
  if (error !== null && error !== undefined && typeof error !== "string") {
    throw new ProtocolFrameError("execute payload 'error' must be a string or null");
  }
  const runnerVersion = payload["runnerVersion"] ?? "";
  if (typeof runnerVersion !== "string") {
    throw new ProtocolFrameError("execute payload 'runnerVersion' must be a string");
  }
  return {
    ok,
    outputs,
    evidenceRefs,
    toolEvents: toolEvents as Array<Record<string, unknown>>,
    usage: isPlainObject(usage) ? usage : null,
    error: (error as string | null) ?? null,
    mode: String(payload["mode"] ?? "fixture"),
    runnerVersion,
  };
}

function requireUsage(usage: unknown): void {
  if (!isPlainObject(usage) || Object.keys(usage).length === 0) {
    throw new MissingMeteringError(
      "execute result is missing usage metering; an unmeasured attempt is never free",
    );
  }
  const numeric = Object.values(usage).filter((v) => typeof v === "number");
  if (numeric.length === 0) {
    throw new MissingMeteringError(
      `usage metering carries no measured quantity: ${JSON.stringify(Object.keys(usage).sort())}`,
    );
  }
  for (const [key, value] of Object.entries(usage)) {
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new ProtocolFrameError(
        `usage metering field ${JSON.stringify(key)} is non-finite (${JSON.stringify(value)}); ` +
          `non-finite metering is rejected (protocol v1.1 §2)`,
      );
    }
  }
}

/** v1.1 identity fields an execute request/response pair must agree on. */
export const IDENTITY_FIELDS = [
  "runId",
  "attemptId",
  "workflowId",
  "caseId",
  "mode",
  "versionDigest",
] as const;

export function buildIdentity(options: {
  runId: string;
  attemptId: string;
  workflowId: string;
  caseId: string;
  mode: string;
  versionDigest?: string | null;
}): Record<string, string> {
  const identity: Record<string, string> = {
    runId: options.runId,
    attemptId: options.attemptId,
    workflowId: options.workflowId,
    caseId: options.caseId,
    mode: options.mode,
  };
  if (options.versionDigest) identity["versionDigest"] = options.versionDigest;
  return identity;
}

/** The echo must be VERBATIM — a response may not add fields the request never carried. */
export function verifyIdentityEcho(
  requestIdentity: Record<string, string>,
  payload: Record<string, unknown>,
): void {
  const echoed = payload["identity"];
  if (!isPlainObject(echoed)) {
    throw new ProtocolFrameError(
      "execute response is missing the v1.1 identity echo (protocol §1)",
    );
  }
  for (const field of IDENTITY_FIELDS) {
    const expected = requestIdentity[field];
    const actual = echoed[field];
    if (expected === undefined) {
      if (field in echoed) {
        throw new ProtocolFrameError(
          `execute response identity echo adds ${JSON.stringify(field)}=${
            JSON.stringify(actual)
          } ` +
            `the request never carried; an echo is verbatim, not supplementary (A6)`,
        );
      }
      continue;
    }
    if (actual !== expected) {
      throw new ProtocolFrameError(
        `execute response identity mismatch on ${JSON.stringify(field)}: requested ` +
          `${JSON.stringify(expected)}, echoed ${JSON.stringify(actual)}`,
      );
    }
  }
}

/** Decode + VERIFY v1.1 collect artifacts: base64 bytes re-hashed against digests. */
export function artifactsFromPayload(
  payload: Record<string, unknown>,
  maxBytes = MAX_ARTIFACT_BYTES,
): Array<{ digest: string; bytes: Uint8Array; kind: string }> {
  const raw = payload["artifacts"] ?? [];
  if (!Array.isArray(raw)) {
    throw new ProtocolFrameError("collect payload 'artifacts' must be a list");
  }
  const out: Array<{ digest: string; bytes: Uint8Array; kind: string }> = [];
  for (const entry of raw) {
    if (!isPlainObject(entry)) throw new ProtocolFrameError("each artifact must be an object");
    const digest = entry["digest"];
    if (typeof digest !== "string" || !digest.startsWith("sha256:") || digest.length !== 71) {
      throw new ProtocolFrameError(`malformed artifact entry: ${JSON.stringify(digest)}`);
    }
    const kind = String(entry["kind"] ?? "evidence");
    if (!["report", "evidence", "usage"].includes(kind)) {
      throw new ProtocolFrameError(`unknown artifact kind ${JSON.stringify(kind)}`);
    }
    const encoded = entry["bytes"];
    if (typeof encoded !== "string") {
      throw new ProtocolFrameError("artifact 'bytes' must be base64 text");
    }
    let bytes: Uint8Array;
    try {
      bytes = decodeBase64(encoded);
    } catch (exc) {
      throw new ProtocolFrameError(`artifact bytes are not valid base64: ${exc}`);
    }
    if (bytes.length > maxBytes) {
      throw new ProtocolFrameError(`artifact ${digest} exceeds the ${maxBytes}-byte transfer cap`);
    }
    const actual = digestBytes(bytes);
    if (actual !== digest) {
      throw new ProtocolFrameError(
        `artifact digest mismatch: declared ${digest}, bytes hash to ${actual}`,
      );
    }
    out.push({ digest, bytes, kind });
  }
  return out;
}

export function digestsFromPayload(payload: Record<string, unknown>): string[] {
  const raw = payload["digests"] ?? [];
  if (!Array.isArray(raw)) throw new ProtocolFrameError("collect payload 'digests' must be a list");
  return raw.map((d) => {
    if (typeof d !== "string" || !d.startsWith("sha256:") || d.length !== 71) {
      throw new ProtocolFrameError(`malformed collect payload: ${JSON.stringify(d)}`);
    }
    return d;
  });
}

/** prepare/cleanup responses: `ok` must be true, else failure. */
export function okFromPayload(payload: Record<string, unknown>, kind: FrameKind): void {
  if (payload["ok"] !== true) {
    const message = String(payload["message"] ?? "");
    const code = String(payload["code"] ?? "vouch/adapter-execution");
    throw new AdapterExecutionError(`${kind} failed (${code}): ${message}`);
  }
}

// --- sanitized stderr diagnostics -----------------------------------------------

const SECRET_KEY_RE =
  /(?:api[-_]?key|authorization|bearer|token|secret|password|passwd|credential)s?(\s*[:= \t]\s*(?:bearer\s+)?[^\s,;]+)/gi;
const BLOB_RE = /\b[0-9a-fA-F]{32,}\b|\b[A-Za-z0-9+/]{40,}={0,2}\b/g;

/** Redact secret-shaped content and tail-limit adapter stderr output. */
export function sanitizeDiagnostics(text: string, limit = MAX_DIAGNOSTICS_CHARS): string {
  if (text.length === 0) return "";
  let redacted = text.replace(
    SECRET_KEY_RE,
    (match, captured: string) => match.replace(captured, "=**[redacted]**"),
  );
  redacted = redacted.replaceAll(BLOB_RE, "[redacted-blob]");
  if (redacted.length > limit) {
    redacted = "…" + redacted.slice(-limit);
  }
  return redacted;
}

export function frameErrorPayload(
  code: string,
  message: string,
  details?: Record<string, unknown>,
): Record<string, unknown> {
  const payload: Record<string, unknown> = { code, message };
  if (details !== undefined) payload["details"] = details;
  return payload;
}

function requireStrField(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new ProtocolFrameError(`payload field ${JSON.stringify(key)} must be a non-empty string`);
  }
  return value;
}

function strList(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || !value.every((v) => typeof v === "string")) {
    throw new ProtocolFrameError(
      `payload field ${JSON.stringify(field)} must be a list of strings`,
    );
  }
  return value as string[];
}

function decodeBase64(encoded: string): Uint8Array {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export function encodeBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

export { ContractError };
