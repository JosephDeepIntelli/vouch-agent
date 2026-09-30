/**
 * Controller side of the bounded worker: spawns the zero-permission worker
 * process and speaks its newline-delimited frame protocol. Reservation
 * AUTHORITY stays here — the worker's budget.reserve requests go through
 * the controller's StepQueryBudget (atomic ledger children), never a worker
 * local counter.
 *
 * Spawn boundary (honest, recorded per spawn):
 *  - dev: `deno run --no-prompt` on worker_main.ts with NO allow flags —
 *    a true Deno permission sandbox (no read/net/env/run).
 *  - compiled with vouch-worker present: that binary, compiled with no
 *    permissions.
 *  - compiled without vouch-worker: refuses isolated execution with an
 *    actionable error (the main binary's compiled-in permissions are too
 *    broad to claim as a worker boundary); `--worker-path` may point at a
 *    zero-permission worker explicitly.
 * When `prlimit` is available it wraps the spawn with a CPU-SECOND cap —
 * that is the only OS resource cap applied: RLIMIT_AS and low RLIMIT_NPROC
 * cannot be used with a V8 runtime (see prlimitWrap). Worker MEMORY has no
 * enforced OS cap; frame caps bound message sizes and step deadlines bound
 * time, but nothing bounds the worker's heap. The exact boundary is
 * recorded per spawn.
 */

import {
  ContractError,
  StepFailureError,
  UnsupportedIsolationError,
  VouchError,
} from "../contracts/common.ts";
import { isFile } from "../contracts/fsutil.ts";
import { isolatedChildSpawn } from "./spawn.ts";
import type {
  ModelCallResult,
  QueryBudgetPort,
  Runtime,
  RuntimeSession,
  WorkerSessionConfig,
} from "./ports.ts";

const MAX_FRAME_BYTES = 32 * 1024 * 1024;

interface WorkerSpawn {
  command: string;
  args: string[];
  boundary: string;
}

export interface WorkerRuntimeOptions {
  /** Explicit path to a zero-permission worker binary (compiled mode). */
  workerPath?: string;
  /** Working directory for the dev-mode deno worker (unused by the worker). */
  cwd?: string;
  /** TEST-ONLY hook: extra deno permission flags for the dev-mode worker so
   * weakened-boundary detection and controller refusal can be exercised
   * end-to-end. Never set by product code. */
  workerExtraArgs?: string[];
}

function fileUrl(path: string): string {
  return `file://${path.startsWith("/") ? "" : "/"}${path.replaceAll(" ", "%20")}`;
}

function resolveWorkerModule(): string {
  return new URL("./worker_main.ts", import.meta.url).pathname;
}

function isDevRuntime(): boolean {
  // In dev the current executable is the deno binary itself.
  return Deno.execPath().endsWith("deno");
}

function locateSpawn(options: WorkerRuntimeOptions): WorkerSpawn {
  if (options.workerPath !== undefined && options.workerPath.length > 0) {
    if (!isFile(options.workerPath)) {
      throw new ContractError(`--worker-path ${options.workerPath} does not exist`);
    }
    return {
      command: options.workerPath,
      args: [],
      boundary: "compiled-worker:no-permissions",
    };
  }
  if (isDevRuntime()) {
    return {
      command: Deno.execPath(),
      args: [
        "run",
        "--no-prompt",
        ...(options.workerExtraArgs ?? []),
        fileUrl(resolveWorkerModule()),
      ],
      boundary: options.workerExtraArgs !== undefined && options.workerExtraArgs.length > 0
        ? "deno-run:test-weakened"
        : "deno-run:no-allow-flags",
    };
  }
  // Compiled main binary: look for vouch-worker beside it.
  const exec = Deno.execPath();
  const sibling = `${exec.slice(0, exec.lastIndexOf("/"))}/vouch-worker`;
  if (isFile(sibling)) {
    return { command: sibling, args: [], boundary: "compiled-worker:no-permissions" };
  }
  throw new UnsupportedIsolationError(
    `isolated execution needs the zero-permission vouch-worker binary next to ${exec} ` +
      `(or --worker-path); the main binary's compiled permissions are too broad to claim as ` +
      `a worker boundary — use --inline for the shipped trusted sample scripts`,
  );
}

function prlimitWrap(spawn: WorkerSpawn, prlimit: string): WorkerSpawn {
  // CPU-second cap only: V8 reserves multi-GB virtual address space and
  // needs >1024 threads, so RLIMIT_AS / low RLIMIT_NPROC break the runtime
  // before it starts (verified on Deno 2.9.7). Worker memory therefore has
  // NO enforced OS cap — an honest gap vs the Python rlimit worker, recorded
  // in the migration protections table; frame caps bound message sizes and
  // deadlines bound time, neither of which bound the heap.
  return {
    command: prlimit,
    args: ["--cpu=30", "--", spawn.command, ...spawn.args],
    boundary: `${spawn.boundary}+prlimit(cpu=30s)`,
  };
}

