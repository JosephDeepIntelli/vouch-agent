/** Read-side report: costs + recovery posture for `vouch status`. */

import { isTerminal } from "../contracts/tasks.ts";
import { ExecutionService } from "./execution.ts";
import type { ProjectWorkspace } from "./workspace.ts";

export interface CostReport {
  totalCapUsd: number;
  measuredUsd: number;
  outstandingReservedUsd: number;
  remainingUsd: number;
}

export interface ResumeState {
  openReservations: number;
  needsReconciliation: string[];
  clean: boolean;
}

export function costReport(workspace: ProjectWorkspace): CostReport {
  const entries = workspace.journal.costEntries();
  const measured = entries.reduce((sum, e) => sum + (e.amountUsd ?? 0), 0);
  return {
    totalCapUsd: workspace.ledger.totalCapUsd(),
    measuredUsd: measured,
    outstandingReservedUsd: workspace.ledger.outstandingUsd(),
    remainingUsd: workspace.ledger.remainingUsd(),
  };
}

export function resumeState(workspace: ProjectWorkspace): ResumeState {
  const needsReconciliation = new ExecutionService(workspace)
    .runs()
    .filter((run) => !isTerminal(run.status) && run.status === "needs-reconciliation")
    .map((run) => run.runId);
  const open = workspace.ledger.openReservations().length;
  return {
    openReservations: open,
    needsReconciliation,
    clean: open === 0 && needsReconciliation.length === 0,
  };
}
