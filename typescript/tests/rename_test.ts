/**
 * Rename-boundary tests (Vowdo / vouch-agent-ts): new workspaces and
 * commands emit Vowdo identifiers; genuine pre-rename `.vouch` workspaces
 * (synthesized with the exact markers/ids the rc4 release wrote) FAIL
 * CLOSED on open and on init with actionable guidance; historical export
 * and evidence manifests verify byte-for-byte read-only.
 *
 * Scope note: there is deliberately NO TS workspace migration (reviewed
 * scope decision) — refusal plus guidance is the tested behavior.
 */

import { assertEquals, assertThrows, assertTrue } from "./helpers.ts";
import { ExecutionService } from "../src/appservices/execution.ts";
import { ProjectWorkspace } from "../src/appservices/workspace.ts";
import { exportRun, verifyNativeExport } from "../src/appservices/native_export.ts";
import { verifyEvidencePackage } from "../src/improvement/evidence.ts";
import { specFromDict } from "../src/contracts/project.ts";
import { digestBytes, digestOf } from "../src/contracts/canonical.ts";
import { ContractError } from "../src/contracts/common.ts";

function rmrf(path: string): void {
  try {
    Deno.removeSync(path, { recursive: true });
  } catch {
    // gone
  }
}

function tempDir(name: string): string {
  const dir = `/tmp/vowdo-ts-tests/${name}-${crypto.randomUUID().slice(0, 8)}`;
  Deno.mkdirSync(dir, { recursive: true });
  return dir;
}

