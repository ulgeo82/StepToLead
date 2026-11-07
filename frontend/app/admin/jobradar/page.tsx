"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { date, PageHeader } from "../leadgen/shared";
import styles from "../leadgen/leadgen.module.css";
import local from "./jobradar.module.css";
import { FilterSourceOptions, SourcesPanel } from "./sources-panel";
import { base, Channel, normalizeValues, send, Settings, shortMessage as message, Source } from "./source-settings";

type Filter = {
  id: number; name: string; mode: "work" | "leads"; keywords: string[]; exclude: string[]; remote_only: boolean;
  regions: string[]; salary_from: number; allow_no_salary: boolean; experience: string[]; sources: string[];
  source_settings: Record<string, Settings>;
  chat_id: string | null; ai_min_score: number; profile: string | null; enabled: boolean; last_run_at: string | null;
  last_result: { found?: number; sent?: number; leads?: number; skipped?: number; ai_low?: number; errors?: Record<string, string>;
    by_source?: Record<string, BySource> };
  stats: { sent_7d: number; leads_7d: number; rated: number; liked_share: number | null };
};
type BySource = { state: "ok" | "skipped" | "error"; found?: number; passed?: number; dropped?: number; reason?: string;
  note?: string; error?: string };
function sourceLine(id: string, b: BySource): string {
  return `${id === "telegram" ? "tg" : id} ${b.found || 0}→${b.passed || 0}`;
}
type Overview = {
  bot_configured: boolean; ai_available: boolean;
  settings: { interval_minutes: number };
  chats: { id: string; title: string }[];
};
type Preview = {
  days: number; total: number; matched: number; companies: number;
  sources: { name: string; found?: number; error?: string; skipped?: string }[];
  items: { title: string; company: string; city: string; remote: boolean; salary_from: number | null; salary_to: number | null;
    url: string; employer_url: string | null; sources: string[]; ok: boolean; reason: string | null }[];
};
type Item = {
  id: number; filter: string; status: string; status_name: string; reason: string | null; score: number | null;
  why: string | null; sources: string[]; feedback: number | null; company_id: number | null; created_at: string | null;
  vacancy: { title: string; company: string | null; city: string | null; remote: boolean; salary_from: number | null;
    salary_to: number | null; url: string | null };
};

const EXPERIENCE: [string, string][] = [["noExperience", "Без опыта"], ["between1And3", "1–3 года"],
  ["between3And6", "3–6 лет"], ["moreThan6", "Больше 6 лет"]];
const STATUSES: [string, string][] = [["", "Все"], ["sent", "Отправлено"], ["lead", "В базе компаний"],
  ["skipped", "Отсеяно"], ["ai_low", "Отсеял ИИ"]];
const tone: Record<string, string> = { sent: "fitYes", lead: "fitYes", skipped: "", ai_low: "fitMaybe", failed: "fitNo" };

const lines = (text: string) => text.split(/[\n,;]/).map((s) => s.trim()).filter(Boolean);
const salary = (v: Item["vacancy"]) => !v.salary_from && !v.salary_to ? "з/п не указана"
  : `${v.salary_from ? `от ${v.salary_from.toLocaleString("ru-RU")}` : ""}${v.salary_to ? ` до ${v.salary_to.toLocaleString("ru-RU")}` : ""} ₽`;
const empty: Omit<Filter, "id" | "last_run_at" | "last_result" | "stats"> = {
  name: "", mode: "work", keywords: [], exclude: [], remote_only: true, regions: [], salary_from: 0, allow_no_salary: true,
  experience: [], sources: ["hh", "trudvsem", "habr"], chat_id: null, ai_min_score: 0, profile: "", enabled: false,
  source_settings: {},
};

