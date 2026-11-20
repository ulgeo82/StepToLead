"use client";

/* Рассылка компании: базы получателей — гипотезы, отправляет одна активная; настройки и аккаунты — общие.
   Сервер — /api/telegram-company (docs/TELEGRAM_COMPANY.md); импорт базы — /api/campaigns/{id}/import. */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import css from "./company.module.css";

type Hyp = { id: number; name: string; description: string | null; active: boolean; status: string; last_error: string | null;
  total: number; waiting: number; sent: number; replied: number; interested: number; refused: number; failed: number };
type Settings = Record<string, string | number | boolean>;
type Company = { settings: Settings; api_key_set: boolean; active_id: number | null; status: string; hypotheses: Hyp[]; accounts: number };
type ImportResult = { imported: number; skipped: number; errors: string[]; already_in_company: number; blocked: number };

const json = (url: string, body: unknown, method = "POST") =>
  api(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const pct = (a: number, b: number) => b ? `${Math.round(a / b * 100)}%` : "—";

export default function Sending({ base }: { base: string }) {
  const [data, setData] = useState<Company | null>(null);
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState(""); const [desc, setDesc] = useState("");
  const [draft, setDraft] = useState<Settings>({}); const [key, setKey] = useState("");
  const load = useCallback(async () => {
    try { const d = await api<Company>("/telegram-company"); setData(d); setDraft(d.settings); }
    catch (e) { setNotice({ text: e instanceof Error ? e.message : "Не удалось загрузить", error: true }); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function run(fn: () => Promise<unknown>, ok?: string) {
    setBusy(true); setNotice(null);
    try { await fn(); await load(); if (ok) setNotice({ text: ok }); }
    catch (e) { setNotice({ text: e instanceof Error ? e.message : "Не удалось", error: true }); }
    finally { setBusy(false); }
  }
  async function upload(id: number, file?: File) {
    if (!file) return;
    const form = new FormData(); form.append("file", file);
    await run(async () => {
      const r = await api<ImportResult>(`/campaigns/${id}/import`, { method: "POST", body: form });
      setTimeout(() => setNotice({ text: `Добавлено ${r.imported}. Уже есть в других гипотезах компании: ${r.already_in_company}. ` +
        `В стоп-листе: ${r.blocked}. Пропущено (дубли, пустые строки): ${r.skipped}.` + (r.errors.length ? ` ${r.errors.slice(0, 3).join("; ")}` : "") }), 0);
    });
  }
  if (!data) return <section className={css.launch}>{notice ? <p className="notice error">{notice.text}</p> : <p className="mutedText">Загружаем…</p>}</section>;
  const running = data.status === "running";
  const active = data.hypotheses.find(h => h.active);
  const field = (k: string, label: string, type = "text", hint?: string) => <label key={k}>{label}
    <input type={type} value={String(draft[k] ?? "")} onChange={e => setDraft(d => ({ ...d, [k]: type === "number" ? Number(e.target.value) : e.target.value }))}/>
    {hint && <small>{hint}</small>}</label>;
  const manual = draft.auto_reply === false;

  return <section className={css.sending}>
    {notice && <div className={`notice ${notice.error ? "error" : ""}`} role="status">{notice.text}<button onClick={() => setNotice(null)} aria-label="Закрыть">×</button></div>}

    <div className={css.engine} data-running={running}>
      <div><strong>{running ? "Рассылка идёт" : "Рассылка на паузе"}</strong>
        <span>{active ? `Первые сообщения — из гипотезы «${active.name}», ждут отправки ${active.waiting}.` : "Отметьте гипотезу, по которой пойдут первые сообщения."}
          {" "}Аккаунтов в работе: {data.accounts}. Лимит касаний — на вкладке <Link href={`${base}/accounts`}>Аккаунты</Link>.</span></div>
      {running ? <button className="button secondary" disabled={busy} onClick={() => void run(() => json("/telegram-company/pause", {}), "Рассылка на паузе")}>Пауза</button>
        : <button className="button primary" disabled={busy || !active} onClick={() => void run(() => json("/telegram-company/start", {}), "Рассылка запущена")}>Запустить рассылку</button>}
    </div>

    <h2 className={css.h2}>Гипотезы</h2>
    <p className={css.lead}>Каждая база — отдельная гипотеза. Первые сообщения отправляет одна, отмеченная. В остальных
      дочитываются и обрабатываются ответы. Одному человеку компания пишет один раз: дубли из других гипотез при загрузке пропускаются.</p>
    <div className="tablePanel"><div className="tableWrap"><table>
      <thead><tr><th>Рассылка</th><th>Гипотеза</th><th>В базе</th><th>Ждут</th><th>Отправлено</th><th>Ответили</th><th>Интерес</th><th>Отказ</th><th>Не дошло</th><th>База</th></tr></thead>
      <tbody>{data.hypotheses.map(h => <tr key={h.id}>
        <td><input type="radio" name="active" checked={h.active} disabled={busy} aria-label={`Рассылка по «${h.name}»`}
          onChange={() => void run(() => json(`/telegram-company/hypotheses/${h.id}/activate`, {}), `Первые сообщения теперь из «${h.name}»`)}/></td>
        <td><strong>{h.name}</strong>{h.description && <small style={{ display: "block" }}>{h.description}</small>}
          {h.last_error && <small style={{ display: "block", color: "var(--alert)" }}>{h.last_error}</small>}</td>
        <td>{h.total}</td><td>{h.waiting}</td><td>{h.sent}</td>
        <td>{h.replied}<small style={{ display: "block" }}>{pct(h.replied, h.sent)}</small></td>
        <td>{h.interested}</td><td>{h.refused}</td><td>{h.failed}</td>
        <td><label className="button secondary" style={{ cursor: "pointer" }}>Загрузить
          <input type="file" accept=".csv,.xlsx" hidden onChange={e => { void upload(h.id, e.target.files?.[0]); e.target.value = ""; }}/></label>
          <Link href={`/admin/campaigns/${h.id}`} style={{ marginLeft: 8, color: "var(--muted)" }}>Диалоги</Link></td>
      </tr>)}</tbody></table>
      {!data.hypotheses.length && <p style={{ padding: 16 }}>Гипотез пока нет — создайте первую ниже.</p>}</div>
      {active && <div style={{ padding: 12 }}><button className="button secondary" disabled={busy}
        onClick={() => void run(() => json("/telegram-company/deactivate", {}), "Первые сообщения остановлены, ответы продолжают обрабатываться")}>Не отправлять первые сообщения</button></div>}
    </div>
    <form className={css.newHyp} onSubmit={e => { e.preventDefault(); if (name.trim()) void run(async () => { await json("/telegram-company/hypotheses", { name, description: desc || null }); setName(""); setDesc(""); }, "Гипотеза создана — загрузите в неё базу"); }}>
      <input placeholder="Название гипотезы, например «Агентства из чатов Авито»" value={name} onChange={e => setName(e.target.value)}/>
      <input placeholder="Сегмент и оффер — коротко, для себя" value={desc} onChange={e => setDesc(e.target.value)}/>
      <button className="button secondary" disabled={busy || !name.trim()}>Новая гипотеза</button>
    </form>
    <p className={css.lead}>Файл базы: колонки <b>username</b> и <b>first_message</b> (Excel или CSV), первое сообщение — своё у каждого получателя.</p>

    <h2 className={css.h2}>Настройки рассылки компании</h2>
    <div className={css.settingsGrid}>
      <label>Ответы лидам<select value={manual ? "manual" : "ai"} onChange={e => setDraft(d => ({ ...d, auto_reply: e.target.value === "ai" }))}>
        <option value="manual">Вручную — операторы во «Входящих»</option><option value="ai">ИИ отвечает сам</option></select>
        <small>{manual ? "ИИ не нужен." : "Ответили вручную — ИИ этот диалог больше не трогает."}</small></label>
      {field("sleep_periods", "Не писать в эти часы", "text", "Например 21:00-09:00. По часовому поясу ниже.")}
      {field("timezone_offset", "Часовой пояс, UTC±", "number")}
      {field("restriction_hours", "Пауза после ограничения Telegram, часов", "number")}
      {field("action_min", "Пауза между сообщениями аккаунта, от (сек)", "number")}
      {field("action_max", "до (сек)", "number")}
      {!manual && <>{field("ai_url", "Адрес API ИИ", "url")}{field("ai_model", "Модель")}
        <label>Ключ API<input type="password" autoComplete="new-password" value={key} onChange={e => setKey(e.target.value)}
          placeholder={data.api_key_set ? "Ключ сохранён — оставьте пустым" : "Ключ"}/></label>
        <label className={css.wide}>Промпт ИИ<textarea rows={6} value={String(draft.system_prompt ?? "")} onChange={e => setDraft(d => ({ ...d, system_prompt: e.target.value }))}/></label></>}
      {field("blacklist", "Не писать этим username (через запятую)")}
    </div>
    <button className="button primary" disabled={busy} onClick={() => void run(async () => { await json("/telegram-company/settings", { settings: draft, api_key: key || null }, "PUT"); setKey(""); }, "Настройки сохранены для всех гипотез компании")}>Сохранить настройки</button>
  </section>;
}
