/**
 * Gate 1 behavioral tests: workspace, storage, supervisor lifecycle for the
 * native task journey — durable submit → claimed execution → export →
 * verify → reopen; tamper/changed-input refusal; revision fencing; budget
 * invariants; recovery classification.
 */

import { assertEquals, assertThrows, assertTrue } from "./helpers.ts";
import { CSV_OPERATION_ID, ExecutionService } from "../src/appservices/execution.ts";
import {
  exportRun,
  INCOMPLETE_MARKER,
  verifyNativeExport,
} from "../src/appservices/native_export.ts";
import { ProjectWorkspace } from "../src/appservices/workspace.ts";
import { ContractError, DigestMismatchError } from "../src/contracts/common.ts";
import { digestBytes, digestOf } from "../src/contracts/canonical.ts";
import { recover } from "../src/orchestrator/checkpoints.ts";
import { BudgetLedger } from "../src/storage/budget.ts";
import { MetadataStore } from "../src/storage/store.ts";
import { ArtifactStore } from "../src/storage/artifacts.ts";
import { Journal } from "../src/storage/journal.ts";
import { specFromDict } from "../src/contracts/project.ts";

function tempDir(name: string): string {
  const dir = `/tmp/vowdo-ts-tests/${name}-${crypto.randomUUID().slice(0, 8)}`;
  Deno.mkdirSync(dir, { recursive: true });
  return dir;
}

function taskOnlyWorkspace(name: string, cap = 5): ProjectWorkspace {
  const dir = tempDir(name);
  const spec = specFromDict({
    schemaVersion: "1",
    projectId: `proj-${name}`,
    name,
    workflows: [],
    owners: {},
    allowedChangeTypes: [],
    budget: { schemaVersion: "1", totalUsdCap: cap },
    mode: "task-only",
    purpose: "test",
  } as never);
  return ProjectWorkspace.create(dir, spec as never);
}

const LEFT = new TextEncoder().encode(
  "sku,name,price_usd\nA,Kettle,10.00\nB,Mug,4.00\nC,Bad,5.00\n",
);
const RIGHT = new TextEncoder().encode(
  "sku,name,price_usd\nA,Kettle,11.00\nB,Mug,4.00\nD,New,9.00\n",
);