export default function JobRadarPage() {
  const [filters, setFilters] = useState<Filter[] | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [sources, setSources] = useState<Source[] | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [editing, setEditing] = useState<Partial<Filter> | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((n) => n + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    const options = { signal: controller.signal };
    const failure = (reason: unknown) => { if (!controller.signal.aborted) setError(message(reason)); };
    api<{ items: Filter[] }>(`${base}/filters`, options)
      .then(d => { if (!controller.signal.aborted) setFilters(d.items); }).catch(failure);
    api<Overview>(`${base}/overview`, options)
      .then(d => { if (!controller.signal.aborted) setOverview(d); }).catch(failure);
    api<{ items: Source[] }>(`${base}/sources`, options)
      .then(d => { if (!controller.signal.aborted) setSources(d.items); }).catch(failure);
    api<{ items: Channel[] }>(`${base}/channels`, options)
      .then(d => { if (!controller.signal.aborted) setChannels(d.items); }).catch(failure);
    return () => controller.abort();
  }, [revision]);

  async function act(f: Filter, kind: "toggle" | "test" | "run" | "delete") {
    if (kind === "delete" && !window.confirm(`Удалить фильтр «${f.name}» вместе с историей?`)) return;
    setBusy(true); setError(""); setNotice("");
    try {
      if (kind === "toggle") await send(`/filters/${f.id}`, { enabled: !f.enabled }, "PATCH");
      if (kind === "test") { await send(`/filters/${f.id}/test`, {}); setNotice("Тестовый пост отправлен — проверьте чат"); }
      if (kind === "run") { await send(`/filters/${f.id}/run`, {}); setNotice("Проверяем сайты — результат появится в ленте через минуту-две"); }
      if (kind === "delete") await api(`${base}/filters/${f.id}`, { method: "DELETE" });
      refresh();
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }

  return (
    <div className={`page ${styles.page}`}>
      <PageHeader title="Радар вакансий" subtitle="Площадки, публичные Telegram-каналы и фильтры вакансий и проектов.">
        <div className={styles.actions}><button type="button" className={styles.primary} disabled={!sources || !overview}
          onClick={() => setEditing({ ...empty, sources: empty.sources.filter(code =>
            sources?.some(s => s.code === code && s.enabled && s.state !== "needs_key")) })}>＋ Новый фильтр</button></div>
      </PageHeader>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}
      {overview && !overview.bot_configured && <p className={styles.error}>Бот портала не подключён — посты в Telegram отправлять некуда. Укажите токен бота в настройках интеграций.</p>}

      {sources ? <SourcesPanel sources={sources} channels={channels} filters={filters || []} refresh={refresh}/>
        : <p role="status" className={styles.muted}>Загружаем площадки…
          <button type="button" className={styles.secondary} onClick={refresh}>Повторить</button></p>}

      <section className={styles.card}>
        <h2>Фильтры</h2>
        {!filters ? <p className={styles.muted} role="status">Загружаем…</p> : !filters.length ? <p className={styles.muted}>Фильтров нет.</p> : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead><tr><th>Фильтр</th><th>Вкл.</th><th>За 7 дней</th><th>👍</th><th>Последняя проверка</th><th /></tr></thead>
              <tbody>
                {filters.map((f) => (
                  <tr key={f.id}>
                    <td><b>{f.name}</b><small>{f.mode === "leads" ? "Лиды → база компаний" : "Вакансии"} · {f.keywords.slice(0, 4).join(", ")}{f.keywords.length > 4 ? "…" : ""}</small></td>
                    <td><label className={styles.check}><input type="checkbox" checked={f.enabled} disabled={busy} onChange={() => act(f, "toggle")} aria-label={`Включить «${f.name}»`} /></label></td>
                    <td>{f.mode === "leads" ? `${f.stats.leads_7d} комп.` : `${f.stats.sent_7d} отправлено`}</td>
                    <td>{f.stats.liked_share == null ? "—" : `${f.stats.liked_share}%`}<small>{f.stats.rated ? `оценок ${f.stats.rated}` : ""}</small></td>
                    <td>{date(f.last_run_at)}
                      <small>{f.last_result.found != null ? `найдено ${f.last_result.found}, ${f.mode === "leads" ? `в базу ${f.last_result.leads ?? 0}` : `отправлено ${f.last_result.sent ?? 0}`}` : ""}</small>
                      {f.last_result.by_source ? <div className={local.breakdown}>
                        {Object.entries(f.last_result.by_source).map(([id, b], index) => (
                          <span key={id} title={message(new Error(b.reason || b.error || b.note || b.state))}
                            className={b.state === "error" ? styles.errorText : ""}>
                            {index > 0 ? " · " : ""}{sourceLine(id, b)}</span>))}</div>
                        : f.last_result.errors && Object.keys(f.last_result.errors).length > 0 && <small className={styles.errorText}>Ошибки: {Object.keys(f.last_result.errors).join(", ")}</small>}</td>
                    <td><div className={styles.rowActions}>
                      <button type="button" className={styles.secondary} onClick={() => setEditing(f)}>Изменить</button>
                      <button type="button" className={styles.secondary} disabled={busy || !f.chat_id} onClick={() => act(f, "test")}>Тестовый пост</button>
                      <button type="button" className={styles.secondary} disabled={busy} onClick={() => act(f, "run")}>Проверить сейчас</button>
                      <button type="button" className={`${styles.secondary} ${styles.danger}`} disabled={busy} onClick={() => act(f, "delete")}>Удалить</button>
                    </div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {overview && <RunInterval overview={overview} onSaved={refresh} />}
      <Feed filters={filters || []} revision={revision} />
      {editing && overview && sources && <FilterForm value={editing} chats={overview.chats}
        sources={sources} channels={channels} aiAvailable={overview.ai_available}
        close={() => setEditing(null)} saved={() => { setEditing(null); setNotice("Фильтр сохранён"); refresh(); }} />}
    </div>
  );
}

function RunInterval({ overview, onSaved }: { overview: Overview; onSaved: () => void }) {
  const [every, setEvery] = useState(overview.settings.interval_minutes);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(e: FormEvent) {
    e.preventDefault(); setError(""); setBusy(true);
    try {
      await send("/settings", { interval_minutes: every }, "PATCH");
      onSaved();
    } catch (err) { setError(message(err)); } finally { setBusy(false); }
  }
  return (
    <section className={styles.card}>
      <h2>Проверка по расписанию</h2>
      <form className={styles.inlineForm} onSubmit={save}>
        <label className={styles.field}>Проверять каждые, мин
          <input type="number" min={5} max={1440} required disabled={busy} value={every} onChange={(e) => setEvery(Number(e.target.value))} />
        </label>
        <button type="submit" disabled={busy} className={styles.secondary}>Сохранить</button>
      </form>
      {error && <p className={styles.error} role="alert">{error}</p>}
    </section>
  );
}

function FilterForm({ value, chats, sources, channels, aiAvailable, close, saved }: {
  value: Partial<Filter>; chats: Overview["chats"]; aiAvailable: boolean; close: () => void; saved: () => void;
  sources: Source[]; channels: Channel[];
}) {
  const [f, setF] = useState({ ...empty, ...value, source_settings: value.source_settings || {} });
  const [keywords, setKeywords] = useState((value.keywords || []).join("\n"));
  const [exclude, setExclude] = useState((value.exclude || []).join("\n"));
  const [regions, setRegions] = useState((value.regions || []).join(", "));
  const [manual, setManual] = useState(!!value.chat_id && !chats.some((c) => c.id === value.chat_id));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [days, setDays] = useState(3);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [looking, setLooking] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const toggle = (key: "experience" | "sources", item: string) =>
    setF({ ...f, [key]: f[key].includes(item) ? f[key].filter((x) => x !== item) : [...f[key], item] });

  const formBody = () => ({ mode: f.mode, keywords: lines(keywords), exclude: lines(exclude), regions: lines(regions),
    remote_only: f.remote_only, salary_from: Number(f.salary_from) || 0, allow_no_salary: f.allow_no_salary,
    experience: f.experience, sources: f.sources,
    source_settings: Object.fromEntries(Object.entries(f.source_settings).map(([code, values]) =>
      [code, normalizeValues(sources.find(s => s.code === code)?.filter_schema || [], values)])) });

  async function look() {
    setLooking(true); setError(""); setPreview(null); setShowAll(false);
    try { setPreview(await send<Preview>("/preview", { ...formBody(), days })); }
    catch (err) { setError(message(err)); } finally { setLooking(false); }
  }

  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError("");
    const body = { name: f.name.trim(), ...formBody(), chat_id: (f.chat_id || "").trim() || null,
      ai_min_score: Number(f.ai_min_score) || 0, profile: (f.profile || "").trim() || null, enabled: f.enabled };
    try {
      if (value.id) await send(`/filters/${value.id}`, body, "PATCH"); else await send("/filters", body);
      saved();
    } catch (err) { setError(message(err)); setBusy(false); }
  }

  return <>
    <button type="button" className={styles.overlay} aria-label="Закрыть" tabIndex={-1} onClick={close} />
    <form role="dialog" aria-modal="true" aria-labelledby="jr-form" className={`${styles.card} ${styles.modal} ${styles.modalWide}`} onSubmit={submit}>
      <h2 id="jr-form">{value.id ? `Фильтр «${value.name}»` : "Новый фильтр"}</h2>
      {error && <p className={styles.error} role="alert">{error}</p>}
      <div className={styles.fields}>
        <label className={styles.field}>Название<input required minLength={2} maxLength={160} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <label className={styles.field}>Режим
          <select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value as Filter["mode"] })}>
            <option value="work">Вакансии — присылать в Telegram</option>
            <option value="leads">Лиды — компании, которые нанимают, в «Базу компаний»</option>
          </select>
          <small>{f.mode === "leads" ? "Компания из вакансии попадёт в базу лидогенерации со списком «Нанимает маркетолога» и ссылкой на вакансию. Дубли и стоп-лист учитываются, сделки в CRM не создаются." : "Новые вакансии приходят постами с кнопками 👍 / 👎 — по ним ИИ учится, что вам подходит."}</small>
        </label>
        <label className={styles.field}>Ключевые слова — хотя бы одно в вакансии или проекте
          <textarea rows={4} value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder={"маркетолог\nдиректолог"} />
          <small>По одному в строке или через запятую. Регистр и «ё/е» не важны.
            Telegram и FL.ru проверяют также текст, getmatch — описание и стек.</small>
        </label>
        <label className={styles.field}>Исключения — совпадение исключает вакансию или проект
          <textarea rows={3} value={exclude} onChange={(e) => setExclude(e.target.value)} placeholder={"senior\nhead of"} />
        </label>
        <label className={styles.check}><input type="checkbox" checked={f.remote_only} onChange={(e) => setF({ ...f, remote_only: e.target.checked })} />Только удалёнка</label>
        <label className={styles.field}>Города<input value={regions} onChange={(e) => setRegions(e.target.value)} placeholder="пусто — вся Россия; например: Самара, Казань" /></label>
        <div className={styles.field}>
          <label className={styles.field}>Зарплата от, ₽<input type="number" min={0} step={1000} value={f.salary_from} onChange={(e) => setF({ ...f, salary_from: Number(e.target.value) })} /></label>
          <label className={styles.check}><input type="checkbox" checked={f.allow_no_salary} onChange={(e) => setF({ ...f, allow_no_salary: e.target.checked })} />Брать вакансии без зарплаты</label>
          <small>0 — зарплата не важна.</small>
        </div>
        <fieldset className={styles.formFieldset}><legend className={styles.legend}>Опыт (hh.ru)</legend>
          {EXPERIENCE.map(([id, name]) => <label key={id} className={styles.check}><input type="checkbox" checked={f.experience.includes(id)} onChange={() => toggle("experience", id)} />{name}</label>)}
          <small className={styles.hint}>Ничего не выбрано — любой опыт.</small>
        </fieldset>
        <FilterSourceOptions sources={sources} selected={f.sources} settings={f.source_settings}
          channels={channels} toggle={code => toggle("sources", code)}
          onChange={source_settings => setF({ ...f, source_settings })}/>
        <label className={styles.field}>Telegram-чат
          {!manual ? (
            <select value={f.chat_id || ""} onChange={(e) => e.target.value === "__manual" ? setManual(true) : setF({ ...f, chat_id: e.target.value || null })}>
              <option value="">{f.mode === "leads" ? "Не присылать — только в базу" : "Выберите чат"}</option>
              {chats.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}
              <option value="__manual">Другой канал — ввести id…</option>
            </select>
          ) : (
            <input value={f.chat_id || ""} onChange={(e) => setF({ ...f, chat_id: e.target.value })} placeholder="-1001234567890 или @имя_канала" />
          )}
          <small>Для канала: добавьте бота портала в канал <b>администратором</b> с правом публикации, затем укажите id канала (вида -100…) или @имя. Проверьте кнопкой «Тестовый пост». Оценки 👍/👎 засчитываются от админов, которые подключили Telegram в портале.</small>
        </label>
        <label className={styles.field}>Порог ИИ, 0–100
          <input type="number" min={0} max={100} value={f.ai_min_score} onChange={(e) => setF({ ...f, ai_min_score: Number(e.target.value) })} />
          <small>{aiAvailable ? "0 — без ИИ-отбора. Например, 60 — присылать только хорошо подходящие (ИИ оценит по профилю и вашим 👍/👎, токены — из резерва агентства)." : "ИИ в портале не подключён — порог не сработает, вакансии придут без оценки."}</small>
        </label>
        <label className={styles.field}>Профиль для ИИ — кого ищем
          <textarea rows={3} maxLength={2000} value={f.profile || ""} onChange={(e) => setF({ ...f, profile: e.target.value })} />
        </label>
        <label className={styles.check}><input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} />Включён — проверять по расписанию</label>
      </div>
      <section className={styles.formFieldset} aria-live="polite">
        <div className={styles.inlineForm}>
          <button type="button" className={styles.secondary} disabled={looking || !lines(keywords).length || !f.sources.length} onClick={look}>
            {looking ? "Ищем на площадках…" : "🔍 Посмотреть, что найдёт"}</button>
          <label className={styles.field}>за последние
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[1, 3, 7, 14].map((d) => <option key={d} value={d}>{d} {d === 1 ? "день" : d < 5 ? "дня" : "дней"}</option>)}
            </select></label>
        </div>
        <small className={styles.hint}>Пробный поиск по условиям выше: ничего не сохраняет, в базу и в Telegram ничего не уходит.</small>
        {preview && <>
          <p><b>Подходит {preview.matched} из {preview.total}</b>{f.mode === "leads" ? ` · компаний: ${preview.companies}` : ""}
            {" · "}{preview.sources.map((s) => s.skipped ? `${s.name}: пропущен` : s.error ? `${s.name}: ошибка` : `${s.name}: ${s.found}`).join(" · ")}</p>
          {preview.sources.filter((s) => s.error || s.skipped).map((s) => <p key={s.name}
            className={s.error ? styles.errorText : styles.muted}>
            {s.name}: {message(new Error(s.error || s.skipped || ""))}</p>)}
          <div className={styles.scroll} style={{ maxHeight: 360 }}>
            <table className={styles.table}>
              <tbody>{preview.items.filter((i) => showAll || i.ok).map((i, n) => (
                <tr key={n}>
                  <td>{i.ok ? "✅" : "—"}</td>
                  <td>{i.url ? <a href={i.url} target="_blank" rel="noopener noreferrer">{i.title}</a> : i.title}
                    <small>{i.company || "—"}{i.employer_url ? <> · <a href={i.employer_url} target="_blank" rel="noopener noreferrer">работодатель</a></> : ""} · {i.sources.join(", ")}</small></td>
                  <td>{i.remote ? "удалёнка" : i.city || "—"}<small>{i.salary_from || i.salary_to ? `${i.salary_from ? `от ${i.salary_from.toLocaleString("ru-RU")}` : ""}${i.salary_to ? ` до ${i.salary_to.toLocaleString("ru-RU")}` : ""} ₽` : "з/п не указана"}</small></td>
                  <td>{i.reason ? <small>{i.reason}</small> : null}</td>
                </tr>))}</tbody>
            </table>
          </div>
          {preview.total > preview.matched && <button type="button" className={styles.secondary} onClick={() => setShowAll(!showAll)}>
            {showAll ? "Только подходящие" : `Показать и отсеянные (${preview.total - preview.matched}) — с причиной`}</button>}
        </>}
      </section>
      <div className={styles.actions}>
        <button type="submit" className={styles.primary} disabled={busy}>{busy ? "Сохраняем…" : "Сохранить"}</button>
        <button type="button" className={styles.secondary} onClick={close}>Отмена</button>
      </div>
    </form>
  </>;
}

