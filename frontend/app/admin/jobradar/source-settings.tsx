"use client";

import { api } from "@/lib/api";
import { message } from "../leadgen/shared";
import styles from "../leadgen/leadgen.module.css";

export const base = "/admin/jobradar";
export const send = <T,>(path: string, body: unknown, method = "POST") =>
  api<T>(`${base}${path}`, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    timeoutMs: path.endsWith("/preview") ? 180_000 : path.endsWith("/check") ? 75_000 : 30_000,
  });
export const shortMessage = (error: unknown) => message(error)
  .replace(/https?:\/\/\S+/gi, "[адрес скрыт]")
  .replace(/\s+/g, " ").slice(0, 80);
export const list = (value: unknown): string[] => Array.isArray(value)
  ? value.filter((v): v is string => typeof v === "string")
  : typeof value === "string" ? value.split(/[\n,;]/).map(s => s.trim()).filter(Boolean) : [];

export type SchemaField = {
  key: string; label: string; type: "bool" | "text" | "number" | "list" | "secret" | "proxy";
  default?: unknown; hint?: string;
};
export type Settings = Record<string, unknown>;
export type ProxyOption = { id: number; label: string; status: string };
export type Source = {
  code: string; title: string; icon: string; purpose: string; needs_key: boolean; enabled: boolean;
  state: "ok" | "needs_key" | "error" | "off" | "unknown"; message: string | null;
  last_ok_at: string | null; stats_24h: { found: number; passed: number; sent: number };
  settings: Settings; settings_schema: SchemaField[]; filter_schema: SchemaField[];
  proxy_options?: ProxyOption[];
};
export type SourcePreview = {
  title: string; company?: string | null; url?: string; city?: string | null; remote: boolean;
  salary_from?: number | null; salary_to?: number | null; salary_known?: boolean; kind?: string;
  published_at?: string | null;
};
export type CheckResult = {
  ok: boolean; message: string; title?: string | null; channel?: string; status?: string;
  preview: SourcePreview[];
};
export type Channel = {
  channel: string; status: "ok" | "unavailable" | "closed" | "unknown";
  last_post_at: string | null; passed_24h: number; error: string | null;
  scanners: { id: number; name: string }[];
};

export function normalizeValues(schema: SchemaField[], values: Settings): Settings {
  const result = { ...values };
  for (const field of schema) {
    if (!(field.key in result)) continue;
    if (field.type === "list") result[field.key] = list(result[field.key]);
  }
  return result;
}

export function SchemaFields({ schema, values, saved = {}, onChange, prefix, proxyOptions = [] }: {
  schema: SchemaField[]; values: Settings; saved?: Settings; prefix: string;
  proxyOptions?: ProxyOption[];
  onChange: (value: Settings) => void;
}) {
  return <div className={styles.fields}>{schema.map(field => {
    const value = values[field.key] ?? saved[field.key] ?? field.default;
    const set = (next: unknown) => onChange({ ...values, [field.key]: next });
    const id = `${prefix}-${field.key}`;
    if (field.type === "bool") return <label key={field.key} className={styles.check}>
      <input id={id} type="checkbox" checked={!!value} onChange={e => set(e.target.checked)}/>
      {field.label}{field.hint && <small>{field.hint}</small>}
    </label>;
    const secret = field.type === "secret";
    const secretSaved = secret && typeof saved[field.key] === "object" &&
      !!(saved[field.key] as { set?: boolean } | null)?.set;
    const text = secret ? typeof values[field.key] === "string" ? String(values[field.key]) : ""
      : Array.isArray(value) ? value.join("\n") : String(value ?? "");
    return <label className={styles.field} key={field.key} htmlFor={id}>
      {field.label}{secret && <small>{secretSaved ? "Секрет сохранён" : "Секрет не задан"}</small>}
      {field.type === "proxy" ? <select id={id} value={text} onChange={e => set(e.target.value ? Number(e.target.value) : null)}>
        <option value="">Без прокси</option>
        {proxyOptions.map(proxy => <option key={proxy.id} value={proxy.id}>
          {proxy.label}{proxy.status === "error" ? " · ошибка" : ""}
        </option>)}
        {text && !proxyOptions.some(p => String(p.id) === text) && <option value={text}>Прокси #{text}</option>}
      </select> : field.type === "list" ? <textarea id={id} rows={3} value={text} onChange={e => set(e.target.value)}/>
        : <input id={id} type={secret ? "password" : field.type === "number" ? "number" : "text"}
          autoComplete={secret ? "new-password" : "off"} spellCheck={false}
          value={text} step={field.type === "number" ? "any" : undefined}
          onChange={e => set(field.type === "number" ? e.target.value === "" ? null : Number(e.target.value)
            : e.target.value)}/>}
      {field.hint && <small>{field.hint}</small>}
      {secret && secretSaved && <button type="button" className={styles.secondary}
        onClick={() => set("")}>Удалить сохранённый секрет при сохранении</button>}
      {secret && values[field.key] === "" && field.key in values &&
        <small>Секрет будет удалён после сохранения.</small>}
    </label>;
  })}</div>;
}

export function Previews({ result }: { result: CheckResult }) {
  return <div role="status">
    <p className={result.ok ? styles.muted : styles.errorText}>{shortMessage(new Error(result.message))}</p>
    {(result.preview || []).slice(0, 3).map((p, index) => <article className={styles.ad} key={index}>
      <strong>{p.url && /^https?:\/\//i.test(p.url)
        ? <a href={p.url} target="_blank" rel="noopener noreferrer">{p.title}</a> : p.title}</strong>
      <p>{[p.company, p.remote ? "Удалёнка" : p.city, p.kind === "project" ? "Проект" : "Вакансия"]
        .filter(Boolean).join(" · ")}</p>
      <small>{p.salary_known === false ? "з/п на сайте" : p.salary_from || p.salary_to
        ? `${p.salary_from ? `от ${p.salary_from.toLocaleString("ru-RU")}` : ""}${
          p.salary_to ? ` до ${p.salary_to.toLocaleString("ru-RU")}` : ""}` : "Зарплата не указана"}</small>
    </article>)}
  </div>;
}
