"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { date } from "../leadgen/shared";
import styles from "../leadgen/leadgen.module.css";
import { base, shortMessage } from "./source-settings";
import { ExternalLink, Scanner, scannerError } from "./scanner-types";

type FeedItem = {
  id: number;
  status: string;
  status_name: string;
  reason: string | null;
  score: number | null;
  why: string | null;
  sources: string[];
  query: string | null;
  feedback: number | null;
  company_id: number | null;
  deal_id: number | null;
  sent_at: string | null;
  created_at: string;
  vacancy: { title: string; company: string; city: string; remote: boolean; salary_from: number | null;
    salary_to: number | null; salary_known: boolean; kind: string; url: string | null; published_at: string | null };
};
const statuses = [
  ["sent", "Отправлено"], ["lead", "В базе"], ["deal", "В CRM клиента"],
  ["found", "Найдено"], ["skipped", "Отсеяно"], ["ai_low", "Отсеял ИИ"],
];
const limit = 50;

export function ScannerFeed({ scanner }: { scanner: Scanner }) {
  const [status, setStatus] = useState("sent");
  const [queryId, setQueryId] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ total: number; items: FeedItem[] } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ status, limit: String(limit), offset: String(offset) });
    if (queryId) params.set("query_id", queryId);
    setLoading(true);
    setData(null);
    setError("");
    api<{ total: number; items: FeedItem[] }>(`${base}/scanners/${scanner.id}/feed?${params}`,
      { signal: controller.signal }).then(value => {
        if (!controller.signal.aborted) setData(value);
      }).catch(e => { if (!controller.signal.aborted) setError(scannerError(e)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [scanner.id, scanner.last_run_at, status, queryId, offset, revision]);
  return <section className={styles.card}>
    <h2>Лента сканера</h2>
    <div className={styles.filters}>
      <div className={styles.tabs}>{statuses.map(([code, name]) => <button type="button" key={code}
        className={styles.tab} aria-pressed={status === code}
        onClick={() => { setStatus(code); setOffset(0); }}>{name}</button>)}</div>
      <label className={styles.field}>Запрос<select value={queryId}
        onChange={e => { setQueryId(e.target.value); setOffset(0); }}>
        <option value="">Все запросы</option>
        {scanner.query_items.map(q => <option key={q.id} value={q.id}>{q.name}</option>)}
      </select></label>
    </div>
    {loading && <p role="status">Загружаем ленту…</p>}
    {error && <p role="alert" className={styles.error}>{error}
      <button type="button" className={styles.secondary} onClick={() => setRevision(n => n + 1)}>Повторить</button></p>}
    {data && !data.items.length && <p className={styles.muted}>В этом разделе пока нет находок.</p>}
    {!!data?.items.length && <div className={styles.scroll}><table className={styles.table}>
      <thead><tr><th>Вакансия / проект</th><th>Условия</th><th>Решение</th><th>Запрос</th><th>Дата</th></tr></thead>
      <tbody>{data.items.map(item => <tr key={item.id}>
        <td><ExternalLink href={item.vacancy.url}><b>{item.vacancy.title}</b></ExternalLink>
          <small>{item.vacancy.company || "—"} · {item.sources.join(", ")}</small>
          {item.company_id != null && <small><Link href={`/admin/leadgen/companies?company=${item.company_id}`}>
            Карточка компании</Link></small>}
          {item.deal_id != null && <small>Сделка #{item.deal_id}</small>}</td>
        <td>{item.vacancy.remote ? "Удалёнка" : item.vacancy.city || "—"}
          <small>{item.vacancy.salary_known === false ? "з/п на сайте"
            : `${item.vacancy.salary_from ?? "—"} – ${item.vacancy.salary_to ?? "—"} ₽`}</small></td>
        <td>{item.status_name}{item.feedback != null && (item.feedback > 0 ? " 👍" : " 👎")}
          {item.score != null && <small>🎯 {item.score}/100</small>}
          {item.reason && <small>{shortMessage(new Error(item.reason))}</small>}
          {item.why && <small>{shortMessage(new Error(item.why))}</small>}</td>
        <td>{item.query || "—"}</td><td>{date(item.sent_at || item.created_at)}</td>
      </tr>)}</tbody>
    </table></div>}
    {data && <div className={styles.actions}>
      <button type="button" className={styles.secondary} disabled={loading || offset === 0}
        onClick={() => setOffset(n => Math.max(0, n - limit))}>Назад</button>
      <span>Найдено: {data.total} · страница {Math.floor(offset / limit) + 1}</span>
      <button type="button" className={styles.secondary} disabled={loading || offset + limit >= data.total}
        onClick={() => setOffset(n => n + limit)}>Далее</button>
    </div>}
  </section>;
}
