"use client";

import { FormEvent, useEffect, useState } from "react";
import { Estimate, EstimateHint, TaskProgress } from "@/components/task-progress";
import { api } from "@/lib/api";
import styles from "../leadgen/leadgen.module.css";
import local from "./jobradar.module.css";
import { base, send, shortMessage } from "./source-settings";
import { experienceLabels, ExternalLink, lines, Query, QueryDraft, Scanner, scannerError } from "./scanner-types";

type Preview = {
  total: number;
  matched: number;
  companies: number;
  sources: { code: string; name: string; found: number | null; skipped: number | string | null;
    error: string | null }[];
  by_query: { id: number | null; name: string; found: number; passed: number }[];
  items: { title: string; company: string; city: string; remote: boolean; url: string; employer_url: string | null;
    salary_from: number | null; salary_to: number | null; salary_known: boolean; kind: string;
    sources: string[]; ok: boolean; reason: string | null; query: string | null }[];
};
const empty: QueryDraft = {
  name: "", enabled: true, keywords: [], exclude: [], regions: [], remote_only: false,
  salary_from: 0, allow_no_salary: true, experience: [], position: 0,
};

export function Queries({ scanner, changed }: { scanner: Scanner; changed: () => void }) {
  const [editing, setEditing] = useState<Query | QueryDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [days, setDays] = useState(3);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [looking, setLooking] = useState(false);
  const [since, setSince] = useState(0);
  const estimate = useEstimate(scanner, null, looking);
  async function look(queries?: QueryDraft[]) {
    setSince(Date.now() / 1000);
    setLooking(true);
    setPreview(null);
    setError("");
    try { setPreview(await send<Preview>(`/scanners/${scanner.id}/preview`, { days, ...(queries ? { queries } : {}) })); }
    catch (e) { setError(scannerError(e)); }
    finally { setLooking(false); }
  }
  async function mutate(query: Query, remove = false) {
    if (remove && !window.confirm(`Удалить запрос «${query.name}»?`)) return;
    setBusy(true);
    setError("");
    try {
      if (remove) await api(`${base}/queries/${query.id}`, { method: "DELETE" });
      else await send(`/queries/${query.id}`, { enabled: !query.enabled }, "PATCH");
      changed();
    } catch (e) { setError(scannerError(e)); }
    finally { setBusy(false); }
  }
  return <section className={styles.card}>
    <div className={local.head}><h2>Запросы</h2>
      <button type="button" className={styles.primary} disabled={busy || looking || !!editing}
        onClick={() => {
          const first = scanner.query_items[0];   // город и формат — как у первого запроса сканера
          setEditing({ ...empty, regions: first?.regions || [], remote_only: first?.remote_only ?? false,
            position: scanner.query_items.length });
          setPreview(null);
        }}>
        ＋ Добавить запрос</button></div>
    {error && <p className={styles.error} role="alert">{error}</p>}
    {!scanner.query_items.length && <p className={styles.muted}>Запросов пока нет.</p>}
    <div className={local.scannerList}>{scanner.query_items.map(query => <article key={query.id}
      className={local.queryCard}>
      <div className={local.head}><h3>{query.name}</h3>
        <label className={styles.check}><input type="checkbox" checked={query.enabled}
          disabled={busy || looking || !!editing} onChange={() => mutate(query)}
          aria-label={`Включить запрос «${query.name}»`}/>Вкл.</label></div>
      <dl className={local.queryFacts}>
        <dt>Слова</dt><dd>{query.keywords.join(", ") || "—"}</dd>
        <dt>Исключения</dt><dd>{query.exclude.join(", ") || "—"}</dd>
        <dt>Города</dt><dd>{query.regions.join(", ") || "Любые"}</dd>
        <dt>Формат</dt><dd>{query.remote_only ? "Только удалёнка" : "Любой"}</dd>
        <dt>Зарплата</dt><dd>{query.salary_from ? `От ${query.salary_from.toLocaleString("ru-RU")} ₽` : "Любая"}
          {query.allow_no_salary ? " · без указанной зарплаты тоже" : " · только с зарплатой"}</dd>
        <dt>Опыт</dt><dd>{query.experience.map(code => experienceLabels[code] || code).join(", ") || "Любой"}</dd>
      </dl>
      <div className={styles.rowActions}>
        <button type="button" className={styles.secondary} disabled={busy || looking || !!editing}
          onClick={() => { setEditing(query); setPreview(null); }}>Изменить</button>
        <button type="button" className={`${styles.secondary} ${styles.danger}`} disabled={busy || looking || !!editing}
          onClick={() => mutate(query, true)}>Удалить</button>
      </div>
    </article>)}</div>
    {editing && <QueryEditor key={"id" in editing ? editing.id : "new"} value={editing} scannerId={scanner.id}
      scanner={scanner} looking={looking} preview={draft => look([draft])} close={() => setEditing(null)}
      saved={() => { setEditing(null); setPreview(null); changed(); }}/>} 
    <div className={styles.inlineForm}>
      <label className={styles.field}>Период предпросмотра<select value={days} disabled={looking}
        onChange={e => setDays(Number(e.target.value))}>
        {[1, 3, 7, 14].map(day => <option value={day} key={day}>{day} дн.</option>)}
      </select></label>
      <button type="button" className={styles.secondary} disabled={looking || busy || !!editing}
        onClick={() => look()}>{looking ? "Ищем…" : "🔍 Посмотреть, что найдёт"}</button>
      {!looking && <EstimateHint estimate={estimate}/>}
    </div>
    <small className={styles.hint}>Без сохранения находок и отправки в Telegram, базу компаний или CRM.</small>
    {looking && <TaskProgress url={`${base}/scanners/${scanner.id}/progress?kind=preview`} active since={since}/>}
    {preview && <PreviewResults preview={preview}/>}
  </section>;
}

