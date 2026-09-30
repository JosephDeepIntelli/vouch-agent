/**
 * Vouch CLI (native TypeScript/Deno) — headless, flag-driven, no prompts.
 * Mirrors the Python reference command surface for the migrated journeys.
 * Every failure prints `fail-closed [<code>]: <message>` on stderr and
 * exits non-zero.
 */

import { ContractError, VouchError } from "../contracts/common.ts";
import { type ProjectSpecData, specFromDict } from "../contracts/project.ts";
import { isPlainObject } from "../contracts/canonical.ts";
import { isTerminal } from "../contracts/tasks.ts";
import { ExecutionService } from "../appservices/execution.ts";
import { exportRun, verifyNativeExport } from "../appservices/native_export.ts";
import { importPythonWorkspace } from "../appservices/import_python.ts";
import { costReport, resumeState } from "../appservices/reporting.ts";
import { ProjectWorkspace } from "../appservices/workspace.ts";
import { runWorkerMain } from "../appservices/run_worker.ts";
import {
  defaultFixturesPath,
  ImprovementFlow,
  loadRunnerConfig,
  saveRunnerConfig,
  SYNTHETIC_FIXTURE_PACK,
} from "../improvement/flow.ts";
import { fixtureAdapterMain } from "../adapters/fixture_adapter.ts";
import { verifyEvidencePackage } from "../improvement/evidence.ts";
import { candidateContentDigest } from "../improvement/contracts.ts";
import { VERSION } from "../version.ts";
import { controllerChildSpawn } from "../runtime/spawn.ts";

const SAMPLE_LEFT = `sku,name,price_usd,stock
AUR-001,Aurora Gooseneck Kettle,95.00,12
AUR-002,Aurora Travel Kettle,59.00,30
BRW-010,Oakline Pour-Over Brewer,42.00,8
MSC-100,Morning Scale,25.00,0
SLM-007,Summer Linen Set,120.00,4
`;

const SAMPLE_RIGHT = `sku,name,price_usd,stock,lead_days
AUR-001,Aurora Gooseneck Kettle,95.00,12,3
AUR-002,Aurora Travel Kettle,54.00,30,5
BRW-010,Oakline Pour-Over Brewer,42.00,8,4
MSC-100,Morning Scale,25.00,0,21
DNR-030,Dawn Mug Set,18.00,60,2
DNR-030,Dawn Mug Set,18.00,55,2
`;

const SAMPLE_README = `SYNTHETIC sample materials for the vouch native journey.
Every row is invented data; no real products, suppliers or prices.

  left  : "产品 目录.csv" (a product catalog; the filename deliberately
                           contains a space and Chinese characters)
  right : "supplier feed.csv" (a supplier feed, with a space too)

join key: sku

What the pair demonstrates:
- AUR-002: price changed (59.00 -> 54.00)
- SLM-007: present left only (missing from the supplier feed)
- DNR-030: present right only, TWICE (an ambiguous duplicate key)
- lead_days: a right-only column (reported as a schema difference)

Run (from the directory holding these files):
  vouch init --task-only --project ./vouch-work --purpose "supplier sync"
  vouch reconcile --project ./vouch-work \\
      --left "产品 目录.csv" --right "supplier feed.csv" --join-key sku
  vouch runs --project ./vouch-work
  vouch run-status --project ./vouch-work <run-id>
  vouch export-run --project ./vouch-work <run-id> --out ./vouch-work/export
  vouch verify-export ./vouch-work/export
`;

interface Args {
  command: string;
  flags: Map<string, string[]>;
  positionals: string[];
}

function parseArgs(argv: string[]): Args {
  const flags = new Map<string, string[]>();
  const positionals: string[] = [];
  let command: string | null = null;
  let i = 0;
  const valueFlags = new Set([
    "--project",
    "-p",
    "--out",
    "--left",
    "--right",
    "--join-key",
    "--delimiter",
    "--ignore-column",
    "--goal",
    "--input",
    "--budget",
    "--max-steps",
    "--provider",
    "--script-file",
    "--require-field",
    "--expect-text",
    "--purpose",
    "--workflow",
    "-w",
    "--cap",
    "--owners",
    "-o",
    "--guardrail",
    "-g",
    "--change-type",
    "--name",
    "--project-id",
    "--worker-path",
    "--from",
    "--to",
    "--note",
    "--reason",
  ]);
  while (i < argv.length) {
    const arg = argv[i];
    if (command === null && !arg.startsWith("-")) {
      command = arg;
      i += 1;
      continue;
    }
    if (arg.startsWith("-")) {
      const eq = arg.indexOf("=");
      let name = arg;
      let value: string | null = null;
      if (eq >= 0) {
        name = arg.slice(0, eq);
        value = arg.slice(eq + 1);
      }
      if (
        value === null && valueFlags.has(name) && i + 1 < argv.length &&
        !argv[i + 1].startsWith("--")
      ) {
        value = argv[i + 1];
        i += 1;
      }
      const list = flags.get(name) ?? [];
      if (value !== null) list.push(value);
      else list.push("");
      flags.set(name, list);
      i += 1;
      continue;
    }
    positionals.push(arg);
    i += 1;
  }
  return { command: command ?? "", flags, positionals };
}

