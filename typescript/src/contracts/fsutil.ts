/** Filesystem existence helpers (Deno.statSync throws when absent). */

export function pathExists(path: string): boolean {
  try {
    Deno.statSync(path);
    return true;
  } catch {
    return false;
  }
}

export function isFile(path: string): boolean {
  try {
    return Deno.statSync(path).isFile;
  } catch {
    return false;
  }
}

export function isDirectory(path: string): boolean {
  try {
    return Deno.statSync(path).isDirectory;
  } catch {
    return false;
  }
}

export function isSymlink(path: string): boolean {
  try {
    return Deno.lstatSync(path).isSymlink;
  } catch {
    return false;
  }
}