function QueryEditor({ value, scanner, scannerId, looking, preview, close, saved }: {
  value: Query | QueryDraft; scanner: Scanner; scannerId: number; looking: boolean;
  preview: (draft: QueryDraft) => void; close: () => void; saved: () => void;
}) {
  const [draft, setDraft] = useState(value);
  const [keywords, setKeywords] = useState(value.keywords.join("\n"));
  const [exclude, setExclude] = useState(value.exclude.join("\n"));
  const [regions, setRegions] = useState(value.regions.join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const draftEstimate = useEstimate(scanner, lines(keywords).length
    ? { name: draft.name || "Черновик", keywords: lines(keywords), regions: lines(regions) } : undefined);
  const body = (): QueryDraft => ({
    name: draft.name.trim(), enabled: draft.enabled, keywords: lines(keywords), exclude: lines(exclude),
    regions: lines(regions), remote_only: draft.remote_only, salary_from: draft.salary_from,
    allow_no_salary: draft.allow_no_salary, experience: draft.experience, position: draft.position,
  });
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if ("id" in value) await send(`/queries/${value.id}`, body(), "PATCH");
      else await send(`/scanners/${scannerId}/queries`, body());
      saved();
    } catch (e) { setError(scannerError(e)); }
    finally { setBusy(false); }
  }
  return <form className={local.queryCard} onSubmit={submit}>
    <h3>{"id" in value ? "Изменить запрос" : "Новый запрос"}</h3>
    {error && <p className={styles.error} role="alert">{error}</p>}
    <fieldset disabled={busy || looking} className={styles.formFieldset}>
      <div className={styles.fields}>
        <label className={styles.field}>Название<input required maxLength={160} value={draft.name}
          onChange={e => setDraft({ ...draft, name: e.target.value })}/></label>
        <label className={styles.field}>Города<input value={regions} onChange={e => setRegions(e.target.value)}/></label>
        <label className={styles.field}>Ключевые слова<textarea required rows={3} value={keywords}
          onChange={e => setKeywords(e.target.value)}/><small>По одному в строке или через запятую.</small></label>
        <label className={styles.field}>Исключения<textarea rows={3} value={exclude}
          onChange={e => setExclude(e.target.value)}/></label>
        <label className={styles.field}>Зарплата от, ₽<input type="number" min={0} value={draft.salary_from}
          onChange={e => setDraft({ ...draft, salary_from: Number(e.target.value) })}/></label>
        <label className={styles.check}><input type="checkbox" checked={draft.remote_only}
          onChange={e => setDraft({ ...draft, remote_only: e.target.checked })}/>Только удалёнка</label>
        <label className={styles.check}><input type="checkbox" checked={draft.allow_no_salary}
          onChange={e => setDraft({ ...draft, allow_no_salary: e.target.checked })}/>Брать без зарплаты</label>
        <label className={styles.check}><input type="checkbox" checked={draft.enabled}
          onChange={e => setDraft({ ...draft, enabled: e.target.checked })}/>Запрос включён</label>
        <fieldset className={styles.formFieldset}><legend>Опыт (hh.ru)</legend>
          {Object.entries(experienceLabels).map(([code, label]) => <label key={code} className={styles.check}>
            <input type="checkbox" checked={draft.experience.includes(code)} onChange={e => setDraft({ ...draft,
              experience: e.target.checked ? [...draft.experience, code] : draft.experience.filter(v => v !== code),
            })}/>{label}</label>)}
        </fieldset>
      </div>
      <div className={styles.actions}>
        <button className={styles.primary} type="submit">Сохранить запрос</button>
        <button className={styles.secondary} type="button" disabled={!lines(keywords).length}
          onClick={() => preview(body())}>🔍 Проверить черновик</button>
        <EstimateHint estimate={draftEstimate}/>
        <button className={styles.secondary} type="button" onClick={close}>Отмена</button>
      </div>
    </fieldset>
  </form>;
}

