"use client";

import { ReactNode } from "react";
import { message } from "../leadgen/shared";
import styles from "../leadgen/leadgen.module.css";
import local from "./jobradar.module.css";
import { Settings } from "./source-settings";

export type Query = {
  id: number;
  name: string;
  enabled: boolean;
  keywords: string[];
  exclude: string[];
  regions: string[];
  remote_only: boolean;
  salary_from: number;
  allow_no_salary: boolean;
  experience: string[];
  position: number;
};
export type QueryDraft = Omit<Query, "id">;
export type Actions = {
  telegram: { enabled: boolean; chat_id: string | null };
  companies: { enabled: boolean; list_name: string; signal: "hh_marketing" | "hiring" };
  client_crm: { enabled: boolean; responsible_user_id: number | null };
};
export type BySource = {
  state: string;
  found?: number;
  passed?: number;
  dropped?: number;
  reason?: string;
  note?: string;
  error?: string;
};
export type Result = {
  found?: number;
  matched?: number;
  sent?: number;
  leads?: number;
  deals?: number;
  skipped?: number;
  ai_low?: number;
  failed?: number;
  by_source?: Record<string, BySource>;
  by_query?: Record<string, { name: string; found: number; passed: number }>;
  errors?: Record<string, string>;
  at?: string;
};
export type ScannerShort = {
  id: number;
  name: string;
  enabled: boolean;
  kind: "jobs";
  template: string | null;
  owner: { project_id: number; name: string } | null;
  sources: string[];
  actions: string[];
  queries: number;
  last_run_at: string | null;
  last_result: Result | null;
  stats: {
    found_7d: number;
    sent_7d: number;
    leads_7d: number;
    deals_7d: number;
    rated: number;
    liked_share: number | null;
  };
};
export type Scanner = Omit<ScannerShort, "actions"> & {
  actions: Actions;
  profile: string | null;
  ai_min_score: number;
  source_settings: Record<string, Settings>;
  query_items: Query[];
};
export type Overview = {
  bot_configured: boolean;
  ai_available: boolean;
  settings: { interval_minutes: number };
  chats: { id: string; title: string }[];
};
export type Client = {
  project_id: number;
  project: string;
  workspace_id: number;
  name: string;
  team: { id: number; name: string; role: string }[];
};
export type Template = {
  code: string;
  name: string;
  description: string;
  icon: string;
  kind: "jobs";
  preview: { queries: number; sources: string[]; actions: string[] };
};
export const experienceLabels: Record<string, string> = {
  noExperience: "Без опыта",
  between1And3: "1–3 года",
  between3And6: "3–6 лет",
  moreThan6: "Больше 6 лет",
};
export const actionLabels: Record<string, string> = {
  telegram: "✈️ Telegram",
  companies: "📇 База компаний",
  client_crm: "🧲 CRM клиента",
};
export const lines = (text: string) => text.split(/[\n,;]/).map(s => s.trim()).filter(Boolean);

// Validation errors must retain the server's explanation, rather than an arbitrary 80-character cut.
export const scannerError = (error: unknown) => message(error).replace(/https?:\/\/\S+/gi, "[адрес скрыт]");

export function Breakdown({ result }: { result: Result | null }) {
  return <div className={local.breakdown}>
    {Object.entries(result?.by_source || {}).map(([code, row]) => <span key={code}
      title={scannerError(new Error(row.reason || row.error || row.note || row.state))}
      className={row.state === "error" ? styles.errorText : ""}>
      {code === "telegram" ? "tg" : code} {row.found ?? 0}→{row.passed ?? 0}
    </span>)}
  </div>;
}

export function ExternalLink({ href, children }: { href: string | null; children: ReactNode }) {
  return href && /^https?:\/\//i.test(href)
    ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <>{children}</>;
}

export function Stats({ scanner }: { scanner: Scanner | ScannerShort }) {
  const actions = Array.isArray(scanner.actions) ? scanner.actions
    : Object.entries(scanner.actions).filter(([, value]) => value.enabled).map(([key]) => key);
  return <div className={local.stats} aria-label="Находки за 7 дней">
    <span><b>{scanner.stats.found_7d}</b>найдено</span>
    {actions.includes("telegram") && <span><b>{scanner.stats.sent_7d}</b>отправлено</span>}
    {actions.includes("companies") && <span><b>{scanner.stats.leads_7d}</b>в базе</span>}
    {actions.includes("client_crm") && <span><b>{scanner.stats.deals_7d}</b>в CRM</span>}
    <span>👍 {scanner.stats.liked_share == null ? "—" : `${scanner.stats.liked_share}%`}
      {` · оценок: ${scanner.stats.rated}`}</span>
  </div>;
}