function flag(args: Args, ...names: string[]): string | undefined {
  for (const name of names) {
    const values = args.flags.get(name);
    if (values !== undefined) {
      const found = values.find((v) => v !== "");
      if (found !== undefined) return found;
      return values[values.length - 1];
    }
  }
  return undefined;
}

function flagList(args: Args, ...names: string[]): string[] {
  for (const name of names) {
    const values = args.flags.get(name);
    if (values !== undefined) return values.filter((v) => v !== "");
  }
  return [];
}

function hasFlag(args: Args, ...names: string[]): boolean {
  return names.some((n) => args.flags.has(n));
}

function fail(message: string, code = "vouch/usage"): never {
  console.error(`fail-closed [${code}]: ${message}`);
  Deno.exit(1);
}

function failClosed(exc: unknown): never {
  if (exc instanceof VouchError) {
    console.error(`fail-closed [${exc.code}]: ${exc.message}`);
    Deno.exit(1);
  }
  console.error(`fail-closed [vouch/internal]: ${exc}`);
  Deno.exit(1);
}

function short(digest: string): string {
  return digest.slice(0, 19) + "…";
}

function openWorkspace(project: string): ProjectWorkspace {
  try {
    return ProjectWorkspace.open(project);
  } catch (exc) {
    return failClosed(exc);
  }
}

function requireImprovementMode(workspace: ProjectWorkspace): void {
  if (workspace.spec.mode !== "improvement") {
    fail(
      "improvement and approval commands are refused until a workspace is re-initialized in " +
        "default (improvement) mode with workflows and owners — task-only workspaces never " +
        "invent owner identities",
      "vouch/contract",
    );
  }
}

const HELP = `vouch-agent ${VERSION} (native TypeScript/Deno; Python reference stays separate)

usage: vouch <command> [flags]

informational
  version                          package + runtime version
  modes                            run modes and what this build ACTUALLY supports

project lifecycle
  init --project DIR [--task-only | --workflow W ... --owners role=name ...]
  import-python --from PY_DIR --to NEW_DIR    explicit read-only Python migration
  status --project DIR              compact project + recovery overview

native task journey
  examples --out DIR                write SYNTHETIC sample CSVs
  reconcile --project DIR --left L --right R --join-key K [--out DIR]
  runs --project DIR                list saved runs
  run-status --project DIR RUN_ID   status + recovery classification
  export-run --project DIR RUN_ID --out DIR
  verify-export DIR                 byte-level export verification
  run --project DIR --goal G --input name=path.json [--require-field F] ...
  run-detach --project DIR ...      submit, then a detached worker executes
  run-cancel --project DIR RUN_ID
  run-resume --project DIR RUN_ID [--note TEXT]
  run-pause --project DIR RUN_ID

Every command accepts --project (default .). Deterministic local mode: no
network, no secret environment, no implicit permission prompts.`;

function readJsonInput(token: string): [string, unknown] {
  const eq = token.indexOf("=");
  if (eq <= 0 || eq === token.length - 1) {
    fail(`--input expects name=path.json, got ${JSON.stringify(token)}`);
  }
  const name = token.slice(0, eq);
  const path = token.slice(eq + 1);
  try {
    return [name, JSON.parse(Deno.readTextFileSync(path))];
  } catch (exc) {
    fail(`cannot read input ${JSON.stringify(name)}: ${exc}`);
  }
}

function completionConditions(args: Args): Array<Record<string, unknown>> {
  const conditions: Array<Record<string, unknown>> = [];
  const requireField = flagList(args, "--require-field");
  if (requireField.length > 0) {
    conditions.push({
      type: "artifact_schema",
      schema: {
        type: "object",
        required: requireField,
        properties: Object.fromEntries(requireField.map((f) => [f, { type: "string" }])),
      },
    });
  }
  const expectText = flag(args, "--expect-text");
  if (expectText !== undefined && expectText !== "") {
    conditions.push({ type: "output_contains", contains: expectText });
  }
  return conditions;
}

