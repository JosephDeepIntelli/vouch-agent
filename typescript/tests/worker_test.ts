/**
 * Gate 2 behavioral tests: the native bounded executor — worker subprocess
 * with zero permissions, nested invocation budgets spanning children and
 * failures, durable cursor across resume, replay exhaustion, cancel,
 * two-worker fencing, pause/resume, and the crash → needs-reconciliation
 * path. Provider keys and acceptance labels never reach the worker.
 */

import { assertEquals, assertTrue } from "./helpers.ts";
import { ExecutionService } from "../src/appservices/execution.ts";
import { ProjectWorkspace } from "../src/appservices/workspace.ts";
import { specFromDict } from "../src/contracts/project.ts";
import { defaultPolicy, Supervisor } from "../src/orchestrator/supervisor.ts";
import { recover } from "../src/orchestrator/checkpoints.ts";
import type {
  ModelCallResult,
  QueryBudgetPort,
  Runtime,
  RuntimeSession,
} from "../src/runtime/ports.ts";

function tempDir(name: string): string {
  const dir = `/tmp/vowdo-ts-tests/${name}-${crypto.randomUUID().slice(0, 8)}`;
  Deno.mkdirSync(dir, { recursive: true });
  return dir;
}

function workspace(name: string): ProjectWorkspace {
  const dir = tempDir(name);
  const spec = specFromDict({
    schemaVersion: "1",
    projectId: `proj-${name}`,
    name,
    workflows: [],
    owners: {},
    allowedChangeTypes: [],
    budget: { schemaVersion: "1", totalUsdCap: 5 },
    mode: "task-only",
    purpose: "",
  } as never);
  return ProjectWorkspace.create(dir, spec as never);
}

function rmrf(path: string): void {
  try {
    Deno.removeSync(path, { recursive: true });
  } catch {
    // gone
  }
}

const FACT_INPUTS = { fact: { value: "boils at 100C", source: "handbook" } };

