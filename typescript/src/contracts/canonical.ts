/**
 * Canonical JSON + content digests — the identity core shared with the
 * Python reference implementation (protocol v1.1 §4).
 *
 * Digest rule (byte-identical to Python
 * `json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
 * followed by sha256 over the UTF-8 bytes):
 *
 * - object keys sorted by Unicode CODE POINT (not UTF-16 code unit);
 * - no whitespace; `,` and `:` separators;
 * - non-ASCII emitted raw (UTF-8), never `\u`-escaped;
 * - `"` and `\` escaped; control characters < 0x20 escaped as
 *   `\b \t \n \f \r` or `\u00xx` (lowercase hex); DEL and U+2028/U+2029
 *   stay raw (exactly what Python emits with ensure_ascii=False);
 * - shared numeric domain (the two languages genuinely disagree outside
 *   it, so digests cannot agree there):
 *     * integers restricted to the JS safe range ±(2^53-1), spelled as
 *       JSON integers; negative zero canonicalizes to `0`;
 *     * non-integral finite doubles in CPython repr spelling (see
 *       {@link cpythonFloatRepr});
 *     * anything else — non-finite floats, integers beyond the safe range,
 *       integral doubles such as `3.0`/`1e16` — is REJECTED, never silently
 *       re-spelled, because re-spelling would silently change identity.
 */

/** Prefix for every content digest in Vowdo. */
import { createHash } from "node:crypto";

export const DIGEST_PREFIX = "sha256:";

/** JS exact-integer bound: 2^53 - 1. */
export const SAFE_INT_MAX = 2 ** 53 - 1;

const SAFE_INT_MIN = -(2 ** 53 - 1);

export class CanonicalJsonError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CanonicalJsonError";
  }
}

function fail(path: string, message: string): never {
  throw new CanonicalJsonError(`${path}: ${message}`);
}

/** Validate that `value` lies inside the shared cross-language digest domain. */
export function validateDigestDomain(value: unknown, path = "$"): void {
  if (typeof value === "boolean" || value === null || typeof value === "string") return;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      fail(path, `non-finite float ${value} is outside the digest domain`);
    }
    if (Number.isInteger(value)) {
      // JS cannot distinguish 3 from 3.0; integers beyond the safe range
      // have no shared spelling across languages.
      if (value < SAFE_INT_MIN || value > SAFE_INT_MAX) {
        fail(
          path,
          `integer ${value} outside the safe-integer digest domain (±${SAFE_INT_MAX}); ` +
            `cross-language digests cannot agree on it`,
        );
      }
      // Negative zero canonicalizes to 0 (shared vector rule); -0 is
      // therefore in-domain and spelled "0".
      return;
    }
    return; // non-integral finite double: in-domain, CPython repr spelling
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => validateDigestDomain(item, `${path}[${index}]`));
    return;
  }
  if (isPlainObject(value)) {
    for (const key of Object.keys(value)) {
      validateDigestDomain(value[key], `${path}.${key}`);
    }
    return;
  }
  fail(path, `value of type ${typeName(value)} is not JSON-serializable`);
}

