import type { ReactNode } from "react";
import { api } from "@/lib/api";
import styles from "./leadgen.module.css";

export const base = "/admin/leadgen";
export const post = <T,>(path: string, body: unknown, signal?: AbortSignal) =>
  api<T>(`${base}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
export function message(error: unknown): string {
  const text = error instanceof Error ? error.message : "Не удалось выполнить запрос. Попробуйте ещё раз.";
  return /Источник не настроен/i.test(text)
    ? "Подключите XMLStock в настройках сервера (XMLSTOCK_USER, XMLSTOCK_KEY, XMLSTOCK_LIVE_URL)."
    : text;
}
export const date = (value?: string | null) => (value ? new Date(value).toLocaleString("ru-RU") : "—");
export const money = (value?: number | null) =>
  value == null ? "—" : `${value.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
export const stages: Record<string, string> = {
  new: "Новая",
  enriched: "Обогащена",
  ready: "Готова",
  in_outreach: "В аутриче",
  replied: "Ответила",
  converted: "Клиент",
  rejected: "Отказ",
  dnc: "Стоп-лист",
};
export const fitLabels: Record<string, string> = { fit: "Подходит", maybe: "Возможно", no: "Не подходит" };
export const signalLabels: Record<string, string> = {
  ad_direct: "В Директе по 3+ ключам",
  ad_premium: "Спецразмещение",
  new_advertiser: "Новый рекламодатель",
  ads_stopped: "Пропал из рекламы",
  has_messenger: "Есть WhatsApp или Telegram",
  site_quiz: "Квиз на сайте",
  no_crm: "Нет CRM на сайте",
  legal_active: "Действующее юрлицо",
  hh_marketing: "Ищут маркетолога",
  twogis_card: "Есть в 2ГИС",
  slow_response: "Медленно отвечают на заявки",
  chain: "Федеральная сеть",
};
export const blocked = (stage: string) => ["dnc", "rejected", "converted"].includes(stage);
export const safeUrl = (value?: string | null): string | undefined => {
  if (!value) return;
  try {
    const url = new URL(
      value.startsWith("/") ? value : /^https?:\/\//i.test(value) ? value : `https://${value}`,
      "https://steptolead.ru",
    );
    return ["https:", "http:"].includes(url.protocol)
      ? value.startsWith("/")
        ? value
        : url.href
      : undefined;
  } catch {
    return;
  }
};
export type Company = {
  id: number;
  display_name: string;
  legal_name?: string | null;
  domain?: string | null;
  inn?: string | null;
  city?: string | null;
  niche?: string | null;
  score: number;
  score_reasons: { kind: string; label: string; weight: number }[];
  stage: string;
  fit_label?: "fit" | "maybe" | "no" | null;
  needs_review: boolean;
  crm_deal_id?: number | null;
  first_seen_at?: string;
  last_seen_at?: string;
};
export type Ad = {
  title?: string | null;
  text?: string | null;
  keywords?: string[];
  landing_url?: string | null;
  placement?: string;
  first_seen_at?: string;
  last_seen_at?: string;
};
export type CompanyDetail = Company & {
  ogrn?: string | null;
  okved?: string | null;
  director_name?: string | null;
  director_post?: string | null;
  revenue_rub?: number | null;
  revenue_year?: number | null;
  legal_status?: string | null;
  fit_reason?: string | null;
  provenance?: unknown;
  ads: Ad[];
  contacts: {
    id: number;
    kind: string;
    value: string;
    is_personal: boolean;
    person_name?: string;
    source?: string;
    source_url?: string;
    found_at?: string;
    verify_status?: string;
    bounced?: boolean;
  }[];
  signals: { kind: string; payload: unknown; observed_at?: string; expires_at?: string }[];
  touches?: {
    channel: string;
    direction: "in" | "out";
    status: string;
    subject?: string | null;
    happened_at?: string | null;
  }[];
};
export type Run = {
  id: number;
  source: string;
  status: string;
  params: { keywords?: string[]; niche?: string; city?: string; region_code?: number };
  stats: {
    requests?: number;
    cost_rub?: number;
    companies?: number;
    new_advertisers?: number;
    already_in_base?: number;
    stopped?: number;
    failed?: Record<string, string>;
  };
  error?: string | null;
  created_at: string;
  finished_at?: string | null;
  companies?: (Company & { is_new: boolean; ad?: Ad | null })[];
};
export type Filters = {
  q?: string;
  niche?: string;
  cities?: string[];
  min_score?: number;
  stages?: string[];
  channels?: string[];
  signals?: string[];
  new_days?: number;
  needs_review?: boolean;
  fit?: "fit" | "maybe" | "no";
};
export type Segment = { id: number; name: string; filters: Filters; count: number };
export type Pipeline = { project_id: number; pipeline_id: number; crm_url: string };
export type Outreach = {
  moved: number;
  skipped: number;
  project_id: number;
  pipeline_id: number;
  results: { company_id: number; ok: boolean; deal_id?: number; reason?: string; reason_text?: string }[];
};
export function Score({ value }: { value: number }) {
  return (
    <span className={`${styles.score} ${value >= 7 ? styles.high : value >= 4 ? styles.medium : styles.low}`}>
      {value}
    </span>
  );
}
export function Fit({ label, reason }: { label?: string | null; reason?: string | null }) {
  if (!label) return null;
  const tone = label === "fit" ? styles.fitYes : label === "no" ? styles.fitNo : styles.fitMaybe;
  return (
    <span className={`${styles.badge} ${tone}`} title={reason || undefined}>
      {fitLabels[label] || label}
    </span>
  );
}
export function PageHeader({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children?: ReactNode;
}) {
  return (
    <>
      <header className="topbar">
        <div className="crumbs">
          <span>StepToLead</span>
          <b>/</b>
          <strong>Лидогенерация</strong>
        </div>
        <span className="userAvatar">ЕД</span>
      </header>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Рост агентства</p>
          <h1>{title}</h1>
          <p className="subtitle">{subtitle}</p>
        </div>
        {children}
      </section>
    </>
  );
}
export function Advertisement({ ad }: { ad: Ad }) {
  return (
    <article className={styles.ad}>
      <strong>{ad.title || "Без заголовка"}</strong>
      <p>{ad.text || "Текст не получен"}</p>
      {ad.landing_url && safeUrl(ad.landing_url) && (
        <a href={safeUrl(ad.landing_url)} target="_blank" rel="noopener noreferrer">
          Посадочная страница ↗
        </a>
      )}
      {ad.keywords?.length ? <small>Ключевые слова: {ad.keywords.join(", ")}</small> : null}
    </article>
  );
}
