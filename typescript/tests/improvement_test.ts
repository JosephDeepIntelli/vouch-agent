/**
 * Gate 3 tests: adapter protocol validation (negative paths), the loopback
 * fixture adapter through the REAL subprocess transport, and the improvement
 * lifecycle — baseline freeze, sealed proposals, paired evaluation,
 * fail-closed verdicts, acceptance binding + approval invalidation, evidence
 * export/verify, rollback records, gate reservations and task-only refusal.
 */

import { assertEquals, assertThrows, assertTrue } from "./helpers.ts";
import {
  artifactsFromPayload,
  buildIdentity,
  encodeBase64,
  executionFromPayload,
  MAX_FRAME_BYTES,
  parseFrame,
  sanitizeDiagnostics,
  verifyIdentityEcho,
} from "../src/adapters/protocol.ts";
import { ProcessAdapterClient } from "../src/adapters/process_adapter.ts";
import {
  AdapterExecutionError,
  ApprovalInvalidatedError,
  ContractError,
  DigestMismatchError,
  GateDeniedError,
  MissingMeteringError,
  ProtocolFrameError,
} from "../src/contracts/common.ts";
import { digestBytes } from "../src/contracts/canonical.ts";
import {
  defaultFixturesPath,
  ImprovementFlow,
  SYNTHETIC_FIXTURE_PACK,
} from "../src/improvement/flow.ts";
import { candidateContentDigest, candidateFromDict } from "../src/improvement/contracts.ts";
import { ProjectWorkspace } from "../src/appservices/workspace.ts";
import { specFromDict } from "../src/contracts/project.ts";
import { verifyEvidencePackage } from "../src/improvement/evidence.ts";

function rmrf(path: string): void {
  try {
    Deno.removeSync(path, { recursive: true });
  } catch {
    // gone
  }
}

const MAIN_MODULE = new URL("../src/cli/main.ts", import.meta.url).href.replace("file://", "");

function improvementWorkspace(name: string): ProjectWorkspace {
  const dir = `/tmp/vowdo-ts-tests/${name}-${crypto.randomUUID().slice(0, 8)}`;
  Deno.mkdirSync(dir, { recursive: true });
  const spec = specFromDict({
    schemaVersion: "1",
    projectId: `proj-${name}`,
    name,
    workflows: [
      {
        schemaVersion: "1",
        workflowId: "W-C3",
        name: "Compare",
        mainObjective: "main",
        guardrails: [],
        locales: ["en"],
        markets: [],
      },
    ],
    owners: { "acceptance-owner": "alice-acceptance", "release-owner": "bob-release" },
    allowedChangeTypes: ["prompt-delta"],
    budget: { schemaVersion: "1", totalUsdCap: 5 },
    mode: "improvement",
    purpose: "",
  } as never);
  const workspace = ProjectWorkspace.create(dir, spec as never);
  Deno.writeTextFileSync(
    defaultFixturesPath(workspace),
    JSON.stringify(SYNTHETIC_FIXTURE_PACK, null, 2) + "\n",
  );
  return workspace;
}

// --- protocol validation --------------------------------------------------------

Deno.test({
  name: "protocol: frame validation rejects malformed, unknown, oversized frames",
  fn() {
    // valid frame parses
    const frame = parseFrame(
      '{"protocolVersion":"1","seq":0,"kind":"describe-request","runId":null,"payload":{}}',
    );
    assertEquals(frame.kind, "describe-request");
    // unknown version
    assertThrows(
      () =>
        parseFrame(
          '{"protocolVersion":"2","seq":0,"kind":"describe-request","runId":null,"payload":{}}',
        ),
      ProtocolFrameError,
    );
    // unknown kind
    assertThrows(
      () => parseFrame('{"protocolVersion":"1","seq":0,"kind":"nope","runId":null,"payload":{}}'),
      ProtocolFrameError,
    );
    // bad seq
    assertThrows(
      () =>
        parseFrame(
          '{"protocolVersion":"1","seq":-1,"kind":"describe-request","runId":null,"payload":{}}',
        ),
      ProtocolFrameError,
    );
    // run-scoped kind without runId
    assertThrows(
      () =>
        parseFrame(
          '{"protocolVersion":"1","seq":0,"kind":"execute-request","runId":null,"payload":{}}',
        ),
      ProtocolFrameError,
    );
    // non-object payload
    assertThrows(
      () =>
        parseFrame(
          '{"protocolVersion":"1","seq":0,"kind":"describe-request","runId":null,"payload":[1]}',
        ),
      ProtocolFrameError,
    );
    // malformed JSON
    assertThrows(() => parseFrame("{not json"), ProtocolFrameError);
    // oversized raw input refused before parse
    const big = new Uint8Array(MAX_FRAME_BYTES + 1);
    assertThrows(() => parseFrame(big), ProtocolFrameError);
  },
});

