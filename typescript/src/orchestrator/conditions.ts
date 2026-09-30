/**
 * Deterministic completion-condition checkers. The trusted supervisor —
 * never the model — decides whether a run completed.
 */

import { digestOf, isPlainObject } from "../contracts/canonical.ts";
import { ContractError } from "../contracts/common.ts";

export const CONDITION_ARTIFACT_SCHEMA = "artifact_schema";
export const CONDITION_OUTPUT_CONTAINS = "output_contains";
export const CONDITION_MAX_COST_USD = "max_cost_usd";
export const CONDITION_NONE = "none";

export const SUPPORTED_SCHEMA_KEYWORDS = new Set(["type", "required", "properties"]);

const JSON_TYPES = new Set(["object", "array", "string", "number", "integer", "boolean", "null"]);
const DEFAULT_ARTIFACT_KEY = "final";
const COST_EPSILON = 1e-9;

export interface Condition {
  type: string;
  artifact?: string;
  schema?: Record<string, unknown>;
  contains?: string;
  maxUsd?: number | null;
}

export function validateSchemaSubset(schema: unknown, where = "schema"): void {
  if (!isPlainObject(schema)) {
    throw new ContractError(`${where} must be a JSON object`);
  }
  const unknown = Object.keys(schema).filter((k) => !SUPPORTED_SCHEMA_KEYWORDS.has(k)).sort();
  if (unknown.length > 0) {
    throw new ContractError(
      `${where} uses unsupported schema keywords ${
        JSON.stringify(unknown)
      }; v1 subset is type/required/properties`,
    );
  }
  const typeName = schema["type"];
  if (typeName !== undefined && (typeof typeName !== "string" || !JSON_TYPES.has(typeName))) {
    throw new ContractError(`${where}.type must be one of ${JSON.stringify([...JSON_TYPES])}`);
  }
  const required = schema["required"];
  if (
    required !== undefined &&
    (!Array.isArray(required) || !required.every((k) => typeof k === "string"))
  ) {
    throw new ContractError(`${where}.required must be a list of strings`);
  }
  const properties = schema["properties"];
  if (properties !== undefined) {
    if (!isPlainObject(properties)) {
      throw new ContractError(`${where}.properties must be a JSON object`);
    }
    for (const [name, subSchema] of Object.entries(properties)) {
      validateSchemaSubset(subSchema, `${where}.properties.${name}`);
    }
  }
}

function typeMatches(value: unknown, typeName: string): boolean {
  switch (typeName) {
    case "object":
      return isPlainObject(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "boolean":
      return typeof value === "boolean";
    case "null":
      return value === null;
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "number":
      return typeof value === "number";
    default:
      throw new ContractError(`unknown JSON type ${JSON.stringify(typeName)}`);
  }
}

export function checkJsonSchemaSubset(
  instance: unknown,
  schema: Record<string, unknown>,
): string | null {
  const typeName = schema["type"];
  if (typeof typeName === "string" && !typeMatches(instance, typeName)) {
    return `expected type ${typeName}, got ${jsonTypeName(instance)}`;
  }
  if ("required" in schema) {
    if (!isPlainObject(instance)) return "required fields apply to JSON objects";
    const required = (schema["required"] as string[]) ?? [];
    const missing = required.filter((k) => !(k in instance)).sort();
    if (missing.length > 0) return `missing required fields ${JSON.stringify(missing)}`;
  }
  const properties = schema["properties"];
  if (properties !== undefined) {
    if (!isPlainObject(instance)) return "properties apply to JSON objects";
    for (const [name, subSchema] of Object.entries(properties as Record<string, unknown>)) {
      if (!(name in instance)) continue;
      const reason = checkJsonSchemaSubset(instance[name], subSchema as Record<string, unknown>);
      if (reason !== null) return `field ${JSON.stringify(name)}: ${reason}`;
    }
  }
  return null;
}

function jsonTypeName(value: unknown): string {
  if (value === null) return "NoneType";
  if (Array.isArray(value)) return "list";
  if (isPlainObject(value)) return "dict";
  if (typeof value === "number") return Number.isInteger(value) ? "int" : "float";
  return typeof value;
}

export function normalizeCriteria(criteria: unknown): Condition[] {
  if (!isPlainObject(criteria)) {
    throw new ContractError("success_criteria must be a JSON object");
  }
  const raw = criteria["conditions"] ?? [];
  if (!Array.isArray(raw)) {
    throw new ContractError("success_criteria.conditions must be a list");
  }
  const normalized: Condition[] = [];
  raw.forEach((entry, index) => {
    const where = `success_criteria.conditions[${index}]`;
    if (!isPlainObject(entry)) throw new ContractError(`${where} must be a JSON object`);
    const type = entry["type"];
    if (
      type !== CONDITION_ARTIFACT_SCHEMA && type !== CONDITION_OUTPUT_CONTAINS &&
      type !== CONDITION_MAX_COST_USD && type !== CONDITION_NONE
    ) {
      throw new ContractError(
        `${where} has unknown condition type ${JSON.stringify(type)}; known types: ` +
          JSON.stringify(["artifact_schema", "max_cost_usd", "none", "output_contains"].sort()),
      );
    }
    if (type === CONDITION_ARTIFACT_SCHEMA) {
      const schema = entry["schema"];
      validateSchemaSubset(schema, `${where}.schema`);
      normalized.push({
        type,
        artifact: artifactKey(entry, where),
        schema: schema as Record<string, unknown>,
      });
    } else if (type === CONDITION_OUTPUT_CONTAINS) {
      const contains = entry["contains"];
      if (typeof contains !== "string" || contains.length === 0) {
        throw new ContractError(`${where}.contains must be a non-empty string`);
      }
      normalized.push({ type, artifact: artifactKey(entry, where), contains });
    } else if (type === CONDITION_MAX_COST_USD) {
      const maxUsd = entry["maxUsd"];
      if (maxUsd !== undefined && maxUsd !== null) {
        if (typeof maxUsd !== "number" || !Number.isFinite(maxUsd)) {
          throw new ContractError(`${where}.maxUsd must be a number`);
        }
        if (maxUsd < 0) throw new ContractError(`${where}.maxUsd must be >= 0`);
      }
      normalized.push({ type, maxUsd: (maxUsd as number | undefined) ?? null });
    } else {
      normalized.push({ type });
    }
  });
  return normalized;
}

function artifactKey(condition: Record<string, unknown>, where: string): string {
  const key = condition["artifact"] ?? DEFAULT_ARTIFACT_KEY;
  if (typeof key !== "string" || key.length === 0) {
    throw new ContractError(`${where}.artifact must be a non-empty string`);
  }
  return key;
}

export interface CheckResult {
  key: string;
  conditionType: string;
  passed: boolean;
  detail: string;
}

export interface ConditionEvaluation {
  results: CheckResult[];
}

export interface ConditionContext {
  artifacts: Map<string, string>;
  loadArtifact: (digest: string) => Uint8Array;
  measuredCostUsd: number;
  hasUnmeasuredCost: boolean;
  costBoundUsd: number | null;
}

export function evaluateConditions(
  conditions: Condition[],
  context: ConditionContext,
): ConditionEvaluation {
  const results: CheckResult[] = [];
  conditions.forEach((condition, index) => {
    const key = `${condition.type}[${index}]`;
    if (condition.type === CONDITION_NONE) {
      results.push({
        key,
        conditionType: condition.type,
        passed: false,
        detail: "explicit human-acceptance marker: not machine-checkable",
      });
      return;
    }
    if (condition.type === CONDITION_MAX_COST_USD) {
      results.push(checkMaxCost(key, condition, context));
      return;
    }
    results.push(checkArtifactCondition(key, condition, context));
  });
  return { results };
}

export function allPassed(evaluation: ConditionEvaluation): boolean {
  return evaluation.results.length > 0 && evaluation.results.every((r) => r.passed);
}

export function machineChecksPassed(evaluation: ConditionEvaluation): boolean {
  return evaluation.results.filter((r) => r.conditionType !== CONDITION_NONE).every((r) =>
    r.passed
  );
}

export function humanMarkers(evaluation: ConditionEvaluation): CheckResult[] {
  return evaluation.results.filter((r) => r.conditionType === CONDITION_NONE);
}

export function checksMap(evaluation: ConditionEvaluation): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const r of evaluation.results) out[r.key] = r.passed;
  return out;
}