function typeName(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * CPython `repr(float)` spelling, which is what `json.dumps` emits for
 * floats. `Number#toString` produces the same SHORTEST round-trip digits
 * (both languages use correctly-rounded shortest representations); the
 * presentation differs, so we re-format:
 *
 * - fixed notation when the decimal point position `decpt` satisfies
 *   `-4 < decpt <= 16`, scientific otherwise;
 * - exponent written as `e+NN` / `e-NN` with at least two digits;
 * - integral floats in fixed notation keep a trailing `.0` — unreachable
 *   here because integral doubles are outside the shared domain, but the
 *   formatter is complete so the boundary is explicit rather than accidental.
 */
export function cpythonFloatRepr(value: number): string {
  if (Number.isNaN(value)) return "nan";
  if (value === Infinity) return "inf";
  if (value === -Infinity) return "-inf";
  const negative = Object.is(value, -0) || 1 / value < 0;
  const abs = Math.abs(value);
  if (abs === 0) return negative ? "-0.0" : "0.0";
  const source = abs.toString(); // shortest round-trip digits, possibly "1e-7"
  const { digits, decpt } = splitDecimal(source);
  const sign = negative ? "-" : "";
  if (decpt <= -4 || decpt > 16) {
    // scientific: d[.ddd]e±NN — exponent is decpt - 1
    const exponent = decpt - 1;
    const mantissa = digits.length > 1 ? `${digits.slice(0, 1)}.${digits.slice(1)}` : digits;
    const expSign = exponent < 0 ? "-" : "+";
    const expDigits = Math.abs(exponent).toString().padStart(2, "0");
    return `${sign}${mantissa}e${expSign}${expDigits}`;
  }
  if (decpt <= 0) {
    return `${sign}0.${"0".repeat(-decpt)}${digits}`;
  }
  if (decpt >= digits.length) {
    const pad = "0".repeat(decpt - digits.length);
    return `${sign}${digits}${pad}.0`;
  }
  return `${sign}${digits.slice(0, decpt)}.${digits.slice(decpt)}`;
}

/** Split a JS decimal string into significant digits and decimal-point position. */
function splitDecimal(source: string): { digits: string; decpt: number } {
  let mantissa = source;
  let exponent = 0;
  const eIndex = source.indexOf("e");
  if (eIndex >= 0) {
    mantissa = source.slice(0, eIndex);
    exponent = Number.parseInt(source.slice(eIndex + 1), 10);
  }
  let digits = mantissa.replace(".", "");
  let pointAt = mantissa.indexOf("."); // digits before the point
  if (pointAt < 0) pointAt = mantissa.length;
  let decpt = pointAt + exponent;
  // strip leading zeros (0.000123 → digits "123", decpt -3)
  const firstSignificant = digits.search(/[1-9]/);
  if (firstSignificant > 0) {
    digits = digits.slice(firstSignificant);
    decpt -= firstSignificant;
  }
  // strip trailing zeros (shortest form never has them, defensive only)
  digits = digits.replace(/0+$/, "") || "0";
  return { digits, decpt };
}

// --- string escaping (exactly Python json.dumps with ensure_ascii=False) ----

const SHORT_ESCAPES: Record<string, string> = {
  "\b": "\\b",
  "\t": "\\t",
  "\n": "\\n",
  "\f": "\\f",
  "\r": "\\r",
};

export function quoteString(value: string): string {
  let out = '"';
  for (const ch of value) {
    const code = ch.codePointAt(0)!;
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (code < 0x20) {
      out += SHORT_ESCAPES[ch] ?? `\\u${code.toString(16).padStart(4, "0")}`;
    } else {
      out += ch;
    }
  }
  return out + '"';
}

/** Compare two strings by Unicode code point (Python `str` ordering). */
export function compareCodePoints(a: string, b: string): number {
  let ia = 0;
  let ib = 0;
  while (ia < a.length && ib < b.length) {
    const ca = a.codePointAt(ia)!;
    const cb = b.codePointAt(ib)!;
    if (ca !== cb) return ca < cb ? -1 : 1;
    ia += ca > 0xffff ? 2 : 1;
    ib += cb > 0xffff ? 2 : 1;
  }
  const restA = a.length - ia;
  const restB = b.length - ib;
  if (restA === restB) return 0;
  return restA < restB ? -1 : 1;
}

/** Serialize to the canonical form used for all digests and persistence. */
export function canonicalJson(value: unknown): string {
  validateDigestDomain(value);
  return writeCanonical(value);
}

function writeCanonical(value: unknown): string {
  if (value === null) return "null";
  switch (typeof value) {
    case "boolean":
      return value ? "true" : "false";
    case "number":
      return writeNumber(value);
    case "string":
      return quoteString(value);
    case "object": {
      if (Array.isArray(value)) {
        return `[${value.map(writeCanonical).join(",")}]`;
      }
      if (isPlainObject(value)) {
        const keys = Object.keys(value).sort(compareCodePoints);
        return writeObject(value, keys);
      }
      throw new CanonicalJsonError(`cannot serialize ${typeName(value)} canonically`);
    }
    default:
      throw new CanonicalJsonError(`cannot serialize ${typeName(value)} canonically`);
  }
}

function writeObject(value: Record<string, unknown>, sortedKeys: string[]): string {
  const parts: string[] = [];
  for (const key of sortedKeys) {
    parts.push(`${quoteString(key)}:${writeCanonical(value[key])}`);
  }
  return `{${parts.join(",")}}`;
}

function writeNumber(value: number): string {
  if (!Number.isInteger(value)) return cpythonFloatRepr(value);
  if (Object.is(value, -0)) return "0"; // negative zero canonicalizes to 0
  return value.toString();
}

/** Content digest over the canonical JSON encoding of `value`. */
export function digestOf(value: unknown): string {
  const payload = new TextEncoder().encode(canonicalJson(value));
  return DIGEST_PREFIX + sha256Hex(payload);
}

/** Content digest for raw artifact bytes. */
export function digestBytes(payload: Uint8Array): string {
  return DIGEST_PREFIX + sha256Hex(payload);
}

/** sha256 hex (no prefix) over bytes. */
export function sha256Hex(payload: Uint8Array): string {
  return createHash("sha256").update(payload).digest("hex");
}

/**
 * Parse canonical JSON text back into a value WITHOUT leaving the shared
 * numeric domain silently:
 *
 * - integer tokens beyond the safe range are rejected (JS would round them
 *   invisibly — a digest would then be computed over a DIFFERENT number);
 * - tokens with a decimal point or exponent whose value is integral
 *   (`3.0`, `2e0`, `-0.0`) are rejected: Python parses them as floats and
 *   re-serializes them with a `.0` spelling that ECMAScript cannot
 *   reproduce, so no shared digest exists;
 * - everything else parses through the ordinary JSON grammar.
 *
 * Use this whenever foreign (Python-written or disk-persisted) canonical
 * text is re-digested or re-persisted.
 */
export function parseCanonical(text: string): unknown {
  const value = JSON.parse(text);
  scanNumberTokens(text);
  return value;
}

/** Reject number tokens outside the shared numeric domain (see parseCanonical). */
function scanNumberTokens(text: string): void {
  const numberToken = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  // Only scan outside strings: walk the text tracking string state.
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
      checkNumberToken(token);
      index += token.length;
      continue;
    }
    index += 1;
  }
}

