/**
 * Native ResultPackage export: staged write, manifest published LAST, so an
 * interrupted export never appears complete. Import-side verification
 * re-checks every artifact byte against its digest, rejects staging
 * markers, unlisted files, and size disagreements. verifyNativeExport also
 * accepts the historical PYTHON export format (manifest kind is stable
 * across implementations) — the read-only backwards path.
 */

import { digestBytes, isPlainObject, pythonPrettyJson } from "../contracts/canonical.ts";
import { ContractError, DigestMismatchError } from "../contracts/common.ts";
import { isTerminal, runFromDict } from "../contracts/tasks.ts";
import { isFile, isSymlink } from "../contracts/fsutil.ts";
import {
  ExecutionService,
  KIND_EXECUTION_CONFIG,
  KIND_RESULT_PACKAGE,
  KIND_TASK_RUN,
} from "./execution.ts";
import type { ProjectWorkspace } from "./workspace.ts";

export const MANIFEST_NAME = "manifest.json";
export const INCOMPLETE_MARKER = ".vouch-export-incomplete";

/** Manifest artifact names: ONE safe filename segment — no traversal, no
 * separators, no hidden files, bounded charset (exports write this shape). */
export const SAFE_ARTIFACT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

const KIND_TASK_SPEC = "task-spec";

export function exportRun(
  workspace: ProjectWorkspace,
  runId: string,
  destination: string,
): string {
  Deno.mkdirSync(destination, { recursive: true });
  destination = Deno.realPathSync(destination);
  const marker = joinPath(destination, INCOMPLETE_MARKER);
  atomicWrite(marker, new TextEncoder().encode(`staged export of run ${runId}`));
  try {
    const runData = workspace.store.load(KIND_TASK_RUN, runId);
    if (runData === null) {
      const known = workspace.store.listIds(KIND_TASK_RUN);
      throw new ContractError(
        `unknown task run ${JSON.stringify(runId)}; nothing to export (runs: ${
          JSON.stringify(known)
        })`,
      );
    }
    const run = runFromDict(runData);
    let result = new ExecutionService(workspace).resultOf(runId);
    if (result === null) {
      const stored = workspace.store.load(KIND_RESULT_PACKAGE, runId);
      if (stored !== null) result = stored as never;
    }
    if (result === null) {
      throw new ContractError(`run ${JSON.stringify(runId)} has no result package to export`);
    }
    if (result.runId !== runId) {
      throw new ContractError(
        `refusing to export an unrelated result package: requested run ${JSON.stringify(runId)} ` +
          `but the package belongs to ${JSON.stringify(result.runId)}`,
      );
    }
    const spec = taskSpecOf(workspace, run);
    const configuration = workspace.store.load(KIND_EXECUTION_CONFIG, runId);
    const manifest = buildManifest(workspace, run, spec, configuration, result as never);

    for (const artifact of manifest["artifacts"] as Array<Record<string, unknown>>) {
      const payload = workspace.artifacts.get(String(artifact["digest"])); // verifies bytes
      artifact["bytes"] = payload.length;
      atomicWrite(joinPath(destination, String(artifact["file"])), payload);
    }
    atomicWrite(
      joinPath(destination, MANIFEST_NAME),
      new TextEncoder().encode(pythonPrettyJson(manifest) + "\n"),
    );
    Deno.removeSync(marker);
  } catch (exc) {
    // the marker stays: an interrupted export is visibly incomplete
    throw exc;
  }
  return joinPath(destination, MANIFEST_NAME);
}