async function main(): Promise<void> {
  const args = parseArgs(Deno.args);

  if (args.command === "__fixture-adapter") {
    await fixtureAdapterMain(Deno.args.slice(1));
    return;
  }

  if (args.command === "__run-worker") {
    await runWorkerMain(args.positionals, {
      workerPath: flag(args, "--worker-path"),
    });
    return;
  }

  switch (args.command) {
    case "":
    case "help":
    case "--help":
      console.log(HELP);
      return;
    case "version":
      console.log(`vouch-agent-ts ${VERSION} (deno-native; Python reference 0.1.0rc1 separate)`);
      return;
    case "modes": {
      console.log("fixture: live-calls=false");
      console.log("offline-evaluation: live-calls=false");
      console.log("authorized-live: live-calls=true");
      console.log("release support: fixture (deterministic offline) SUPPORTED");
      console.log(
        "release support: offline-evaluation / authorized-live NOT SUPPORTED in this preview — " +
          "no live model execution is implemented or verified",
      );
      return;
    }
    case "examples": {
      const out = flag(args, "--out");
      if (out === undefined) fail("--out is required");
      Deno.mkdirSync(out, { recursive: true });
      Deno.writeTextFileSync(`${out}/产品 目录.csv`, SAMPLE_LEFT);
      Deno.writeTextFileSync(`${out}/supplier feed.csv`, SAMPLE_RIGHT);
      Deno.writeTextFileSync(`${out}/README-examples.txt`, SAMPLE_README);
      console.log(`wrote: ${out}/产品 目录.csv`);
      console.log(`wrote: ${out}/supplier feed.csv`);
      console.log(`wrote: ${out}/README-examples.txt`);
      console.log("SYNTHETIC: invented data; see README-examples.txt for the journey");
      return;
    }
    case "verify-export": {
      const dir = args.positionals[0];
      if (dir === undefined) fail("verify-export takes an export directory argument");
      try {
        const manifest = verifyNativeExport(dir);
        const artifacts = (manifest["artifacts"] as unknown[]) ?? [];
        console.log(
          `verified export at ${dir}: ${artifacts.length} artifact(s) match their digests`,
        );
        for (const artifact of artifacts) {
          if (isPlainObject(artifact)) {
            console.log(`  ${artifact["file"]}: ${short(String(artifact["digest"]))}`);
          }
        }
      } catch (exc) {
        failClosed(exc);
      }
      return;
    }
    case "init":
      cmdInit(args);
      return;
    case "import-python": {
      const from = flag(args, "--from");
      const to = flag(args, "--to");
      if (from === undefined || to === undefined) fail("--from and --to are required");
      try {
        const report = importPythonWorkspace({ sourceProjectDir: from, targetProjectDir: to });
        console.log(
          `imported ${report.recordsImported} record(s), ${report.runsImported} run(s), ` +
            `${report.artifactsCopied} artifact(s) from ${report.source}`,
        );
        console.log(`target: ${report.target} (TS-namespaced storage; source untouched)`);
        if (report.digestRemaps.length > 0) {
          console.log(`digest remaps: ${report.digestRemaps.length} (integral floats → integers)`);
        }
        if (report.transformations.length > 0) {
          console.log(`numeric transformations: ${report.transformations.length}`);
        }
      } catch (exc) {
        failClosed(exc);
      }
      return;
    }
    case "reconcile":
      await cmdReconcile(args);
      return;
    case "runs":
      cmdRuns(args);
      return;
    case "run-status":
      cmdRunStatus(args);
      return;
    case "export-run": {
      const runId = args.positionals[0];
      const out = flag(args, "--out");
      const project = flag(args, "--project", "-p") ?? ".";
      if (runId === undefined) fail("export-run takes a run id argument");
      if (out === undefined) fail("--out is required");
      const workspace = openWorkspace(project);
      try {
        const manifest = exportRun(workspace, runId, out);
        console.log(`exported: ${manifest}`);
      } catch (exc) {
        failClosed(exc);
      } finally {
        workspace.close();
      }
      return;
    }
    case "run":
      await cmdRun(args);
      return;
    case "run-detach":
      await cmdRunDetach(args);
      return;
    case "run-cancel": {
      const runId = args.positionals[0];
      if (runId === undefined) fail("run-cancel takes a run id argument");
      const project = flag(args, "--project", "-p") ?? ".";
      const workspace = openWorkspace(project);
      try {
        const service = new ExecutionService(workspace, {
          workerPath: flag(args, "--worker-path"),
        });
        const run = service.cancel(runId, flag(args, "--reason") ?? "cancelled by operator");
        if (run !== null) console.log(`run ${runId}: ${run.status}`);
        else fail(`unknown run ${JSON.stringify(runId)}`);
      } catch (exc) {
        failClosed(exc);
      } finally {
        workspace.close();
      }
      return;
    }
    case "run-pause": {
      const runId = args.positionals[0];
      if (runId === undefined) fail("run-pause takes a run id argument");
      const project = flag(args, "--project", "-p") ?? ".";
      const workspace = openWorkspace(project);
      try {
        const run = await new ExecutionService(workspace).pause(runId);
        if (run !== null) console.log(`run ${runId}: ${run.status}`);
        else fail(`unknown run ${JSON.stringify(runId)}`);
      } catch (exc) {
        failClosed(exc);
      } finally {
        workspace.close();
      }
      return;
    }
    case "run-resume": {
      const runId = args.positionals[0];
      if (runId === undefined) fail("run-resume takes a run id argument");
      const project = flag(args, "--project", "-p") ?? ".";
      const workspace = openWorkspace(project);
      try {
        const service = new ExecutionService(workspace, {
          workerPath: flag(args, "--worker-path"),
        });
        const outcome = await service.resume(runId, flag(args, "--note") ?? "");
        console.log(`run: ${outcome.runId} status: ${outcome.status}`);
        if (outcome.error !== null) console.error(`error: ${outcome.error}`);
        if (isTerminal(outcome.status) && outcome.status !== "completed") Deno.exit(1);
      } catch (exc) {
        failClosed(exc);
      } finally {
        workspace.close();
      }
      return;
    }
    case "improve": {
      await cmdImprove(args);
      return;
    }
    case "runners": {
      cmdRunners(args);
      return;
    }
    case "verify-evidence": {
      const dir = args.positionals[0];
      if (dir === undefined) fail("verify-evidence takes an evidence package directory");
      try {
        const manifest = verifyEvidencePackage(dir);
        console.log(`verified evidence package at ${dir}: verdict=${manifest["verdict"]}`);
        console.log(`  manifest digest: ${manifest["manifestDigest"]}`);
        const artifacts = manifest["artifacts"] as unknown[];
        console.log(`  ${artifacts.length} artifact(s) match their digests`);
      } catch (exc) {
        failClosed(exc);
      }
      return;
    }
    case "status": {
      const project = flag(args, "--project", "-p") ?? ".";
      const workspace = openWorkspace(project);
      try {
        const costs = costReport(workspace);
        const resume = resumeState(workspace);
        const spec = workspace.spec;
        console.log(`project: ${spec.projectId} (${spec.name})`);
        console.log(
          `workflows: ${spec.workflows.map((w) => w.workflowId).join(", ") || "(task-only)"}`,
        );
        console.log(
          `budget: cap $${costs.totalCapUsd.toFixed(2)} measured $${
            costs.measuredUsd.toFixed(4)
          } ` +
            `reserved $${costs.outstandingReservedUsd.toFixed(4)} remaining $${
              costs.remainingUsd.toFixed(4)
            }`,
        );
        console.log(`runs: ${new ExecutionService(workspace).runs().length}`);
        if (resume.clean) {
          console.log("recovery: clean (no open reservations, nothing needs reconciliation)");
        } else {
          console.error(
            `recovery: ${resume.openReservations} open reservation(s), ` +
              `${resume.needsReconciliation.length} subject(s) need reconciliation`,
          );
        }
      } finally {
        workspace.close();
      }
      return;
    }
    default:
      fail(
        `unknown command ${JSON.stringify(args.command)}; run 'vouch help' for the command list`,
      );
  }
}