/** Сколько займёт предпросмотр: по сохранённым запросам (draft = null) или по черновику. Пауза 0,6 с после ввода. */
function useEstimate(scanner: Scanner, draft: { name: string; keywords: string[]; regions: string[] } | null | undefined,
  busy = false) {
  const [value, setValue] = useState<Estimate | null>(null);
  const key = JSON.stringify([scanner.sources, scanner.source_settings, draft === null
    ? scanner.query_items.map(q => [q.enabled, q.keywords, q.regions]) : draft]);
  useEffect(() => {
    if (draft === undefined || busy) { if (draft === undefined) setValue(null); return; }
    let alive = true;
    const timer = setTimeout(() => {
      send<Estimate>(`/scanners/${scanner.id}/estimate`, { kind: "preview", ...(draft ? { queries: [draft] } : {}) })
        .then(v => { if (alive) setValue(v); }).catch(() => { if (alive) setValue(null); });
    }, 600);
    return () => { alive = false; clearTimeout(timer); };
  }, [scanner.id, key, busy]);   // eslint-disable-line react-hooks/exhaustive-deps
  return value;
}

function PreviewResults({ preview }: { preview: Preview }) {
  const [all, setAll] = useState(true);
  return <div aria-live="polite" className={local.preview}>
    <h3>Подходит {preview.matched} из {preview.total} · компаний: {preview.companies}</h3>
    <div className={local.breakdown}>{preview.sources.map(row => <span key={row.code}>
      {row.name}: {row.found ?? "—"}
      {row.error ? ` · ${shortMessage(new Error(row.error))}` : row.skipped
        ? ` · пропущено: ${shortMessage(new Error(String(row.skipped)))}` : ""}
    </span>)}</div>
    <div className={local.breakdown}>{preview.by_query.map((row, index) => <span key={row.id ?? index}>
      {row.name}: {row.found}→{row.passed}</span>)}</div>
    <label className={styles.check}><input type="checkbox" checked={all} onChange={e => setAll(e.target.checked)}/>
      Показывать отсеянные с причиной</label>
    <div className={styles.scroll}><table className={styles.table}>
      <thead><tr><th>Решение</th><th>Вакансия / проект</th><th>Условия</th><th>Запрос</th></tr></thead>
      <tbody>{preview.items.filter(item => all || item.ok).map((item, index) => <tr key={index}>
        <td>{item.ok ? "✅" : shortMessage(new Error(item.reason || "Отсеяно"))}</td>
        <td><ExternalLink href={item.url}>{item.title}</ExternalLink><small>{item.company || "—"}
          {" · "}{item.sources.join(", ")}</small>
          {item.employer_url && <ExternalLink href={item.employer_url}>Работодатель</ExternalLink>}</td>
        <td>{item.remote ? "Удалёнка" : item.city || "—"}<small>{item.salary_known === false ? "з/п на сайте"
          : `${item.salary_from ?? "—"} – ${item.salary_to ?? "—"} ₽`}</small></td>
        <td>{item.query || "—"}</td>
      </tr>)}</tbody>
    </table></div>
    {!preview.items.length && <p>Ничего не найдено.</p>}
  </div>;
}