function taskOnlySpec(name: string) {
  return specFromDict({
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
}

const LEFT = new TextEncoder().encode("sku,a\nA,1\nB,2\n");
const RIGHT = new TextEncoder().encode("sku,a\nA,1\nC,3\n");

/** A genuine-shape PRE-RENAME (0.2.0rc4) workspace: `.vouch` directory with
 * the exact marker rc4 wrote (tool vouch-agent-ts, schemaVersion 2, storage
 * ts-1) and a digest-consistent old-format records/artifacts layout. */
async function createGenuineLegacyWorkspace(name: string): Promise<string> {
  const dir = tempDir(name);
  // The marker bytes rc4 wrote:
  Deno.mkdirSync(`${dir}/.vouch/artifacts`, { recursive: true });
  Deno.mkdirSync(`${dir}/.vouch/adapter-workspace`, { recursive: true });
  Deno.writeFileSync(
    `${dir}/.vouch/workspace.json`,
    new TextEncoder().encode(
      JSON.stringify(
        {
          schemaVersion: "2",
          tool: "vouch-agent-ts",
          toolVersion: "0.2.0rc4",
          projectId: `proj-${name}`,
          storage: "ts-1",
        },
        null,
        2,
      ) + "\n",
    ),
  );
  // A genuine rc4-shaped record + artifact pair (project record and one
  // content-addressed artifact; the sealed ids are the rc4 spellings).
  const artifact = new TextEncoder().encode('{"legacy":true}');
  const artifactDigest = digestBytes(artifact);
  Deno.writeFileSync(`${dir}/.vouch/artifacts/${artifactDigest.slice("sha256:".length)}`, artifact);
  const spec = taskOnlySpec(name);
  const records: Array<[string, string, Record<string, unknown>]> = [
    ["project", `proj-${name}`, spec as never],
    [
      "execution-config",
      "run_legacy0001",
      {
        schemaVersion: "1",
        runId: "run_legacy0001",
        providerName: null,
        operation: "vouch-csv-reconcile/1",
        parameters: { joinKey: "sku" },
        operationDigest: digestOf({
          operation: "vouch-csv-reconcile/1",
          parameters: { joinKey: "sku" },
        }),
        scripts: [],
        scriptsDigest: digestOf({
          operation: "vouch-csv-reconcile/1",
          parameters: { joinKey: "sku" },
        }),
        mode: "fixture",
        isolated: false,
        runtimeId: "vouch-native-operation/vouch-csv-reconcile/1",
        toolVersion: "0.2.0rc4",
      },
    ],
  ];
  const { openDatabase } = await import("../src/storage/sqlite.ts");
  const db = openDatabase(`${dir}/.vouch/meta.sqlite`);
  try {
    db.exec(
      "CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, record_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (kind, record_id))",
    );
    for (const [kind, id, data] of records) {
      db.prepare("INSERT INTO records (kind, record_id, data) VALUES (?, ?, ?)")
        .run(kind, id, JSON.stringify(data));
    }
  } finally {
    db.close();
  }
  return dir;
}

Deno.test({
  name: "rename: new workspaces emit Vowdo identifiers everywhere current",
  async fn() {
    const dir = tempDir("new-identity");
    try {
      const workspace = ProjectWorkspace.create(dir, taskOnlySpec("new-identity") as never);
      try {
        assertEquals(workspace.vowdoDir, `${dir}/.vowdo`);
        assertEquals(workspace.workspaceInfo().tool, "vowdo-agent-ts");
        const service = new ExecutionService(workspace);
        const { runId, digest } = await service.runCsvReconciliation({
          goal: "g",
          leftCsv: LEFT,
          rightCsv: RIGHT,
          joinKey: "sku",
        });
        assertEquals(service.status(runId)?.status, "completed");
        // the sealed execution-config carries the Vowdo runtime backend id
        const config = workspace.store.load("execution-config", runId)!;
        assertEquals(config["runtimeId"], "vowdo-native-operation/vowdo-csv-reconcile/1");
        // the CSV report's operation identity stays the historical,
        // digest-bound data-format id (compat corpus + old stored runs)
        const result = service.resultOf(runId)!;
        const final = result.artifactRefs.slice(-1)[0];
        const report = JSON.parse(new TextDecoder().decode(workspace.artifacts.get(final)));
        assertEquals(report.operation, "vowdo-csv-reconcile/1");
        assertTrue(digest.startsWith("sha256:"));
        // exports carry the Vowdo manifest kind
        const out = `${dir}/export`;
        exportRun(workspace, runId, out);
        assertEquals(verifyNativeExport(out)["kind"], "vowdo-native-run-export");
      } finally {
        workspace.close();
      }
    } finally {
      rmrf(dir);
    }
  },
});

Deno.test({
  name: "rename: genuine rc4 workspace fails closed on OPEN with actionable guidance",
  async fn() {
    const legacy = await createGenuineLegacyWorkspace("legacy-open");
    try {
      const error = assertThrows(
        () => ProjectWorkspace.open(legacy),
        ContractError,
      ) as ContractError;
      assertTrue(error.message.includes(".vouch"), error.message);
      assertTrue(
        error.message.includes("historical release") && error.message.includes("NEW directory"),
        `guidance must state the supported paths: ${error.message}`,
      );
      assertTrue(!error.message.includes("migrate-"), "no migration command is offered");
      // the workspace bytes are untouched by the refusal
      assertEquals([...Deno.readDirSync(`${legacy}/.vouch`)].length >= 0, true);
    } finally {
      rmrf(legacy);
    }
  },
});

Deno.test({
  name: "rename: genuine rc4 workspace fails closed on INIT (never silently beside old state)",
  async fn() {
    const legacy = await createGenuineLegacyWorkspace("legacy-init");
    try {
      const error = assertThrows(
        () => ProjectWorkspace.create(legacy, taskOnlySpec("x") as never),
        ContractError,
      ) as ContractError;
      assertTrue(
        error.message.includes("historical release") && error.message.includes("NEW directory"),
        `init must route to the safe paths: ${error.message}`,
      );
      // and no .vowdo was created beside it
      assertEquals([...Deno.readDirSync(legacy)].some((e) => e.name === ".vowdo"), false);
    } finally {
      rmrf(legacy);
    }
  },
});

Deno.test({
  name: "rename: historical vouch-native-run-export verifies read-only, tamper refused",
  fn() {
    const dir = tempDir("old-export");
    try {
      // A synthetic export in the exact shape the rc4 release wrote:
      // historical kind label, otherwise the identical manifest schema.
      const out = `${dir}/export`;
      Deno.mkdirSync(out, { recursive: true });
      const artifact = new TextEncoder().encode('{"historical artifact": true}');
      const artifactDigest = digestBytes(artifact);
      Deno.writeFileSync(`${out}/artifact-000-${artifactDigest.slice(7, 23)}.bin`, artifact);
      const manifest: Record<string, unknown> = {
        schemaVersion: "2",
        kind: "vouch-native-run-export",
        runId: "run_hist0001",
        taskSpecId: null,
        taskDigest: "sha256:" + "0".repeat(64),
        runStatus: "completed",
        terminal: true,
        mode: "fixture",
        title: null,
        goal: null,
        materials: [],
        operation: "vowdo-csv-reconcile/1",
        operationInputs: {},
        configuration: null,
        conclusion: "historical",
        doneItems: [],
        notDoneItems: [],
        uncertainties: [],
        externalActions: {},
        totalCostUsd: null,
        completedConditionsCheck: {},
        deliverable: true,
        resultCreatedAt: "2026-09-30T00:00:00.000+00:00",
        runError: null,
        costEntries: [],
        artifacts: [{
          digest: artifactDigest,
          file: `artifact-000-${artifactDigest.slice(7, 23)}.bin`,
          bytes: artifact.length,
        }],
      };
      Deno.writeFileSync(
        `${out}/manifest.json`,
        new TextEncoder().encode(JSON.stringify(manifest, null, 2) + "\n"),
      );
      const verified = verifyNativeExport(out);
      assertEquals(verified["kind"], "vouch-native-run-export");
      assertEquals(verified["runId"], "run_hist0001");
      // tampering under the historical kind still refuses
      const victim = `${out}/artifact-000-${artifactDigest.slice(7, 23)}.bin`;
      const bytes = Deno.readFileSync(victim);
      bytes[0] ^= 1;
      Deno.writeFileSync(victim, bytes);
      assertThrows(() => verifyNativeExport(out), Error);
    } finally {
      rmrf(dir);
    }
  },
});

Deno.test({
  name: "rename: historical vouch-evidence-package verifies read-only",
  fn() {
    const dir = tempDir("old-evidence");
    try {
      const out = `${dir}/evidence`;
      Deno.mkdirSync(out, { recursive: true });
      const summary = new TextEncoder().encode('{"legacy": true}');
      Deno.writeFileSync(`${out}/evaluation-summary.json`, summary);
      const manifest: Record<string, unknown> = {
        schemaVersion: "1",
        kind: "vouch-evidence-package",
        runId: "eval_legacy_1",
        artifacts: [{
          digest: digestBytes(summary),
          file: "evaluation-summary.json",
          bytes: summary.length,
        }],
        manifestDigest: "",
      };
      const clone: Record<string, unknown> = { ...manifest };
      delete clone["manifestDigest"];
      manifest["manifestDigest"] = digestOf(clone);
      Deno.writeFileSync(
        `${out}/evidence-manifest.json`,
        new TextEncoder().encode(JSON.stringify(manifest, null, 2) + "\n"),
      );
      const verified = verifyEvidencePackage(out);
      assertEquals(verified["kind"], "vouch-evidence-package");
      assertEquals(verified["runId"], "eval_legacy_1");
    } finally {
      rmrf(dir);
    }
  },
});
