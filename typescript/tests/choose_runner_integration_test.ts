/**
 * Real Choose-runner protocol integration (genuinely ignored when
 * unconfigured): drives the independently maintained Choose runner through
 * this TS ProcessAdapterClient — describe → prepare → execute → collect →
 * cleanup over the actual subprocess protocol. Proves cross-implementation
 * protocol v1/v1.1 compatibility with zero sibling-source imports (the
 * process boundary and frames are the whole contract).
 *
 * Configuration (explicit, no machine-specific paths in this file):
 *   VOWDO_TEST_CHOOSE_ROOT=<choose-website checkout>
 * The test runner grants a SCOPED env read for exactly this variable
 * (see the `test` task in deno.json). Unset ⇒ the test is reported by the
 * runner as IGNORED (never counted as passed). Configured but broken ⇒
 * failure, not a skip. No network, no credentials: the runner enforces
 * fixture mode itself.
 */

import { assertEquals, assertTrue } from "./helpers.ts";
import { ProcessAdapterClient } from "../src/adapters/process_adapter.ts";
import { isFile } from "../src/contracts/fsutil.ts";

function chooseRoot(): string | undefined {
  try {
    const value = Deno.env.get("VOWDO_TEST_CHOOSE_ROOT");
    return value !== undefined && value.trim().length > 0 ? value.trim() : undefined;
  } catch {
    // env permission not granted for this variable — same as unconfigured
    return undefined;
  }
}

const ROOT = chooseRoot();

Deno.test({
  name: "choose runner: protocol handshake over the real subprocess",
  ignore: ROOT === undefined,
  async fn() {
    const root = ROOT!;
    const runner = `${root}/scripts/vowdo/runner.ts`;
    const tsx = `${root}/node_modules/.bin/tsx`;
    assertTrue(
      isFile(runner),
      `runner missing: ${runner} (configured root is not a Choose checkout)`,
    );
    assertTrue(isFile(tsx), `tsx missing: ${tsx} (run npm install in the Choose checkout)`);
    // The configured runner's environment is CALLER AUTHORITY, passed
    // explicitly: exactly what a node/tsx interpreter needs to resolve
    // itself (PATH) and locate its home (HOME) — nothing ambient, never the
    // sentinel-bearing inherited environment (clearEnv still applies).
    const runnerEnv: Record<string, string> = {};
    for (const name of ["PATH", "HOME"]) {
      const value = Deno.env.get(name);
      if (value !== undefined) runnerEnv[name] = value;
    }
    const client = new ProcessAdapterClient([tsx, runner], {
      label: "runner:choose",
      requestTimeoutS: 120,
      executeTimeoutS: 180,
      cwd: root, // the runner resolves its own project's path aliases
      env: runnerEnv,
    });
    try {
      const descriptor = await client.describe();
      assertTrue(descriptor.adapterId.length > 0, "adapterId");
      assertEquals(descriptor.protocolVersion, "1");
      assertEquals(descriptor.enforcedModes.includes("fixture"), true);
      const runId = "eval_choose_integration_1";
      await client.prepare(runId, "fixture");
      // A configured, supported runner advertising ZERO evaluation cases is a
      // broken advertisement — a failure, never a logged skip counted as pass.
      const payload = await client.describePayload();
      const evaluationCases = (payload["evaluationCases"] as Array<Record<string, unknown>>) ?? [];
      assertTrue(
        evaluationCases.length > 0,
        `configured Choose runner advertises no evaluation cases; refusing to count an empty integration as passed`,
      );
      const firstCase = String(evaluationCases[0]["caseId"]);
      const workflowId = String(evaluationCases[0]["workflowId"] ?? "W-C2");
      const execution = await client.execute({
        runId,
        attemptId: "att_choose_1",
        workflowId,
        caseInput: { caseId: firstCase },
        mode: "fixture",
      });
      // Metering separation (protocol v1.1 §2): scripted/measured fields, no
      // invented costUsd in fixture mode.
      const usage = execution.usage ?? {};
      assertTrue(
        typeof usage["tokensScripted"] === "number" ||
          typeof usage["elapsedMsMeasured"] === "number",
        `metering present: ${JSON.stringify(usage)}`,
      );
      assertEquals(usage["costUsd"], undefined, "fixture runs carry no priced cost");
      const digests = await client.collect(runId);
      assertTrue(Array.isArray(digests));
      await client.cleanup(runId);
    } finally {
      await client.close();
    }
  },
});
