/**
 * Evidence package export/verify — the manifest digest is the anchor an
 * acceptance decision binds to (INTEGRITY anchor only: it proves the bytes
 * an acceptance decision pointed at; it does not authenticate authorship).
 * Artifacts travel as digest-verified bytes with safe single-segment names
 * (shared rule with native exports); a package that does not verify —
 * traversal names, linked entries, unlisted content, size mismatches, or a
 * leftover staging marker — can never back a verdict.
 */

import { digestBytes, digestOf, isPlainObject, pythonPrettyJson } from "../contracts/canonical.ts";
import { ContractError, DigestMismatchError } from "../contracts/common.ts";
import { isFile, isSymlink } from "../contracts/fsutil.ts";
import { SAFE_ARTIFACT_NAME_RE } from "../appservices/native_export.ts";
import type { EvaluationRun } from "./controller.ts";
import type { ProjectWorkspace } from "../appservices/workspace.ts";

export const EVIDENCE_MANIFEST = "evidence-manifest.json";
export const INCOMPLETE_MARKER = ".vowdo-export-incomplete";

export interface EvidencePackage {
  manifestDigest: string;
  directory: string;
  artifactCount: number;
}

export function exportEvidencePackage(
  workspace: ProjectWorkspace,
  run: EvaluationRun,
  destination: string,
): EvidencePackage {
  Deno.mkdirSync(destination, { recursive: true });
  // Destination descendants this export will write must not be pre-existing
  // links: writing "through" them would touch files outside the destination.
  // Every planned filename is known up front here.
  const plannedNames = [EVIDENCE_MANIFEST, INCOMPLETE_MARKER, "evaluation-summary.json"];
  for (const name of plannedNames) {
    const path = `${destination}/${name}`;
    if (isSymlink(path)) {
      throw new ContractError(
        `evidence export destination contains a symbolic link at ${JSON.stringify(name)}; ` +
          `refusing to write through it`,
      );
    }
  }
  Deno.writeFileSync(
    `${destination}/${INCOMPLETE_MARKER}`,
    new TextEncoder().encode(`staged evidence export of run ${run.runId}`),
  );
  try {
    const artifacts: Array<Record<string, unknown>> = [];
    const files: Array<{ digest: string; bytes: Uint8Array; kind: string; name: string }> = [];
    const summaryBytes = new TextEncoder().encode(
      pythonPrettyJson({
        runId: run.runId,
        candidateDigest: run.candidateDigest,
        rubricDigest: run.rubricDigest,
        caseSetDigest: run.caseSetDigest,
        split: run.split,
        verdict: run.verdict,
        comparison: run.comparison,
        attempts: run.attempts.map((a) => ({
          attemptId: a.attemptId,
          caseId: a.caseId,
          repeat: a.repeat,
          side: a.side,
          ok: a.ok,
          metric: a.metric,
          guardrails: a.guardrails,
          usage: a.usage,
          error: a.error,
        })),
      }),
    );
    files.push({
      digest: digestBytes(summaryBytes),
      bytes: summaryBytes,
      kind: "report",
      name: "evaluation-summary.json",
    });
    let index = 0;
    for (const file of files) {
      artifacts.push({
        digest: file.digest,
        file: file.name,
        bytes: file.bytes.length,
        kind: file.kind,
      });
      // Staged write + atomic rename: a reader never sees a half-written
      // artifact, and a failed export leaves the incomplete marker behind.
      const tmp = `${destination}/.${file.name}.staged-${crypto.randomUUID().slice(0, 8)}`;
      Deno.writeFileSync(tmp, file.bytes);
      Deno.renameSync(tmp, `${destination}/${file.name}`);
      index += 1;
    }
    const manifest: Record<string, unknown> = {
      schemaVersion: "1",
      kind: "vowdo-evidence-package",
      runId: run.runId,
      workflowId: run.workflowId,
      candidateId: run.candidateId,
      candidateDigest: run.candidateDigest,
      rubricDigest: run.rubricDigest,
      caseSetDigest: run.caseSetDigest,
      split: run.split,
      verdict: run.verdict,
      mode: run.mode,
      exportedAt: new Date().toISOString().replace("Z$", "+00:00"),
      artifacts,
    };
    const manifestDigest = digestOf(manifest);
    manifest["manifestDigest"] = manifestDigest;
    const manifestTmp = `${destination}/.${EVIDENCE_MANIFEST}.staged-${
      crypto.randomUUID().slice(0, 8)
    }`;
    Deno.writeFileSync(manifestTmp, new TextEncoder().encode(pythonPrettyJson(manifest) + "\n"));
    Deno.renameSync(manifestTmp, `${destination}/${EVIDENCE_MANIFEST}`);
    // run-export record: the durable pointer a decision anchors to
    workspace.store.save("run-export", run.runId, {
      schemaVersion: "1",
      runId: run.runId,
      manifestDigest,
      directory: destination,
    });
    Deno.removeSync(`${destination}/${INCOMPLETE_MARKER}`);
    return { manifestDigest, directory: destination, artifactCount: index };
  } catch (exc) {
    // the marker stays: an interrupted export is visibly incomplete
    throw exc;
  }
}