function Feed({ filters, revision }: { filters: Filter[]; revision: number }) {
  const [status, setStatus] = useState("");
  const [filterId, setFilterId] = useState("");
  const [data, setData] = useState<{ total: number; items: Item[] } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const p = new URLSearchParams({ limit: "100" });
    if (status) p.set("status", status);
    if (filterId) p.set("filter_id", filterId);
    api<{ total: number; items: Item[] }>(`${base}/vacancies?${p}`).then(setData).catch((e) => setError(message(e)));
  }, [status, filterId, revision]);
  return (
    <section className={styles.card}>
      <h2>Найденные вакансии</h2>
      <div className={styles.filters}>
        <div className={styles.tabs}>{STATUSES.map(([id, name]) => <button key={id} type="button" className={styles.tab} aria-pressed={status === id} onClick={() => setStatus(id)}>{name}</button>)}</div>
        {filters.length > 1 && <select value={filterId} onChange={(e) => setFilterId(e.target.value)} aria-label="Фильтр">
          <option value="">Все фильтры</option>{filters.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}</select>}
      </div>
      {error && <p className={styles.error}>{error}</p>}
      {!data ? <p className={styles.muted}>Загружаем…</p> : !data.items.length ? <p className={styles.muted}>Пока пусто. Включите фильтр или нажмите «Проверить сейчас».</p> : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead><tr><th>Вакансия</th><th>Условия</th><th>Решение</th><th>Когда</th></tr></thead>
            <tbody>{data.items.map((m) => (
              <tr key={m.id}>
                <td>{m.vacancy.url ? <a href={m.vacancy.url} target="_blank" rel="noopener noreferrer"><b>{m.vacancy.title}</b></a> : <b>{m.vacancy.title}</b>}
                  <small>{m.vacancy.company || "—"} · {m.sources.join(", ")}{filters.length > 1 ? ` · ${m.filter}` : ""}</small></td>
                <td>{salary(m.vacancy)}<small>{m.vacancy.remote ? "удалёнка" : m.vacancy.city || ""}</small></td>
                <td><span className={`${styles.badge} ${tone[m.status] ? styles[tone[m.status]] : ""}`}>{m.status_name}</span>
                  {m.feedback != null && <span> {m.feedback > 0 ? "👍" : "👎"}</span>}
                  <small>{m.reason ? message(new Error(m.reason)) : ""}{m.score != null ? ` 🎯 ${m.score}/100${m.why ? ` — ${m.why}` : ""}` : ""}</small>
                  {m.company_id && <small><Link href={`/admin/leadgen/companies?company=${m.company_id}`}>Карточка компании</Link></small>}</td>
                <td>{date(m.created_at)}</td>
              </tr>
            ))}</tbody>
          </table>
          {data.total > data.items.length && <p className={styles.muted}>Показаны последние {data.items.length} из {data.total}.</p>}
        </div>
      )}
    </section>
  );
}
