/** Cross-language canonical-JSON digest vectors (protocol v1.1 §4). */

import { assertEquals, assertThrows } from "./helpers.ts";
import {
  canonicalJson,
  CanonicalJsonError,
  cpythonFloatRepr,
  digestBytes,
  digestOf,
  parseCanonical,
  pythonPlainJson,
  sha256Hex,
} from "../src/contracts/canonical.ts";

const vectorsFile = JSON.parse(
  new TextDecoder().decode(
    Deno.readFileSync(new URL("./fixtures/test-vectors.json", import.meta.url)),
  ),
) as {
  schemaVersion: number;
  vectors: Array<{
    name: string;
    input: unknown;
    canonical?: string;
    sha256?: string;
    expectedRejected?: boolean;
  }>;
};

Deno.test({
  name: "all accept vectors hash identically to the shared corpus",
  fn() {
    const accepted = vectorsFile.vectors.filter((v) => !v.expectedRejected);
    assertEquals(accepted.length >= 20, true, "corpus shrunk");
    for (const vector of accepted) {
      assertEquals(canonicalJson(vector.input), vector.canonical, vector.name);
      assertEquals(digestOf(vector.input), `sha256:${vector.sha256}`, vector.name);
    }
  },
});

Deno.test({
  name: "rejected vectors are rejected here too",
  fn() {
    const rejected = vectorsFile.vectors.filter((v) => v.expectedRejected);
    assertEquals(rejected.length, 4);
    for (const vector of rejected) {
      assertThrows(() => canonicalJson(vector.input), CanonicalJsonError, undefined, vector.name);
    }
  },
});

Deno.test({
  name: "TS-side domain guards (unsafe integers, non-finite, integral floats)",
  fn() {
    assertThrows(() => canonicalJson({ n: Number.MAX_SAFE_INTEGER + 2 }), CanonicalJsonError);
    assertThrows(() => canonicalJson({ cost: NaN }), CanonicalJsonError);
    assertThrows(() => canonicalJson({ cost: Infinity }), CanonicalJsonError);
    // integral doubles within the safe range cannot be spelled in the shared
    // domain from ECMAScript: 3.0 collapses to 3 and would silently change
    // identity against Python's "3.0" — canonicalization rejects the boundary
    // only where it is detectable (foreign text), while numbers built in TS
    // are always integers or true fractions.
    assertEquals(canonicalJson({ n: Number.MAX_SAFE_INTEGER }), `{"n":9007199254740991}`);
    assertEquals(canonicalJson({ cost: 0.01 }), `{"cost":0.01}`);
    assertEquals(canonicalJson(-0), "0", "negative zero canonicalizes to 0");
  },
});

Deno.test({
  name: "parseCanonical rejects silently-rounding number tokens",
  fn() {
    // 9007199254740993 rounds in ECMAScript — must refuse, not digest wrongly
    assertThrows(() => parseCanonical('{"n": 9007199254740993}'), CanonicalJsonError);
    // integral float spellings Python would re-serialize as "3.0"
    assertThrows(() => parseCanonical('{"n": 3.0}'), CanonicalJsonError);
    assertThrows(() => parseCanonical('{"n": -0.0}'), CanonicalJsonError);
    assertThrows(() => parseCanonical('{"n": 2e0}'), CanonicalJsonError);
    // inside strings numbers are not numbers
    assertEquals(parseCanonical('{"a": "3.0"}'), { a: "3.0" });
    // legitimate values pass
    assertEquals(parseCanonical('{"a": 3, "b": 0.5, "c": [1, -2.25]}'), {
      a: 3,
      b: 0.5,
      c: [1, -2.25],
    });
  },
});

Deno.test({
  name: "cpython float repr boundary spellings",
  fn() {
    const cases: Array<[number, string]> = [
      [0.5, "0.5"],
      [-2.25, "-2.25"],
      [0.30000000000000004, "0.30000000000000004"],
      [1e-7, "1e-07"],
      [1.25e-7, "1.25e-07"],
      [0.0001, "0.0001"],
      [1e-5, "1e-05"],
      [1234567890123456.8, "1234567890123456.8"],
      [5e-324, "5e-324"],
      [0.000001, "1e-06"],
      [1e-300, "1e-300"],
      [3.0, "3.0"],
      [-0.0, "-0.0"],
      [123.456, "123.456"],
    ];
    for (const [value, expected] of cases) {
      assertEquals(cpythonFloatRepr(value), expected, String(value));
    }
  },
});

Deno.test({
  name: "code-point key ordering (astral vs BMP surrogates)",
  fn() {
    // UTF-16 code-unit order would place the astral key BEFORE U+FFFF;
    // code-point order (Python) places it after.
    const value = { "￿": 1, "\u{10000}": 2 };
    assertEquals(canonicalJson(value), '{"￿":1,"\u{10000}":2}');
  },
});

Deno.test({
  name: "python plain/pretty JSON forms",
  fn() {
    assertEquals(pythonPlainJson({ b: 1, a: [1, 2] }), `{"a": [1, 2], "b": 1}`);
    assertEquals(
      pythonPlainJson({ name: "迪普智选", price: 0.5 }),
      `{"name": "迪普智选", "price": 0.5}`,
    );
  },
});

Deno.test({
  name: "digest bytes over raw payloads",
  fn() {
    // sha256 of empty string and "abc" — fixed reference values
    const enc = new TextEncoder();
    assertEquals(
      sha256Hex(enc.encode("")),
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
    assertEquals(
      digestBytes(enc.encode("abc")),
      "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  },
});
