/**
 * The in-repo TS fixture adapter — a scripted, never-networked runner that
 * speaks adapter protocol v1/v1.1 and serves IMPROVEMENT-semantics cases:
 * each case declares paired baseline/candidate outcomes (ok, metric,
 * guardrails, usage) from a synthetic JSON pack. Deterministic; no model, no
 * credentials. Distinct from the Python journey-corpus fixture adapter
 * (recorded gap: the Choose-journey corpus is served by the real Choose
 * runner through this same protocol).
 *
 * Run as: <vouch> __fixture-adapter --fixtures <pack.json> [--workspace DIR]
 */

import { canonicalJson, digestBytes, digestOf } from "../contracts/canonical.ts";
import {
  encodeBase64,
  type Frame,
  frameErrorPayload,
  type FrameKind,
  frameToJsonLine,
  parseFrame,
  SequenceTracker,
} from "./protocol.ts";

interface ScenarioSide {
  ok: boolean;
  metric?: number | null;
  guardrails?: Record<string, boolean>;
  usage?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  failStatus?: string; // "failed" | "timeout" (measured-bad simulation)
}

interface Scenario {
  scenarioId: string;
  workflowId: string;
  split?: "development" | "selection-validation" | "final-acceptance";
  baseline: ScenarioSide;
  candidate: ScenarioSide;
}

interface FixturePack {
  schemaVersion: number;
  synthetic: boolean;
  packId: string;
  guardrailFamilies?: Array<{ familyId: string; fixtureIds: string[] }>;
  scenarios: Scenario[];
}

export function loadFixturePack(path: string): FixturePack {
  const data = JSON.parse(Deno.readTextFileSync(path));
  if (data["schemaVersion"] !== 1) {
    throw new Error(`fixture pack ${path}: unsupported schemaVersion ${data["schemaVersion"]}`);
  }
  if (data["synthetic"] !== true) {
    throw new Error(`fixture pack ${path}: must be marked synthetic`);
  }
  const scenarios = (data["scenarios"] ?? []).map((s: Record<string, unknown>) => ({
    scenarioId: String(s["scenarioId"]),
    workflowId: String(s["workflowId"]),
    split: s["split"],
    baseline: side(s, "baseline"),
    candidate: side(s, "candidate"),
  }));
  for (const scenario of scenarios) {
    if (scenario.baseline.usage === undefined || scenario.candidate.usage === undefined) {
      throw new Error(`scenario ${scenario.scenarioId}: both sides must declare usage metering`);
    }
  }
  return {
    schemaVersion: 1,
    synthetic: true,
    packId: String(data["packId"] ?? "unnamed"),
    guardrailFamilies: (data["guardrailFamilies"] ?? []) as FixturePack["guardrailFamilies"],
    scenarios,
  };
}

function side(scenario: Record<string, unknown>, which: "baseline" | "candidate"): ScenarioSide {
  const raw = scenario[which];
  if (typeof raw !== "object" || raw === null) {
    throw new Error(`scenario ${String(scenario["scenarioId"])}: missing ${which} side`);
  }
  const s = raw as Record<string, unknown>;
  return {
    ok: Boolean(s["ok"]),
    metric: (s["metric"] as number | null | undefined) ?? null,
    guardrails: (s["guardrails"] as Record<string, boolean> | undefined) ?? {},
    usage: s["usage"] as Record<string, unknown>,
    outputs: (s["outputs"] as Record<string, unknown> | undefined) ?? {},
    failStatus: s["failStatus"] as string | undefined,
  };
}

/** Deterministic metering derived from the outputs (tokens ≈ chars/4). */
function deterministicMetering(scenario: ScenarioSide): Record<string, unknown> {
  return {
    ...scenario.usage,
    tokensScripted: (scenario.usage?.["tokensScripted"] as number | undefined) ??
      Math.floor(canonicalJson(scenario.outputs ?? {}).length / 4 + 8),
    elapsedMsMeasured: (scenario.usage?.["elapsedMsMeasured"] as number | undefined) ?? 42,
  };
}

