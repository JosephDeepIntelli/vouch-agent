/**
 * The worker process entrypoint — zero-permission bounded step executor.
 *
 * Compiled as its own binary (`vouch-worker`) with NO permission flags, or
 * run in dev as `deno run --no-prompt worker_main.ts` with no allows. It
 * reads newline-delimited JSON frames from stdin and writes them to stdout:
 * everything it needs (script pool, scope) arrives in frames; it never
 * reads files, network or environment. At startup it PROBES its own
 * permissions and reports the boundary it actually runs under — a weakened
 * boundary (permissions granted) is reported honestly to the controller.
 */

import { executeScriptedStep, type InvokePort } from "./scripted_engine.ts";

const MAX_FRAME_BYTES = 32 * 1024 * 1024;

interface OpenFrame {
  t: "open";
  config: { mode: string; maxSteps: number; wallClockS: number; maxCostUsd: number };
  scripts: string[];
  cursor: number;
}
interface StepFrame {
  t: "step";
  instruction: string;
  scope: { materials?: unknown; prior_artifacts?: unknown };
}
type BudgetReply =
  | { ok: true; reservationId: string }
  | { ok: false; code: string; message: string };
type Inbound =
  | OpenFrame
  | StepFrame
  | { t: "close" }
  | { t: "budget.ok"; rid: string; reservationId: string }
  | { t: "budget.denied"; rid: string; code: string; message: string };

/**
 * Every runtime permission class the worker must not retain.
 *
 * Scoped grants are invisible to a name-only permission query: with
 * `--allow-write=/tmp`, `query({name:"write"})` reports `prompt` (the
 * grant does not cover ALL writes), so "no global grants" must NEVER be
 * labeled "no permissions" (review finding). The worker therefore:
 *
 *   1. REVOKES every class at startup — revocation neutralizes scoped and
 *      global grants alike (verified: a scoped /tmp write grant goes
 *      granted -> prompt and the write then throws NotCapable);
 *   2. functionally PROVES denial for the abusable classes by attempting a
 *      synthetic, side-effect-free operation and requiring NotCapable;
 *   3. reports the effective boundary honestly: "revoked" classes plus
 *      denied proofs are neutralization, not a launch-time absence of
 *      grants — and ANY surviving grant or succeeding probe is reported as
 *      weakened, which the controller refuses.
 */
const WORKER_MUST_LACK = [
  "read",
  "write",
  "net",
  "env",
  "run",
  "ffi",
  "sys",
  "import",
] as const;

type PermissionName = (typeof WORKER_MUST_LACK)[number];

/** Synthetic, side-effect-free probes per class: each must throw
 * Deno.errors.NotCapable after revocation; success means the grant is
 * still effective and the boundary is weakened. */
const FUNCTIONAL_PROBES: Partial<Record<PermissionName, () => unknown>> = {
  read: () => Deno.readTextFile("/dev/null"),
  write: () => Deno.writeTextFile(`/vouch-worker-boundary-probe-denied`, ""),
  env: () => Deno.env.get("HOME"),
  run: () => new Deno.Command("/bin/true").output(),
  net: () => Deno.connect({ hostname: "127.0.0.1", port: 1 }),
  sys: () => Deno.loadavg(),
};

function isNotCapable(exc: unknown): boolean {
  return exc instanceof Error && exc.name === "NotCapable";
}

export async function enforceAndProbeBoundary(): Promise<string> {
  const weakened: string[] = [];
  const revoked: string[] = [];
  for (const name of WORKER_MUST_LACK) {
    // 1. revoke (neutralizes scoped AND global grants for this class)
    try {
      await Deno.permissions.revoke({ name } as Deno.PermissionDescriptor);
      revoked.push(name);
    } catch {
      // revocation unavailable for this class: fall through to the probes
    }
    // 2. post-revocation name query: any still-granted class is weakened
    try {
      const status = await Deno.permissions.query({ name } as Deno.PermissionDescriptor);
      if (status.state === "granted") weakened.push(`${name}:granted-after-revoke`);
    } catch {
      // class not queryable in this build: the functional probes decide
    }
  }
  // 3. functional proofs for the abusable classes
  for (const [name, probe] of Object.entries(FUNCTIONAL_PROBES)) {
    try {
      await probe!();
      weakened.push(`${name}:probe-succeeded`);
    } catch (exc) {
      if (!isNotCapable(exc)) {
        // a non-permission failure means the operation was ATTEMPTED — the
        // grant was effective (e.g. a network connect reached the OS)
        weakened.push(`${name}:probe-attempted(${exc instanceof Error ? exc.name : "unknown"})`);
      }
    }
  }
  if (weakened.length > 0) {
    return `deno-permissions-granted:${weakened.join(",")} (weakened)`;
  }
  return `deno-permissions:none (all classes revoked at startup; scoped grants neutralized; ` +
    `probes denied)`;
}

