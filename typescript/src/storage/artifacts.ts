/**
 * Content-addressed artifact bytes under `<root>/artifacts/`.
 *
 * Names are `sha256:<hex>` digests; anything not exactly that shape is
 * refused BEFORE it reaches the filesystem. Digest-shaped names alone do
 * NOT make escapes impossible: a symlinked artifact DIRECTORY would redirect
 * writes outside the root, and a symlinked artifact FILE would feed
 * external bytes back as if they were ours (even digest-matching ones).
 * Every operation therefore verifies with lstat — never following links —
 * that the artifacts directory is a real directory bound to the real path
 * fixed at construction (re-checked on EVERY operation, so a deterministic
 * swap-after-open fails closed) and the artifact is a real regular file
 * inside it, and every read re-verifies the content
 * digest so on-disk tampering surfaces as a DigestMismatchError. A
 * pre-existing artifact whose bytes do not digest to its name is refused on
 * put as well — never silently treated as an idempotent hit.
 */

import { digestBytes } from "../contracts/canonical.ts";
import { ContractError, DigestMismatchError } from "../contracts/common.ts";

const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;

function lstatIsRegularFile(path: string): boolean {
  try {
    return Deno.lstatSync(path).isFile;
  } catch {
    return false;
  }
}

function lstatIsSymlink(path: string): boolean {
  try {
    return Deno.lstatSync(path).isSymlink;
  } catch {
    return false;
  }
}

export class ArtifactStore {
  private root: string;
  private dir: string;
  /** Real path of the artifacts directory, fixed at construction. */
  private realDir: string;

  constructor(root: string) {
    this.root = root;
    this.dir = join(root, "artifacts");
    Deno.mkdirSync(this.dir, { recursive: true });
    // Fail closed on a symlinked artifacts directory: writes would land
    // outside the workspace root (review follow-up: symlink escape).
    if (lstatIsSymlink(this.dir)) {
      throw new ContractError(
        `${this.dir} is a symbolic link; refusing to use it as the artifact directory — ` +
          `artifact writes must stay inside the workspace`,
      );
    }
    const stat = Deno.lstatSync(this.dir);
    if (!stat.isDirectory) {
      throw new ContractError(`${this.dir} is not a directory`);
    }
    this.realDir = Deno.realPathSync(this.dir);
  }

  get artifactsDir(): string {
    return this.dir;
  }

  /**
   * Re-validate the artifacts directory binding at EVERY operation: the
   * directory must still be a real (non-symlink) directory whose real path
   * is the one fixed at construction. This makes the deterministic
   * swap-after-open attack (rename the directory away, symlink a different
   * directory into its place) fail BEFORE any external read/write. Remaining
   * limit, stated honestly: a same-user attacker racing INSIDE the window
   * between this check and the file operation can still win — this is a
   * fail-closed control against deterministic substitution, not a sandbox;
   * Deno exposes no openat/no-follow handles to close that window fully.
   */
  private revalidateDirectory(operation: string): void {
    let stat: Deno.FileInfo;
    try {
      if (Deno.lstatSync(this.dir).isSymlink) {
        throw new ContractError(`artifact directory was replaced by a symbolic link`);
      }
      stat = Deno.lstatSync(this.dir);
    } catch (exc) {
      if (exc instanceof ContractError) {
        throw new ContractError(
          `${exc.message}; refusing ${operation} (artifact writes must stay inside the workspace)`,
        );
      }
      throw new ContractError(
        `artifact directory ${this.dir} disappeared; refusing ${operation}`,
      );
    }
    if (!stat.isDirectory) {
      throw new ContractError(
        `artifact path ${this.dir} is no longer a directory; refusing ${operation}`,
      );
    }
    let current: string;
    try {
      current = Deno.realPathSync(this.dir);
    } catch {
      throw new ContractError(
        `artifact directory ${this.dir} cannot be resolved; refusing ${operation}`,
      );
    }
    if (current !== this.realDir) {
      throw new ContractError(
        `artifact directory changed under the store (${current} != ${this.realDir}); ` +
          `refusing ${operation} — possible substitution; fail closed`,
      );
    }
  }

  private pathFor(digest: string): string {
    if (typeof digest !== "string" || !DIGEST_RE.test(digest)) {
      throw new ContractError(
        `artifact name must be a 'sha256:<64 hex>' digest, got ${JSON.stringify(digest)}`,
      );
    }
    // Only [0-9a-f]{64} ever reaches the filesystem — no separators, no dot
    // segments, no way to spell a traversal with the name itself.
    return join(this.dir, digest.slice("sha256:".length));
  }

  put(payload: Uint8Array): string {
    this.revalidateDirectory("put");
    const digest = digestBytes(payload);
    const path = this.pathFor(digest);
    if (lstatIsSymlink(path)) {
      throw new ContractError(
        `artifact ${digest} exists as a symbolic link; refusing to follow it (artifact ` +
          `files must be real files inside ${this.realDir})`,
      );
    }
    if (lstatIsRegularFile(path)) {
      // Idempotent ONLY for genuinely matching bytes: corrupt pre-existing
      // content under a digest name is refused, never silently accepted.
      const existing = Deno.readFileSync(path);
      if (digestBytes(existing) !== digest) {
        throw new DigestMismatchError(
          `artifact ${digest} already exists but its content does not match its name — ` +
            `tampering or corruption; refusing to overwrite or reuse it`,
        );
      }
      return digest;
    }
    const tmp = join(this.dir, `.${basename(path)}.tmp-${crypto.randomUUID().slice(0, 12)}`);
    try {
      Deno.writeFileSync(tmp, payload);
      // rename over any concurrently-appeared symlink REPLACES the link
      // rather than following it; re-verify the final entry afterwards.
      Deno.renameSync(tmp, path);
      if (!lstatIsRegularFile(path)) {
        throw new ContractError(
          `artifact ${digest} is not a regular file after write; refusing the store`,
        );
      }
    } finally {
      try {
        Deno.removeSync(tmp);
      } catch {
        // renamed away or never materialized — nothing to clean
      }
    }
    return digest;
  }

  get(digest: string): Uint8Array {
    this.revalidateDirectory("get");
    const path = this.pathFor(digest);
    if (lstatIsSymlink(path)) {
      throw new ContractError(
        `artifact ${digest} is a symbolic link; refusing to read through it — artifact ` +
          `files must be real files inside ${this.realDir}`,
      );
    }
    let data: Uint8Array;
    try {
      const stat = Deno.lstatSync(path);
      if (!stat.isFile) {
        throw new ContractError(`artifact ${digest} is not a regular file`);
      }
      data = Deno.readFileSync(path);
    } catch (exc) {
      if (exc instanceof ContractError) throw exc;
      throw new ContractError(`artifact ${digest} not found`);
    }
    if (digestBytes(data) !== digest) {
      throw new DigestMismatchError(
        `artifact ${digest} content does not match its name — tampering or corruption`,
      );
    }
    return data;
  }

  exists(digest: string): boolean {
    this.revalidateDirectory("exists");
    const path = this.pathFor(digest);
    return lstatIsRegularFile(path) && !lstatIsSymlink(path);
  }
}

function join(dir: string, name: string): string {
  return dir.endsWith("/") ? `${dir}${name}` : `${dir}/${name}`;
}

function basename(path: string): string {
  const idx = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return idx >= 0 ? path.slice(idx + 1) : path;
}
