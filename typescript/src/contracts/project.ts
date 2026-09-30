/** ProjectSpec: workflows, owners, allowed changes, budget, workspace mode. */

import { digestOf } from "./canonical.ts";
import { checkVersion, ContractError, requireStr, type Role, ROLES, utcNowIso } from "./common.ts";

export interface WorkflowDeclarationData {
  schemaVersion: string;
  workflowId: string;
  name: string;
  mainObjective: string;
  guardrails: string[];
  locales: string[];
  markets: string[];
}

export interface BudgetPolicyData {
  schemaVersion: string;
  totalUsdCap: number;
  maxConcurrentAttempts: number;
  perAttemptTimeoutS: number;
  unknownPricePolicy: "refuse" | "conservative-reserve";
  conservativeReserveUsd: number;
}

export interface ProjectSpecData {
  schemaVersion: string;
  projectId: string;
  name: string;
  workflows: WorkflowDeclarationData[];
  owners: Partial<Record<Role, string>>;
  allowedChangeTypes: string[];
  dataAuthorization: string;
  processingLocation: string;
  mode: "improvement" | "task-only";
  purpose: string;
  budget: BudgetPolicyData;
  stopConditions: string[];
  createdAt: string;
}

export function workflowFromDict(data: Record<string, unknown>): WorkflowDeclarationData {
  checkVersion(data);
  return {
    schemaVersion: "1",
    workflowId: requireStr(data["workflowId"], "workflowId"),
    name: requireStr(data["name"], "name"),
    mainObjective: requireStr(data["mainObjective"], "mainObjective"),
    guardrails: strs(data["guardrails"]),
    locales: strs(data["locales"]).length ? strs(data["locales"]) : ["en"],
    markets: strs(data["markets"]),
  };
}

export function budgetFromDict(data: Record<string, unknown>): BudgetPolicyData {
  checkVersion(data);
  const policy = {
    schemaVersion: "1" as const,
    totalUsdCap: num(data["totalUsdCap"]),
    maxConcurrentAttempts: Math.trunc(num(data["maxConcurrentAttempts"] ?? 1)),
    perAttemptTimeoutS: num(data["perAttemptTimeoutS"] ?? 300),
    unknownPricePolicy: String(data["unknownPricePolicy"] ?? "refuse") as
      | "refuse"
      | "conservative-reserve",
    conservativeReserveUsd: num(data["conservativeReserveUsd"] ?? 1),
  };
  if (policy.totalUsdCap < 0) throw new ContractError("totalUsdCap must be >= 0");
  if (policy.maxConcurrentAttempts < 1) {
    throw new ContractError("maxConcurrentAttempts must be >= 1");
  }
  if (
    policy.unknownPricePolicy !== "refuse" && policy.unknownPricePolicy !== "conservative-reserve"
  ) {
    throw new ContractError(
      `unknown unknownPricePolicy ${JSON.stringify(policy.unknownPricePolicy)}`,
    );
  }
  return policy;
}

export function specFromDict(data: Record<string, unknown>): ProjectSpecData {
  checkVersion(data);
  const mode = String(data["mode"] ?? "improvement");
  if (mode !== "improvement" && mode !== "task-only") {
    throw new ContractError(`unknown workspace mode ${JSON.stringify(mode)}`);
  }
  const workflows = (Array.isArray(data["workflows"]) ? data["workflows"] : [])
    .map((w) => workflowFromDict(w as Record<string, unknown>));
  const ownersRaw = (data["owners"] ?? {}) as Record<string, string>;
  const owners: Partial<Record<Role, string>> = {};
  for (const [role, who] of Object.entries(ownersRaw)) {
    if (!ROLES.includes(role as Role)) {
      throw new ContractError(`unknown owner role ${JSON.stringify(role)}`);
    }
    owners[role as Role] = String(who);
  }
  if (mode === "task-only") {
    if (workflows.length > 0) {
      throw new ContractError("a task-only workspace declares no improvement workflows");
    }
  } else {
    if (workflows.length === 0) {
      throw new ContractError("ProjectSpec must declare at least one workflow");
    }
    const missing = (["acceptance-owner", "release-owner"] as Role[]).filter((r) => !owners[r]);
    if (missing.length > 0) {
      throw new ContractError(`ProjectSpec.owners missing required roles: ${missing}`);
    }
  }
  return {
    schemaVersion: "1",
    projectId: requireStr(data["projectId"], "projectId"),
    name: requireStr(data["name"], "name"),
    workflows,
    owners,
    allowedChangeTypes: strs(data["allowedChangeTypes"]),
    dataAuthorization: String(data["dataAuthorization"] ?? "internal-own-workflows"),
    processingLocation: String(data["processingLocation"] ?? "local"),
    mode,
    purpose: String(data["purpose"] ?? ""),
    budget: budgetFromDict(
      (data["budget"] as Record<string, unknown>) ?? { totalUsdCap: 0 },
    ),
    stopConditions: strs(data["stopConditions"]).length
      ? strs(data["stopConditions"])
      : ["budget-exhausted", "guardrail-violation", "timeout"],
    createdAt: requireStr(data["createdAt"] ?? utcNowIso(), "createdAt"),
  };
}

export function specDigest(spec: ProjectSpecData): string {
  return digestOf(spec);
}

function strs(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((v) => String(v));
}

function num(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ContractError(`expected a finite number, got ${JSON.stringify(value)}`);
  }
  return value;
}
