/**
 * Subprocess spawn construction with EXPLICIT environment isolation.
 *
 * Deno's `Deno.Command` MERGES a provided `env` with the inherited
 * environment by default — a filtered-looking env object still leaks every
 * unlisted variable (review finding: a synthetic sentinel set in the parent
 * remained visible in children). Every child this product spawns therefore
 * uses `clearEnv: true` plus a MINIMAL, intentional, caller-owned env.
 * There is NO automatic credential forwarding in fixture mode: variables
 * reach a child only when the caller passes an explicit `env` (documented
 * caller authority — e.g. a pilot service that owns credential resolution).
 */

export interface SpawnSpec {
  command: string;
  args: string[];
  clearEnv: true;
  env: Record<string, string>;
}

/** Names read from the parent environment for trusted controller children
 * (the detached run-worker): interpreter resolution + temp/locale basics
 * only — an allowlist of NAMES, never a wholesale copy. */
export const CONTROLLER_CHILD_ENV_NAMES = ["PATH", "HOME", "TMPDIR", "LANG", "TZ"] as const;

function readAllowlisted(names: readonly string[]): Record<string, string> {
  const env: Record<string, string> = {};
  for (const name of names) {
    try {
      const value = Deno.env.get(name);
      if (value !== undefined) env[name] = value;
    } catch {
      // env read not permitted for this name — omit it (never guess)
    }
  }
  return env;
}

/**
 * Spawn spec for the trusted detached run-worker: cleared environment plus
 * the named allowlist above.
 */
export function controllerChildSpawn(command: string, args: string[]): SpawnSpec {
  return { command, args, clearEnv: true, env: readAllowlisted(CONTROLLER_CHILD_ENV_NAMES) };
}

/**
 * Spawn spec for UNTRUSTED/least-privilege children (the isolated step
 * worker, adapter subprocesses in fixture mode): cleared environment and
 * NOTHING else unless the caller explicitly provides `env` (full caller
 * authority over that child's environment).
 */
export function isolatedChildSpawn(
  command: string,
  args: string[],
  explicitEnv?: Record<string, string>,
): SpawnSpec {
  return {
    command,
    args,
    clearEnv: true,
    env: explicitEnv !== undefined ? { ...explicitEnv } : {},
  };
}
