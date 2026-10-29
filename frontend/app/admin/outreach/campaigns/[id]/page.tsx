"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { base, date, message, post } from "../../../leadgen/shared";
import styles from "../../../leadgen/leadgen.module.css";
import { timezoneGroups, timezones } from "../../email/types";
import type { Mailbox, Sequence } from "../../email/types";
import { Campaign, Check, clientLabel, Column, ImportReport, Lead, OutreachClient, scheduleText, STATUS, STOP_REASONS, UploadPreview, WEEKDAYS } from "../types";

const zoneName = (tz: string) => timezones.find(([z]) => z === tz)?.[1] || tz;
const nowIn = (tz: string) => {
  try { return new Date().toLocaleString("ru-RU", { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
};

type Tab = "leads" | "letters" | "settings";
const LEAD_FILTERS: [string, string][] = [["", "Все"], ["active", "В работе"], ["replied", "Ответили"], ["finished", "Цепочка пройдена"],
  ["bounced", "Не доставлено"], ["stopped", "Остановлены"]];
const send = <T,>(path: string, body: object, method = "PATCH") =>
  api<T>(`${base}${path}`, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export default function CampaignPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [c, setC] = useState<Campaign | null>(null);
  const [tab, setTab] = useState<Tab>("leads");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((n) => n + 1), []);

  useEffect(() => {
    api<Campaign>(`${base}/campaigns/${id}`).then(setC).catch((e) => setError(message(e)));
  }, [id, revision]);

  async function status(next: "active" | "paused" | "done") {
    if (next === "done" && !window.confirm("Завершить кампанию? Письма по ней больше не уйдут, ответы останутся во «Входящих».")) return;
    setBusy(true); setError(""); setNotice("");
    try { setC(await send<Campaign>(`/campaigns/${id}`, { status: next })); setNotice(next === "active" ? "Кампания запущена — письма уходят в окно отправки, по лимитам ящиков" : next === "paused" ? "Кампания на паузе" : "Кампания завершена"); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function remove() {
    if (!window.confirm("Удалить кампанию вместе с лидами? Компании останутся в базе.")) return;
    try { await api(`${base}/campaigns/${id}`, { method: "DELETE" }); router.push("/admin/outreach/campaigns"); }
    catch (e) { setError(message(e)); }
  }

  if (!c) return <div className={`page ${styles.page}`}>{error ? <p className={styles.error}>{error}</p> : <p role="status">Загружаем кампанию…</p>}</div>;
  const s = c.stats;
  return (
    <div className={`page ${styles.page}`}>
      <header className="topbar"><div className="crumbs"><Link href="/admin/outreach/campaigns">Кампании</Link><b>/</b><strong>{c.name}</strong></div><span className="userAvatar">ЕД</span></header>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">{c.client ? <>Для клиента <b>{clientLabel(c.client)}</b> · </> : "Кампания · "}<span className={`${styles.badge} ${STATUS[c.status].tone ? styles[STATUS[c.status].tone] : ""}`}>{STATUS[c.status].name}</span></p>
          <h1>{c.name}</h1>
          <p className="subtitle">Письма уходят: {scheduleText(c.window, zoneName(c.window.tz))} · сейчас там {nowIn(c.window.tz)}
            <br />{c.start_at && c.status !== "done" ? `Старт ${date(c.start_at)} · ` : ""}{c.mailbox_ids.length} ящ. · {c.steps} шаг. в цепочке</p>
        </div>
        <div className={styles.actions}>
          {(c.status === "draft" || c.status === "paused") && <button type="button" className={styles.primary} disabled={busy} onClick={() => status("active")}>▶ Запустить</button>}
          {c.status === "active" && <button type="button" className={styles.secondary} disabled={busy} onClick={() => status("paused")}>⏸ Пауза</button>}
          {c.status !== "done" && c.status !== "draft" && <button type="button" className={styles.secondary} disabled={busy} onClick={() => status("done")}>Завершить</button>}
          {c.status === "draft" && !s?.sent && <button type="button" className={`${styles.secondary} ${styles.danger}`} onClick={remove}>Удалить</button>}
          <Link className={styles.secondary} href={`/admin/outreach/inbox?campaign=${c.id}`}>Входящие кампании</Link>
        </div>
      </section>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}

      {s && (
        <section className={styles.card}>
          <dl className={styles.metrics}>
            <div><dt>Лидов</dt><dd>{s.leads}</dd></div>
            <div><dt>В работе</dt><dd>{s.waiting}</dd></div>
            <div><dt>Написали</dt><dd>{s.contacted}</dd></div>
            <div><dt>Писем отправлено</dt><dd>{s.sent}</dd></div>
            <div><dt>Ответили</dt><dd>{s.replied}{s.contacted ? <small className={styles.muted}> · {s.reply_rate}%</small> : null}</dd></div>
            <div><dt>Интерес</dt><dd>{s.interested}</dd></div>
            <div><dt>🔥 Горячие</dt><dd>{s.hot}</dd></div>
            <div><dt>Отписались</dt><dd>{s.unsubscribed}</dd></div>
            <div><dt>Не доставлено</dt><dd>{s.bounced}</dd></div>
          </dl>
        </section>
      )}

      <div className={styles.tabs}>
        {([["leads", "Лиды"], ["letters", "Письма"], ["settings", "Расписание и ящики"]] as [Tab, string][]).map(([k, n]) => (
          <button key={k} type="button" className={styles.tab} aria-pressed={tab === k} onClick={() => setTab(k)}>{n}</button>
        ))}
      </div>
      {tab === "leads" && <LeadsTab c={c} onChange={(next) => { if (next) setC(next); refresh(); }} />}
      {tab === "letters" && <LettersTab c={c} />}
      {tab === "settings" && <SettingsTab c={c} onSaved={setC} />}
    </div>
  );
}

function LeadsTab({ c, onChange }: { c: Campaign; onChange: (c?: Campaign) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [pv, setPv] = useState<UploadPreview | null>(null);
  const [cols, setCols] = useState<Column[]>([]);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [leads, setLeads] = useState<{ total: number; items: Lead[] } | null>(null);
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");
  const [rev, setRev] = useState(0);

  useEffect(() => {
    const p = new URLSearchParams({ limit: "200" });
    if (filter) p.set("status", filter);
    if (q.trim()) p.set("q", q.trim());
    const t = setTimeout(() => api<{ total: number; items: Lead[] }>(`${base}/campaigns/${c.id}/leads?${p}`).then(setLeads).catch((e) => setError(message(e))), q ? 300 : 0);
    return () => clearTimeout(t);
  }, [c.id, filter, q, rev]);

  async function choose(f: File | null) {
    setFile(f); setPv(null); setReport(null); setError("");
    if (!f) return;
    const form = new FormData();
    form.append("file", f);
    setBusy(true);
    try {
      const r = await api<UploadPreview>(`${base}/campaigns/${c.id}/leads/preview`, { method: "POST", body: form });
      setPv(r); setCols(r.columns);
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function upload() {
    if (!file || !pv) return;
    const form = new FormData();
    form.append("file", file);
    form.append("mapping", JSON.stringify(cols.map((x) => ({ index: x.index, field: x.field, var: x.var || null }))));
    setBusy(true); setError("");
    try {
      const r = await api<ImportReport>(`${base}/campaigns/${c.id}/leads/import`, { method: "POST", body: form, timeoutMs: 300_000 });
      setReport(r); setPv(null); setFile(null); setRev((n) => n + 1); onChange(r.campaign);
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function stop(lead: Lead) {
    try { await post(`/campaigns/${c.id}/leads/${lead.id}/stop`, {}); setRev((n) => n + 1); onChange(); }
    catch (e) { setError(message(e)); }
  }
  const emailCols = cols.filter((x) => x.field === "email").length;

  return <>
    {error && <p className={styles.error} role="alert">{error}</p>}
    {c.status !== "done" && (
      <section className={styles.card}>
        <h2>Загрузить лидов</h2>
        <p className={styles.muted}>CSV или Excel: столбец с email обязателен; компания, сайт, имя — по желанию. Любой другой столбец станет переменной для писем: «Боль» → <code>{"{{боль}}"}</code>.</p>
        <div className={styles.actions}>
          <label className={styles.secondary}>
            {file ? file.name : "Выбрать файл…"}
            <input type="file" hidden accept=".csv,.xlsx,.xlsm,.txt" onChange={(e) => void choose(e.target.files?.[0] || null)} />
          </label>
          <Link className={styles.secondary} href={`/admin/leadgen/companies?campaign=${c.id}`}>Добавить из базы компаний →</Link>
          {busy && <span className={styles.muted} role="status">Обрабатываем…</span>}
        </div>
        {pv && <>
          <p>В файле <b>{pv.rows}</b> строк. Проверьте, что означает каждый столбец:</p>
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead><tr><th>Столбец в файле</th><th>Примеры</th><th>Что это</th></tr></thead>
              <tbody>
                {cols.map((col, i) => (
                  <tr key={col.index}>
                    <td><b>{col.header}</b><small>заполнено {col.filled} из {pv.rows}</small></td>
                    <td className={styles.muted}>{col.sample.join(" · ") || "—"}</td>
                    <td>
                      <select value={col.field} onChange={(e) => setCols(cols.map((x, j) => j === i ? { ...x, field: e.target.value, var: e.target.value === "var" ? (x.var || x.header.toLowerCase().replace(/[^0-9a-zа-яё]+/gi, "_").replace(/^_|_$/g, "")) : x.var } : x))}>
                        {Object.entries(pv.fields).map(([k, n]) => <option key={k} value={k}>{n}</option>)}
                      </select>
                      {col.field === "var" && (
                        <label className={styles.inlineForm}><small className={styles.muted}>{"{{"}</small>
                          <input value={col.var || ""} onChange={(e) => setCols(cols.map((x, j) => j === i ? { ...x, var: e.target.value } : x))} aria-label="Имя переменной" />
                          <small className={styles.muted}>{"}}"}</small></label>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {emailCols !== 1 && <p className={styles.errorText}>Выберите ровно один столбец «Email».</p>}
          <p className={styles.hint}>Перед загрузкой каждый адрес проверим: формат, есть ли у домена почта, нет ли в стоп-листе, не писали ли недавно, нет ли в другой кампании.</p>
          <div className={styles.actions}>
            <button type="button" className={styles.primary} disabled={busy || emailCols !== 1} onClick={upload}>{busy ? "Проверяем адреса…" : `Загрузить ${pv.rows} лидов`}</button>
            <button type="button" className={styles.secondary} onClick={() => { setPv(null); setFile(null); }}>Отмена</button>
          </div>
        </>}
        {report && (
          <div className={styles.exportSummary}>
            <p>Добавлено <b>{report.added}</b>{report.skipped ? <> · пропущено <b>{report.skipped}</b>:</> : null}</p>
            {Object.entries(report.reasons).map(([r, n]) => <p key={r} className={styles.muted}>— {r}: {n}</p>)}
            {report.warnings.map((w) => <p key={w} className={styles.hint}>⚠️ {w}</p>)}
            {!!report.skipped_rows.length && <details><summary>Какие строки пропущены</summary>
              <ul>{report.skipped_rows.map((r, i) => <li key={i} className={styles.muted}>{r.line ? `стр. ${r.line}: ` : ""}{r.email || r.company || "—"} — {r.reason_text}</li>)}</ul></details>}
          </div>
        )}
      </section>
    )}

    <section className={styles.card}>
      <div className={styles.inboxTools}>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Статус">
          {LEAD_FILTERS.map(([k, n]) => <option key={k} value={k}>{n}</option>)}
        </select>
        <input type="search" placeholder="Компания, домен, email" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {!leads ? <p className={styles.muted}>Загружаем…</p> : !leads.items.length ? <p className={styles.muted}>Лидов нет.</p> : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead><tr><th>Компания</th><th>Email</th><th>Статус</th><th>Шаг</th><th>Следующее письмо</th><th>Ящик</th><th /></tr></thead>
            <tbody>
              {leads.items.map((l) => (
                <tr key={l.id}>
                  <td><b>{l.company}</b><small>{l.domain}</small></td>
                  <td>{l.email || "—"}</td>
                  <td>{l.status_name}{l.stop_reason && <small>{STOP_REASONS[l.stop_reason] || l.stop_reason}</small>}</td>
                  <td>{l.step}</td>
                  <td>{l.status === "active" ? date(l.next_at) : "—"}</td>
                  <td>{l.mailbox || <span className={styles.muted}>выберется при отправке</span>}</td>
                  <td>{l.status === "active" && <button type="button" className={styles.secondary} onClick={() => stop(l)}>Убрать</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {leads.total > leads.items.length && <p className={styles.muted}>Показаны {leads.items.length} из {leads.total}. Уточните поиск.</p>}
        </div>
      )}
    </section>
  </>;
}

function LettersTab({ c }: { c: Campaign }) {
  const [seq, setSeq] = useState<Sequence | null>(null);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [lead, setLead] = useState("");
  const [preview, setPreview] = useState<{ subject: string | null; body: string; channel: string; html?: string | null }[] | null>(null);
  const [to, setTo] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [check, setCheck] = useState<Check | null>(null);

  useEffect(() => {
    api<{ items: Sequence[] }>(`${base}/sequences?include=${c.sequence_id}`).then((d) => setSeq(d.items.find((x) => x.id === c.sequence_id) || null)).catch((e) => setError(message(e)));
    api<{ items: Lead[] }>(`${base}/campaigns/${c.id}/leads?limit=50&status=active`).then((d) => { setLeads(d.items); if (d.items[0]) setLead(String(d.items[0].id)); }).catch(() => {});
    api<Check>(`${base}/campaigns/${c.id}/check`).then(setCheck).catch(() => {});
  }, [c.id, c.sequence_id]);

  async function showPreview() {
    if (!seq || !lead) return;
    setError("");
    try { setPreview((await post<{ steps: { subject: string | null; body: string; channel: string; html?: string | null }[] }>("/sequences/preview", { enrollment_id: Number(lead), steps: seq.steps })).steps); }
    catch (e) { setError(message(e)); }
  }
  async function test() {
    if (!seq) return;
    setError(""); setNotice("");
    try {
      const r = await post<{ sent: number; from: string }>("/sequences/test", { to, steps: seq.steps, enrollment_id: lead ? Number(lead) : null, mailbox_id: c.mailbox_ids[0] || null });
      setNotice(`Отправлено ${r.sent} писем с ${r.from} на ${to} — с пометкой [ТЕСТ]`);
    } catch (e) { setError(message(e)); }
  }

  return <>
    {error && <p className={styles.error} role="alert">{error}</p>}
    {notice && <p className={styles.notice} role="status">{notice}</p>}
    <section className={styles.card}>
      <div className={styles.inboxHead}>
        <h2>Письма кампании</h2>
        <Link className={styles.primary} href={`/admin/outreach/email?tab=sequences&sequence=${c.sequence_id}&campaign=${c.id}`}>Редактировать письма</Link>
      </div>
      {!!c.variables.length && <p className={styles.muted}>Ваши переменные из файла: {c.variables.map((v) => <code key={v}>{`{{${v}}}`} </code>)} — значение по умолчанию: <code>{"{{боль|заявки}}"}</code></p>}
      {!seq ? <p className={styles.muted}>Загружаем…</p> : (
        <ol className={styles.history}>
          {seq.steps.map((st, i) => (
            <li key={i}>
              <b>{st.channel === "email" ? `✉️ Письмо${i ? "" : " 1"}` : st.channel === "call" ? "📞 Позвонить (только для заведённых в CRM)" : `💬 ${st.channel}`}</b>
              <small className={styles.muted}> · через {st.delay_days} дн.</small>
              {st.subject && <p><b>{st.subject}</b></p>}
              <p className={styles.muted} style={{ whiteSpace: "pre-wrap" }}>{st.body.slice(0, 400)}{st.body.length > 400 ? "…" : ""}</p>
            </li>
          ))}
        </ol>
      )}
    </section>

    {check && (
      <section className={styles.card}>
        <h2>Проверка перед запуском</h2>
        {check.problems.map((p) => <p key={p} className={styles.errorText}>✕ {p}</p>)}
        {check.missing.length ? check.missing.map((m) => (
          <p key={m.variable} className={styles.errorText}>✕ У {m.leads} лидов пусто в <code>{`{{${m.variable}}}`}</code> — добавьте значение по умолчанию: <code>{`{{${m.variable}|…}}`}</code></p>
        )) : <p>✓ У всех лидов заполнены переменные (или есть значение по умолчанию)</p>}
        <p className={styles.muted}>Сегодня выбранные ящики могут отправить до <b>{check.daily_capacity}</b> холодных писем{check.days_estimate ? ` — первое письмо всем ${check.leads} лидам уйдёт примерно за ${check.days_estimate} дн.` : ""}. Первые 7 дней прогрева ящик холодные не шлёт.</p>
      </section>
    )}

    <section className={styles.card}>
      <h2>Предпросмотр и тест</h2>
      <div className={styles.actions}>
        <select value={lead} onChange={(e) => setLead(e.target.value)} aria-label="Лид для предпросмотра">
          {!leads.length && <option value="">Нет лидов</option>}
          {leads.map((l) => <option key={l.id} value={l.id}>{l.company} · {l.email}</option>)}
        </select>
        <button type="button" className={styles.secondary} disabled={!lead} onClick={showPreview}>Показать письма этому лиду</button>
      </div>
      {preview && preview.filter((p) => p.channel === "email").map((p, i) => (
        <article key={i} className={styles.msgOut} style={{ maxWidth: "100%", marginBottom: 10 }}>
          {p.subject && <strong>{p.subject}</strong>}
          {p.html ? <div className={styles.formatted} dangerouslySetInnerHTML={{ __html: p.html }} /> : <p>{p.body}</p>}
        </article>
      ))}
      <div className={styles.actions}>
        <input type="email" placeholder="Ваш адрес для теста" value={to} onChange={(e) => setTo(e.target.value)} />
        <button type="button" className={styles.secondary} disabled={!to.includes("@")} onClick={test}>Отправить тест себе</button>
      </div>
    </section>
  </>;
}

function SettingsTab({ c, onSaved }: { c: Campaign; onSaved: (c: Campaign) => void }) {
  const [boxes, setBoxes] = useState<Mailbox[]>([]);
  const [chosen, setChosen] = useState<number[]>(c.mailbox_ids);
  const [start, setStart] = useState(c.start_at ? c.start_at.slice(0, 16) : "");
  const [win, setWin] = useState(c.window);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [client, setClient] = useState<OutreachClient | null>(null);
  const [responsible, setResponsible] = useState(c.responsible_user_id ? String(c.responsible_user_id) : "");
  const [cc, setCc] = useState(c.handoff_cc || "");
  const [auto, setAuto] = useState(c.auto_handoff);
  useEffect(() => {
    const owner = c.client?.project_id || null;
    api<{ items: Mailbox[] }>(`${base}/mailboxes`).then((d) => setBoxes(d.items.filter((m) => m.role !== "helper"
      && (m.client?.project_id || null) === owner))).catch((e) => setError(message(e)));
    if (owner) api<{ items: OutreachClient[] }>(`${base}/clients`).then((d) => setClient(d.items.find((x) => x.project_id === owner) || null)).catch(() => {});
  }, [c.client?.project_id]);
  const handoffChanged = !!c.client && (responsible !== (c.responsible_user_id ? String(c.responsible_user_id) : "")
    || cc.trim().toLowerCase() !== (c.handoff_cc || "") || auto !== c.auto_handoff);
  const changed = useMemo(() => JSON.stringify([...chosen].sort()) !== JSON.stringify([...c.mailbox_ids].sort())
    || start !== (c.start_at ? c.start_at.slice(0, 16) : "") || JSON.stringify(win) !== JSON.stringify(c.window), [chosen, start, win, c]) || handoffChanged;

  async function save() {
    setError(""); setNotice("");
    try {
      if (win.start_hour >= win.end_hour) { setError("Час начала должен быть раньше часа окончания"); return; }
      if (!win.weekdays.length) { setError("Выберите хотя бы один день"); return; }
      const body: Record<string, unknown> = { mailbox_ids: chosen, window: win };
      if (c.client) {
        if (responsible) body.responsible_user_id = Number(responsible); else body.clear_responsible = true;
        body.handoff_cc = cc.trim();
        body.auto_handoff = auto;
      }
      if (start) body.start_at = new Date(start).toISOString(); else body.clear_start = true;
      onSaved(await send<Campaign>(`/campaigns/${c.id}`, body));
      setNotice("Сохранено");
    } catch (e) { setError(message(e)); }
  }

  return (
    <section className={styles.card}>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}
      {c.client && <>
        <h2>Передача лидов клиенту</h2>
        <p className={styles.muted}>Ответившие попадают в CRM «{clientLabel(c.client)}» — воронка «Аутрич», источник «Аутрич (email)», вся переписка в ленте сделки и задача «Связаться». Отказы, отписки и автоответы не передаём.</p>
        <div className={styles.fields}>
          <label className={styles.check}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            <span>Передавать сами «Интересно» и горячие ответы<small className={styles.hint}>Остальные — кнопкой «Передать клиенту» во «Входящих». Выключите — всё только кнопкой.</small></span>
          </label>
          <label className={styles.field}>Ответственный у клиента (задача «Связаться»)
            <select value={responsible} onChange={(e) => setResponsible(e.target.value)}>
              <option value="">Собственник клиента</option>
              {(client?.team || []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
            </select>
            <small>Горячим — срок 15 минут рабочего времени клиента, остальным — до конца его рабочего дня.</small>
          </label>
          <label className={styles.field}>Почта менеджера клиента для копии
            <input type="email" maxLength={254} placeholder="manager@client.ru" value={cc} onChange={(e) => setCc(e.target.value)} />
            <small>Подставится в копию, когда отвечаете лиду во «Входящих»: менеджер получит всю ветку и продолжит сам.</small>
          </label>
        </div>
      </>}
      <h2>Когда отправлять</h2>
      <p className={styles.muted}>Время — по часовому поясу <b>получателей</b>: для США выберите их пояс, и письма придут в их рабочие часы, а не ночью.</p>
      <div className={styles.fields}>
        <label className={styles.field}>Часовой пояс получателей
          <select value={win.tz} onChange={(e) => setWin({ ...win, tz: e.target.value })}>
            {!timezones.some(([z]) => z === win.tz) && <option value={win.tz}>{win.tz}</option>}
            {timezoneGroups.map(([group, zones]) => (
              <optgroup key={group} label={group}>{zones.map(([z, n]) => <option key={z} value={z}>{n}</option>)}</optgroup>
            ))}
          </select>
          <small>Сейчас там: {nowIn(win.tz)}</small>
        </label>
        <div className={styles.field}>Дни
          <div className={styles.actions} style={{ margin: 0 }}>
            {WEEKDAYS.map((d, i) => (
              <button key={d} type="button" className={styles.tab} aria-pressed={win.weekdays.includes(i)}
                onClick={() => setWin({ ...win, weekdays: win.weekdays.includes(i) ? win.weekdays.filter((x) => x !== i) : [...win.weekdays, i].sort() })}>{d}</button>
            ))}
          </div>
        </div>
        <div className={styles.inlineForm}>
          <label className={styles.field}>С часа
            <input type="number" min={0} max={23} value={win.start_hour} onChange={(e) => setWin({ ...win, start_hour: Number(e.target.value) })} />
          </label>
          <label className={styles.field}>До часа
            <input type="number" min={1} max={24} value={win.end_hour} onChange={(e) => setWin({ ...win, end_hour: Number(e.target.value) })} />
          </label>
          <label className={styles.field}>Пауза между письмами с ящика, мин
            <input type="number" min={0} max={240} value={win.gap_minutes} onChange={(e) => setWin({ ...win, gap_minutes: Number(e.target.value) })} />
          </label>
          <label className={styles.field}>Новых лидов в день
            <input type="number" min={0} max={2000} value={win.daily_new} onChange={(e) => setWin({ ...win, daily_new: Number(e.target.value) })} />
            <small>0 — без ограничения (лимиты ящиков всё равно действуют)</small>
          </label>
        </div>
      </div>
      <h2>С каких ящиков слать</h2>
      {!boxes.length && <p className={styles.muted}>{c.client ? "У клиента нет своих ящиков — в «Ящики и шаблоны писем» добавьте ящик на домене клиента и выберите его в поле «Клиент»." : "Нет ящиков агентства."}</p>}
      {boxes.map((m) => (
        <label key={m.id} className={styles.check}>
          <input type="checkbox" checked={chosen.includes(m.id)} onChange={(e) => setChosen(e.target.checked ? [...chosen, m.id] : chosen.filter((x) => x !== m.id))} />
          <span>{m.email}{!m.is_active && " (выключен)"}
            <small className={styles.hint}>холодных сегодня ещё {m.left_today}{m.cold_paused ? " · ⏸ на паузе" : ""}{m.health != null ? ` · здоровье ${m.health}` : ""}</small></span>
        </label>
      ))}
      <p className={styles.hint}>Продолжение переписки с лидом всегда идёт с того же ящика, что и первое письмо. Лимит ящика в нескольких кампаниях делится поровну. Подробнее о лимитах — в <Link href="/admin/outreach/warmup">«Прогреве»</Link>.</p>
      <h2>Когда начать</h2>
      <label className={styles.field}>Дата и время старта — по вашему времени (пусто — сразу после «Запустить»)
        <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />
        <small>Дальше письма уходят только в окно «Когда отправлять» выше — по времени получателей.</small>
      </label>
      <div className={styles.actions}><button type="button" className={styles.primary} disabled={!changed} onClick={save}>Сохранить</button></div>
    </section>
  );
}