function cmdInit(args: Args): void {
  const project = flag(args, "--project", "-p");
  if (project === undefined) fail("--project is required");
  const taskOnly = hasFlag(args, "--task-only");
  const purpose = flag(args, "--purpose") ?? "";
  const cap = Number(flag(args, "--cap") ?? "5");
  if (!Number.isFinite(cap) || cap < 0) fail("--cap must be a number >= 0");
  const name = flag(args, "--name");
  const projectId = flag(args, "--project-id");

  const dirName = project.split("/").filter(Boolean).pop() ?? "vouch-work";
  if (taskOnly) {
    if (hasFlag(args, "--workflow", "-w") || hasFlag(args, "--owners", "-o")) {
      fail(
        "--task-only takes no --workflow/--owners; improvement configuration belongs in a " +
          "default-mode workspace",
      );
    }
    const spec = specFromDict({
      schemaVersion: "1",
      projectId: projectId ?? `proj-${dirName.toLowerCase()}`,
      name: name ?? dirName,
      workflows: [],
      owners: {},
      allowedChangeTypes: [],
      budget: { schemaVersion: "1", totalUsdCap: cap },
      mode: "task-only",
      purpose,
    } as never);
    try {
      ProjectWorkspace.create(project, spec as unknown as ProjectSpecData);
    } catch (exc) {
      failClosed(exc);
    }
    console.log(`initialized ${project}/.vouch`);
    console.log(`project: ${spec.projectId} (${spec.name}) mode: task-only`);
    if (purpose !== "") console.log(`purpose: ${purpose}`);
    console.log(
      `reservation cap $${spec.budget.totalUsdCap.toFixed(2)} (local task reservations only; ` +
        `task-only work is never paid)`,
    );
    console.log(
      "native journey: vouch examples --out samples && vouch reconcile --project . " +
        "--left samples/… --right samples/… --join-key sku",
    );
    console.log(
      "improvement commands are refused in this workspace until it is re-initialized in default " +
        "mode with workflows and owners",
    );
    return;
  }
  const workflows = flagList(args, "--workflow", "-w");
  if (workflows.length === 0) {
    fail(
      "pass --task-only for the native journey, or at least one --workflow for improvement mode",
    );
  }
  const ownerTokens = flagList(args, "--owners", "-o");
  const owners: Record<string, string> = {};
  for (const token of ownerTokens) {
    const eq = token.indexOf("=");
    if (eq <= 0) fail(`--owners expects role=name, got ${JSON.stringify(token)}`);
    owners[token.slice(0, eq).trim()] = token.slice(eq + 1).trim();
  }
  for (const role of ["acceptance-owner", "release-owner"]) {
    if (!(role in owners)) {
      fail(`--owners must define ${role}; ownership is never implicit`);
    }
  }
  const declarations = workflows.map((token) => {
    const eq = token.indexOf("=");
    const workflowId = eq > 0 ? token.slice(0, eq) : token;
    const displayName = eq > 0 ? token.slice(eq + 1) : workflowId;
    return {
      schemaVersion: "1",
      workflowId,
      name: displayName,
      mainObjective: "recordedClaims",
      guardrails: flagList(args, "--guardrail", "-g"),
      locales: ["en"],
      markets: [],
    };
  });
  const spec = specFromDict({
    schemaVersion: "1",
    projectId: projectId ?? `proj-${dirName.toLowerCase()}`,
    name: name ?? dirName,
    workflows: declarations,
    owners,
    allowedChangeTypes: [],
    budget: { schemaVersion: "1", totalUsdCap: cap },
    mode: "improvement",
    purpose,
  } as never);
  try {
    ProjectWorkspace.create(project, spec as unknown as ProjectSpecData);
  } catch (exc) {
    failClosed(exc);
  }
  console.log(`initialized ${project}/.vouch`);
  console.log(
    `project: ${spec.projectId} (${spec.name}) cap $${spec.budget.totalUsdCap.toFixed(2)}`,
  );
  for (const w of spec.workflows) {
    console.log(
      `frozen workflow ${w.workflowId}: ${w.name} (objective=${w.mainObjective}, ` +
        `guardrails=${w.guardrails.length > 0 ? JSON.stringify(w.guardrails) : "none"})`,
    );
  }
  console.log(`owners: ${JSON.stringify(spec.owners)}`);
}