Deno.test({
  name: "protocol: metering separation and non-finite rejection",
  fn() {
    // missing usage → MissingMeteringError (unmeasured is never free)
    assertThrows(
      () => executionFromPayload({ ok: true, outputs: {}, mode: "fixture" }),
      MissingMeteringError,
    );
    // usage with no numeric quantity
    assertThrows(
      () => executionFromPayload({ ok: true, outputs: {}, usage: { note: "x" }, mode: "fixture" }),
      MissingMeteringError,
    );
    // non-finite metering rejected (v1.1 §2)
    const bad: Record<string, unknown> = { ok: true, outputs: {}, mode: "fixture" };
    bad["usage"] = { tokensScripted: Number.NaN };
    assertThrows(() => executionFromPayload(bad), ProtocolFrameError);
    // token counts are simulated quantities, costUsd only when real
    const execution = executionFromPayload({
      ok: true,
      outputs: {},
      usage: { tokensScripted: 10, elapsedMsMeasured: 5 },
      mode: "fixture",
    });
    assertEquals(execution.usage?.["costUsd"], undefined);
  },
});

Deno.test({
  name: "protocol: identity echo must be verbatim (no invented ancestry)",
  fn() {
    const identity = buildIdentity({
      runId: "eval_1",
      attemptId: "att_1",
      workflowId: "W-C3",
      caseId: "case-1",
      mode: "fixture",
    });
    verifyIdentityEcho(identity, { identity });
    // mismatched field
    assertThrows(
      () => verifyIdentityEcho(identity, { identity: { ...identity, caseId: "case-2" } }),
      ProtocolFrameError,
    );
    // echo ADDS a versionDigest the request never carried
    assertThrows(
      () =>
        verifyIdentityEcho(identity, {
          identity: { ...identity, versionDigest: "sha256:" + "a".repeat(64) },
        }),
      ProtocolFrameError,
      "adds",
    );
  },
});

Deno.test({
  name:
    "protocol: collect artifacts are digest-verified in-frame (host paths are not capabilities)",
  fn() {
    const bytes = new TextEncoder().encode("evidence-bytes");
    const digest = digestBytes(bytes);
    const good = artifactsFromPayload({
      artifacts: [{ digest, bytes: encodeBase64(bytes), kind: "evidence" }],
    });
    assertEquals(new TextDecoder().decode(good[0].bytes), "evidence-bytes");
    // digest mismatch
    assertThrows(
      () =>
        artifactsFromPayload({
          artifacts: [{
            digest,
            bytes: encodeBase64(new TextEncoder().encode("tampered")),
            kind: "evidence",
          }],
        }),
      ProtocolFrameError,
      "digest mismatch",
    );
    // host path is ignored — bytes are the only transfer form
    assertThrows(
      () =>
        artifactsFromPayload({
          artifacts: [{ digest, bytes: 42 as never, kind: "evidence" }],
        }),
      ProtocolFrameError,
    );
  },
});

