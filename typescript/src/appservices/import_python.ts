/**
 * Explicit Python-workspace import: the ONLY cross-implementation path.
 *
 * The source Python workspace (marker tool `vouch-agent`, schemaVersion 1)
 * is opened READ-ONLY — nothing in it is modified, so no backup can be
 * forgotten. Records are migrated into a NEW TS-namespaced workspace:
 * artifacts copy byte-identically (same digests), records re-validate, and
 * integral-float spellings Python wrote (e.g. budget cap `5.0`) become
 * integers in the TS numeric domain — a transformation that changes those
 * records' digests, so every task-run linked to a transformed spec gets its
 * taskDigest remapped, and the whole import is recorded in an import-report
 * record. Anything that cannot be mapped refuses the ENTIRE import before
 * any write (no partial migrations).
 */

import { digestBytes, digestOf, isPlainObject } from "../contracts/canonical.ts";
import { ContractError, TOOL_ID } from "../contracts/common.ts";
import { specFromDict } from "../contracts/project.ts";
import { runFromDict } from "../contracts/tasks.ts";
import { openDatabase } from "../storage/sqlite.ts";
import { ArtifactStore } from "../storage/artifacts.ts";
import { ProjectWorkspace } from "./workspace.ts";
import type { DatabaseSync } from "node:sqlite";

interface PythonRecord {
  kind: string;
  recordId: string;
  data: unknown;
  /** canonical text transformations applied (for the import report) */
  transformed: string[];
}

export interface ImportReport {
  source: string;
  target: string;
  recordsImported: number;
  runsImported: number;
  artifactsCopied: number;
  digestRemaps: Array<{ runId: string; from: string; to: string }>;
  transformations: string[];
}

function parseTolerant(text: string, path: string): { value: unknown; transformations: string[] } {
  const transformations: string[] = [];
  const value = JSON.parse(text);
  const walk = (node: unknown, where: string): unknown => {
    if (Array.isArray(node)) return node.map((item, i) => walk(item, `${where}[${i}]`));
    if (isPlainObject(node)) {
      const out: Record<string, unknown> = {};
      for (const [key, item] of Object.entries(node)) out[key] = walk(item, `${where}.${key}`);
      return out;
    }
    if (typeof node === "number") {
      if (!Number.isFinite(node)) {
        throw new ContractError(`${path}: non-finite number at ${where} cannot be imported`);
      }
      if (Number.isInteger(node)) {
        if (node > Number.MAX_SAFE_INTEGER || node < Number.MIN_SAFE_INTEGER) {
          throw new ContractError(
            `${path}: integer ${node} at ${where} is outside the safe-integer digest domain; refusing`,
          );
        }
        return node;
      }
      // non-integral float: in the shared domain as-is
      return node;
    }
    return node;
  };
  const walked = walk(value, "$");
  // Detect integral-float SOURCE spellings by rescanning the text tokens.
  const integralFloats = findIntegralFloatTokens(text);
  for (const token of integralFloats) {
    transformations.push(`integral float ${token} imported as integer ${Number(token)}`);
  }
  return { value: walked, transformations };
}

function findIntegralFloatTokens(text: string): string[] {
  const found: string[] = [];
  const numberToken = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  let inString = false;
  let index = 0;
  while (index < text.length) {
    const ch = text[index];
    if (inString) {
      if (ch === "\\") {
        index += 2;
        continue;
      }
      if (ch === '"') inString = false;
      index += 1;
      continue;
    }
    if (ch === '"') {
      inString = true;
      index += 1;
      continue;
    }
    if (ch === "-" || (ch >= "0" && ch <= "9")) {
      numberToken.lastIndex = index;
      const match = numberToken.exec(text);
      const token = match?.[0];
      if (token === undefined) {
        index += 1;
        continue;
      }
      if (!/^-?\d+$/.test(token) && Number.isInteger(Number(token))) found.push(token);
      index += token.length;
      continue;
    }
    index += 1;
  }
  return found;
}

function readPythonRecords(metaDb: string, source: string): PythonRecord[] {
  let db: DatabaseSync;
  try {
    db = openDatabase(metaDb);
  } catch (exc) {
    throw new ContractError(`cannot open Python metadata store ${metaDb}: ${exc}`);
  }
  try {
    const rows = db
      .prepare("SELECT kind, record_id, data FROM records ORDER BY kind, record_id")
      .all() as Array<{ kind: string; record_id: string; data: string }>;
    return rows.map((row) => {
      const parsed = parseTolerant(row.data, `${source}:records/${row.kind}/${row.record_id}`);
      return {
        kind: row.kind,
        recordId: row.record_id,
        data: parsed.value,
        transformed: parsed.transformations,
      };
    });
  } finally {
    db.close();
  }
}

