/**
 * The detached run worker: an independent controller process that takes a
 * submitted run through its sealed configuration to a terminal or paused
 * state. The client may exit; closing it never cancels or duplicates the
 * work (the run's ownership lease, checkpoints and durable cursor all live
 * in the workspace stores). Polls durable pause/cancel requests at each
 * step boundary through the supervisor's loop.
 */

import { ExecutionService } from "./execution.ts";
import { ProjectWorkspace } from "./workspace.ts";
import { isTerminal } from "../contracts/tasks.ts";

export async function runWorkerMain(
  positionals: string[],
  options: { workerPath?: string } = {},
): Promise<void> {
  // positionals: [runId, ...]; --project arrives via re-parse below
  const argv = ["__run-worker", ...Deno.args];
  const projectFlag = valueAfter(argv, "--project") ?? ".";
  const runId = positionals.find((p) => !p.startsWith("--")) ??
    fail("run worker requires a run id");
  const provider = valueAfter(argv, "--provider") ?? "extract-fact";
  const workspace = ProjectWorkspace.open(projectFlag);
  const service = new ExecutionService(workspace, { workerPath: options.workerPath });
  try {
    const attempt = await service.execute(runId, { provider, isolated: true });
    if (!isTerminal(attempt.status)) {
      // Poll for external control decisions (pause/cancel) until terminal.
      // BOUNDED: a persistently failing execute (e.g. the worker cannot be
      // spawned on this host) must terminate honestly after a fixed number
      // of no-progress attempts, never spin forever.
      const MAX_NO_PROGRESS_ATTEMPTS = 25;
      let noProgress = 0;
      let lastStatus = attempt.status;
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 200));
        const current = service.status(runId);
        if (current === null) break;
        if (isTerminal(current.status)) break;
        if (current.status === "paused" || current.status === "needs-reconciliation") break;
        const again = await service.execute(runId, { provider, isolated: true });
        if (isTerminal(again.status)) break;
        if (again.status === lastStatus) {
          noProgress += 1;
          if (noProgress >= MAX_NO_PROGRESS_ATTEMPTS) {
            console.error(
              `run-worker giving up on ${runId}: ${MAX_NO_PROGRESS_ATTEMPTS} consecutive ` +
                `attempts made no progress (last status ${again.status}${
                  again.error ? `, error: ${again.error}` : ""
                })`,
            );
            break;
          }
        } else {
          noProgress = 0;
          lastStatus = again.status;
        }
      }
    }
    const final = service.status(runId);
    console.log(
      `run-worker finished ${runId}: ${final?.status ?? "unknown"}${
        final?.error ? ` (${final.error})` : ""
      }`,
    );
  } finally {
    workspace.close();
  }
}

function valueAfter(argv: string[], name: string): string | undefined {
  const index = argv.indexOf(name);
  if (index >= 0 && index + 1 < argv.length) return argv[index + 1];
  return undefined;
}

function fail(message: string): never {
  console.error(`fail-closed [vouch/usage]: ${message}`);
  Deno.exit(1);
}