async function cmdReconcile(args: Args): Promise<void> {
  const left = flag(args, "--left");
  const right = flag(args, "--right");
  const joinKey = flag(args, "--join-key");
  const project = flag(args, "--project", "-p") ?? ".";
  const out = flag(args, "--out");
  if (left === undefined || right === undefined || joinKey === undefined) {
    fail("--left, --right and --join-key are required");
  }
  const workspace = openWorkspace(project);
  try {
    const service = new ExecutionService(workspace);
    const leftBytes = Deno.readFileSync(left);
    const rightBytes = Deno.readFileSync(right);
    const leftName = left.split("/").pop() ?? "left.csv";
    const rightName = right.split("/").pop() ?? "right.csv";
    const { runId, report, digest } = await service.runCsvReconciliation({
      goal: `Reconcile ${leftName} vs ${rightName} on ${joinKey}`,
      leftCsv: leftBytes,
      rightCsv: rightBytes,
      joinKey,
      leftName,
      rightName,
      delimiter: flag(args, "--delimiter") ?? ",",
      ignoreColumns: flagList(args, "--ignore-column"),
    });
    console.log(
      `reconciled on ${joinKey}: ${report.rowCounts.matched} matched, ${report.changed.length} ` +
        `changed, ${report.missingInLeft.length} missing-left, ${report.missingInRight.length} ` +
        `missing-right, ${report.duplicateKeys.length} duplicate keys`,
    );
    console.log(`run: ${runId} report digest: ${short(digest)}`);
    if (out !== undefined) {
      const manifest = exportRun(workspace, runId, out);
      console.log(`exported: ${manifest}`);
    }
  } catch (exc) {
    failClosed(exc);
  } finally {
    workspace.close();
  }
}

function cmdRuns(args: Args): void {
  const project = flag(args, "--project", "-p") ?? ".";
  const workspace = openWorkspace(project);
  try {
    const history = new ExecutionService(workspace).runs();
    if (history.length === 0) {
      console.log("no task runs yet — try: vouch run --goal ... --input fact=fact.json");
      return;
    }
    for (const run of history) {
      console.log(
        `${run.runId}  ${
          run.status.padEnd(12)
        } steps=${run.steps.length}  updated=${run.updatedAt}`,
      );
    }
  } finally {
    workspace.close();
  }
}

function cmdRunStatus(args: Args): void {
  const runId = args.positionals[0];
  if (runId === undefined) fail("run-status takes a run id argument");
  const project = flag(args, "--project", "-p") ?? ".";
  const workspace = openWorkspace(project);
  try {
    const service = new ExecutionService(workspace);
    const run = service.status(runId);
    if (run === null) fail(`unknown run ${JSON.stringify(runId)}`);
    console.log(`status: ${run.status}  steps: ${run.steps.length}`);
    if (run.error !== null) console.log(`error: ${run.error}`);
    const report = service.recoveryReport(runId);
    console.log(`recovery: ${report.classification}  (${report.detail})`);
    const result = service.resultOf(runId);
    if (result !== null) {
      console.log(`conclusion: ${result.conclusion}`);
      const checks = Object.entries(result.completedConditionsCheck ?? {});
      const deliverable = checks.length > 0 && checks.every(([, v]) => v) &&
        !Object.values(result.externalActions ?? {}).some((s) => s === "pending");
      console.log(`deliverable: ${deliverable}`);
    }
  } catch (exc) {
    failClosed(exc);
  } finally {
    workspace.close();
  }
}

