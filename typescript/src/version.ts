/** Version identity of the native TypeScript/Deno product candidate. */

import type { ProjectWorkspace } from "./appservices/workspace.ts";

/** Candidate identity — distinct from the Python reference 0.1.0rc1. */
export const VERSION = "0.2.0rc4";

/** Runtime identity reported to gates, exports and sealed configs. */
export const RUNTIME_ID = "vouch-agent-ts/deno-2.9.7";

export type { ProjectWorkspace };
