"use client";

/* Прогрев Telegram-аккаунтов выбранного клиента (docs/TELEGRAM_WARMUP.md): этапы, активность сегодня,
   рекомендуемый лимит холодных касаний (сам лимит — на странице «Аккаунты»), журнал, настройки компании. */

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import css from "../telegram-infra.module.css";

type Row = {
  id: number; phone: string; username: string | null; name: string; avatar_thumb: string | null; status: string;
  proxy_id: number | null; daily_first_limit: number | null; enabled: boolean; paused: boolean; stage: string | null;
  stage_name: string; day: number | null; warmed: boolean; recommended_limit: number | null;
  today: { sessions?: number; channels?: number; messages?: number; actions?: number };
  next_at: string | null; last: string | null; prep: Record<string, string>; has_twofa: boolean;
};
type Settings = { intensity: "soft" | "normal"; channels: string[]; pool: boolean };
type Event = { id: number; account_id: number; at: string | null; kind: string; text: string };

const post = (url: string, body: unknown, method = "POST") =>
  api(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const when = (v: string | null) => v ? new Date(v).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—";
const stages = ["prep", "rest", "passive", "social", "ready"];

export default function TgWarmupPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [channels, setChannels] = useState("");
  const [events, setEvents] = useState<Event[]>([]);
  const [checked, setChecked] = useState<number[]>([]);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [r, s, e] = await Promise.all([api<Row[]>("/telegram-warmup/accounts"), api<Settings>("/telegram-warmup/settings"),
        api<Event[]>("/telegram-warmup/events?limit=60")]);
      setRows(r); setSettings(s); setChannels(s.channels.join("\n")); setEvents(e);
    } catch (e) { setNotice(e instanceof Error ? e.message : "Не удалось загрузить"); }
  }, []);
  useEffect(() => { void load(); const t = window.setInterval(() => void load(), 30_000); return () => window.clearInterval(t); }, [load]);

  async function act(action: string) {
    if (!checked.length) return;
    if (action === "reset" && !window.confirm("Начать прогрев отмеченных заново (день 0)?")) return;
    setBusy(true);
    try { await post(`/telegram-warmup/${action}`, { ids: checked }); setChecked([]); await load(); }
    catch (e) { setNotice(e instanceof Error ? e.message : "Не удалось"); }
    finally { setBusy(false); }
  }
  async function saveSettings(patch: Partial<Settings>) {
    try { const s = await api<Settings>("/telegram-warmup/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch) }); setSettings(s); setChannels(s.channels.join("\n")); setNotice("Настройки прогрева сохранены"); }
    catch (e) { setNotice(e instanceof Error ? e.message : "Не удалось сохранить"); }
  }
  async function showPassword(id: number) {
    try { const r = await api<{ password: string }>(`/telegram-warmup/accounts/${id}/twofa`);
      window.prompt("Облачный пароль аккаунта (скопируйте и храните в надёжном месте):", r.password); }
    catch (e) { setNotice(e instanceof Error ? e.message : "Пароля нет"); }
  }

  const byStage = stages.map(s => [s, rows.filter(r => r.enabled && r.stage === s).length] as const);
  const names = Object.fromEntries(rows.map(r => [r.id, r.name || r.phone]));

  return <div className={`page ${css.screen}`}>
    <header className="pageHeader"><div><p className="eyebrow">Telegram-аутрич</p><h1>Прогрев аккаунтов</h1>
      <p className="subtitle">Подготовка (скрыть номер, облачный пароль, завершить чужие сессии), ежедневная живая
        активность днём — каналы, просмотры, реакции, переписка со своими аккаунтами — и проверка @SpamBot раз в сутки.
        Лимит холодных касаний задаёте вы на странице «Аккаунты»; здесь — рекомендация по возрасту аккаунта.</p></div></header>
    {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice("")} aria-label="Закрыть">×</button></div>}
    <div className={css.stats}>{byStage.map(([s, n]) => <div key={s}><small>{rows.find(r => r.stage === s)?.stage_name ?? {
      prep: "Подготовка", rest: "Отлёжка", passive: "Каналы и чтение", social: "Общение", ready: "Готов" }[s]}</small><strong>{n}</strong></div>)}
      <div><small>Не в прогреве</small><strong>{rows.filter(r => !r.enabled).length}</strong></div></div>
    <div className={css.toolbar} aria-label="Действия с отмеченными">
      <span>Отмечено: {checked.length}</span>
      <button className="button primary" disabled={busy || !checked.length} onClick={() => void act("start")}>Запустить прогрев</button>
      <button className="button secondary" disabled={busy || !checked.length} onClick={() => void act("pause")}>Пауза</button>
      <button className="button secondary" disabled={busy || !checked.length} onClick={() => void act("resume")}>Продолжить</button>
      <button className="button secondary" disabled={busy || !checked.length} onClick={() => void act("warmed")}
        title="Купленный прогретый аккаунт: подготовка + сразу этап «Готов»">Уже прогрет</button>
      <button className="button secondary" disabled={busy || !checked.length} onClick={() => void act("reset")}>Начать заново</button>
    </div>
    <section className={`tablePanel ${css.panel}`}><div className={css.scroll}>
      <table><thead><tr>
        <th><input type="checkbox" aria-label="Отметить все" checked={!!rows.length && checked.length === rows.length}
          onChange={e => setChecked(e.target.checked ? rows.map(r => r.id) : [])}/></th>
        <th>Аккаунт</th><th>Этап</th><th>День</th><th>Сегодня</th><th>Реком. холодных/день</th><th>Подготовка</th>
        <th>Последнее</th><th>Следующая сессия</th></tr></thead>
      <tbody>{rows.map(r => <tr key={r.id}>
        <td><input type="checkbox" checked={checked.includes(r.id)} aria-label={`Отметить ${r.phone}`}
          onChange={() => setChecked(c => c.includes(r.id) ? c.filter(x => x !== r.id) : [...c, r.id])}/></td>
        <td className={css.who}>{r.avatar_thumb ? <img className={css.avatar} src={r.avatar_thumb} alt=""/>
          : <span className={css.avatar} aria-hidden="true">{(r.name || "?").slice(0, 1).toUpperCase()}</span>}
          <span>{r.name || r.phone}<small>{r.username ? `@${r.username} · ` : ""}{r.phone}{!r.proxy_id && " · нет прокси"}</small></span></td>
        <td>{r.enabled ? `${r.stage_name}${r.paused ? " · пауза" : ""}${r.warmed ? " · куплен прогретым" : ""}` : "Выключен"}</td>
        <td>{r.day ?? "—"}</td>
        <td>{r.enabled ? `сессий ${r.today.sessions ?? 0} · каналы ${r.today.channels ?? 0} · сообщений ${r.today.messages ?? 0}` : "—"}</td>
        <td>{r.recommended_limit ?? "—"}<small>ваш лимит: {r.daily_first_limit ?? "как у компании"}</small></td>
        <td><small>{r.prep.privacy ? "✓ номер скрыт" : "· номер"}</small><small>{r.prep.twofa ? "✓ облачный пароль" : "· пароль"}
          {r.has_twofa && <button className="button secondary" style={{ marginLeft: 6 }} onClick={() => void showPassword(r.id)}>показать</button>}</small>
          <small>{r.prep.sessions ? "✓ чужие сессии завершены" : r.prep.sessions_retry ? "· сессии — через сутки" : "· сессии"}</small></td>
        <td style={{ whiteSpace: "normal", minWidth: 220 }}>{r.last ?? "—"}</td>
        <td>{r.enabled && !r.paused ? when(r.next_at) : "—"}</td>
      </tr>)}</tbody></table>
      {!rows.length && <p>Аккаунтов нет. Добавьте их на странице «Аккаунты».</p>}
    </div></section>
    {settings && <section className="tablePanel" style={{ marginTop: 16 }}>
      <div className="tableTitle"><h2>Настройки прогрева компании</h2></div>
      <div className={css.toolbar}>
        <label>Интенсивность <select value={settings.intensity} onChange={e => void saveSettings({ intensity: e.target.value as Settings["intensity"] })}>
          <option value="normal">Обычная</option><option value="soft">Мягкая (меньше действий)</option></select></label>
        <label><input type="checkbox" checked={settings.pool} onChange={e => void saveSettings({ pool: e.target.checked })}/>
          Переписка со своими аккаунтами из общего пула агентства</label>
      </div>
      <label>Каналы для чтения (по одному: @name или ссылка t.me/name)
        <textarea rows={5} value={channels} onChange={e => setChannels(e.target.value)} style={{ width: "100%" }}/></label>
      <button className="button secondary" onClick={() => void saveSettings({ channels: channels.split(/\s+/).filter(Boolean) })}>Сохранить каналы</button>
    </section>}
    <section className="tablePanel" style={{ marginTop: 16 }}><div className="tableTitle"><h2>Журнал</h2></div>
      {events.length ? <ul>{events.map(e => <li key={e.id}><small>{when(e.at)}</small> {names[e.account_id] ?? `#${e.account_id}`}: {e.text}</li>)}</ul>
        : <p className="mutedText">Пока пусто.</p>}</section>
  </div>;
}