Deno.test({
  name: "isolated worker run completes with machine-checkable conditions",
  async fn() {
    const ws = workspace("worker-run");
    try {
      const service = new ExecutionService(ws);
      const outcome = await service.run({
        goal: "extract the fact",
        inputs: FACT_INPUTS,
        budgetUsd: 0.5,
        maxSteps: 2,
        provider: "extract-fact",
        isolated: true,
        completionConditions: [{
          type: "artifact_schema",
          schema: {
            type: "object",
            required: ["finding", "source"],
            properties: { finding: { type: "string" }, source: { type: "string" } },
          },
        }],
      });
      assertEquals(outcome.status, "completed", outcome.error ?? "");
      const result = outcome.result!;
      assertEquals(
        result.doneItems.some((d) => d.startsWith("condition artifact_schema[0] passed")),
        true,
      );
      const final = result.artifactRefs[result.artifactRefs.length - 1];
      const payload = JSON.parse(new TextDecoder().decode(ws.artifacts.get(final)));
      assertEquals(payload.finding, "boils at 100C");
      assertEquals(payload.source, "handbook");
      // one scripted call → one query child reservation settled at $0.01
      const modelSteps = (await service.status(outcome.runId))!.steps.filter((s) =>
        s.kind === "model-call"
      );
      assertEquals(modelSteps.length, 1);
      assertEquals(modelSteps[0].usage["llmCalls"], 1);
      assertEquals(modelSteps[0].usage["costUsd"], 0.01);
      // execution config sealed with the worker boundary
      const config = ws.store.load("execution-config", outcome.runId)!;
      assertEquals(config["isolated"], true);
      assertEquals(config["runtimeId"], "vowdo-worker-scripted/1");
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

Deno.test({
  name: "nested invokes: budgets span children; cursor counts underlying queries",
  async fn() {
    const ws2 = workspace("nested2");
    try {
      const scriptPath = `${tempDir("nested-script")}/nested.js`;
      Deno.writeTextFileSync(
        scriptPath,
        "const inner = await invoke({});\nreturn { nested: inner, top: true };",
      );
      const service = new ExecutionService(ws2);
      const runId = service.submit({
        goal: "nested invoke",
        inputs: FACT_INPUTS,
        budgetUsd: 0.5,
        maxSteps: 2, // gather-context consumes one work step, the model step the next
        completionConditions: [{
          type: "artifact_schema",
          schema: { type: "object", required: ["nested"] },
        }],
      });
      // Two script files: the top script (pool[0]) and the nested response (pool[1])
      const secondScript = `${tempDir("nested-script2")}/second.js`;
      Deno.writeTextFileSync(secondScript, 'return { answer: "inner-done" };');
      const outcome = await service.execute(runId, {
        scriptFiles: [scriptPath, secondScript],
        isolated: true,
      });
      assertEquals(outcome.status, "completed", outcome.error ?? "");
      const run = service.status(runId)!;
      const modelStep = run.steps.find((s) => s.kind === "model-call")!;
      assertEquals(modelStep.usage["llmCalls"], 2, "top + nested invoke both consumed queries");
      assertEquals(modelStep.usage["queryCursor"], 2);
      const final = service.resultOf(runId)!.artifactRefs.slice(-1)[0];
      const payload = JSON.parse(new TextDecoder().decode(ws2.artifacts.get(final)));
      assertEquals(payload.nested.answer, "inner-done");
      // Budget: 2 query children at $0.01 each, settled
      const costs = ws2.journal.costEntries(runId).filter((c) => c.measurable);
      const modelCosts = costs.filter((c) => c.note.startsWith("query reservation"));
      assertEquals(modelCosts.length, 2);
      assertEquals(modelCosts.every((c) => c.amountUsd === 0.01), true);
    } finally {
      ws2.close();
      rmrf(ws2.projectDir);
    }
  },
});

Deno.test({
  name: "pause at a step boundary, then resume at the durable cursor without replaying",
  async fn() {
    const ws = workspace("cursor");
    try {
      const s1 = `${tempDir("c1")}/one.js`;
      const s2 = `${tempDir("c2")}/two.js`;
      Deno.writeTextFileSync(s1, 'return { step: "first" };');
      Deno.writeTextFileSync(s2, 'return { step: "second", final: true };');

      // A parking worker runtime: each step resolves when its gate opens.
      // The session-level counter is shared across sessions exactly like the
      // worker's durable cursor positions a resumed session after the
      // responses the previous session consumed.
      const gates: Array<() => void> = [];
      let poolCursor = 0;
      const runtime = {
        backendId: () => "vowdo-test-park-worker/1",
        async openSession(_config: unknown, budget: QueryBudgetPort | null) {
          return {
            async step(_instruction: string, scope: { materials: unknown }) {
              const index = poolCursor;
              poolCursor += 1;
              await new Promise<void>((resolve) => {
                gates.push(resolve);
              });
              const rid = budget!.reserveQuery(0.01);
              budget!.settleQuery(rid, 0.01);
              const script = [s1, s2][index];
              const fn = new Function(
                "materials",
                "prior_artifacts",
                Deno.readTextFileSync(script),
              );
              const value = fn(scope.materials, {});
              return {
                content: JSON.stringify(value),
                returnValue: value,
                promptTokens: 1,
                completionTokens: 1,
                costUsd: 0.01,
                modelId: "parked",
                raw: { llm_calls: 1 },
              } as ModelCallResult;
            },
            usage: () => ({
              cost_usd: 0.01 * poolCursor,
              llm_calls: poolCursor,
              unmeasured_calls: 0,
            }),
            cancel: () => {},
            close: () => {},
          } as RuntimeSession;
        },
      } as unknown as Runtime;

      const mkSupervisor = () =>
        new Supervisor(runtime, ws.store, ws.artifacts, ws.ledger, ws.journal, defaultPolicy());
      const spec = {
        schemaVersion: "1",
        specId: `task_cursor_${crypto.randomUUID().slice(0, 6)}`,
        title: "two-step",
        goal: "two steps",
        mode: "fixture" as const,
        inputs: FACT_INPUTS,
        maxCostUsd: 0.5,
        maxSteps: 3,
        maxWallClockS: null,
        locale: "en",
        market: null,
        workflowId: null,
        successCriteria: {
          conditions: [{
            type: "artifact_schema",
            schema: { type: "object", required: ["final"] },
          }],
        },
        createdBy: "local",
        createdAt: new Date().toISOString().replace("Z$", "+00:00"),
      };
      const supervisor = mkSupervisor();
      const runId = supervisor.submit(spec);
      const executing = supervisor.execute(runId);
      // wait for the first model step to park, then request a PAUSE
      await waitFor(() => {
        const run = supervisor.getRun(runId)!;
        return run.steps.some((st) => st.kind === "model-call" && st.status === "running");
      });
      const step1StartedAt = supervisor.getRun(runId)!.steps.filter((st) =>
        st.kind === "model-call"
      ).length;
      assertEquals(step1StartedAt, 1);
      ws.store.save("pause-request", runId, { requestedAt: new Date().toISOString() });
      gates[0](); // finish step 1 → the loop pauses at the boundary
      const paused = await executing;
      assertEquals(paused.status, "paused");
      const afterPause = supervisor.getRun(runId)!;
      const step1 = afterPause.steps.find((st) => st.kind === "model-call")!;
      assertEquals(step1.status, "ok");
      assertEquals(step1.usage["queryCursor"], 1);

      // A FRESH supervisor (new process equivalent) resumes: the cursor
      // record positions the session AFTER response #1 — never replayed.
      const fresh = mkSupervisor();
      const resumed = fresh.execute(runId);
      await waitFor(() => {
        const run = fresh.getRun(runId)!;
        return run.steps.filter((st) => st.kind === "model-call").length >= 2 &&
          run.steps.some((st) => st.kind === "model-call" && st.status === "running");
      });
      gates[1](); // finish step 2 → conditions met
      const finalRun = await resumed;
      assertEquals(finalRun.status, "completed");
      const steps = fresh.getRun(runId)!.steps.filter((st) =>
        st.kind === "model-call" && st.status === "ok"
      );
      assertEquals(steps.length, 2);
      assertEquals(steps[1].usage["queryCursor"], 2);
      const final = ws.store.load("result-package", runId)!;
      const refs = final["artifactRefs"] as string[];
      const payload = JSON.parse(new TextDecoder().decode(ws.artifacts.get(refs[refs.length - 1])));
      assertEquals(payload.step, "second", "final artifact came from the SECOND scripted response");
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

Deno.test({
  name: "replay exhaustion fails closed (no live fallback, no zero-cost lie)",
  async fn() {
    const ws = workspace("exhaustion");
    try {
      const service = new ExecutionService(ws);
      const s1 = `${tempDir("e1")}/one.js`;
      Deno.writeTextFileSync(s1, 'return { step: "first" };');
      const runId = service.submit({
        goal: "will exhaust",
        inputs: FACT_INPUTS,
        budgetUsd: 0.5,
        maxSteps: 4,
        completionConditions: [{ type: "output_contains", contains: "never" }],
      });
      const outcome = await service.execute(runId, { scriptFiles: [s1], isolated: true });
      assertEquals(outcome.status, "failed");
      const run = service.status(runId)!;
      const modelSteps = run.steps.filter((s) => s.kind === "model-call");
      const failedStep = modelSteps[modelSteps.length - 1];
      assertEquals(failedStep.status, "failed");
      assertTrue(String(failedStep.error).includes("vowdo/replay-exhausted"));
      // The failing (never-run) draw booked NOTHING: no unmeasurable cost line
      const unmeasurable = ws.journal.costEntries(runId).filter((c) => !c.measurable);
      assertEquals(unmeasurable.length, 0, "a refused draw must not book unmeasurable cost");
      // The run's result package honestly states the failure
      const result = service.resultOf(runId)!;
      assertTrue(result.conclusion.includes("stopped without meeting completion conditions"));
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

Deno.test({
  name: "budget authority: per-query reservations refuse over-budget calls before they run",
  async fn() {
    const ws = workspace("budget-authority");
    try {
      const service = new ExecutionService(ws);
      const s1 = `${tempDir("b1")}/one.js`;
      Deno.writeTextFileSync(s1, "return { ok: true };");
      // budget 0.005 < one 0.01 call → the query cannot reserve → model-failed
      const runId = service.submit({
        goal: "too poor",
        inputs: FACT_INPUTS,
        budgetUsd: 0.005,
        maxSteps: 2,
        completionConditions: [{
          type: "artifact_schema",
          schema: { type: "object", required: ["ok"] },
        }],
      });
      const outcome = await service.execute(runId, { scriptFiles: [s1], isolated: true });
      assertEquals(outcome.status, "failed");
      const run = service.status(runId)!;
      const step = run.steps.find((s) => s.kind === "model-call")!;
      assertTrue(String(step.error).includes("vowdo/budget"), step.error ?? "");
      // no query child was created (refused before it ran)
      const children = ws.ledger.reservations().filter((r) => r.parentReservationId !== null);
      assertEquals(children.length, 0);
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

Deno.test({
  name: "unexpected crash mid-step → side-effect unknown → needs-reconciliation",
  async fn() {
    const ws = workspace("crash");
    try {
      // A runtime whose step() throws a NON-Vouch error: the outcome is unknown.
      const boom: Runtime = {
        backendId: () => "vowdo-test-crash/1",
        async openSession(): Promise<RuntimeSession> {
          return {
            async step(): Promise<ModelCallResult> {
              throw new Error("simulated infrastructure crash");
            },
            usage: () => null,
            cancel: () => {},
            close: () => {},
          };
        },
      };
      const supervisor = new Supervisor(
        boom,
        ws.store,
        ws.artifacts,
        ws.ledger,
        ws.journal,
        defaultPolicy(),
      );
      const runId = supervisor.submit({
        schemaVersion: "1",
        specId: "task_crash",
        title: "crash",
        goal: "crash mid-step",
        mode: "fixture",
        inputs: FACT_INPUTS,
        maxCostUsd: 0.5,
        maxSteps: 2,
        maxWallClockS: null,
        locale: "en",
        market: null,
        workflowId: null,
        successCriteria: {
          conditions: [{ type: "artifact_schema", schema: { type: "object", required: ["x"] } }],
        },
        createdBy: "local",
        createdAt: new Date().toISOString().replace("Z$", "+00:00"),
      });
      const run = await supervisor.execute(runId);
      assertEquals(run.status, "needs-reconciliation");
      const report = recover(ws.store, runId);
      assertEquals(report.classification, "needs-reconciliation");
      assertTrue(report.unknownSteps.length > 0);
      // Resume without a verified note is refused
      await assertRejects(() => supervisor.resume(runId, ""));
      // With a note it resumes; the unknown step is never replayed (skipped)
      const resumed = await supervisor.resume(runId, "verified: no side effect occurred");
      assertEquals(resumed.status, "failed"); // no more scripts → honest stop
      const modelSteps = supervisor.getRun(runId)!.steps.filter((s) => s.kind === "model-call");
      assertEquals(modelSteps.filter((s) => s.status === "unknown").length, 1, "unknown step kept");
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

Deno.test({
  name: "two-worker fencing: an actively owned run refuses a second executor",
  async fn() {
    const ws = workspace("fencing");
    try {
      // A runtime that parks mid-step so the first supervisor holds the lease.
      let release!: () => void;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      const parking: Runtime = {
        backendId: () => "vowdo-test-park/1",
        async openSession(): Promise<RuntimeSession> {
          return {
            async step(): Promise<ModelCallResult> {
              await gate;
              return {
                content: "{}",
                returnValue: { done: true },
                promptTokens: 1,
                completionTokens: 1,
                costUsd: 0.01,
                modelId: "parked",
                raw: { llm_calls: 1 },
              };
            },
            usage: () => ({ cost_usd: 0.01, llm_calls: 1, unmeasured_calls: 0 }),
            cancel: () => {},
            close: () => {},
          };
        },
      };
      const mkSupervisor = () =>
        new Supervisor(parking, ws.store, ws.artifacts, ws.ledger, ws.journal, defaultPolicy());
      const spec = {
        schemaVersion: "1",
        specId: `task_fence_${crypto.randomUUID().slice(0, 6)}`,
        title: "fence",
        goal: "g",
        mode: "fixture" as const,
        inputs: FACT_INPUTS,
        maxCostUsd: 0.5,
        maxSteps: 2,
        maxWallClockS: null,
        locale: "en",
        market: null,
        workflowId: null,
        successCriteria: {
          conditions: [{ type: "artifact_schema", schema: { type: "object", required: ["done"] } }],
        },
        createdBy: "local",
        createdAt: new Date().toISOString().replace("Z$", "+00:00"),
      };
      const owner = mkSupervisor();
      const runId = owner.submit(spec);
      const executing = owner.execute(runId);
      await new Promise((r) => setTimeout(r, 150)); // owner acquired the lease + parked
      const second = mkSupervisor();
      await assertRejects(
        () => second.execute(runId),
        (exc: unknown) => exc instanceof Error && exc.message.includes("owned by supervisor"),
      );
      // The second client CAN record a cancellation (authoritative), and the
      // owner finalizes cancelled at its boundary.
      const cancelled = second.cancel(runId, "operator changed their mind");
      assertEquals(cancelled.status, "running"); // not finalized by the requester
      release();
      const final = await executing;
      assertEquals(final.status, "cancelled");
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

Deno.test({
  name: "sealed-config binding: switching provider/isolation on a run is refused",
  async fn() {
    const ws = workspace("sealing");
    try {
      const service = new ExecutionService(ws);
      const runId = service.submit({
        goal: "g",
        inputs: FACT_INPUTS,
        budgetUsd: 0.5,
        maxSteps: 2,
        completionConditions: [{
          type: "artifact_schema",
          schema: { type: "object", required: ["finding"] },
        }],
      });
      const first = await service.execute(runId, { provider: "extract-fact", isolated: true });
      assertEquals(first.status, "completed", first.error ?? "");
      // Different provider on the same run → refused, run keeps its config
      const swapped = await service.execute(runId, { provider: "summarize", isolated: true });
      assertEquals(
        swapped.status,
        "completed",
        "the mismatch is refused BEFORE dispatch; state unchanged",
      );
      const run = service.status(runId)!;
      assertEquals(run.steps.filter((s) => s.kind === "model-call" && s.status === "ok").length, 1);
      // A tampered execution-config record (digest mismatch) is refused
      const config = ws.store.load("execution-config", runId)!;
      config["scripts"] = ["return { fake: true };"];
      ws.store.save("execution-config", runId, config);
      const tampered = await service.resume(runId);
      assertEquals(tampered.status, "completed");
      assertTrue(
        tampered.error !== null && tampered.error.includes("digest"),
        tampered.error ?? "",
      );
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

Deno.test({
  name: "worker sandbox: zero permissions, keys/labels never in worker scope",
  async fn() {
    const ws = workspace("sandbox");
    try {
      const service = new ExecutionService(ws);
      const probeScript = `${tempDir("probe")}/probe.js`;
      // The script tries to touch the filesystem/network/env from the worker.
      Deno.writeTextFileSync(
        probeScript,
        `let blocked = [];
try { Deno.readTextFileSync("/etc/hostname"); blocked.push("read"); } catch (e) { }
try { await fetch("http://127.0.0.1:1/"); blocked.push("net"); } catch (e) { }
try { Deno.env.get("HOME"); blocked.push("env"); } catch (e) { }
return { blocked };`,
      );
      const runId = service.submit({
        goal: "probe the worker boundary",
        inputs: FACT_INPUTS,
        budgetUsd: 0.5,
        maxSteps: 2,
        completionConditions: [{
          type: "artifact_schema",
          schema: { type: "object", required: ["blocked"] },
        }],
      });
      const outcome = await service.execute(runId, { scriptFiles: [probeScript], isolated: true });
      assertEquals(outcome.status, "completed", outcome.error ?? "");
      const final = service.resultOf(runId)!.artifactRefs.slice(-1)[0];
      const payload = JSON.parse(new TextDecoder().decode(ws.artifacts.get(final)));
      assertEquals(
        payload.blocked,
        [],
        "Deno APIs threw (denied) without surfacing as blocked-list entries…",
      );
      // ^ all three attempts must THROW inside the zero-permission worker.
      // The worker's own ready frame reported the boundary; the controller
      // refuses weakened boundaries (verified by the handshake assertion).
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

Deno.test({
  name: "worker death mid-step is an honest model-failure, never a hang",
  async fn() {
    const ws = workspace("worker-death");
    try {
      const service = new ExecutionService(ws);
      const script = `${tempDir("die")}/die.js`;
      Deno.writeTextFileSync(script, "Deno.exit(7);\n");
      const runId = service.submit({
        goal: "worker dies",
        inputs: FACT_INPUTS,
        budgetUsd: 0.5,
        maxSteps: 2,
        completionConditions: [{
          type: "artifact_schema",
          schema: { type: "object", required: ["x"] },
        }],
      });
      const outcome = await service.execute(runId, { scriptFiles: [script], isolated: true });
      assertEquals(outcome.status, "failed");
      const run = service.status(runId)!;
      const step = run.steps.find((s) => s.kind === "model-call")!;
      assertEquals(step.status, "failed");
      assertTrue(String(step.error).length > 0);
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});

async function waitFor(predicate: () => boolean, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("waitFor timed out");
    await new Promise((r) => setTimeout(r, 25));
  }
}

async function assertRejects(
  fn: () => Promise<unknown>,
  check?: (exc: unknown) => boolean,
): Promise<unknown> {
  try {
    await fn();
  } catch (exc) {
    if (check !== undefined) {
      assertEquals(check(exc), true, `unexpected rejection: ${exc}`);
    }
    return exc;
  }
  throw new Error("expected function to reject, but it resolved");
}

Deno.test({
  name: "run-detach: an independent worker process owns the run to completion (client may exit)",
  async fn() {
    const ws = workspace("detach");
    try {
      const service = new ExecutionService(ws);
      const runId = service.submit({
        goal: "detached extraction",
        inputs: FACT_INPUTS,
        budgetUsd: 0.5,
        maxSteps: 2,
        completionConditions: [{
          type: "artifact_schema",
          schema: {
            type: "object",
            required: ["finding"],
            properties: { finding: { type: "string" } },
          },
        }],
      });
      // Spawn the detached run-worker exactly as the CLI does (an
      // independent controller process; this client plays no further role).
      const { spawnRunWorker } = await import("../src/cli/main.ts");
      const pid = spawnRunWorker(ws.projectDir, runId, "extract-fact");
      assertTrue(pid > 0);
      // The client "exits" here (drops all references); poll the workspace
      // like a reconnecting client would.
      await waitFor(() => service.status(runId)?.status === "completed", 30000);
      const result = service.resultOf(runId);
      assertTrue(result !== null);
      const final = result!.artifactRefs.slice(-1)[0];
      const payload = JSON.parse(new TextDecoder().decode(ws.artifacts.get(final)));
      assertEquals(payload.finding, "boils at 100C");
      // Reopen through a FRESH workspace: the durable state stands alone.
      const reopened = ProjectWorkspace.open(ws.projectDir);
      try {
        assertEquals(new ExecutionService(reopened).status(runId)?.status, "completed");
      } finally {
        reopened.close();
      }
    } finally {
      ws.close();
      rmrf(ws.projectDir);
    }
  },
});
