/**
 * ProtocolTransport + ProcessAdapterClient over a Deno.Command subprocess.
 *
 * The controller owns the process lifecycle, validates every inbound frame
 * (version, sequence, size — enforced WHILE ACCUMULATING bytes), and kills
 * the subprocess on wall-clock overrun. After a kill the client refuses
 * further work (fail closed). Vouch never imports the runner's source or
 * shares credentials with it: the process boundary and the framed protocol
 * are the whole contract.
 */

import {
  type AdapterDescriptor,
  type AdapterExecution,
  artifactsFromPayload,
  buildIdentity,
  descriptorFromPayload,
  digestsFromPayload,
  executionFromPayload,
  type Frame,
  FRAME_KINDS,
  frameErrorPayload,
  type FrameKind,
  frameToJsonLine,
  MAX_DIAGNOSTICS_BYTES,
  MAX_FRAME_BYTES,
  okFromPayload,
  parseFrame,
  PROTOCOL_VERSION,
  REQUEST_TO_RESPONSE,
  sanitizeDiagnostics,
  SequenceTracker,
  verifyIdentityEcho,
} from "./protocol.ts";
import { AdapterExecutionError, ContractError, ProtocolFrameError } from "../contracts/common.ts";

export class ProtocolTransport {
  private sendSeq = new SequenceTracker();
  private recvSeq = new SequenceTracker();
  private buffer = new Uint8Array(0);
  private diagnosticsText = "";
  private closed = false;
  private writer: WritableStreamDefaultWriter<Uint8Array>;
  private reader: ReadableStreamDefaultReader<Uint8Array>;
  private stderrReader: ReadableStreamDefaultReader<Uint8Array> | null = null;

  constructor(private child: Deno.ChildProcess) {
    this.writer = child.stdin.getWriter();
    this.reader = child.stdout.getReader();
    if (child.stderr !== null) {
      this.stderrReader = child.stderr.getReader();
    }
  }

  get dead(): boolean {
    return this.closed;
  }

  /**
   * Send one request frame and return its matched response. A response is
   * accepted only when it answers THIS request: the expected kind AND a
   * matching run identity — a frame naming a different run never speaks for
   * the one asked about (A6); such a contradiction kills the process.
   */
  async request(
    kind: FrameKind,
    options: { runId: string | null; payload: Record<string, unknown>; timeoutS: number },
  ): Promise<Frame> {
    if (this.dead) {
      throw new AdapterExecutionError("adapter process is not running (killed or exited)");
    }
    await this.sendFrame(kind, options.runId, options.payload);
    const expected = REQUEST_TO_RESPONSE[kind];
    const deadline = Date.now() + options.timeoutS * 1000;
    for (;;) {
      const frame = await this.readFrame(deadline, kind, options.timeoutS);
      if (frame.runId !== null && frame.runId !== options.runId) {
        await this.killAndDrain();
        throw new ProtocolFrameError(
          `${frame.kind} frame carries runId ${JSON.stringify(frame.runId)} while the request ` +
            `was for ${JSON.stringify(options.runId)}: a response never speaks for another run ` +
            `(protocol identity agreement, A6); process killed`,
        );
      }
      if (frame.kind === "error") {
        const payload = frame.payload;
        const code = String(payload["code"] ?? "vouch/adapter-execution");
        const message = String(payload["message"] ?? "adapter reported an error");
        const details = payload["details"];
        const suffix = details !== undefined ? ` details=${JSON.stringify(details)}` : "";
        throw new AdapterExecutionError(`adapter error (${code}): ${message}${suffix}`);
      }
      if (frame.kind !== expected) {
        throw new ProtocolFrameError(
          `unexpected frame kind ${frame.kind} while awaiting ${expected} (seq ${frame.seq})`,
        );
      }
      if (frame.protocolVersion !== PROTOCOL_VERSION) {
        throw new ProtocolFrameError(
          `${frame.kind} declares protocolVersion ${JSON.stringify(frame.protocolVersion)}, ` +
            `not ${JSON.stringify(PROTOCOL_VERSION)}`,
        );
      }
      return frame;
    }
  }