async function cmdRun(args: Args): Promise<void> {
  const goal = flag(args, "--goal");
  if (goal === undefined || goal === "") fail("--goal is required");
  const project = flag(args, "--project", "-p") ?? ".";
  const inputs: Record<string, unknown> = {};
  for (const token of flagList(args, "--input")) {
    const [name, value] = readJsonInput(token);
    inputs[name] = value;
  }
  if (Object.keys(inputs).length === 0) {
    fail("at least one --input is required (task materials)");
  }
  const conditions = completionConditions(args);
  if (conditions.length === 0) {
    console.error(
      "note: no --require-field/--expect-text given; completion is not machine-checkable and " +
        "the run will stop honestly without them",
    );
  }
  const workspace = openWorkspace(project);
  try {
    const service = new ExecutionService(workspace, { workerPath: flag(args, "--worker-path") });
    const inline = hasFlag(args, "--inline");
    const outcome = await service.run({
      goal,
      inputs,
      budgetUsd: Number(flag(args, "--budget") ?? "0.5"),
      maxSteps: Number(flag(args, "--max-steps") ?? "4"),
      provider: flag(args, "--provider") ?? "extract-fact",
      scriptFiles: flagList(args, "--script-file"),
      isolated: !inline,
      completionConditions: conditions,
    });
    if (outcome.result !== null && (outcome.result.artifactRefs ?? []).length > 0) {
      const final = outcome.result.artifactRefs[outcome.result.artifactRefs.length - 1];
      try {
        const payload = new TextDecoder().decode(workspace.artifacts.get(final));
        const preview = JSON.stringify(JSON.parse(payload));
        console.log(
          `delivered: ${preview.length > 400 ? preview.slice(0, 400) + "…" : preview}`,
        );
      } catch {
        console.log(`delivered: non-JSON artifact ${short(final)}`);
      }
    }
    console.log("execution: fixture providers, deterministic; not a live model");
    console.log(
      `isolation: ${
        inline
          ? "in-process (trusted sample scripts only)"
          : "worker subprocess (permission-bounded)"
      }`,
    );
    console.log(`run: ${outcome.runId} status: ${outcome.status}`);
    if (outcome.error !== null) console.error(`error: ${outcome.error}`);
    const result = outcome.result;
    if (result !== null) {
      console.log(`conclusion: ${result.conclusion}`);
      if ((result.doneItems ?? []).length > 0) {
        console.log(`done: ${(result.doneItems ?? []).join("; ")}`);
      }
      if ((result.notDoneItems ?? []).length > 0) {
        console.error(`not done: ${(result.notDoneItems ?? []).join("; ")}`);
      }
      if ((result.uncertainties ?? []).length > 0) {
        console.error(`uncertainties: ${(result.uncertainties ?? []).join("; ")}`);
      }
      const refs = result.artifactRefs ?? [];
      if (refs.length > 0) {
        const pairs: Array<[string, string]> = [
          ["inputs", refs[0]],
          ["final", refs[refs.length - 1]],
        ];
        for (const [key, digest] of pairs) {
          if (digest !== undefined && digest !== "") {
            console.log(`artifact ${key}: ${short(digest)}`);
          }
        }
      }
      const cost = result.totalCostUsd;
      console.log(
        `cost: ${cost === null ? "unmeasured" : `$${cost.toFixed(4)}`} (fixture providers)`,
      );
    }
    if (outcome.status !== "completed") Deno.exit(1);
  } catch (exc) {
    failClosed(exc);
  } finally {
    workspace.close();
  }
}

async function cmdRunDetach(args: Args): Promise<void> {
  const goal = flag(args, "--goal");
  if (goal === undefined || goal === "") fail("--goal is required");
  const project = flag(args, "--project", "-p") ?? ".";
  const inputs: Record<string, unknown> = {};
  for (const token of flagList(args, "--input")) {
    const [name, value] = readJsonInput(token);
    inputs[name] = value;
  }
  if (Object.keys(inputs).length === 0) fail("at least one --input is required (task materials)");
  const workspace = openWorkspace(project);
  let runId: string;
  try {
    const service = new ExecutionService(workspace, { workerPath: flag(args, "--worker-path") });
    runId = service.submit({
      goal,
      inputs,
      budgetUsd: Number(flag(args, "--budget") ?? "0.5"),
      maxSteps: Number(flag(args, "--max-steps") ?? "4"),
      completionConditions: completionConditions(args),
    });
  } catch (exc) {
    failClosed(exc);
  } finally {
    workspace.close();
  }
  const workerPath = flag(args, "--worker-path");
  const pid = spawnRunWorker(
    project,
    runId,
    flag(args, "--provider") ?? "extract-fact",
    workerPath,
  );
  console.log(`detached run: ${runId} (worker pid ${pid})`);
  console.log("client may exit; reconnect with: vouch runs / vouch run-status / vouch export-run");
}