function writeLine(frame: Record<string, unknown>): void {
  Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify(frame) + "\n"));
}

async function handleStep(frame: StepFrame, state: {
  scripts: string[];
  cursorRef: { value: number };
  costRef: { value: number };
  callsRef: { value: number };
  budget: InvokePort;
}): Promise<void> {
  try {
    const result = await executeScriptedStep({
      scripts: state.scripts,
      cursor: state.cursorRef.value,
      instruction: frame.instruction,
      materials: frame.scope?.materials,
      priorArtifacts: frame.scope?.prior_artifacts,
      budget: state.budget,
    });
    state.cursorRef.value += result.llmCalls;
    state.costRef.value += result.costUsd;
    state.callsRef.value += result.llmCalls;
    if (result.error === null) {
      writeLine({
        t: "step.result",
        content: result.content,
        returnValue: result.returnValue,
        promptTokens: result.promptTokens,
        completionTokens: result.completionTokens,
        costUsd: result.costUsd,
        llmCalls: result.llmCalls,
        raw: { return_value: result.returnValue, llm_calls: result.llmCalls },
        usage: {
          cost_usd: state.costRef.value,
          llm_calls: state.callsRef.value,
          unmeasured_calls: 0,
        },
      });
    } else {
      writeLine({
        t: "step.error",
        code: result.error.code,
        message: result.error.message,
        llmCalls: result.llmCalls,
        costUsd: result.costUsd,
        usage: {
          cost_usd: state.costRef.value,
          llm_calls: state.callsRef.value,
          unmeasured_calls: 0,
        },
      });
    }
  } catch (exc) {
    writeLine({
      t: "fatal",
      code: "vouch/internal",
      message: exc instanceof Error ? exc.message : String(exc),
    });
  }
}

async function main(): Promise<void> {
  const boundary = await enforceAndProbeBoundary();
  const scripts: string[] = [];
  const cursorRef = { value: 0 };
  const costRef = { value: 0 };
  const callsRef = { value: 0 };
  const pending = new Map<string, (reply: BudgetReply) => void>();
  let handleStepCurrent: ((frame: StepFrame) => Promise<void>) | null = null;

  const budget: InvokePort = {
    async reserve(estimateUsd: number | null): Promise<string> {
      const rid = `w-${pending.size + 1}-${crypto.randomUUID().slice(0, 8)}`;
      writeLine({ t: "budget.reserve", rid, estimateUsd });
      const reply: BudgetReply = await new Promise((resolve) => {
        pending.set(rid, resolve);
      });
      if (!reply.ok) {
        throw new Error(`[${reply.code}] ${reply.message}`);
      }
      return reply.reservationId;
    },
    settle(reservationId: string, actualUsd: number | null): void {
      writeLine({ t: "budget.settle", rid: reservationId, actualUsd });
    },
    release(reservationId: string): void {
      writeLine({ t: "budget.release", rid: reservationId });
    },
  };

  writeLine({ t: "ready", backendId: "vouch-worker-scripted/1", boundary });

  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of Deno.stdin.readable) {
    buffer += decoder.decode(chunk, { stream: true });
    if (buffer.length > MAX_FRAME_BYTES * 2) {
      writeLine({ t: "fatal", code: "vouch/protocol-frame", message: "frame overflow" });
      return;
    }
    let newlineIndex: number;
    while ((newlineIndex = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newlineIndex);
      buffer = buffer.slice(newlineIndex + 1);
      if (line.trim().length === 0) continue;
      let frame: Inbound;
      try {
        frame = JSON.parse(line);
      } catch {
        writeLine({ t: "fatal", code: "vouch/protocol-frame", message: "unparseable frame" });
        return;
      }
      if (frame.t === "open") {
        scripts.splice(0, scripts.length, ...(frame.scripts ?? []));
        cursorRef.value = frame.cursor ?? 0;
        costRef.value = 0;
        callsRef.value = 0;
        handleStepCurrent = (stepFrame: StepFrame) =>
          handleStep(stepFrame, { scripts, cursorRef, costRef, callsRef, budget });
        writeLine({ t: "opened", cursor: cursorRef.value });
      } else if (frame.t === "budget.ok" || frame.t === "budget.denied") {
        const resolve = pending.get(frame.rid);
        if (resolve !== undefined) {
          pending.delete(frame.rid);
          resolve(
            frame.t === "budget.ok"
              ? { ok: true, reservationId: frame.reservationId }
              : { ok: false, code: frame.code, message: frame.message },
          );
        }
      } else if (frame.t === "step") {
        // Handle the step WITHOUT blocking the reader loop: the script may
        // await budget.reserve, whose reply arrives on this same stdin.
        if (handleStepCurrent === null) {
          writeLine({ t: "fatal", code: "vouch/protocol-frame", message: "step before open" });
          return;
        }
        void handleStepCurrent(frame);
      } else if (frame.t === "close") {
        return;
      }
    }
  }
}

if (import.meta.main) {
  await main();
}
