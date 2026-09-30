/** Tiny zero-dependency assertion helpers (node:assert under the hood). */

import * as assert from "node:assert";

export function assertEquals(actual: unknown, expected: unknown, message?: string): void {
  assert.deepStrictEqual(actual, expected, message);
}

export function assertThrows(
  fn: () => unknown,
  errorType?: new (...args: never[]) => unknown,
  messageIncludes?: string,
  label?: string,
): unknown {
  try {
    fn();
  } catch (exc) {
    if (errorType !== undefined && !(exc instanceof errorType)) {
      throw new Error(
        `${label ?? "assertThrows"}: expected ${errorType.name}, got ${
          (exc as Error).constructor.name
        }: ${exc}`,
      );
    }
    if (messageIncludes !== undefined) {
      const text = String((exc as Error).message);
      if (!text.includes(messageIncludes)) {
        throw new Error(
          `${label ?? "assertThrows"}: message ${JSON.stringify(text)} does not include ${
            JSON.stringify(messageIncludes)
          }`,
        );
      }
    }
    return exc;
  }
  throw new Error(`${label ?? "assertThrows"}: expected function to throw, but it returned`);
}

export function assertTrue(value: unknown, message?: string): void {
  assert.ok(value, message);
}

export function fail(message: string): never {
  throw new Error(message);
}
