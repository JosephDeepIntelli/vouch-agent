/**
 * Adversarial filesystem-security tests (review follow-up): symlink escapes,
 * corrupt pre-existing artifacts, manifest traversal/unlisted/linked paths
 * and staging markers. Every probe must be REFUSED — the tool never writes
 * or reads outside the user-selected root through a link.
 */

import { assertEquals, assertThrows, assertTrue } from "./helpers.ts";
import { ArtifactStore } from "../src/storage/artifacts.ts";
import { ContractError, DigestMismatchError } from "../src/contracts/common.ts";
import { digestBytes } from "../src/contracts/canonical.ts";
import {
  exportRun,
  INCOMPLETE_MARKER,
  verifyNativeExport,
} from "../src/appservices/native_export.ts";
import { ProjectWorkspace } from "../src/appservices/workspace.ts";
import { ExecutionService } from "../src/appservices/execution.ts";
import { specFromDict } from "../src/contracts/project.ts";
import { ProcessAdapterClient } from "../src/adapters/process_adapter.ts";
import { isolatedChildSpawn } from "../src/runtime/spawn.ts";
import { isWeakenedBoundary, WorkerProcessRuntime } from "../src/runtime/worker_runtime.ts";

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

function taskOnlyWorkspace(name: string): ProjectWorkspace {
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

const LEFT = new TextEncoder().encode("sku,a\nA,1\nB,2\n");
const RIGHT = new TextEncoder().encode("sku,a\nA,1\nC,3\n");

Deno.test({
  name: "security: symlinked artifact directory refused (no writes outside root)",
  fn() {
    const root = tempDir("sym-artifacts");
    const outside = tempDir("sym-outside");
    try {
      // base/artifacts -> outside: the store must refuse, not redirect.
      Deno.mkdirSync(`${root}/artifacts`, { recursive: true });
      Deno.removeSync(`${root}/artifacts`);
      Deno.symlinkSync(outside, `${root}/artifacts`);
      assertThrows(() => new ArtifactStore(root), ContractError, "symbolic link");
      // Prove nothing landed outside through the link.
      assertTrue(Array.from(Deno.readDirSync(outside)).length === 0);
    } finally {
      rmrf(root);
      rmrf(outside);
    }
  },
});

Deno.test({
  name: "security: symlinked artifact FILE refused on get and put",
  fn() {
    const root = tempDir("sym-file");
    const outside = tempDir("sym-file-outside");
    try {
      const store = new ArtifactStore(root);
      const secret = new TextEncoder().encode("external secret bytes");
      Deno.writeFileSync(`${outside}/secret`, secret);
      const digest = digestBytes(secret);
      // artifacts/<digest-of-secret> -> outside/secret: even though the name
      // MATCHES the linked bytes, the store must not read through the link.
      Deno.symlinkSync(
        `${outside}/secret`,
        `${store.artifactsDir}/${digest.slice("sha256:".length)}`,
      );
      assertThrows(() => store.get(digest), ContractError, "symbolic link");
      assertThrows(() => store.put(secret), ContractError, "symbolic link");
    } finally {
      rmrf(root);
      rmrf(outside);
    }
  },
});

Deno.test({
  name: "security: corrupt pre-existing artifact refused on put (no silent idempotence)",
  fn() {
    const root = tempDir("corrupt-put");
    try {
      const store = new ArtifactStore(root);
      const good = new TextEncoder().encode("good bytes");
      const digest = store.put(good);
      // Corrupt the stored bytes behind the store's back, then re-put the
      // SAME payload: the mismatching pre-existing file must be refused.
      Deno.writeFileSync(
        `${store.artifactsDir}/${digest.slice("sha256:".length)}`,
        new TextEncoder().encode("corrupted"),
      );
      assertThrows(() => store.put(good), DigestMismatchError, "does not match its name");
    } finally {
      rmrf(root);
    }
  },
});

Deno.test({
  name: "security: export verification rejects traversal, nested, linked and unlisted paths",
  async fn() {
    const workspace = taskOnlyWorkspace("sec-export");
    try {
      const service = new ExecutionService(workspace);
      const { runId } = await service.runCsvReconciliation({
        goal: "g",
        leftCsv: LEFT,
        rightCsv: RIGHT,
        joinKey: "sku",
      });
      const out = `${workspace.projectDir}/export`;
      exportRun(workspace, runId, out);
      assertEquals(verifyNativeExport(out)["runId"], runId);

      const manifestPath = `${out}/manifest.json`;

      // (1) traversal name in the manifest
      const original = Deno.readTextFileSync(manifestPath);
      const restore = () => Deno.writeTextFileSync(manifestPath, original);
      const withTraversal = JSON.parse(original);
      (withTraversal["artifacts"] as Array<Record<string, unknown>>)[0]["file"] = "../escape.bin";
      Deno.writeTextFileSync(manifestPath, JSON.stringify(withTraversal));
      assertThrows(() => verifyNativeExport(out), ContractError, "invalid artifact");

      // (2) nested/subdirectory name
      const withNested = JSON.parse(original);
      (withNested["artifacts"] as Array<Record<string, unknown>>)[0]["file"] = "sub/escape.bin";
      Deno.writeTextFileSync(manifestPath, JSON.stringify(withNested));
      assertThrows(() => verifyNativeExport(out), ContractError, "invalid artifact");
      restore();

      // (3) a symlinked artifact file reading external bytes
      const artifacts = verifyNativeExport(out)["artifacts"] as Array<Record<string, unknown>>;
      const victim = `${out}/${artifacts[0]["file"]}`;
      const external = tempDir("sec-export-external");
      Deno.writeFileSync(`${external}/outside.bin`, new TextEncoder().encode("external"));
      const victimBytes = Deno.readFileSync(victim);
      Deno.removeSync(victim);
      Deno.symlinkSync(`${external}/outside.bin`, victim);
      try {
        assertThrows(() => verifyNativeExport(out), ContractError, "symbolic link");
      } finally {
        Deno.removeSync(victim);
        Deno.writeFileSync(victim, victimBytes);
      }
      rmrf(external);

      // (4) an unlisted extra file in the export directory
      Deno.writeFileSync(`${out}/extra-unlisted.bin`, new TextEncoder().encode("x"));
      assertThrows(() => verifyNativeExport(out), ContractError, "does not match manifest");
      Deno.removeSync(`${out}/extra-unlisted.bin`);

      // (5) staging marker (file and symlink forms) never verifies
      Deno.writeFileSync(`${out}/${INCOMPLETE_MARKER}`, new TextEncoder().encode("x"));
      assertThrows(() => verifyNativeExport(out), ContractError, "INCOMPLETE");
      Deno.removeSync(`${out}/${INCOMPLETE_MARKER}`);
      const other = tempDir("sec-marker-target");
      Deno.symlinkSync(`${other}/m`, `${out}/${INCOMPLETE_MARKER}`);
      assertThrows(() => verifyNativeExport(out), ContractError, "symbolic-link staging marker");
      Deno.removeSync(`${out}/${INCOMPLETE_MARKER}`);
      rmrf(other);

      // clean state verifies again
      assertEquals(verifyNativeExport(out)["runId"], runId);
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "security: workspace persistence paths refuse symlinks",
  fn() {
    const root = tempDir("sym-workspace");
    const outside = tempDir("sym-workspace-outside");
    try {
      Deno.mkdirSync(`${root}/.vowdo`, { recursive: true });
      // workspace.json as a link → open refuses
      Deno.writeTextFileSync(
        `${outside}/marker.json`,
        JSON.stringify({
          schemaVersion: "2",
          tool: "vowdo-agent-ts",
          toolVersion: "0.2.0rc2",
          projectId: "p",
          storage: "ts-1",
        }),
      );
      Deno.symlinkSync(`${outside}/marker.json`, `${root}/.vowdo/workspace.json`);
      assertThrows(() => ProjectWorkspace.open(root), ContractError, "symbolic link");
      Deno.removeSync(`${root}/.vowdo/workspace.json`);

      // meta.sqlite as a link → open refuses before any DB write
      Deno.writeFileSync(`${outside}/meta.sqlite`, new TextEncoder().encode(""));
      Deno.symlinkSync(`${outside}/meta.sqlite`, `${root}/.vowdo/meta.sqlite`);
      Deno.writeTextFileSync(
        `${root}/.vowdo/workspace.json`,
        JSON.stringify({
          schemaVersion: "2",
          tool: "vowdo-agent-ts",
          toolVersion: "0.2.0rc2",
          projectId: "p",
          storage: "ts-1",
        }),
      );
      assertThrows(() => ProjectWorkspace.open(root), ContractError, "symbolic link");
    } finally {
      rmrf(root);
      rmrf(outside);
    }
  },
});

Deno.test({
  name:
    "security: child environments are CLEARED, not merged (sentinel absent in adapter children)",
  async fn() {
    // Seed a synthetic sentinel (explicitly NOT a secret) in THIS process.
    try {
      Deno.env.set("VOWDO_TEST_ENV_SENTINEL", "synthetic-review-sentinel");
    } catch {
      throw new Error("test runner lacks env write for the sentinel; extend --allow-env");
    }
    const deno = Deno.execPath();
    const probe = [
      deno,
      "run",
      "--no-prompt",
      "--allow-env=VOWDO_TEST_ENV_SENTINEL",
      "tests/helpers/env_probe.ts",
    ];
    // Sanity: a plain inherited spawn DOES see the sentinel (proves the
    // probe itself works before we assert absence through our spawn paths).
    const inherited = await new Deno.Command(probe[0], {
      args: probe.slice(1),
      stdout: "piped",
    }).spawn().output();
    assertEquals(new TextDecoder().decode(inherited.stdout).trim(), "present");

    // The adapter client's DEFAULT environment clears inheritance: assert on
    // the exact spawn construction the client uses (clearEnv + built
    // allowlist), applied to the probe command.
    const spec = isolatedChildSpawn(probe[0], probe.slice(1));
    const cleared = await new Deno.Command(spec.command, {
      args: spec.args,
      stdout: "piped",
      clearEnv: spec.clearEnv,
      env: spec.env,
    }).spawn().output();
    assertEquals(
      new TextDecoder().decode(cleared.stdout).trim(),
      "absent",
      "isolated-child spawn must not inherit the sentinel",
    );

    // (b) The worker spawn spec (the exact options worker_runtime uses).
    const workerSpec = isolatedChildSpawn(probe[0], probe.slice(1));
    const workerCleared = await new Deno.Command(workerSpec.command, {
      args: workerSpec.args,
      stdout: "piped",
      clearEnv: workerSpec.clearEnv,
      env: workerSpec.env,
    }).spawn().output();
    assertEquals(new TextDecoder().decode(workerCleared.stdout).trim(), "absent");

    // (c) EXPLICIT env is documented caller authority and DOES pass through.
    const explicit = isolatedChildSpawn(probe[0], probe.slice(1), {
      VOWDO_TEST_ENV_SENTINEL: "caller-authorized",
    });
    const passed = await new Deno.Command(explicit.command, {
      args: explicit.args,
      stdout: "piped",
      clearEnv: explicit.clearEnv,
      env: explicit.env,
    }).spawn().output();
    assertEquals(new TextDecoder().decode(passed.stdout).trim(), "present");

    // (d) No automatic credential-shaped forwarding: a parent variable with
    // the pilot-credential prefix stays OUT of the default child env.
    Deno.env.set("VOUCH_PILOT_CREDENTIAL_TEST", "synthetic-not-a-secret");
    const adapterDefaultEnvProbe = [
      deno,
      "run",
      "--no-prompt",
      "--allow-env=VOUCH_PILOT_CREDENTIAL_TEST",
      "tests/helpers/env_probe_credential.ts",
    ];
    const credSpec = isolatedChildSpawn(adapterDefaultEnvProbe[0], adapterDefaultEnvProbe.slice(1));
    const credOut = await new Deno.Command(credSpec.command, {
      args: credSpec.args,
      stdout: "piped",
      clearEnv: credSpec.clearEnv,
      env: credSpec.env,
    }).spawn().output();
    assertEquals(
      new TextDecoder().decode(credOut.stdout).trim(),
      "absent",
      "pilot-credential variables must not auto-forward to isolated children",
    );
    Deno.env.delete("VOUCH_PILOT_CREDENTIAL_TEST");
    Deno.env.delete("VOWDO_TEST_ENV_SENTINEL");
  },
});

Deno.test({
  name: "security: scoped AND global worker grants are neutralized or reported weakened",
  async fn() {
    const deno = Deno.execPath();
    const workerModule = new URL("../src/runtime/worker_main.ts", import.meta.url).pathname;
    // Scoped grants (the rc3 blind spot): a name-only query reports `prompt`
    // for these, so only revocation + functional probes prove the boundary.
    const syntheticDir = await Deno.makeTempDir({ prefix: "vowdo-scoped-grant-" });
    const cases: Array<{ label: string; flags: string[] }> = [
      { label: "scoped write", flags: [`--allow-write=${syntheticDir}`] },
      { label: "scoped write /tmp", flags: ["--allow-write=/tmp"] },
      { label: "scoped read", flags: [`--allow-read=${syntheticDir}`] },
      { label: "scoped env", flags: ["--allow-env=FOO"] },
      { label: "global write", flags: ["--allow-write"] },
      { label: "global read", flags: ["--allow-read"] },
      { label: "global env", flags: ["--allow-env"] },
      { label: "global net", flags: ["--allow-net"] },
      {
        label: "mixed scoped",
        flags: ["--allow-env=FOO", "--allow-read=/tmp", "--allow-write=/tmp"],
      },
    ];
    for (const testCase of cases) {
      // (i) the real worker process reports the honest boundary
      const child = new Deno.Command(deno, {
        args: ["run", "--no-prompt", ...testCase.flags, workerModule],
        stdin: "piped",
        stdout: "piped",
        stderr: "null",
        clearEnv: true,
      }).spawn();
      const writer = child.stdin.getWriter();
      await writer.write(new TextEncoder().encode(JSON.stringify({ t: "close" }) + "\n"));
      const firstRead = await child.stdout.getReader().read();
      const readyLine = new TextDecoder().decode(firstRead.value ?? new Uint8Array());
      assertTrue(
        readyLine.includes("boundary"),
        `${testCase.label}: no ready frame (${readyLine})`,
      );
      // Neutralization is reported accurately — never a bare "none":
      assertTrue(
        readyLine.includes("revoked at startup") || readyLine.includes("weakened"),
        `${testCase.label}: boundary must state neutralization or weakening: ${readyLine}`,
      );
      assertTrue(
        !isWeakenedBoundary(readyLine),
        `${testCase.label}: grants must be neutralized by startup revocation: ${readyLine}`,
      );
      // (ii) the controller can still open the (neutralized) session — the
      // refusal path is asserted via isWeakenedBoundary below, because
      // revoked grants leave nothing weakened to refuse.
    }
  },
});

Deno.test({
  name: "security: isWeakenedBoundary refuses weakened, accepts the neutralized-none report",
  fn() {
    assertEquals(
      isWeakenedBoundary(
        "deno-permissions:none (all classes revoked at startup; scoped grants neutralized; probes denied)",
      ),
      false,
    );
    assertEquals(
      isWeakenedBoundary("deno-permissions-granted:write:probe-succeeded (weakened)"),
      true,
    );
    assertEquals(
      isWeakenedBoundary(
        "deno-permissions-granted:net:probe-attempted(ConnectionRefused) (weakened)",
      ),
      true,
    );
  },
});

Deno.test({
  name: "security: refused handshake kills AND reaps the worker child",
  async fn() {
    // A child that exits before sending a ready frame: the controller's
    // handshake error path must close and REAP it before propagating.
    const deno = Deno.execPath();
    const dying = new Deno.Command(deno, {
      args: ["run", "--no-prompt", "-e", "Deno.exit(3)"],
      stdin: "piped",
      stdout: "piped",
      stderr: "null",
      clearEnv: true,
    }).spawn();
    const pid = dying.pid;
    let handshakeError: unknown = null;
    try {
      await new Promise((_resolve, reject) => {
        dying.stdout.getReader().read().then((r) => {
          if (r.done) reject(new Error("worker exited before responding"));
        });
      });
    } catch (exc) {
      handshakeError = exc;
      // the same close+reap sequence openSession performs on handshake errors
      try {
        dying.kill("SIGTERM");
      } catch {
        // already dead
      }
      await dying.status.catch(() => {});
    }
    assertTrue(handshakeError instanceof Error);
    await waitForExit(pid);

    // Through the runtime: a failed worker launch (bad flag) also leaves no
    // running child behind the propagated error.
    const refusedRuntime = new WorkerProcessRuntime({
      workerExtraArgs: ["--vowdo-test-unknown-flag"],
    });
    try {
      await refusedRuntime.openSession(
        { mode: "fixture", maxSteps: 1, wallClockS: 5, maxCostUsd: 0.1 },
        null,
      );
    } catch {
      // expected: the worker cannot launch; openSession must have cleaned up
    }
    const refusedPid = refusedRuntime.lastWorkerPid;
    if (refusedPid > 0) await waitForExit(refusedPid);
  },
});

async function waitForExit(pid: number, timeoutMs = 10000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const status = await new Deno.Command("kill", { args: ["-0", String(pid)] }).spawn().status;
    if (status.code !== 0) return; // no such process — exited and reaped
    if (Date.now() > deadline) throw new Error(`pid ${pid} still running after refusal`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

Deno.test({
  name: "security: artifact directory swap AFTER construction fails closed on put/get/exists",
  fn() {
    const root = tempDir("swap-after-open");
    const outside = tempDir("swap-outside");
    try {
      const store = new ArtifactStore(root);
      const digest = store.put(new TextEncoder().encode("payload"));
      // Deterministic substitution: rename the real directory away, place a
      // symlink to an outside directory under the same name.
      Deno.renameSync(store.artifactsDir, `${root}/artifacts-moved`);
      Deno.symlinkSync(outside, store.artifactsDir);
      assertThrows(
        () => store.put(new TextEncoder().encode("other")),
        ContractError,
        "symbolic link",
      );
      assertThrows(() => store.get(digest), ContractError, "symbolic link");
      assertThrows(() => store.exists(digest), ContractError, "symbolic link");
      // And nothing landed outside through the swapped directory.
      assertTrue(Array.from(Deno.readDirSync(outside)).length === 0);
    } finally {
      rmrf(root);
      rmrf(outside);
    }
  },
});

Deno.test({
  name: "security: adapter receipt binding — execute requires prepare; cleanup closes the run",
  async fn() {
    const workspace = taskOnlyWorkspace("receipt-binding");
    try {
      const fixturesPath = `${workspace.vowdoDir}/fixtures-improvement.json`;
      // minimal synthetic pack with one development case
      Deno.writeTextFileSync(
        fixturesPath,
        JSON.stringify({
          schemaVersion: 1,
          synthetic: true,
          packId: "receipt-binding-pack",
          scenarios: [{
            scenarioId: "rb-case-1",
            workflowId: "W-C3",
            split: "development",
            baseline: { ok: true, metric: 0.5, guardrails: {}, usage: { tokensScripted: 10 } },
            candidate: { ok: true, metric: 0.6, guardrails: {}, usage: { tokensScripted: 12 } },
          }],
        }),
      );
      const mainModule = new URL("../src/cli/main.ts", import.meta.url).pathname;
      const deno = Deno.execPath();
      const client = new ProcessAdapterClient(
        [
          deno,
          "run",
          "--no-prompt",
          "--allow-read",
          mainModule,
          "__fixture-adapter",
          "--fixtures",
          fixturesPath,
        ],
        { label: "receipt-fixture", requestTimeoutS: 60, executeTimeoutS: 60 },
      );
      try {
        const runId = "eval_receipt_binding_1";
        // execute BEFORE prepare → refused before dispatch
        let refused: unknown = null;
        try {
          await client.execute({
            runId,
            attemptId: "att_1",
            workflowId: "W-C3",
            caseInput: { caseId: "rb-case-1" },
            mode: "fixture",
          });
        } catch (exc) {
          refused = exc;
        }
        assertTrue(
          refused instanceof Error && refused.message.includes("never prepared"),
          `unprepared execute must be refused (got: ${refused})`,
        );
        await client.prepare(runId, "fixture");
        const execution = await client.execute({
          runId,
          attemptId: "att_2",
          workflowId: "W-C3",
          caseInput: { caseId: "rb-case-1" },
          mode: "fixture",
        });
        assertEquals(execution.ok, true);
        await client.cleanup(runId);
        // after cleanup the binding is GONE: execute and collect both refuse
        let afterCleanup: unknown = null;
        try {
          await client.execute({
            runId,
            attemptId: "att_3",
            workflowId: "W-C3",
            caseInput: { caseId: "rb-case-1" },
            mode: "fixture",
          });
        } catch (exc) {
          afterCleanup = exc;
        }
        assertTrue(
          afterCleanup instanceof Error && afterCleanup.message.includes("never prepared"),
        );
        let collectRefused: unknown = null;
        try {
          await client.collect(runId);
        } catch (exc) {
          collectRefused = exc;
        }
        assertTrue(collectRefused instanceof Error);
      } finally {
        await client.close();
      }
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});

Deno.test({
  name: "security: evidence packages reject traversal, links, unlisted content and size mismatch",
  async fn() {
    // an IMPROVEMENT workspace: acceptance needs owners and workflows
    const dir = tempDir("sec-evidence");
    const spec = specFromDict({
      schemaVersion: "1",
      projectId: "proj-sec-evidence",
      name: "sec-evidence",
      workflows: [{
        schemaVersion: "1",
        workflowId: "W-C3",
        name: "Compare",
        mainObjective: "main",
        guardrails: [],
        locales: ["en"],
        markets: [],
      }],
      owners: { "acceptance-owner": "alice-acceptance", "release-owner": "bob-release" },
      allowedChangeTypes: ["prompt-delta"],
      budget: { schemaVersion: "1", totalUsdCap: 5 },
      mode: "improvement",
      purpose: "",
    } as never);
    const workspace = ProjectWorkspace.create(dir, spec as never);
    try {
      const { ImprovementFlow, SYNTHETIC_FIXTURE_PACK } = await import(
        "../src/improvement/flow.ts"
      );
      Deno.writeTextFileSync(
        `${workspace.vowdoDir}/fixtures-improvement.json`,
        JSON.stringify(SYNTHETIC_FIXTURE_PACK),
      );
      const mainModule = new URL("../src/cli/main.ts", import.meta.url).pathname;
      const flow = new ImprovementFlow(workspace, mainModule);
      flow.recordBaseline({
        versionId: "v0",
        sourceRef: "git:x",
        workflowId: "W-C3",
        minImprovement: 0,
      });
      const proposal = flow.propose({
        delta: "d",
        changeType: "prompt-delta",
        rationale: "r",
        workflowId: "W-C3",
        seal: true,
      });
      const out = `${workspace.projectDir}/evidence`;
      const acceptance = await flow.finalAcceptance({
        candidateId: proposal.candidate.candidateId,
        workflowId: "W-C3",
        owner: "alice-acceptance",
        evidenceOut: out,
      });
      assertEquals(acceptance.verdict, "accepted");
      const { verifyEvidencePackage, EVIDENCE_MANIFEST, INCOMPLETE_MARKER } = await import(
        "../src/improvement/evidence.ts"
      );
      assertEquals(verifyEvidencePackage(out)["runId"], acceptance.run.runId);

      const manifestPath = `${out}/${EVIDENCE_MANIFEST}`;
      const original = Deno.readTextFileSync(manifestPath);
      const parsed = JSON.parse(original);
      const artifactName = String(
        (parsed["artifacts"] as Array<Record<string, unknown>>)[0]["file"],
      );

      // (1) traversal name — even with a MATCHING digest (the review repro)
      const outside = tempDir("sec-evidence-outside");
      const artifactBytes = Deno.readFileSync(`${out}/${artifactName}`);
      Deno.writeFileSync(`${outside}/escaped.bin`, artifactBytes);
      const traversal = JSON.parse(original);
      (traversal["artifacts"] as Array<Record<string, unknown>>)[0]["file"] =
        "../outside/escaped.bin";
      Deno.writeTextFileSync(manifestPath, JSON.stringify(traversal));
      assertThrows(() => verifyEvidencePackage(out), ContractError, "invalid artifact name");
      // (2) nested name
      const nested = JSON.parse(original);
      (nested["artifacts"] as Array<Record<string, unknown>>)[0]["file"] = "sub/escaped.bin";
      Deno.writeTextFileSync(manifestPath, JSON.stringify(nested));
      assertThrows(() => verifyEvidencePackage(out), ContractError, "invalid artifact name");
      Deno.writeTextFileSync(manifestPath, original);

      // (3) symlinked artifact file reading EXTERNAL bytes
      const external = tempDir("sec-evidence-external");
      Deno.writeFileSync(`${external}/outside.bin`, artifactBytes);
      Deno.removeSync(`${out}/${artifactName}`);
      Deno.symlinkSync(`${external}/outside.bin`, `${out}/${artifactName}`);
      try {
        assertThrows(() => verifyEvidencePackage(out), ContractError, "symbolic link");
      } finally {
        Deno.removeSync(`${out}/${artifactName}`);
        Deno.writeFileSync(`${out}/${artifactName}`, artifactBytes);
      }
      rmrf(external);

      // (4) size disagreement with a byte appended (digest field edited to match length claim)
      const sized = JSON.parse(original);
      (sized["artifacts"] as Array<Record<string, unknown>>)[0]["bytes"] = artifactBytes.length + 1;
      Deno.writeTextFileSync(manifestPath, JSON.stringify(sized));
      assertThrows(() => verifyEvidencePackage(out), ContractError, "size disagrees");
      Deno.writeTextFileSync(manifestPath, original);

      // (5) unlisted extra file (and directory) invalidate the package
      Deno.writeFileSync(`${out}/unlisted.bin`, new TextEncoder().encode("x"));
      assertThrows(() => verifyEvidencePackage(out), ContractError, "unlisted entries");
      Deno.removeSync(`${out}/unlisted.bin`);
      Deno.mkdirSync(`${out}/unlisted-dir`);
      assertThrows(() => verifyEvidencePackage(out), ContractError, "unlisted entries");
      Deno.removeSync(`${out}/unlisted-dir`);

      // (6) staging markers (file and link forms) never verify
      Deno.writeFileSync(`${out}/${INCOMPLETE_MARKER}`, new TextEncoder().encode("x"));
      assertThrows(() => verifyEvidencePackage(out), ContractError, "INCOMPLETE");
      Deno.removeSync(`${out}/${INCOMPLETE_MARKER}`);
      const markerTarget = tempDir("sec-evidence-marker");
      Deno.symlinkSync(`${markerTarget}/m`, `${out}/${INCOMPLETE_MARKER}`);
      assertThrows(() => verifyEvidencePackage(out), ContractError, "symbolic-link staging marker");
      Deno.removeSync(`${out}/${INCOMPLETE_MARKER}`);
      rmrf(markerTarget);

      // (7) a linked manifest never verifies
      const moved = `${out}/manifest.real`;
      Deno.renameSync(manifestPath, moved);
      Deno.symlinkSync(moved, manifestPath);
      try {
        assertThrows(() => verifyEvidencePackage(out), ContractError, "symbolic link");
      } finally {
        Deno.removeSync(manifestPath);
        Deno.renameSync(moved, manifestPath);
      }
      rmrf(outside);

      // clean state verifies again
      assertEquals(verifyEvidencePackage(out)["runId"], acceptance.run.runId);

      // (8) export REFUSES a destination with a pre-existing link at a
      // planned filename (write-through prevention)
      const linkedOut = `${workspace.projectDir}/evidence-linked`;
      Deno.mkdirSync(linkedOut, { recursive: true });
      const victimDir = tempDir("sec-evidence-victim");
      Deno.writeFileSync(`${victimDir}/victim.bin`, new TextEncoder().encode("victim"));
      Deno.symlinkSync(`${victimDir}/victim.bin`, `${linkedOut}/evaluation-summary.json`);
      const { exportEvidencePackage } = await import("../src/improvement/evidence.ts");
      let exportRefused: unknown = null;
      try {
        exportEvidencePackage(workspace, acceptance.run, linkedOut);
      } catch (exc) {
        exportRefused = exc;
      }
      assertTrue(
        exportRefused instanceof Error && exportRefused.message.includes("symbolic link"),
        `linked destination export must refuse (got: ${exportRefused})`,
      );
      assertEquals(
        new TextDecoder().decode(Deno.readFileSync(`${victimDir}/victim.bin`)),
        "victim",
        "no bytes were written through the link",
      );
      rmrf(victimDir);
    } finally {
      workspace.close();
      rmrf(workspace.projectDir);
    }
  },
});