/** Absolute path of prlimit, or null. Env access may be denied (deterministic
 * local mode: no secret inheritance), so PATH is only ONE probe — standard
 * absolute locations are checked directly, and the wrapper always spawns by
 * ABSOLUTE path so a restricted child PATH cannot break the launch. */
export function prlimitPath(): string | null {
  const candidates: string[] = [];
  try {
    const path = Deno.env.get("PATH");
    if (path !== undefined) {
      candidates.push(...path.split(":").filter((d) => d.length > 0).map((d) => `${d}/prlimit`));
    }
  } catch {
    // env permission denied — standard locations only
  }
  candidates.push("/usr/local/bin/prlimit", "/usr/bin/prlimit", "/bin/prlimit");
  for (const candidate of candidates) {
    try {
      if (Deno.statSync(candidate).isFile) return candidate;
    } catch {
      // keep looking
    }
  }
  return null;
}

export function prlimitAvailable(): boolean {
  return prlimitPath() !== null;
}

/** The controller refuses any boundary reporting itself as weakened. */
export function isWeakenedBoundary(boundary: string): boolean {
  return boundary.includes("weakened");
}

export class WorkerProcessRuntime implements Runtime {
  private lastBoundary = "";
  private lastWorkerPidValue = 0;

  constructor(private options: WorkerRuntimeOptions = {}) {}

  /** Boundary description of the most recent spawn (for sealed configs). */
  boundaryOfLastSpawn(): string {
    return this.lastBoundary;
  }

  /** Pid of the most recent worker spawn (test-support: termination checks). */
  get lastWorkerPid(): number {
    return this.lastWorkerPidValue;
  }

  backendId(): string {
    return "vouch-worker-scripted/1";
  }

  async openSession(
    config: WorkerSessionConfig,
    budget: QueryBudgetPort | null,
  ): Promise<RuntimeSession> {
    if (config.mode !== "fixture") {
      throw new ContractError(
        "direct execution ships with fixture-mode providers only; authorized-live wiring is a " +
          "separate, reviewed milestone",
      );
    }
    const base = locateSpawn(this.options);
    const prlimit = prlimitPath();
    const spawn = prlimit !== null ? prlimitWrap(base, prlimit) : base;
    this.lastBoundary = spawn.boundary;
    // Clear the inherited environment entirely: the worker is an
    // untrusted-side executor and receives NO ambient variables (a Deno
    // child would otherwise inherit everything despite any filtered env).
    const spec = isolatedChildSpawn(spawn.command, spawn.args);
    const child = new Deno.Command(spec.command, {
      args: spec.args,
      stdin: "piped",
      stdout: "piped",
      stderr: "piped",
      clearEnv: spec.clearEnv,
      env: spec.env,
    }).spawn();
    this.lastWorkerPidValue = child.pid;
    const session = new WorkerSessionImpl(child, spawn.boundary, config, budget);
    try {
      await session.handshake();
    } catch (exc) {
      // Fail-closed cleanup: a rejected or malformed worker handshake must
      // not leave the child running (or unreaped) behind the propagated
      // error — close() kills AND awaits the child before we rethrow.
      session.close();
      await session.reaped();
      throw exc;
    }
    return session;
  }
}

class WorkerSessionImpl implements RuntimeSession {
  private writer: WritableStreamDefaultWriter<Uint8Array>;
  private reader: ReadableStreamDefaultReader<Uint8Array>;
  private buffer = "";
  private closed = false;
  private usageSnapshot: Record<string, unknown> | null = null;
  readonly boundary: string;

  constructor(
    private child: Deno.ChildProcess,
    boundary: string,
    private config: WorkerSessionConfig,
    private budget: QueryBudgetPort | null,
  ) {
    this.writer = child.stdin.getWriter();
    this.reader = child.stdout.getReader();
    this.boundary = boundary;
  }

  async handshake(): Promise<void> {
    const first = await this.readLine();
    if (first === null || first["t"] !== "ready") {
      throw new ContractError("worker did not send a ready frame");
    }
    if (typeof first["boundary"] === "string" && isWeakenedBoundary(first["boundary"])) {
      throw new ContractError(
        `worker reports a WEAKENED boundary (${first["boundary"]}); refusing to execute in it`,
      );
    }
    await this.writeLine({
      t: "open",
      config: {
        mode: this.config.mode,
        maxSteps: this.config.maxSteps,
        wallClockS: this.config.wallClockS,
        maxCostUsd: this.config.maxCostUsd,
      },
      scripts: this.config.scriptedResponses ?? [],
      cursor: this.config.scriptedCursor ?? 0,
    });
    const opened = await this.readLine();
    if (opened === null || opened["t"] !== "opened") {
      throw new ContractError("worker did not acknowledge the session open");
    }
  }