  private async sendFrame(
    kind: FrameKind,
    runId: string | null,
    payload: Record<string, unknown>,
  ): Promise<void> {
    const line = frameToJsonLine({
      seq: this.sendSeq.next(),
      kind,
      payload,
      runId,
      protocolVersion: PROTOCOL_VERSION,
    });
    try {
      await this.writer.write(new TextEncoder().encode(line + "\n"));
    } catch (exc) {
      throw new AdapterExecutionError(`adapter closed its stdin before answering ${kind}: ${exc}`);
    }
  }

  /**
   * Read one complete frame line before the deadline; kill on overrun. The
   * byte cap is enforced WHILE ACCUMULATING, before any newline is seen
   * (A8): an unterminated or oversized line kills the process the moment
   * pending bytes pass MAX_FRAME_BYTES.
   */
  private async readFrame(deadline: number, requestKind: string, timeoutS: number): Promise<Frame> {
    const text = () => new TextDecoder().decode(this.buffer);
    for (;;) {
      const current = text();
      const newline = current.indexOf("\n");
      const pending = newline < 0 ? this.buffer.length : newline;
      if (pending > MAX_FRAME_BYTES) {
        await this.killAndDrain();
        throw new ProtocolFrameError(
          `inbound frame of ${pending} bytes exceeds MAX_FRAME_BYTES=${MAX_FRAME_BYTES} before a ` +
            `newline arrived; process killed (A8: the cap holds while accumulating)`,
        );
      }
      if (newline >= 0) {
        const raw = current.slice(0, newline);
        this.buffer = new TextEncoder().encode(current.slice(newline + 1));
        const frame = parseFrame(raw);
        this.recvSeq.observe(frame);
        return frame;
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        await this.killAndDrain();
        throw new AdapterExecutionError(
          `adapter overrun: no response to ${requestKind} within ${timeoutS}s; process killed`,
        );
      }
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await Promise.race([
          this.reader.read(),
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new AdapterExecutionError("read-timeout")), remaining)
          ),
        ]);
      } catch (exc) {
        await this.killAndDrain();
        if (exc instanceof AdapterExecutionError && exc.message === "read-timeout") {
          throw new AdapterExecutionError(
            `adapter overrun: stdout timed out after ${timeoutS}s; process killed`,
          );
        }
        throw exc;
      }
      if (chunk.done) {
        await this.killAndDrain();
        throw new AdapterExecutionError(
          `adapter exited or closed stdout before responding (diagnostics=${
            this.diagnosticsText || "none"
          })`,
        );
      }
      const merged = new Uint8Array(this.buffer.length + chunk.value.length);
      merged.set(this.buffer);
      merged.set(chunk.value, this.buffer.length);
      this.buffer = merged;
    }
  }

  /** Read the child's stderr, keeping only a bounded TAIL (A8). */
  private async drainStderr(): Promise<void> {
    if (this.stderrReader === null) return;
    const tail = new Uint8Array(0);
    let acc = tail;
    let dropped = 0;
    try {
      for (;;) {
        const { done, value } = await this.stderrReader.read();
        if (done) break;
        const merged = new Uint8Array(acc.length + value.length);
        merged.set(acc);
        merged.set(value, acc.length);
        acc = merged;
        if (acc.length > MAX_DIAGNOSTICS_BYTES) {
          const drop = acc.length - MAX_DIAGNOSTICS_BYTES;
          acc = acc.slice(drop);
          dropped += drop;
        }
      }
    } catch {
      // pipe torn down mid-drain
    }
    let text = new TextDecoder("utf-8", { fatal: false }).decode(acc);
    if (dropped > 0) {
      text = `… [${dropped} older stderr bytes dropped by the diagnostics cap]\n` + text;
    }
    this.diagnosticsText = sanitizeDiagnostics(text);
  }

  async killAndDrain(): Promise<void> {
    if (!this.closed) {
      try {
        this.child.kill("SIGKILL");
      } catch {
        // already dead
      }
      try {
        await this.child.status;
      } catch {
        // reaped elsewhere
      }
      await this.drainStderr();
      this.closed = true;
    }
  }

  async close(): Promise<void> {
    if (this.closed) {
      await this.drainStderr();
      return;
    }
    try {
      this.child.kill("SIGTERM");
      await this.child.status;
    } catch {
      // terminate is best-effort
    }
    await this.drainStderr();
    this.closed = true;
  }

  diagnostics(): string {
    return this.diagnosticsText;
  }
}

export const DEFAULT_REQUEST_TIMEOUT_S = 30;
export const DEFAULT_EXECUTE_TIMEOUT_S = 120;