/** Verify an exported native run directory (TS or historical Python). */
export function verifyNativeExport(destination: string): Record<string, unknown> {
  if (isSymlink(joinPath(destination, INCOMPLETE_MARKER))) {
    throw new ContractError(
      `export at ${destination} has a symbolic-link staging marker; refusing to verify`,
    );
  }
  if (isFile(joinPath(destination, INCOMPLETE_MARKER))) {
    throw new ContractError(
      `export at ${destination} is INCOMPLETE (staging marker present); an interrupted export ` +
        `never counts as a deliverable — re-export the run`,
    );
  }
  const manifestPath = joinPath(destination, MANIFEST_NAME);
  let manifest: unknown;
  if (isSymlink(manifestPath)) {
    throw new ContractError(
      `export manifest of ${destination} is a symbolic link; refusing to read through it`,
    );
  }
  if (!isFile(manifestPath)) {
    throw new ContractError(`no ${MANIFEST_NAME} in ${destination}: not a complete export`);
  }
  try {
    manifest = JSON.parse(Deno.readTextFileSync(manifestPath));
  } catch (exc) {
    throw new ContractError(`export manifest of ${destination} is not valid JSON: ${exc}`);
  }
  if (!isPlainObject(manifest) || manifest["kind"] !== "vouch-native-run-export") {
    throw new ContractError(`manifest of ${destination} is not a native run export`);
  }
  const listed = manifest["artifacts"];
  if (!Array.isArray(listed)) {
    throw new ContractError(`manifest of ${destination} lists no artifacts`);
  }
  const expected = new Set([MANIFEST_NAME]);
  for (const entry of listed) {
    if (!isPlainObject(entry)) {
      throw new ContractError(`manifest of ${destination} lists an invalid artifact entry`);
    }
    const name = String(entry["file"] ?? "");
    // Manifest-supplied names are untrusted: exactly one safe path segment
    // (no traversal, no separators, no hidden files), and the entry itself
    // must be a real regular file — never a link reading external bytes.
    if (!SAFE_ARTIFACT_NAME_RE.test(name) || name === MANIFEST_NAME) {
      throw new ContractError(
        `manifest of ${destination} lists an invalid artifact ${JSON.stringify(name)}`,
      );
    }
    expected.add(name);
    const path = joinPath(destination, name);
    if (isSymlink(path)) {
      throw new ContractError(
        `export artifact ${JSON.stringify(name)} is a symbolic link; refusing to read through it`,
      );
    }
    if (!isFile(path)) {
      throw new ContractError(`export ${destination} is missing artifact ${JSON.stringify(name)}`);
    }
    const bytes = Deno.readFileSync(path);
    const actual = digestBytes(bytes);
    if (actual !== entry["digest"]) {
      throw new DigestMismatchError(
        `export artifact ${JSON.stringify(name)} digests to ${actual}, manifest promised ` +
          `${entry["digest"]}`,
      );
    }
    if (bytes.length !== entry["bytes"]) {
      throw new ContractError(
        `export artifact ${JSON.stringify(name)} size disagrees with the manifest`,
      );
    }
  }
  const present = new Set(
    [...Deno.readDirSync(destination)].filter((e) => e.isFile).map((e) => e.name),
  );
  const sameSize = present.size === expected.size &&
    [...present].every((name) => expected.has(name));
  if (!sameSize) {
    throw new ContractError(
      `export ${destination} content does not match manifest: present ${
        JSON.stringify([...present].sort())
      }, ` +
        `expected ${JSON.stringify([...expected].sort())}`,
    );
  }
  return manifest;
}

function indexedSpecId(workspace: ProjectWorkspace, runId: string): string | null {
  const index = workspace.store.load("run-index", runId);
  if (isPlainObject(index)) {
    const specId = index["specId"];
    return typeof specId === "string" && specId.length > 0 ? specId : null;
  }
  return null;
}

function taskSpecOf(
  workspace: ProjectWorkspace,
  run: { runId: string; specId: string | null },
): Record<string, unknown> | null {
  const specId = run.specId ?? indexedSpecId(workspace, run.runId);
  if (specId === null) return null;
  return workspace.store.load(KIND_TASK_SPEC, specId);
}

function materialEntries(spec: Record<string, unknown> | null): Array<Record<string, unknown>> {
  if (spec === null) return [];
  const materials = (spec["inputs"] as Record<string, unknown>)?.["materials"];
  if (!Array.isArray(materials)) return [];
  const entries: Array<Record<string, unknown>> = [];
  for (const material of materials) {
    if (!isPlainObject(material)) continue;
    const attachment = material["attachment"];
    if (isPlainObject(attachment)) {
      entries.push({
        attachmentId: material["attachmentId"] ?? attachment["name"] ?? null,
        displayName: material["displayName"] ?? null,
        kind: attachment["kind"] ?? null,
        digest: attachment["contentDigest"] ?? null,
        sizeBytes: attachment["sizeBytes"] ?? null,
      });
    } else {
      entries.push({
        attachmentId: material["name"] ?? null,
        displayName: null,
        kind: material["kind"] ?? null,
        digest: material["contentDigest"] ?? null,
        sizeBytes: material["sizeBytes"] ?? null,
      });
    }
  }
  return entries;
}

