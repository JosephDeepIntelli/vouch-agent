/**
 * Fail-closed verdict ordering (design §3.1/§7.2), ported from
 * the historical Python reference:
 *
 * 1. the rubric must be frozen (error, not a verdict);
 * 2. any hard guardrail violation => REJECTED (never averaged away);
 * 3. a measured-bad CANDIDATE outcome (failed/timeout) where baseline was
 *    usable => REJECTED;
 * 4. any other incomplete pair, missing guardrail measurement, unmeasurable
 *    cost, or fewer usable repeats than demanded => INCONCLUSIVE;
 * 5. main metric must cover all complete pairs with direction-adjusted
 *    improvement >= min-main-improvement => else REJECTED (shortfall) or
 *    INCONCLUSIVE (unmeasurable / partially covered).
 */

import { ApprovalInvalidatedError, ContractError } from "../contracts/common.ts";
import {
  type ComparisonSummary,
  type Rubric,
  THRESHOLD_MIN_COMPLETE_PAIRS,
  THRESHOLD_MIN_MAIN_IMPROVEMENT,
  type Verdict,
} from "./contracts.ts";

const MEASURED_BAD = new Set(["failed", "timeout"]);

function isMeasuredBadCandidate(reason: string): boolean {
  const parts = reason.split(":");
  if (parts.length < 2) return false;
  const [side, status] = [parts[parts.length - 2], parts[parts.length - 1]];
  return side === "candidate" && MEASURED_BAD.has(status);
}

export function verdictFromSummary(summary: ComparisonSummary, rubric: Rubric): Verdict {
  if (rubric.frozenBy === null || rubric.frozenAt === null) {
    throw new ContractError(
      "rubric thresholds are not frozen; refusing to verdict (fail closed) — freeze the rubric " +
        "before candidate measurement",
    );
  }
  if (summary.totalPairs === 0) return "inconclusive";

  // (1) hard guardrails: one violation on ANY attempt is enough.
  if (summary.hardViolations.length > 0) return "rejected";

  // (2) measured-bad candidate outcomes on otherwise measurable repeats.
  for (const reason of Object.values(summary.incompleteReasons)) {
    if (isMeasuredBadCandidate(reason)) return "rejected";
  }

  // (3) anything still unknown => inconclusive, never pass.
  if (summary.completePairs < summary.totalPairs) return "inconclusive";
  if (summary.missingGuardrailMeasurements.length > 0) return "inconclusive";
  if (summary.cost === null || !summary.cost.measurable) return "inconclusive";
  if (summary.pairsBelowRequestedRepeats > 0) return "inconclusive";

  // (4) main metric: unmeasurable or partially covered => inconclusive.
  const main = summary.metricDeltas.find((m) => m.metric === rubric.mainMetric);
  const expectedPairs = summary.completeRepeatPairs || summary.completePairs;
  if (main === undefined || main.nPairs < expectedPairs) return "inconclusive";

  // (5) measured shortfall against the frozen threshold => rejected.
  const improvement = rubric.direction === "increase" ? main.meanDelta : -main.meanDelta;
  const required = rubric.thresholds[THRESHOLD_MIN_MAIN_IMPROVEMENT] ?? 0;
  if (improvement < required - 1e-12) return "rejected";

  const minPairs = rubric.thresholds[THRESHOLD_MIN_COMPLETE_PAIRS];
  if (minPairs !== undefined && summary.completePairs < minPairs) return "inconclusive";

  return "accepted";
}

export { ApprovalInvalidatedError };