/**
 * Narrow default environment allowlist (v1.1 / review B2): the child gets
 * what a runtime needs and nothing that could smuggle credentials into a
 * fixture subprocess. NODE_* / PYTHON_* (beyond the explicit unbuffered flag)
 * are deliberately excluded.
 */
export const DEFAULT_ENV_ALLOWLIST = [
  "PATH",
  "HOME",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "TZ",
  "TMPDIR",
  "TEMP",
  "TMP",
  "SYSTEMROOT",
  "USERPROFILE",
  "APPDATA",
  "PATHEXT",
] as const;

export class ProcessAdapterClient {
  private transport: ProtocolTransport | null = null;
  private descriptorValue: AdapterDescriptor | null = null;
  private describePayloadValue: Record<string, unknown> | null = null;
  private preparedRuns = new Set<string>();

  constructor(
    private command: string[],
    private options: {
      requestTimeoutS?: number;
      executeTimeoutS?: number;
      env?: Record<string, string>;
      cwd?: string;
      label?: string;
    } = {},
  ) {}

  get label(): string {
    return this.options.label ?? this.command[0] ?? "adapter";
  }

  get dead(): boolean {
    return this.transport !== null && this.transport.dead;
  }

  /**
   * The child's environment. An EXPLICIT `env` from the caller wins entirely
   * (documented caller authority — e.g. a pilot service resolving
   * credentials by name). The default is a NARROW NAME allowlist built with
   * scoped reads — never a copy of the inherited environment — plus the
   * unbuffered flag; combined with clearEnv:true nothing else reaches the
   * child. No automatic VOUCH_PILOT_CREDENTIAL_* forwarding exists in this
   * default path.
   */
  private subprocessEnv(): Record<string, string> {
    if (this.options.env !== undefined) return { ...this.options.env };
    const filtered: Record<string, string> = {};
    for (const name of DEFAULT_ENV_ALLOWLIST) {
      try {
        const value = Deno.env.get(name);
        if (value !== undefined) filtered[name] = value;
      } catch {
        // this name is not readable under the current permissions: omit it
      }
    }
    filtered["PYTHONUNBUFFERED"] = "1";
    return filtered;
  }

  private ensureProcess(): ProtocolTransport {
    if (this.transport !== null) {
      if (this.transport.dead) {
        throw new AdapterExecutionError(
          `adapter ${JSON.stringify(this.label)} process was killed or exited; recreate the ` +
            `client for a new attempt (fail closed, no silent respawn)`,
        );
      }
      return this.transport;
    }
    const env = this.subprocessEnv();
    const child = new Deno.Command(this.command[0], {
      args: this.command.slice(1),
      stdin: "piped",
      stdout: "piped",
      stderr: "piped",
      cwd: this.options.cwd,
      // clearEnv: the child starts from an EMPTY environment; only the
      // explicitly constructed allowlist (or a caller-provided env, which is
      // documented caller authority) reaches it. No automatic credential
      // forwarding in fixture mode.
      clearEnv: true,
      env,
    }).spawn();
    this.transport = new ProtocolTransport(child);
    return this.transport;
  }

  async close(): Promise<void> {
    if (this.transport !== null) await this.transport.close();
  }

  diagnostics(): string {
    return this.transport?.diagnostics() ?? "";
  }

  async describe(): Promise<AdapterDescriptor> {
    if (this.descriptorValue !== null) return this.descriptorValue;
    const transport = this.ensureProcess();
    const frame = await transport.request("describe-request", {
      runId: null,
      payload: {},
      timeoutS: this.options.requestTimeoutS ?? DEFAULT_REQUEST_TIMEOUT_S,
    });
    const descriptor = descriptorFromPayload(frame.payload);
    if (descriptor.protocolVersion !== "1") {
      throw new AdapterExecutionError(
        `adapter ${JSON.stringify(descriptor.adapterId)} speaks protocol ` +
          `${JSON.stringify(descriptor.protocolVersion)}, not v1`,
      );
    }
    this.descriptorValue = descriptor;
    this.describePayloadValue = { ...frame.payload };
    return descriptor;
  }

  async describePayload(): Promise<Record<string, unknown>> {
    if (this.describePayloadValue === null) {
      await this.describe();
    }
    return { ...this.describePayloadValue! };
  }

