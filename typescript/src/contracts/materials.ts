/** Material contracts: immutable, typed, content-addressed inputs. */

import { checkVersion, ContractError, requireDigest, requireStr } from "./common.ts";

export const MAX_MATERIAL_BYTES = 8 * 1024 * 1024;

export const SUPPORTED_MEDIA_TYPES = new Set([
  "application/json",
  "text/plain; charset=utf-8",
  "text/csv",
]);

export type MaterialKindValue = "json" | "text" | "csv";

export interface TaskAttachmentData {
  schemaVersion: string;
  name: string;
  kind: MaterialKindValue;
  mediaType: string;
  contentDigest: string;
  sizeBytes: number;
  csvConfig: Record<string, unknown>;
}

const NAME_RE = /^[A-Za-z0-9._-]{1,64}$/;

export function attachmentFromDict(data: Record<string, unknown>): TaskAttachmentData {
  checkVersion(data);
  const name = requireStr(data["name"], "name");
  const mediaType = requireStr(data["mediaType"], "mediaType");
  if (!NAME_RE.test(name)) {
    throw new ContractError(
      `material name ${JSON.stringify(name)} must be 1-64 chars of [A-Za-z0-9._-]`,
    );
  }
  if (!SUPPORTED_MEDIA_TYPES.has(mediaType)) {
    throw new ContractError(
      `unsupported material media type ${JSON.stringify(mediaType)}; supported: ${
        [...SUPPORTED_MEDIA_TYPES].sort().join(", ")
      }`,
    );
  }
  const sizeBytes = data["sizeBytes"];
  if (typeof sizeBytes !== "number" || sizeBytes < 0 || sizeBytes > MAX_MATERIAL_BYTES) {
    throw new ContractError(
      `material ${JSON.stringify(name)} size ${
        JSON.stringify(sizeBytes)
      } outside 0..${MAX_MATERIAL_BYTES}`,
    );
  }
  const kind = requireStr(data["kind"], "kind") as MaterialKindValue;
  const csvConfig = { ...(data["csvConfig"] as Record<string, unknown> ?? {}) };
  if (kind === "csv" && !("delimiter" in csvConfig)) {
    throw new ContractError(
      `CSV material ${JSON.stringify(name)} must state its delimiter in csvConfig`,
    );
  }
  return {
    schemaVersion: "1",
    name,
    kind,
    mediaType,
    contentDigest: requireDigest(data["contentDigest"], "contentDigest"),
    sizeBytes,
    csvConfig,
  };
}

/** Best-effort classification by content, cross-checked for sanity. */
export function classifyMediaType(payload: Uint8Array, name: string): MaterialKindValue {
  if (payload.length > MAX_MATERIAL_BYTES) {
    throw new ContractError(
      `material ${JSON.stringify(name)} exceeds the ${MAX_MATERIAL_BYTES}-byte cap`,
    );
  }
  const head = new TextDecoder("utf-8", { fatal: false }).decode(payload.slice(0, 4096));
  const stripped = head.replace(/^\s+/, "");
  if (stripped.startsWith("{") || stripped.startsWith("[")) {
    try {
      JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(payload));
    } catch (exc) {
      throw new ContractError(
        `material ${JSON.stringify(name)} looks like JSON but does not parse: ${exc}`,
      );
    }
    return "json";
  }
  const firstLine = head.split("\n", 1)[0];
  const looksTabular = firstLine.includes(",") || firstLine.includes(";") ||
    firstLine.includes("\t");
  if (looksTabular && name.toLowerCase().endsWith(".csv")) return "csv";
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(payload);
  } catch (exc) {
    throw new ContractError(`material ${JSON.stringify(name)} is not valid UTF-8: ${exc}`);
  }
  return "text";
}