function buildManifest(
  workspace: ProjectWorkspace,
  run: ReturnType<typeof runFromDict>,
  spec: Record<string, unknown> | null,
  configuration: Record<string, unknown> | null,
  result: Record<string, unknown>,
): Record<string, unknown> {
  const inputs = (spec?.["inputs"] as Record<string, unknown> | undefined) ?? {};
  const operationInputs: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(inputs)) {
    if (key !== "materials" && key !== "operation") operationInputs[key] = value;
  }
  const manifest: Record<string, unknown> = {
    schemaVersion: "2",
    kind: "vouch-native-run-export",
    runId: run.runId,
    taskSpecId: run.specId ?? indexedSpecId(workspace, run.runId),
    taskDigest: run.taskDigest,
    runStatus: run.status,
    terminal: isTerminal(run.status),
    mode: run.mode,
    title: spec?.["title"] ?? null,
    goal: spec?.["goal"] ?? null,
    materials: materialEntries(spec),
    operation: inputs["operation"] ?? null,
    operationInputs,
    configuration: configuration === null ? null : {
      providerName: configuration["providerName"] ?? null,
      scriptsDigest: configuration["scriptsDigest"] ?? null,
      runtimeId: configuration["runtimeId"] ?? null,
      toolVersion: configuration["toolVersion"] ?? null,
      isolated: configuration["isolated"] ?? null,
      mode: configuration["mode"] ?? null,
    },
    conclusion: result["conclusion"],
    doneItems: result["doneItems"] ?? [],
    notDoneItems: result["notDoneItems"] ?? [],
    uncertainties: result["uncertainties"] ?? [],
    externalActions: result["externalActions"] ?? {},
    totalCostUsd: result["totalCostUsd"] ?? null,
    completedConditionsCheck: result["completedConditionsCheck"] ?? {},
    deliverable: deliverableOf(result),
    resultCreatedAt: result["createdAt"] ?? null,
    runError: run.error,
    costEntries: workspace.journal.costEntries(run.runId).map((entry) => ({
      schemaVersion: entry.schemaVersion,
      entryId: entry.entryId,
      category: entry.category,
      subject: entry.subject,
      amountUsd: entry.amountUsd,
      humanMinutes: entry.humanMinutes,
      measurable: entry.measurable,
      mode: entry.mode,
      recordedAt: entry.recordedAt,
      note: entry.note,
    })),
    artifacts: [] as Array<Record<string, unknown>>,
  };
  const artifactRefs = (result["artifactRefs"] as string[]) ?? [];
  artifactRefs.forEach((digest, index) => {
    (manifest["artifacts"] as Array<Record<string, unknown>>).push({
      digest,
      file: artifactFilename(index, digest),
      bytes: 0,
    });
  });
  return manifest;
}

function deliverableOf(result: Record<string, unknown>): boolean {
  const checks = (result["completedConditionsCheck"] as Record<string, boolean>) ?? {};
  const values = Object.values(checks);
  const external = (result["externalActions"] as Record<string, string>) ?? {};
  return values.length > 0 && values.every(Boolean) &&
    !Object.values(external).some((s) => s === "pending");
}

function artifactFilename(index: number, digest: string): string {
  const keep = new Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-");
  const safe = [...digest].map((c) => (keep.has(c) ? c : "_")).join("");
  return `artifact-${String(index).padStart(3, "0")}-${safe.slice(0, 16)}.bin`;
}

function atomicWrite(path: string, payload: Uint8Array): void {
  const tmp = joinPath(
    path.slice(0, path.lastIndexOf("/")),
    `.vouch-export-${crypto.randomUUID().slice(0, 12)}`,
  );
  try {
    Deno.writeFileSync(tmp, payload);
    Deno.renameSync(tmp, path);
  } catch (exc) {
    try {
      Deno.removeSync(tmp);
    } catch {
      // nothing to clean
    }
    throw exc;
  }
}

function joinPath(dir: string, name: string): string {
  return dir.endsWith("/") ? `${dir}${name}` : `${dir}/${name}`;
}