  async prepare(runId: string, mode: string): Promise<void> {
    const transport = this.ensureProcess();
    const frame = await transport.request("prepare-request", {
      runId,
      payload: { mode },
      timeoutS: this.options.requestTimeoutS ?? DEFAULT_REQUEST_TIMEOUT_S,
    });
    okFromPayload(frame.payload, "prepare-response");
    this.preparedRuns.add(runId);
  }

  async execute(options: {
    runId: string;
    attemptId: string;
    workflowId: string;
    caseInput: Record<string, unknown>;
    mode: string;
    timeoutS?: number;
  }): Promise<AdapterExecution> {
    // Fail closed BEFORE dispatch: results are only ever attributed to a
    // run this client actually prepared (review finding: execute skipped
    // the binding collect/cleanup already enforced).
    this.requirePrepared(options.runId);
    const transport = this.ensureProcess();
    const versionDigest = options.caseInput["versionDigest"];
    if (
      versionDigest !== undefined && versionDigest !== null && typeof versionDigest !== "string"
    ) {
      throw new ProtocolFrameError(
        "execute case_input 'versionDigest' must be a digest string when present (protocol v1.1 §1, A6)",
      );
    }
    const identity = buildIdentity({
      runId: options.runId,
      attemptId: options.attemptId,
      workflowId: options.workflowId,
      caseId: String(options.caseInput["caseId"] ?? ""),
      mode: options.mode,
      versionDigest: versionDigest ? (versionDigest as string) : null,
    });
    const frame = await transport.request("execute-request", {
      runId: options.runId,
      payload: {
        attemptId: options.attemptId,
        workflowId: options.workflowId,
        caseInput: options.caseInput,
        mode: options.mode,
        identity,
      },
      timeoutS: options.timeoutS ?? this.options.executeTimeoutS ?? DEFAULT_EXECUTE_TIMEOUT_S,
    });
    const execution = executionFromPayload(frame.payload);
    verifyIdentityEcho(identity, frame.payload);
    if (execution.mode !== options.mode) {
      // A child answering "authorized-live" to a fixture request is
      // misreporting what it did — refuse before storage or billing.
      await this.transport!.killAndDrain();
      throw new ProtocolFrameError(
        `execute response mode disagreement: requested ${JSON.stringify(options.mode)}, identity ` +
          `echoed ${JSON.stringify(identity["mode"])}, payload reports ${
            JSON.stringify(execution.mode)
          }; ` +
          `contradictory mode never reaches storage or acceptance (A6)`,
      );
    }
    return execution;
  }

  async collect(runId: string): Promise<string[]> {
    this.requirePrepared(runId);
    const transport = this.ensureProcess();
    const frame = await transport.request("collect-request", {
      runId,
      payload: {},
      timeoutS: this.options.requestTimeoutS ?? DEFAULT_REQUEST_TIMEOUT_S,
    });
    return digestsFromPayload(frame.payload);
  }

  async collectArtifacts(
    runId: string,
  ): Promise<Array<{ digest: string; bytes: Uint8Array; kind: string }>> {
    this.requirePrepared(runId);
    const transport = this.ensureProcess();
    const frame = await transport.request("collect-request", {
      runId,
      payload: { withArtifacts: true },
      timeoutS: this.options.executeTimeoutS ?? DEFAULT_EXECUTE_TIMEOUT_S,
    });
    return artifactsFromPayload(frame.payload);
  }

  async cleanup(runId: string): Promise<void> {
    this.requirePrepared(runId);
    const transport = this.ensureProcess();
    const frame = await transport.request("cleanup-request", {
      runId,
      payload: {},
      timeoutS: this.options.requestTimeoutS ?? DEFAULT_REQUEST_TIMEOUT_S,
    });
    okFromPayload(frame.payload, "cleanup-response");
    // The run is closed: drop the binding so a later execute/collect for
    // this run id is refused instead of reusing a cleaned-up session.
    this.preparedRuns.delete(runId);
  }

  private requirePrepared(runId: string): void {
    if (!this.preparedRuns.has(runId)) {
      throw new AdapterExecutionError(
        `run ${JSON.stringify(runId)} was never prepared on this client; results are never ` +
          `attributed to a run this client did not set up (A6)`,
      );
    }
  }
}

export { ContractError, FRAME_KINDS, frameErrorPayload };