async function cmdImprove(args: Args): Promise<void> {
  const project = flag(args, "--project", "-p") ?? ".";
  const sub = args.positionals[0] ?? "";
  const workspace = openWorkspace(project);
  const flow = new ImprovementFlow(workspace, import.meta.url.replace("file://", ""));
  try {
    requireImprovementMode(workspace);
    switch (sub) {
      case "baseline": {
        const workflowId = flag(args, "--workflow", "-w") ??
          workspace.spec.workflows[0]?.workflowId;
        if (workflowId === undefined) fail("--workflow is required");
        const result = flow.recordBaseline({
          versionId: flag(args, "--version") ?? fail("--version is required"),
          sourceRef: flag(args, "--source-ref") ?? fail("--source-ref is required"),
          workflowId,
          mainMetric: flag(args, "--main-metric"),
          minImprovement: flag(args, "--min-improvement") !== undefined
            ? Number(flag(args, "--min-improvement"))
            : 0,
          direction: (flag(args, "--direction") as "increase" | "decrease" | undefined) ??
            "increase",
          repeats: flag(args, "--repeats") !== undefined ? Number(flag(args, "--repeats")) : 1,
          frozenBy: flag(args, "--frozen-by"),
        });
        console.log(`baseline recorded: ${result.baselineRecordId}`);
        console.log(`rubric frozen: ${result.rubricDigest}`);
        return;
      }
      case "fixtures": {
        const out = flag(args, "--out") ?? defaultFixturesPath(workspace);
        Deno.writeTextFileSync(out, JSON.stringify(SYNTHETIC_FIXTURE_PACK, null, 2) + "\n");
        console.log(`wrote SYNTHETIC improvement fixtures: ${out}`);
        console.log(
          "every scenario is invented data; not evidence of model or product improvement",
        );
        return;
      }
      case "propose": {
        const workflowId = flag(args, "--workflow", "-w") ??
          workspace.spec.workflows[0]?.workflowId;
        if (workflowId === undefined) fail("--workflow is required");
        const result = flow.propose({
          delta: flag(args, "--delta") ?? fail("--delta is required"),
          changeType: flag(args, "--change-type") ?? "prompt-delta",
          rationale: flag(args, "--rationale") ?? fail("--rationale is required"),
          workflowId,
          expectedImpact: flag(args, "--expected-impact") ?? "",
          proposer: flag(args, "--proposer"),
          seal: hasFlag(args, "--seal"),
        });
        console.log(
          `candidate: ${result.candidate.candidateId} (${result.sealed ? "sealed" : "proposed"})`,
        );
        console.log(`content digest: ${result.contentDigest}`);
        return;
      }
      case "seal": {
        const candidateId = args.positionals[1];
        if (candidateId === undefined) fail("improve seal takes a candidate id");
        const sealed = flow.controller.seal(candidateId);
        console.log(`candidate ${candidateId}: sealed (${candidateContentDigest(sealed)})`);
        return;
      }
      case "evaluate": {
        const candidateId = args.positionals[1];
        if (candidateId === undefined) fail("improve evaluate takes a candidate id");
        const workflowId = flag(args, "--workflow", "-w") ??
          workspace.spec.workflows[0]?.workflowId;
        if (workflowId === undefined) fail("--workflow is required");
        const run = await flow.evaluate({
          candidateId,
          workflowId,
          adapterKind: flag(args, "--adapter") ?? "scripted",
          split: (flag(args, "--split") as "development" | "selection-validation" | undefined) ??
            "development",
          repeats: flag(args, "--repeats") !== undefined
            ? Number(flag(args, "--repeats"))
            : undefined,
        });
        const comparison = run.comparison!;
        console.log(
          `run ${run.runId}: ${comparison.completePairs}/${comparison.totalPairs} complete pairs, ` +
            `${comparison.hardViolations.length} hard violation(s), verdict=${run.verdict}`,
        );
        if (comparison.incompleteReasons && Object.keys(comparison.incompleteReasons).length > 0) {
          console.error(`incomplete: ${JSON.stringify(comparison.incompleteReasons)}`);
        }
        return;
      }
      case "accept": {
        const candidateId = args.positionals[1];
        if (candidateId === undefined) fail("improve accept takes a candidate id");
        const workflowId = flag(args, "--workflow", "-w") ??
          workspace.spec.workflows[0]?.workflowId;
        if (workflowId === undefined) fail("--workflow is required");
        const owner = flag(args, "--owner") ??
          fail("--owner is required (the acceptance owner identity)");
        const evidenceOut = flag(args, "--evidence-out") ??
          `${project}/.vouch/evidence/${candidateId}`;
        const outcome = await flow.finalAcceptance({
          candidateId,
          workflowId,
          adapterKind: flag(args, "--adapter") ?? "scripted",
          owner,
          evidenceOut,
        });
        console.log(`final acceptance run ${outcome.run.runId}: verdict=${outcome.verdict}`);
        console.log(`evidence package: ${outcome.evidenceDigest} (${evidenceOut})`);
        if (outcome.decisionId !== null) {
          console.log(`decision: ${outcome.decisionId} (bound, accepted)`);
        }
        return;
      }
      case "approve": {
        const candidateId = args.positionals[1];
        if (candidateId === undefined) fail("improve approve takes a candidate id");
        const approver = flag(args, "--approver") ??
          fail("--approver is required (the release owner identity)");
        if (approver !== (workspace.spec.owners["release-owner"] ?? "")) {
          fail(
            `--approver must be the workspace release-owner (${
              JSON.stringify(
                workspace.spec.owners["release-owner"] ?? "",
              )
            }); release approval is never implicit`,
          );
        }
        const result = flow.controller.approve({ candidateId, approver });
        console.log(`candidate ${result.candidateId}: approved (decision ${result.decisionId})`);
        return;
      }
      case "release": {
        const candidateId = args.positionals[1];
        if (candidateId === undefined) fail("improve release takes a candidate id");
        const record = flow.controller.recordRelease({
          candidateId,
          deployedVersion: flag(args, "--deployed-version") ??
            fail("--deployed-version is required"),
          deployedBy: flag(args, "--deployed-by") ?? fail("--deployed-by is required"),
          observedWindow: flag(args, "--observed-window"),
        });
        console.log(
          `release recorded: ${record.releaseId} (rollback via 'vouch improve rollback')`,
        );
        return;
      }
      case "rollback": {
        const releaseId = args.positionals[1];
        if (releaseId === undefined) fail("improve rollback takes a release id");
        const updated = flow.controller.recordRollback(
          releaseId,
          flag(args, "--trigger") ?? "operator rollback",
        );
        console.log(
          `rollback recorded for ${updated.releaseId} (compensation ${updated.compensationStatus})`,
        );
        return;
      }
      default:
        fail(
          `unknown improve subcommand ${
            JSON.stringify(sub)
          }; subcommands: baseline fixtures propose seal evaluate accept approve release rollback`,
        );
    }
  } catch (exc) {
    failClosed(exc);
  } finally {
    workspace.close();
  }
}