function checkNumberToken(token: string): void {
  const value = Number(token);
  if (!Number.isFinite(value)) return; // grammar-valid JSON never lands here
  if (Number.isInteger(value)) {
    const isIntegerSpelling = /^-?\d+$/.test(token);
    if (isIntegerSpelling) {
      if (value > SAFE_INT_MAX || value < SAFE_INT_MIN) {
        throw new CanonicalJsonError(
          `number token ${token} is outside the safe-integer digest domain ` +
            `(±${SAFE_INT_MAX}); ECMAScript would round it silently — refusing`,
        );
      }
      return;
    }
    // "3.0", "-0.0", "2e0": Python reads a float and re-spells it with a
    // different canonical form; no shared digest exists.
    throw new CanonicalJsonError(
      `number token ${token} is an integral float spelling; Python digests it ` +
        `as a float ("${value}.0"-style) which ECMAScript cannot reproduce — refusing`,
    );
  }
}

/**
 * Python `json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)`
 * byte-for-byte — the pretty form used for report and manifest artifacts.
 * Same numeric domain rules as the canonical form.
 */
export function pythonPrettyJson(value: unknown): string {
  validateDigestDomain(value);
  return writePretty(value, 0);
}

function writePretty(value: unknown, depth: number): string {
  if (
    value === null || typeof value === "boolean" || typeof value === "string" ||
    typeof value === "number"
  ) {
    return writeCanonical(value);
  }
  const indent = "  ".repeat(depth + 1);
  const closing = "  ".repeat(depth);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    const items = value.map((item) => writePretty(item, depth + 1));
    return `[\n${indent}${items.join(`,\n${indent}`)}\n${closing}]`;
  }
  if (isPlainObject(value)) {
    const keys = Object.keys(value).sort(compareCodePoints);
    if (keys.length === 0) return "{}";
    const items = keys.map((k) => `${quoteString(k)}: ${writePretty(value[k], depth + 1)}`);
    return `{\n${indent}${items.join(`,\n${indent}`)}\n${closing}}`;
  }
  throw new CanonicalJsonError(`cannot serialize ${typeName(value)}`);
}

/**
 * Python `json.dumps(obj, ensure_ascii=False, sort_keys=True)` — the default
 * separators (`, ` and `: `), no indent. This is the byte form used for
 * model-step artifacts built from evaluated return values.
 */
export function pythonPlainJson(value: unknown): string {
  validateDigestDomain(value);
  return writePlain(value);
}

function writePlain(value: unknown): string {
  if (
    value === null || typeof value === "boolean" || typeof value === "number" ||
    typeof value === "string"
  ) {
    return writeCanonical(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(writePlain).join(", ")}]`;
  }
  if (isPlainObject(value)) {
    const keys = Object.keys(value).sort(compareCodePoints);
    const parts = keys.map((k) => `${quoteString(k)}: ${writePlain(value[k])}`);
    return `{${parts.join(", ")}}`;
  }
  throw new CanonicalJsonError(`cannot serialize ${typeName(value)}`);
}