async function serveAdapter(fixturesPath: string): Promise<void> {
  const pack = loadFixturePack(fixturesPath);
  const recvSeq = new SequenceTracker();
  const sendSeq = new SequenceTracker();
  const prepared = new Set<string>();
  const artifacts = new Map<string, { bytes: Uint8Array; kind: string }>();

  const write = (frame: Omit<Frame, "protocolVersion">) => {
    const line = frameToJsonLine({ ...frame, protocolVersion: "1" });
    Deno.stdout.writeSync(new TextEncoder().encode(line + "\n"));
  };

  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of Deno.stdin.readable) {
    buffer += decoder.decode(chunk, { stream: true });
    let newline: number;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (line.trim().length === 0) continue;
      let frame: Frame;
      try {
        frame = parseFrame(line);
      } catch (exc) {
        write({
          seq: sendSeq.next(),
          kind: "error",
          runId: null,
          payload: frameErrorPayload("vouch/protocol-frame", `bad frame: ${exc}`),
        });
        continue;
      }
      try {
        recvSeq.observe(frame);
        await handleFrame(frame);
      } catch (exc) {
        write({
          seq: sendSeq.next(),
          kind: "error",
          runId: frame.runId,
          payload: frameErrorPayload("vouch/adapter-execution", String(exc)),
        });
      }
    }
  }

  async function handleFrame(frame: Frame): Promise<void> {
    const reply = (payload: Record<string, unknown>) => {
      write({ seq: sendSeq.next(), kind: responseOf(frame.kind), runId: frame.runId, payload });
    };
    if (frame.kind === "describe-request") {
      reply({
        adapterId: `vouch-fixture-adapter/${pack.packId}`,
        workflows: [...new Set(pack.scenarios.map((s) => s.workflowId))].sort(),
        actions: ["run-adapter-attempt"],
        enforcedModes: ["fixture"],
        notes:
          "SYNTHETIC scripted adapter: deterministic improvement-semantics scenarios; not evidence " +
          "of model or product improvement",
        evaluationCases: pack.scenarios.map((s) => ({
          caseId: s.scenarioId,
          workflowId: s.workflowId,
          split: s.split ?? "development",
        })),
        guardrailFamilies: pack.guardrailFamilies ?? [],
      });
      return;
    }
    if (frame.kind === "prepare-request") {
      prepared.add(frame.runId!);
      reply({ ok: true });
      return;
    }
    if (frame.kind === "execute-request" || frame.kind === "apply-config-request") {
      const runId = frame.runId!;
      if (!prepared.has(runId)) {
        throw new Error(`execute before prepare for run ${runId}`);
      }
      const caseInput = (frame.payload["caseInput"] ?? {}) as Record<string, unknown>;
      const caseId = String(caseInput["caseId"] ?? "");
      const scenario = pack.scenarios.find((s) => s.scenarioId === caseId);
      if (scenario === undefined) {
        throw new Error(`unknown case ${JSON.stringify(caseId)} in pack ${pack.packId}`);
      }
      // Baseline requests carry no versionDigest; candidate requests carry
      // the sealed candidate's delta digest.
      const identity = (frame.payload["identity"] ?? {}) as Record<string, string>;
      const isCandidate = identity["versionDigest"] !== undefined &&
        identity["versionDigest"] !== "";
      const side = isCandidate ? scenario.candidate : scenario.baseline;
      const usage = deterministicMetering(side);
      if (side.failStatus === "timeout") {
        // Simulate a measured timeout: the adapter simply never answers.
        return;
      }
      const outputs: Record<string, unknown> = {
        ...(side.outputs ?? {}),
        caseId,
        side: isCandidate ? "candidate" : "baseline",
        metric: side.metric ?? null,
        guardrails: side.guardrails ?? {},
      };
      // Seal an evidence artifact for collect.
      const evidenceBytes = new TextEncoder().encode(
        canonicalJson({
          caseId,
          runId,
          side: isCandidate ? "candidate" : "baseline",
          outputs,
          ok: side.ok,
        }),
      );
      const digest = digestBytes(evidenceBytes);
      artifacts.set(digest, { bytes: evidenceBytes, kind: "evidence" });
      reply({
        ok: side.ok,
        outputs,
        evidenceRefs: [digest],
        toolEvents: [],
        usage,
        error: side.ok ? null : "scenario scripted failure",
        mode: "fixture",
        runnerVersion: "vouch-fixture-adapter/1",
        identity: identity,
      });
      return;
    }
    if (frame.kind === "collect-request") {
      const runId = frame.runId!;
      if (!prepared.has(runId)) {
        throw new Error(`collect before prepare for run ${runId}`);
      }
      const mine = [...artifacts.entries()];
      const withArtifacts = Boolean(frame.payload["withArtifacts"]);
      reply(
        withArtifacts
          ? {
            digests: mine.map(([d]) => d),
            artifacts: mine.map(([d, a]) => ({
              digest: d,
              bytes: encodeBase64(a.bytes),
              kind: a.kind,
            })),
          }
          : { digests: mine.map(([d]) => d) },
      );
      return;
    }
    if (frame.kind === "cleanup-request") {
      prepared.delete(frame.runId!);
      reply({ ok: true });
      return;
    }
    throw new Error(`client sent a response/unexpected frame ${frame.kind}`);
  }
}

function responseOf(kind: string): FrameKind {
  const map: Record<string, FrameKind> = {
    "describe-request": "describe-response",
    "prepare-request": "prepare-response",
    "execute-request": "execute-response",
    "apply-config-request": "apply-config-response",
    "collect-request": "collect-response",
    "cleanup-request": "cleanup-response",
  };
  const reply = map[kind];
  if (reply === undefined) throw new Error(`no response kind for ${kind}`);
  return reply;
}

/** Digest of the pack (identity of the fixture corpus an evaluation used). */
export function fixturePackDigest(pack: FixturePack): string {
  return digestOf({
    packId: pack.packId,
    scenarioIds: pack.scenarios.map((s) => s.scenarioId),
  });
}

export async function fixtureAdapterMain(args: string[]): Promise<void> {
  const fixtures = valueAfter(args, "--fixtures");
  if (fixtures === undefined) {
    console.error("usage: __fixture-adapter --fixtures <pack.json>");
    Deno.exit(2);
  }
  await serveAdapter(fixtures);
}

function valueAfter(argv: string[], name: string): string | undefined {
  const index = argv.indexOf(name);
  if (index >= 0 && index + 1 < argv.length) return argv[index + 1];
  return undefined;
}

if (import.meta.main) {
  await fixtureAdapterMain(Deno.args);
}