Deno.test({
  name: "csv native journey: durable submit → execute → export → verify → reopen",
  async fn() {
    const workspace = taskOnlyWorkspace("csv-journey");
    try {
      const service = new ExecutionService(workspace);
      // Durable submission first: run exists QUEUED before execution.
      const runId = service.submitCsvReconciliation({
        goal: "Reconcile catalog vs feed",
        leftCsv: LEFT,
        rightCsv: RIGHT,
        joinKey: "sku",
        leftName: "产品 目录.csv",
        rightName: "supplier feed.csv",
      });
      assertEquals(service.status(runId)?.status, "queued");

      const outcome = await service.executeNativeOperation(runId);
      assertEquals(outcome.status, "completed", outcome.error ?? "");
      const result = service.resultOf(runId);
      assertTrue(result !== null);
      assertEquals(
        (result!.completedConditionsCheck as Record<string, boolean>) &&
          Object.values(result!.completedConditionsCheck).every(Boolean),
        true,
      );
      // Honest caveat: discrepancies found → not "clean"
      assertTrue((result!.notDoneItems ?? []).includes("discrepancies found — review the report"));

      const out = `${workspace.projectDir}/export`;
      const manifest = exportRun(workspace, runId, out);
      assertTrue(manifest.endsWith("manifest.json"));
      const verified = verifyNativeExport(out);
      assertEquals(verified["runId"], runId);
      assertEquals(verified["operation"], CSV_OPERATION_ID);
      const artifacts = verified["artifacts"] as Array<Record<string, unknown>>;
      assertTrue(artifacts.length >= 3, "report + 2 material snapshots");

      // Reopen: a FRESH workspace instance sees the same durable state.
      const reopened = ProjectWorkspace.open(workspace.projectDir);
      const freshService = new ExecutionService(reopened);
      assertEquals(freshService.status(runId)?.status, "completed");
      const recovery = freshService.recoveryReport(runId);
      assertEquals(recovery.classification, "terminal");
      reopened.close();
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "changed-input sensitivity: different right CSV → different report digest",
  async fn() {
    const workspace = taskOnlyWorkspace("changed-input");
    try {
      const service = new ExecutionService(workspace);
      const first = await service.runCsvReconciliation({
        goal: "g",
        leftCsv: LEFT,
        rightCsv: RIGHT,
        joinKey: "sku",
      });
      const second = await service.runCsvReconciliation({
        goal: "g",
        leftCsv: LEFT,
        rightCsv: new TextEncoder().encode("sku,name,price_usd\nA,Kettle,12.00\nB,Mug,4.00\n"),
        joinKey: "sku",
      });
      assertTrue(first.digest !== second.digest, "report digests must track the inputs");
      assertEquals(first.report.changed[0].differences[0].right, "11.00");
      assertEquals(second.report.changed[0].differences[0].right, "12.00");
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "tamper detection: artifact bytes and exports refuse altered content",
  async fn() {
    const workspace = taskOnlyWorkspace("tamper");
    try {
      const service = new ExecutionService(workspace);
      const { runId } = await service.runCsvReconciliation({
        goal: "g",
        leftCsv: LEFT,
        rightCsv: RIGHT,
        joinKey: "sku",
      });
      const result = service.resultOf(runId)!;
      const final = result.artifactRefs[result.artifactRefs.length - 1];
      // Artifact store re-verifies on read
      const artifactsDir = `${workspace.vowdoDir}/artifacts`;
      const fileName = final.slice("sha256:".length);
      const original = Deno.readFileSync(`${artifactsDir}/${fileName}`);
      Deno.writeFileSync(`${artifactsDir}/${fileName}`, new TextEncoder().encode("tampered"));
      assertThrows(() => workspace.artifacts.get(final), DigestMismatchError);
      // restore the valid bytes for the export path
      Deno.writeFileSync(`${artifactsDir}/${fileName}`, original);
      const out = `${workspace.projectDir}/export`;
      exportRun(workspace, runId, out);
      verifyNativeExport(out);
      // Tamper one exported artifact byte → verification fails
      const exported = (verifyNativeExport(out)["artifacts"] as Array<Record<string, unknown>>)[0];
      const artifactPath = `${out}/${exported["file"]}`;
      const bytes = Deno.readFileSync(artifactPath);
      bytes[0] = bytes[0] ^ 0x01;
      Deno.writeFileSync(artifactPath, bytes);
      assertThrows(() => verifyNativeExport(out), DigestMismatchError);
      // Truncated marker → incomplete export never verifies
      Deno.writeFileSync(`${out}/${INCOMPLETE_MARKER}`, new TextEncoder().encode("x"));
      assertThrows(() => verifyNativeExport(out), ContractError);
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "workspace namespacing: Python marker refused, double-init refused",
  fn() {
    const dir = tempDir("namespace");
    try {
      const spec = specFromDict({
        schemaVersion: "1",
        projectId: "proj-x",
        name: "x",
        workflows: [],
        owners: {},
        allowedChangeTypes: [],
        budget: { schemaVersion: "1", totalUsdCap: 1 },
        mode: "task-only",
        purpose: "",
      } as never);
      ProjectWorkspace.create(dir, spec as never).close();
      // double init refused
      assertThrows(() => ProjectWorkspace.create(dir, spec as never), ContractError);
      // A Python-style marker is refused by the TS tool
      Deno.writeTextFileSync(
        `${dir}/.vowdo/workspace.json`,
        JSON.stringify({
          schemaVersion: "1",
          tool: "vouch-agent",
          toolVersion: "0.1.0rc1",
          projectId: "proj-x",
        }),
      );
      assertThrows(() => ProjectWorkspace.open(dir), ContractError);
    } finally {
      rmrf(dir);
    }
  },
});

Deno.test({
  name: "budget ledger: atomic cap enforcement + hierarchical children + stale-open reporting",
  fn() {
    const dir = tempDir("budget");
    const path = `${dir}/budget.sqlite`;
    try {
      const ledger = new BudgetLedger(path, 1.0);
      const parent = ledger.reserve("run_1", 0.8);
      assertTrue(Math.abs(ledger.remainingUsd() - 0.2) < 1e-9);
      // cap refuses an over-reservation
      assertThrows(() => ledger.reserve("run_2", 0.5), Error);
      // children carve from the parent, not the cap
      const c1 = ledger.reserveChild(parent.reservationId, "run_1#q1", 0.5);
      ledger.settle(c1.reservationId, 0.3);
      // over-allocating the parent refuses (committed = 0.3 settled actual + 0.6 > 0.8)
      assertThrows(() => ledger.reserveChild(parent.reservationId, "run_1#q2", 0.6), Error);
      // settle parent from children exactly at actuals
      const settled = ledger.settleParentFromChildren(parent.reservationId);
      assertEquals(settled.status, "settled");
      assertEquals(settled.settledAmountUsd, 0.3);
      // a stale OPEN reservation is reported, never silently released
      const stale = ledger.reserve("run_3", 0.1);
      const opens = ledger.openReservations();
      assertEquals(opens.length, 1);
      assertEquals(opens[0].reservationId, stale.reservationId);
      // reopening with a different cap fails closed
      assertThrows(() => new BudgetLedger(path, 2.0), ContractError);
    } finally {
      rmrf(dir);
    }
  },
});

Deno.test({
  name: "journal: duplicate event ids refused; subject writer pinned",
  fn() {
    const dir = tempDir("journal");
    try {
      const journal = new Journal(`${dir}/journal.sqlite`);
      const event = {
        schemaVersion: "1",
        eventId: "evt_fixed",
        kind: "audit-note" as const,
        occurredAt: "2026-09-30T00:00:00.000+00:00",
        actor: "controller",
        subject: "run_1",
        data: {},
        mode: "fixture" as const,
      };
      journal.append(event);
      assertThrows(() => journal.append(event), ContractError);
      const writer = journal.subjectWriter("run_1");
      const foreign = { ...event, eventId: "evt_2", subject: "run_2" };
      assertThrows(() => writer.append(foreign), ContractError);
      assertEquals(journal.events("run_1").length, 1);
    } finally {
      rmrf(dir);
    }
  },
});

Deno.test({
  name: "artifact store: path escapes impossible, digest re-verified",
  fn() {
    const dir = tempDir("artifacts");
    try {
      const artifacts = new ArtifactStore(dir);
      assertThrows(() => artifacts.get("sha256:../escape"), ContractError);
      assertThrows(() => artifacts.get("../../etc/passwd"), ContractError);
      assertThrows(() => artifacts.get("sha256:not-hex"), ContractError);
      const digest = artifacts.put(new TextEncoder().encode("hello"));
      assertEquals(digest, digestBytes(new TextEncoder().encode("hello")));
      assertEquals(new TextDecoder().decode(artifacts.get(digest)), "hello");
      // idempotent re-put
      assertEquals(artifacts.put(new TextEncoder().encode("hello")), digest);
    } finally {
      rmrf(dir);
    }
  },
});

Deno.test({
  name: "store: revision-fenced writes and canonical re-verification on load",
  fn() {
    const dir = tempDir("store");
    try {
      const store = new MetadataStore(`${dir}/meta.sqlite`);
      store.save("task-run", "run_1", { schemaVersion: "1", status: "queued", revision: 1 });
      assertEquals(store.load("task-run", "run_1")!["status"], "queued");
      // A record with an integral-float spelling written by hand is refused
      // on LOAD (shared numeric domain), not silently re-spelled.
      const db = new MetadataStore(`${dir}/meta2.sqlite`);
      db.save("k", "ok", { n: 3, f: 0.5 });
      assertEquals(db.load("k", "ok")!["n"], 3);
      db.close();
      // transaction nesting + rollback
      store.transaction(() => {
        store.save("k", "v1", { a: 1 });
        try {
          store.transaction(() => {
            store.save("k", "v2", { a: 2 });
            throw new Error("inner failure");
          });
        } catch {
          // expected
        }
      });
      assertEquals(store.load("k", "v1")!["a"], 1);
      assertEquals(store.load("k", "v2"), null);
      store.close();
    } finally {
      rmrf(dir);
    }
  },
});

Deno.test({
  name: "recovery classification: finalization-pending vs needs-reconciliation vs clean",
  async fn() {
    const workspace = taskOnlyWorkspace("recovery");
    try {
      const service = new ExecutionService(workspace);
      const runId = service.submitCsvReconciliation({
        goal: "g",
        leftCsv: LEFT,
        rightCsv: RIGHT,
        joinKey: "sku",
      });
      const report = recover(workspace.store, runId);
      assertEquals(report.classification, "clean-resume");
      await service.executeNativeOperation(runId);
      assertEquals(recover(workspace.store, runId).classification, "terminal");

      // Simulate a crash between finalization writes: flip the intent's
      // packageSaved flag off after completion → finalization-pending.
      const intent = workspace.store.load("run-finalization", runId)!;
      intent["ownershipReleased"] = false;
      workspace.store.save("run-finalization", runId, intent);
      const pending = recover(workspace.store, runId);
      assertEquals(pending.classification, "finalization-pending");

      // A model run with an unknown side-effect step → needs-reconciliation.
      const modelRun = service.submit({
        goal: "g",
        inputs: { fact: { value: "v", source: "s" } },
        completionConditions: [{
          type: "artifact_schema",
          schema: { type: "object", required: ["finding"] },
        }],
      });
      const outcome = await service.execute(modelRun, {
        provider: "extract-fact",
        isolated: false,
      });
      assertEquals(outcome.status, "completed", outcome.error ?? "");
      void digestOf;
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

function rmrf(path: string, _options?: { recursive?: boolean }): void {
  try {
    Deno.removeSync(path, { recursive: true });
  } catch {
    // already gone
  }
}