export function verifyEvidencePackage(destination: string): Record<string, unknown> {
  if (isSymlink(`${destination}/${INCOMPLETE_MARKER}`)) {
    throw new ContractError(
      `evidence package at ${destination} has a symbolic-link staging marker; refusing`,
    );
  }
  if (fileExists(`${destination}/${INCOMPLETE_MARKER}`)) {
    throw new ContractError(
      `evidence package at ${destination} is INCOMPLETE (staging marker present)`,
    );
  }
  const manifestPath = `${destination}/${EVIDENCE_MANIFEST}`;
  if (isSymlink(manifestPath)) {
    throw new ContractError(
      `evidence manifest of ${destination} is a symbolic link; refusing to read through it`,
    );
  }
  if (!fileExists(manifestPath)) {
    throw new ContractError(
      `no ${EVIDENCE_MANIFEST} in ${destination}: not a complete evidence package`,
    );
  }
  const manifest = JSON.parse(Deno.readTextFileSync(manifestPath));
  // Historical pre-rename evidence packages ("vouch-evidence-package")
  // verify read-only under the same rules.
  if (
    !isPlainObject(manifest) ||
    (manifest["kind"] !== "vowdo-evidence-package" &&
      manifest["kind"] !== "vouch-evidence-package")
  ) {
    throw new ContractError(`manifest of ${destination} is not an evidence package`);
  }
  const listed = manifest["artifacts"];
  if (!Array.isArray(listed)) throw new ContractError("evidence manifest lists no artifacts");
  const expected = new Set<string>([EVIDENCE_MANIFEST]);
  for (const entry of listed) {
    if (!isPlainObject(entry)) throw new ContractError("invalid artifact entry");
    const name = String(entry["file"] ?? "");
    // Manifest-supplied names are untrusted: exactly ONE safe path segment
    // (shared rule with native exports) — traversal and nested names refuse.
    if (!SAFE_ARTIFACT_NAME_RE.test(name)) {
      throw new ContractError(
        `evidence manifest lists an invalid artifact name ${JSON.stringify(name)}`,
      );
    }
    expected.add(name);
    const path = `${destination}/${name}`;
    if (isSymlink(path)) {
      throw new ContractError(
        `evidence artifact ${JSON.stringify(name)} is a symbolic link; refusing to read through it`,
      );
    }
    if (!fileExists(path)) throw new ContractError(`evidence package is missing ${name}`);
    const bytes = Deno.readFileSync(path);
    if (digestBytes(bytes) !== entry["digest"]) {
      throw new DigestMismatchError(
        `evidence artifact ${name} digests to ${digestBytes(bytes)}, manifest promised ${
          entry["digest"]
        }`,
      );
    }
    if (typeof entry["bytes"] === "number" && bytes.length !== entry["bytes"]) {
      throw new ContractError(
        `evidence artifact ${JSON.stringify(name)} size disagrees with the manifest`,
      );
    }
  }
  // Unlisted entries (files, directories or links) invalidate the package:
  // the directory content must be exactly the manifest plus the manifest.
  const present = Array.from(Deno.readDirSync(destination))
    .filter((e) => e.isFile || e.isDirectory || e.isSymlink)
    .map((e) => e.name);
  const unexpected = present.filter((name) => !expected.has(name));
  if (unexpected.length > 0) {
    throw new ContractError(
      `evidence package content does not match its manifest: unlisted entries ${
        JSON.stringify(unexpected.sort())
      }`,
    );
  }
  // The manifest digest must itself re-verify against the digest-free copy.
  const claimed = manifest["manifestDigest"];
  const clone = { ...manifest };
  delete clone["manifestDigest"];
  if (digestOf(clone) !== claimed) {
    throw new DigestMismatchError(
      "evidence manifest content does not match its own manifest digest — tampered manifest",
    );
  }
  return manifest;
}

function fileExists(path: string): boolean {
  return isFile(path);
}