Deno.test({
  name: "protocol: diagnostics redaction removes secret shapes",
  fn() {
    // Clearly SYNTHETIC, low-entropy DUMMY markers (secret scanners must
    // stay clean on this repo — no real key shapes) that still exercise
    // BOTH redaction rules: the secret key=value pattern and the long-blob
    // pattern ([A-Za-z0-9+/]{40,}).
    const dirty = "api_key=DUMMY-VALUE-0 token=DUMMY-VALUE-1 " +
      "blob=EXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE done";
    const clean = sanitizeDiagnostics(dirty);
    assertTrue(!clean.includes("DUMMY-VALUE-0"), clean);
    assertTrue(!clean.includes("EXAMPLEEXAMPLEEXAMPLE"), clean);
    assertTrue(clean.includes("[redacted"), clean);
  },
});

// --- loopback fixture adapter through the real subprocess transport ---------------

Deno.test({
  name: "adapter: loopback fixture adapter over the real subprocess protocol",
  async fn() {
    const workspace = improvementWorkspace("adapter-loopback");
    try {
      const fixturesPath = defaultFixturesPath(workspace);
      const client = new ProcessAdapterClient(
        [
          "deno",
          "run",
          "--no-prompt",
          "--allow-read",
          MAIN_MODULE,
          "__fixture-adapter",
          "--fixtures",
          fixturesPath,
        ],
        { label: "fixture-adapter", requestTimeoutS: 60, executeTimeoutS: 60 },
      );
      try {
        const descriptor = await client.describe();
        assertEquals(
          descriptor.adapterId,
          "vowdo-fixture-adapter/vowdo-improvement-fixtures-v1",
        );
        assertEquals(descriptor.enforcedModes, ["fixture"]);
        assertTrue(descriptor.workflows.includes("W-C3"));
        const runId = "eval_test_run_1";
        await client.prepare(runId, "fixture");
        const execution = await client.execute({
          runId,
          attemptId: "att_1",
          workflowId: "W-C3",
          caseInput: { caseId: "improve-normal-en" },
          mode: "fixture",
        });
        assertEquals(execution.ok, true);
        assertEquals(typeof execution.usage?.["tokensScripted"], "number");
        assertEquals(execution.outputs["caseId"], "improve-normal-en");
        // an unknown case is an ADAPTER-REPORTED failure (error frame), and
        // the client surfaces it as AdapterExecutionError — fail closed
        await assertRejectsType(
          () =>
            client.execute({
              runId,
              attemptId: "att_2",
              workflowId: "W-C3",
              caseInput: { caseId: "nope" },
              mode: "fixture",
            }),
          AdapterExecutionError,
        );
        const digests = await client.collect(runId);
        assertEquals(digests.length > 0, true);
        const artifacts = await client.collectArtifacts(runId);
        assertEquals(artifacts.every((a) => digestBytes(a.bytes) === a.digest), true);
        await client.cleanup(runId);
      } finally {
        await client.close();
      }
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

// --- improvement lifecycle ---------------------------------------------------------

Deno.test({
  name:
    "improvement: baseline freeze, sealed proposal, evaluation, acceptance, approval, release, rollback",
  async fn() {
    const workspace = improvementWorkspace("lifecycle");
    try {
      const flow = new ImprovementFlow(workspace, MAIN_MODULE);
      // Task-only refusal first
      // (covered separately below)

      flow.recordBaseline({
        versionId: "v0",
        sourceRef: "git:0000000",
        workflowId: "W-C3",
        minImprovement: 0.05,
        repeats: 1,
      });
      const proposal = flow.propose({
        delta: "tweak clarification prompt",
        changeType: "prompt-delta",
        rationale: "reduce over-clarification",
        workflowId: "W-C3",
        seal: true,
      });
      assertEquals(proposal.sealed, true);
      assertEquals(proposal.candidate.state, "sealed");

      // Development evaluation: both dev cases improve; no guardrail hits on
      // the dev split → evaluated with an accepted-direction comparison.
      const devRun = await flow.evaluate({
        candidateId: proposal.candidate.candidateId,
        workflowId: "W-C3",
      });
      assertEquals(devRun.verdict, "accepted", JSON.stringify(devRun.comparison));
      assertEquals(devRun.comparison!.completePairs, 2);

      // Final acceptance (the acceptance side owns this split)
      const acceptance = await flow.finalAcceptance({
        candidateId: proposal.candidate.candidateId,
        workflowId: "W-C3",
        owner: "alice-acceptance",
        evidenceOut: `${workspace.projectDir}/evidence-final`,
      });
      assertEquals(acceptance.verdict, "accepted");
      assertTrue(acceptance.decisionId !== null);
      // Evidence package verifies
      const manifest = verifyEvidencePackage(`${workspace.projectDir}/evidence-final`);
      assertEquals(manifest["manifestDigest"], acceptance.evidenceDigest);
      // Tampered evidence never verifies
      Deno.writeFileSync(
        `${workspace.projectDir}/evidence-final/evaluation-summary.json`,
        new TextEncoder().encode("tampered"),
      );
      assertThrows(
        () => verifyEvidencePackage(`${workspace.projectDir}/evidence-final`),
        DigestMismatchError,
      );

      // Approval re-verifies the binding and advances the state machine.
      // (The CLI additionally requires the workspace release-owner identity.)
      const approved = flow.controller.approve({
        candidateId: proposal.candidate.candidateId,
        approver: "bob-release",
      });
      assertEquals(approved.state, "approved");

      // Release + rollback records
      const release = flow.controller.recordRelease({
        candidateId: proposal.candidate.candidateId,
        deployedVersion: "v1-candidate",
        deployedBy: "bob-release",
      });
      const rolled = flow.controller.recordRollback(
        release.releaseId,
        "guardrail observed in production",
      );
      assertEquals(rolled.compensationStatus, "pending");
      const finalCandidate = candidateFromDict(
        workspace.store.load("candidate", proposal.candidate.candidateId)!,
      );
      assertEquals(finalCandidate.state, "rolled-back");
      // Content digest NEVER changed across the whole lifecycle
      assertEquals(candidateContentDigest(finalCandidate), proposal.contentDigest);
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "improvement: guardrail violation rejects, never averages away",
  async fn() {
    const workspace = improvementWorkspace("guardrail");
    try {
      // Pack where the candidate hits the hard guardrail on the dev split
      const pack = JSON.parse(JSON.stringify(SYNTHETIC_FIXTURE_PACK));
      // deno-lint-ignore no-explicit-any
      pack.scenarios = (pack.scenarios as any[]).map((s) => {
        if (s.scenarioId === "improve-normal-en") {
          return {
            ...s,
            candidate: { ...s.candidate, guardrails: { "no-hallucinated-citations": false } },
          };
        }
        return s;
      });
      Deno.writeTextFileSync(defaultFixturesPath(workspace), JSON.stringify(pack));
      const flow = new ImprovementFlow(workspace, MAIN_MODULE);
      flow.recordBaseline({
        versionId: "v0",
        sourceRef: "git:0000000",
        workflowId: "W-C3",
        minImprovement: 0,
      });
      const proposal = flow.propose({
        delta: "faster but invents citations",
        changeType: "prompt-delta",
        rationale: "test guardrail",
        workflowId: "W-C3",
        seal: true,
      });
      const run = await flow.evaluate({
        candidateId: proposal.candidate.candidateId,
        workflowId: "W-C3",
      });
      assertEquals(run.verdict, "rejected");
      assertEquals(run.comparison!.hardViolations.length > 0, true);
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "improvement: approval invalidation when a bound digest changes",
  async fn() {
    const workspace = improvementWorkspace("invalidation");
    try {
      const flow = new ImprovementFlow(workspace, MAIN_MODULE);
      flow.recordBaseline({
        versionId: "v0",
        sourceRef: "git:0000000",
        workflowId: "W-C3",
        minImprovement: 0,
      });
      const proposal = flow.propose({
        delta: "original delta",
        changeType: "prompt-delta",
        rationale: "r",
        workflowId: "W-C3",
        seal: true,
      });
      const acceptance = await flow.finalAcceptance({
        candidateId: proposal.candidate.candidateId,
        workflowId: "W-C3",
        owner: "alice-acceptance",
        evidenceOut: `${workspace.projectDir}/evidence`,
      });
      assertEquals(acceptance.verdict, "accepted");
      // Tamper the sealed candidate's content behind the controller's back
      const record = workspace.store.load("candidate", proposal.candidate.candidateId)!;
      record["delta"] = "sneaky edited delta";
      workspace.store.save("candidate", proposal.candidate.candidateId, record);
      assertThrows(
        () =>
          flow.controller.approve({
            candidateId: proposal.candidate.candidateId,
            approver: "bob-release",
          }),
        ApprovalInvalidatedError,
        "binding",
      );
      // The candidate is now invalidated (audit trail preserved)
      const invalidated = candidateFromDict(
        workspace.store.load("candidate", proposal.candidate.candidateId)!,
      );
      assertEquals(invalidated.state, "invalidated");
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "improvement: task-only workspace refuses improvement commands (no invented owners)",
  fn() {
    const dir = `/tmp/vowdo-ts-tests/taskonly-refuse-${crypto.randomUUID().slice(0, 8)}`;
    Deno.mkdirSync(dir, { recursive: true });
    const spec = specFromDict({
      schemaVersion: "1",
      projectId: "p",
      name: "p",
      workflows: [],
      owners: {},
      allowedChangeTypes: [],
      budget: { schemaVersion: "1", totalUsdCap: 1 },
      mode: "task-only",
      purpose: "",
    } as never);
    const workspace = ProjectWorkspace.create(dir, spec as never);
    try {
      const flow = new ImprovementFlow(workspace, MAIN_MODULE);
      assertThrows(
        () => flow.recordBaseline({ versionId: "v0", sourceRef: "x", workflowId: "W" }),
        ContractError,
        "task-only",
      );
    } finally {
      workspace.close();
      rmrf(dir);
    }
  },
});

Deno.test({
  name: "improvement: unconfigured runners refuse honestly (no invented commands)",
  fn() {
    const workspace = improvementWorkspace("runners-refuse");
    try {
      const flow = new ImprovementFlow(workspace, MAIN_MODULE);
      assertThrows(
        () => flow.buildAdapter("runner:choose"),
        ContractError,
        "not configured",
      );
      assertThrows(
        () => flow.buildAdapter("runner:visibility"),
        ContractError,
        "not configured",
      );
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "improvement: gate refuses non-eval runs, unimported cases, oversized reservations",
  async fn() {
    const workspace = improvementWorkspace("gate");
    try {
      const flow = new ImprovementFlow(workspace, MAIN_MODULE);
      const cases = flow.importFixtureCases(defaultFixturesPath(workspace), "development");
      const { authorizeAdapterAttempt } = await import("../src/improvement/controller.ts");
      // non-eval run refused
      assertThrows(
        () =>
          authorizeAdapterAttempt(workspace, {
            runId: "run_123",
            caseIds: cases.map((c) => c.caseId),
            reserveUsd: 0.05,
          }),
        GateDeniedError,
        "eval_",
      );
      // unimported case refused
      assertThrows(
        () =>
          authorizeAdapterAttempt(workspace, {
            runId: "eval_ok_1",
            caseIds: ["never-imported"],
            reserveUsd: 0.05,
          }),
        GateDeniedError,
        "never imported",
      );
      // oversized reservation refused
      assertThrows(
        () =>
          authorizeAdapterAttempt(workspace, {
            runId: "eval_ok_2",
            caseIds: cases.map((c) => c.caseId),
            reserveUsd: 0.9,
          }),
        GateDeniedError,
        "cap",
      );
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

async function assertRejectsType(
  fn: () => Promise<unknown>,
  type: new (...args: never[]) => unknown,
): Promise<void> {
  try {
    await fn();
  } catch (exc) {
    assertTrue(exc instanceof (type as new (...a: never[]) => unknown), `wrong error: ${exc}`);
    return;
  }
  throw new Error("expected rejection");
}