function cmdRunners(args: Args): void {
  const project = flag(args, "--project", "-p") ?? ".";
  const workspace = openWorkspace(project);
  try {
    const config = loadRunnerConfig(workspace);
    if (args.positionals[0] === "set") {
      const name = args.positionals[1];
      const command = flagList(args, "--command");
      if (name !== "choose" && name !== "visibility") {
        fail("runners set takes choose or visibility");
      }
      if (command.length === 0) fail("--command <argv...> is required (explicit runner location)");
      const next = { ...(config ?? { schemaVersion: "1" as const }) };
      next[name] = { command };
      const path = saveRunnerConfig(workspace, next);
      console.log(`recorded runner ${name}: ${JSON.stringify(command)} (${path})`);
      return;
    }
    if (config === null) {
      console.log(
        `no runner configuration (create ${workspace.vouchDir}/runners.json or use 'vouch runners set')`,
      );
      console.log("fixture loopback (scripted) is available without configuration");
      return;
    }
    console.log(`fixture: ${config.fixture?.fixturesPath ?? defaultFixturesPath(workspace)}`);
    for (const name of ["choose", "visibility"] as const) {
      const runner = config[name];
      console.log(
        `${name}: ${
          runner === undefined
            ? "(unconfigured — evaluate --adapter runner:${name} refuses)"
            : JSON.stringify(runner.command)
        }`,
      );
    }
  } finally {
    workspace.close();
  }
}

function selfExecArgs(subcommand: string, extra: string[]): { command: string; args: string[] } {
  const exec = Deno.execPath();
  if (exec.endsWith("deno")) {
    // dev: re-run this CLI module through deno with explicit permissions
    const mainModule = new URL(import.meta.url).pathname;
    return {
      command: exec,
      args: [
        "run",
        "--no-prompt",
        "--allow-read",
        "--allow-write",
        // the detached run-worker is trusted controller code: it spawns the
        // isolated worker (deno, by absolute path) and prlimit — a name-
        // restricted allow-run cannot express that, so it gets process-spawn
        // authority like the CLI itself, still with no net/env.
        "--allow-run",
        mainModule,
        subcommand,
        ...extra,
      ],
    };
  }
  return { command: exec, args: [subcommand, ...extra] };
}

export function spawnRunWorker(
  project: string,
  runId: string,
  provider: string,
  workerPath?: string,
): number {
  const extra = ["--project", project, runId, "--provider", provider];
  if (workerPath !== undefined) extra.push("--worker-path", workerPath);
  const { command, args } = selfExecArgs("__run-worker", extra);
  const spec = controllerChildSpawn(command, args);
  const child = new Deno.Command(spec.command, {
    args: spec.args,
    stdin: "null",
    stdout: "null",
    stderr: "null",
    clearEnv: spec.clearEnv,
    env: spec.env,
  }).spawn();
  return child.pid;
}

if (import.meta.main) {
  try {
    await main();
  } catch (exc) {
    if (exc instanceof ContractError) failClosed(exc);
    failClosed(exc);
  }
}