function checkArtifactCondition(
  key: string,
  condition: Condition,
  context: ConditionContext,
): CheckResult {
  const artifactKey = condition.artifact ?? "final";
  const digest = context.artifacts.get(artifactKey);
  if (digest === undefined) {
    return {
      key,
      conditionType: condition.type,
      passed: false,
      detail: `artifact ${JSON.stringify(artifactKey)} was not produced by the run`,
    };
  }
  let payload: Uint8Array;
  try {
    payload = context.loadArtifact(digest);
  } catch (exc) {
    return {
      key,
      conditionType: condition.type,
      passed: false,
      detail: `artifact ${JSON.stringify(artifactKey)} could not be loaded: ${exc}`,
    };
  }
  const text = new TextDecoder("utf-8", { fatal: false }).decode(payload);
  if (condition.type === CONDITION_OUTPUT_CONTAINS) {
    const contains = condition.contains!;
    const passed = text.includes(contains);
    return {
      key,
      conditionType: condition.type,
      passed,
      detail: `artifact ${JSON.stringify(artifactKey)} ${
        passed ? "contains" : "does not contain"
      } ${reprLite(contains)}`,
    };
  }
  let instance: unknown;
  try {
    instance = JSON.parse(text);
  } catch (exc) {
    return {
      key,
      conditionType: condition.type,
      passed: false,
      detail: `artifact ${JSON.stringify(artifactKey)} is not valid JSON: ${exc}`,
    };
  }
  const reason = checkJsonSchemaSubset(instance, condition.schema!);
  const schemaDigest = digestOf(condition.schema!);
  const passed = reason === null;
  return {
    key,
    conditionType: condition.type,
    passed,
    detail: `artifact ${JSON.stringify(artifactKey)} against schema ${schemaDigest}: ` +
      (passed ? "matched" : `mismatch — ${reason}`),
  };
}

function checkMaxCost(key: string, condition: Condition, context: ConditionContext): CheckResult {
  let bound = condition.maxUsd ?? null;
  if (bound === null) bound = context.costBoundUsd;
  if (bound === null) {
    return {
      key,
      conditionType: CONDITION_MAX_COST_USD,
      passed: false,
      detail: "no cost bound declared to check against",
    };
  }
  if (context.hasUnmeasuredCost) {
    return {
      key,
      conditionType: CONDITION_MAX_COST_USD,
      passed: false,
      detail:
        "total cost is unmeasured (at least one unmeasurable model call); cannot verify the bound",
    };
  }
  const spent = context.measuredCostUsd;
  const passed = spent <= bound + COST_EPSILON;
  return {
    key,
    conditionType: CONDITION_MAX_COST_USD,
    passed,
    detail: `measured $${spent.toFixed(6)} vs bound $${bound.toFixed(6)}`,
  };
}

/** Python `repr`-ish rendering for condition details. */
function reprLite(value: string): string {
  return `'${value}'`;
}
