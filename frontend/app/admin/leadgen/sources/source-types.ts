import type { Estimate } from "@/components/task-progress";
import type { QueryPurpose } from "../ai-query-utils";

export type Values = Record<string, unknown>;
export type Field = {
  key: string;
  label: string;
  type: "text" | "number" | "bool" | "list" | "secret" | "select" | "region" | "multi";
  ai?: { purpose: QueryPurpose };
  default?: unknown;
  hint?: string;
  options?: { value: string | number; label: string }[];
  required?: boolean;
  min?: number;
  max?: number;
};
export type CompanySource = {
  code: string;
  kind: "finder" | "enricher";
  title: string;
  icon: string;
  purpose: string;
  enabled: boolean;
  needs_key: boolean;
  ready: boolean;
  ready_reason: string | null;
  settings_schema: Field[];
  settings: Values;
  query_schema: Field[];
  state: "ok" | "error" | "never" | "off";
  last_ok_at: string | null;
  last_error: string | null;
  signals: { kind: string; label: string }[];
  chainable?: boolean;
  chain_default?: boolean;
};
export type Target = { list_id: number } | { segment_id: number };
export type RunBody = { query?: Values; target?: Target; list_name?: string; then?: string[] };
export type ChainSummary = {
  done: number; skipped: number; failed: number;
  reasons?: Record<string, number>; errors?: Record<string, string>;
  valid?: number; invalid?: number; risky?: number; cleaned?: number; site_found?: number; inn_found?: number;
};
export type SourceEstimate = Estimate & {
  units: number; companies?: number; cost?: { requests: number; rub: number | null };
};
export type SourceRun = {
  id: number;
  source: string;
  list_id: number | null;
  result: {
    found: number;
    new: number;
    updated: number;
    skipped: number;
    signals: Record<string, number>;
    errors: Record<string, string>;
    chain?: Record<string, ChainSummary>;
    reasons?: Record<string, number>;
    valid?: number;
    invalid?: number;
    risky?: number;
  };
};

export function shortError(reason: unknown): string {
  const text = reason instanceof Error ? reason.message : String(reason || "Не удалось выполнить запрос");
  const clean = text.replace(/https?:\/\/\S+|\b(?:[\w-]+\.)+[a-z]{2,}(?:\/\S*)?/gi, "[адрес скрыт]")
    .replace(/\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:\/\S*)?/g, "[адрес скрыт]")
    .replace(/\s+/g, " ").trim();
  return /[а-яё]/i.test(clean) ? clean.slice(0, 180) : "Не удалось выполнить запрос. Попробуйте ещё раз.";
}

export function defaults(schema: Field[]): Values {
  return Object.fromEntries(schema.filter(f => f.default !== undefined && f.type !== "secret" &&
    f.type !== "region" && !/city|region|город/i.test(f.key)).map(f => [f.key, f.default]));
}

export function normalize(schema: Field[], values: Values): Values {
  const result: Values = {};
  for (const field of schema) {
    if (!(field.key in values)) continue;
    const value = values[field.key];
    if (field.type === "secret") {
      if (typeof value === "string") result[field.key] = value;
    } else if (field.type === "list") {
      result[field.key] = Array.isArray(value) ? value : String(value ?? "").split(/\r?\n/)
        .map(line => line.trim()).filter(Boolean);
    } else if (field.type === "multi") {
      result[field.key] = Array.isArray(value) ? [...new Set(value)] : [];
    } else if (field.type === "number") {
      if (value !== "" && value != null && Number.isFinite(Number(value))) result[field.key] = Number(value);
    } else if (value !== "" && value != null) result[field.key] = value;
    else if (field.type === "text" || field.type === "region") result[field.key] = "";
  }
  return result;
}

export function valid(schema: Field[], values: Values): boolean {
  const body = normalize(schema, values);
  return schema.every(field => {
    const value = body[field.key];
    if (field.type === "number" && values[field.key] !== "" && values[field.key] != null && value == null) {
      return false;
    }
    if (field.required && (value == null || value === "" ||
      (Array.isArray(value) && !value.length) || (typeof value === "string" && !value.trim()))) return false;
    if (field.type === "number" && value != null) {
      return Number.isFinite(Number(value)) && (field.min == null || Number(value) >= field.min) &&
        (field.max == null || Number(value) <= field.max);
    }
    if (field.type === "multi" && field.key in values) {
      return Array.isArray(values[field.key]) && (value as unknown[]).every(item =>
        field.options?.some(option => option.value === item));
    }
    return true;
  });
}