  private async writeLine(frame: Record<string, unknown>): Promise<void> {
    if (this.closed) throw new ContractError("worker session is closed");
    const text = JSON.stringify(frame) + "\n";
    if (text.length > MAX_FRAME_BYTES) {
      throw new ContractError(`worker frame exceeds ${MAX_FRAME_BYTES} bytes`);
    }
    await this.writer.write(new TextEncoder().encode(text));
  }

  private async readLine(): Promise<Record<string, unknown> | null> {
    while (!this.buffer.includes("\n")) {
      const { done, value } = await this.reader.read();
      if (done) return null;
      this.buffer += new TextDecoder().decode(value, { stream: true });
      if (this.buffer.length > MAX_FRAME_BYTES * 2) {
        throw new ContractError("worker frame overflow");
      }
    }
    const index = this.buffer.indexOf("\n");
    const line = this.buffer.slice(0, index);
    this.buffer = this.buffer.slice(index + 1);
    if (line.trim().length === 0) return this.readLine();
    return JSON.parse(line) as Record<string, unknown>;
  }

  async step(
    instruction: string,
    scope: { materials: unknown; priorArtifacts: unknown },
  ): Promise<ModelCallResult> {
    await this.writeLine({
      t: "step",
      instruction,
      scope: { materials: scope.materials, prior_artifacts: scope.priorArtifacts },
    });
    // Serve the worker's budget requests until the step result/error frame.
    for (;;) {
      const frame = await this.readLine();
      if (frame === null) {
        throw new StepFailureError(
          "vouch/adapter-execution",
          "worker exited mid-step",
          this.usageSnapshot,
        );
      }
      if (frame["t"] === "budget.reserve") {
        const rid = String(frame["rid"]);
        const estimate = frame["estimateUsd"] === null ? null : Number(frame["estimateUsd"]);
        if (this.budget === null) {
          await this.writeLine({
            t: "budget.denied",
            rid,
            code: "vouch/budget",
            message: "no query budget is attached to this session",
          });
          continue;
        }
        try {
          const reservationId = this.budget.reserveQuery(estimate);
          await this.writeLine({ t: "budget.ok", rid, reservationId });
        } catch (exc) {
          if (exc instanceof VouchError) {
            await this.writeLine({
              t: "budget.denied",
              rid,
              code: exc.code,
              message: exc.message,
            });
            continue;
          }
          throw exc;
        }
        continue;
      }
      if (frame["t"] === "budget.settle") {
        this.budget?.settleQuery(
          String(frame["rid"]),
          frame["actualUsd"] === null ? null : Number(frame["actualUsd"]),
        );
        continue;
      }
      if (frame["t"] === "budget.release") {
        this.budget?.releaseQuery(String(frame["rid"]));
        continue;
      }
      if (frame["t"] === "usage" || frame["t"] === "step.result") {
        if (frame["usage"] !== undefined && typeof frame["usage"] === "object") {
          this.usageSnapshot = frame["usage"] as Record<string, unknown>;
        }
      }
      if (frame["t"] === "step.result") {
        const raw = (frame["raw"] as Record<string, unknown>) ?? {};
        return {
          content: String(frame["content"] ?? ""),
          returnValue: frame["returnValue"] ?? raw["return_value"] ?? null,
          promptTokens: numOrNull(frame["promptTokens"]),
          completionTokens: numOrNull(frame["completionTokens"]),
          costUsd: numOrNull(frame["costUsd"]),
          modelId: String(raw["model"] ?? "vouch-scripted/fixture-1"),
          raw,
        };
      }
      if (frame["t"] === "step.error") {
        if (frame["usage"] !== undefined && typeof frame["usage"] === "object") {
          this.usageSnapshot = frame["usage"] as Record<string, unknown>;
        }
        throw new StepFailureError(
          String(frame["code"] ?? "vouch/script-error"),
          String(frame["message"] ?? ""),
          this.usageSnapshot,
        );
      }
      if (frame["t"] === "fatal") {
        throw new StepFailureError(
          String(frame["code"] ?? "vouch/protocol-frame"),
          String(frame["message"] ?? "worker fatal"),
          this.usageSnapshot,
        );
      }
      throw new ContractError(`unexpected worker frame ${JSON.stringify(frame["t"])}`);
    }
  }

  usage(): Record<string, unknown> | null {
    return this.usageSnapshot;
  }

  cancel(reason: string): void {
    try {
      this.child.kill("SIGTERM");
    } catch {
      // already dead
    }
    void reason;
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    // The worker is stateless between steps: terminate the process rather
    // than negotiating a close frame over possibly-broken pipes.
    try {
      this.child.kill("SIGTERM");
    } catch {
      // already dead
    }
    try {
      this.child.status.catch(() => {
        // reaped; close must never surface as an unhandled rejection
      });
    } catch {
      // already reaped
    }
  }

  /** Resolves once the child has actually exited and been reaped. */
  async reaped(): Promise<void> {
    try {
      await this.child.status;
    } catch {
      // already reaped elsewhere
    }
  }
}

function numOrNull(value: unknown): number | null {
  if (value === undefined || value === null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
