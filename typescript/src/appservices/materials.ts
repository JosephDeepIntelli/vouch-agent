/**
 * Material snapshot helpers: one digest per material; internal attachment
 * ids are safe generated identifiers while the user-visible filename
 * (spaces, Chinese characters and all) travels beside it as displayName.
 */

import { digestBytes } from "../contracts/canonical.ts";
import { ContractError, requireDigest } from "../contracts/common.ts";
import { MAX_MATERIAL_BYTES } from "../contracts/materials.ts";
import type { ArtifactStore } from "../storage/artifacts.ts";

export const MAX_DISPLAY_NAME_CHARS = 200;
const FORBIDDEN_NAME_CHARS = ["/", "\\", "\x00"];

export function validateDisplayName(name: string): string {
  if (typeof name !== "string" || name.length === 0) {
    throw new ContractError("displayName must be a non-empty string");
  }
  if (name.trim().length === 0) {
    throw new ContractError("display name must not be empty or whitespace-only");
  }
  if (name.length > MAX_DISPLAY_NAME_CHARS) {
    throw new ContractError(
      `display name ${
        JSON.stringify(name.slice(0, 32))
      }... exceeds ${MAX_DISPLAY_NAME_CHARS} characters`,
    );
  }
  for (const ch of FORBIDDEN_NAME_CHARS) {
    if (name.includes(ch)) {
      throw new ContractError(
        `display name ${JSON.stringify(name)} contains ${
          JSON.stringify(ch)
        }; pass a bare file name, not a path`,
      );
    }
  }
  if ([...name].some((ch) => ch.codePointAt(0)! < 0x20)) {
    throw new ContractError(`display name ${JSON.stringify(name)} contains control characters`);
  }
  return name;
}

export function safeAttachmentId(ordinal: number, digest: string, prefix = "mat"): string {
  requireDigest(digest, "digest");
  if (ordinal < 0 || ordinal > 9999) {
    throw new ContractError(`material ordinal ${ordinal} outside 0..9999`);
  }
  const identifier = `${prefix}-${String(ordinal).padStart(2, "0")}-${
    digest.slice("sha256:".length).slice(0, 16)
  }`;
  if (identifier.length > 64) {
    throw new ContractError(
      `generated attachment id ${JSON.stringify(identifier)} exceeds 64 chars`,
    );
  }
  return identifier;
}

export interface MaterialSnapshot {
  attachment: {
    schemaVersion: string;
    name: string;
    kind: "json" | "text" | "csv";
    mediaType: string;
    contentDigest: string;
    sizeBytes: number;
    csvConfig: Record<string, unknown>;
  };
  displayName: string;
  payload: Uint8Array;
}

export function snapshotCsvMaterial(
  payload: Uint8Array,
  options: { displayName: string; ordinal: number; delimiter: string },
): MaterialSnapshot {
  validateDisplayName(options.displayName);
  checkSize(payload, options.displayName);
  const digest = digestBytes(payload);
  return {
    attachment: {
      schemaVersion: "1",
      name: safeAttachmentId(options.ordinal, digest),
      kind: "csv",
      mediaType: "text/csv",
      contentDigest: digest,
      sizeBytes: payload.length,
      csvConfig: { delimiter: options.delimiter, header: true },
    },
    displayName: options.displayName,
    payload,
  };
}

export function snapshotJsonMaterial(
  payload: Uint8Array,
  options: { displayName: string; ordinal: number },
): MaterialSnapshot {
  validateDisplayName(options.displayName);
  checkSize(payload, options.displayName);
  const digest = digestBytes(payload);
  return {
    attachment: {
      schemaVersion: "1",
      name: safeAttachmentId(options.ordinal, digest),
      kind: "json",
      mediaType: "application/json",
      contentDigest: digest,
      sizeBytes: payload.length,
      csvConfig: {},
    },
    displayName: options.displayName,
    payload,
  };
}

export function materialInputRecords(snapshots: MaterialSnapshot[]): Record<string, unknown>[] {
  return snapshots.map((s) => ({
    schemaVersion: "1",
    attachmentId: s.attachment.name,
    displayName: s.displayName,
    attachment: s.attachment,
  }));
}

/** Store EVERY snapshot under its OWN digest; returns per-material digests. */
export function storeMaterialSnapshots(
  artifacts: ArtifactStore,
  snapshots: MaterialSnapshot[],
): string[] {
  const ids = new Set(snapshots.map((s) => s.attachment.name));
  if (ids.size !== snapshots.length) {
    throw new ContractError("material attachment ids collided; ordinals must be unique");
  }
  return snapshots.map((s) => {
    const stored = artifacts.put(s.payload);
    if (stored !== s.attachment.contentDigest) {
      throw new ContractError(
        `artifact store returned ${stored} for material ${JSON.stringify(s.displayName)} ` +
          `whose bytes digest to ${s.attachment.contentDigest}`,
      );
    }
    return stored;
  });
}

function checkSize(payload: Uint8Array, displayName: string): void {
  if (payload.length > MAX_MATERIAL_BYTES) {
    throw new ContractError(
      `material ${
        JSON.stringify(displayName)
      } exceeds the ${MAX_MATERIAL_BYTES}-byte cap (${payload.length} bytes)`,
    );
  }
}