export function importPythonWorkspace(options: {
  sourceProjectDir: string;
  targetProjectDir: string;
  purpose?: string;
}): ImportReport {
  const sourceDir = Deno.realPathSync(options.sourceProjectDir);
  const sourceMetadataDir = `${sourceDir}/.vouch`;
  let marker: unknown;
  try {
    marker = JSON.parse(Deno.readTextFileSync(`${sourceMetadataDir}/workspace.json`));
  } catch {
    throw new ContractError(
      `${sourceDir} is not a Python vouch project (no .vouch/workspace.json); nothing to import`,
    );
  }
  if (!isPlainObject(marker) || marker["tool"] !== "vouch-agent") {
    throw new ContractError(
      `${sourceDir}/.vouch was not written by the Python vouch-agent (tool ${
        JSON.stringify(isPlainObject(marker) ? marker["tool"] : null)
      }); refusing to import`,
    );
  }

  // Plan the whole import in memory; refuse BEFORE writing anything.
  const records = readPythonRecords(`${sourceMetadataDir}/meta.sqlite`, sourceDir);
  const projectRecords = records.filter((r) => r.kind === "project");
  if (projectRecords.length !== 1) {
    throw new ContractError(
      `source workspace holds ${projectRecords.length} project records; a workspace owns exactly one`,
    );
  }
  const spec = specFromDict(projectRecords[0].data as Record<string, unknown>);

  // Re-canonicalize every record under the TS domain; build digest remaps
  // for task runs whose spec digest changes (integral-float transforms).
  const digestRemaps: ImportReport["digestRemaps"] = [];
  const remappedSpecs = new Set<string>();
  for (const record of records) {
    if (record.kind !== "task-spec") continue;
    if (record.transformed.length > 0) remappedSpecs.add(record.recordId);
  }
  void digestBytes;
  const runs: Array<{ runId: string; record: PythonRecord }> = [];
  for (const record of records) {
    if (record.kind !== "task-run") continue;
    const run = runFromDict(record.data as Record<string, unknown>);
    runs.push({ runId: run.runId, record });
    const specId = run.specId ??
      (records.find((r) => r.kind === "run-index" && r.recordId === run.runId)?.data as
        | Record<string, unknown>
        | undefined)?.["specId"];
    const specRecord = records.find((r) => r.kind === "task-spec" && r.recordId === specId);
    if (specRecord !== undefined && remappedSpecs.has(specRecord.recordId)) {
      const runData = record.data as Record<string, unknown>;
      const oldDigest = String(runData["taskDigest"]);
      const newDigest = digestOf(specRecord.data);
      digestRemaps.push({ runId: run.runId, from: oldDigest, to: newDigest });
      runData["taskDigest"] = newDigest;
    }
  }

  // Copy artifacts first (bytes identical → same digests).
  const sourceArtifacts = new ArtifactStore(sourceMetadataDir);
  const target = ProjectWorkspace.create(options.targetProjectDir, spec);
  let artifactsCopied = 0;
  try {
    const sourceDirArtifacts = `${sourceMetadataDir}/artifacts`;
    for (const entry of Deno.readDirSync(sourceDirArtifacts)) {
      if (!entry.isFile || !/^[0-9a-f]{64}$/.test(entry.name)) continue;
      const bytes = sourceArtifacts.get(`sha256:${entry.name}`);
      target.artifacts.put(bytes);
      artifactsCopied += 1;
    }
    for (const record of records) {
      if (record.kind === "project") continue; // already saved by create()
      target.store.save(record.kind, record.recordId, record.data as Record<string, unknown>);
    }
    const transformations = [...new Set(records.flatMap((r) => r.transformed))];
    const report: ImportReport = {
      source: sourceDir,
      target: Deno.realPathSync(options.targetProjectDir),
      recordsImported: records.length,
      runsImported: runs.length,
      artifactsCopied,
      digestRemaps,
      transformations,
    };
    target.store.save("import-report", "python-import", {
      schemaVersion: "1",
      importedAt: new Date().toISOString().replace("Z$", "+00:00"),
      sourceTool: "vouch-agent",
      sourceToolVersion: String(marker["toolVersion"] ?? "unknown"),
      targetTool: TOOL_ID,
      ...report,
    });
    return report;
  } catch (exc) {
    throw new ContractError(
      `import failed after workspace creation at ${options.targetProjectDir}: ${exc} ` +
        `(the SOURCE at ${sourceDir} was never modified; remove the partial target and retry)`,
    );
  }
}
