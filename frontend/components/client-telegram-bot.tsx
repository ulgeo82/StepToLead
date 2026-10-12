"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type BotUser = { id: number; name: string; status: "company_bot" | "agency_bot" | "none" };
type Bot = { connected: boolean; username?: string; title?: string; active?: boolean; last_error?: string | null; url?: string | null; users: BotUser[]; invited?: number };
const userStatus: Record<BotUser["status"], string> = { company_bot: "в боте компании", agency_bot: "пока в боте агентства", none: "Telegram не подключён" };

export const BOT_STEPS = "В Telegram откройте @BotFather → /newbot → придумайте название (например «Кухни Север — заявки») и адрес, оканчивающийся на bot. BotFather пришлёт токен — вставьте его сюда.";

// Свой бот компании: уведомления о заявках, SLA, еженедельный отчёт и меню CRM приходят от бота с её названием.
export function ClientTelegramBot({ workspaceId }: { workspaceId: number }) {
  const [data, setData] = useState<Bot | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const load = useCallback(() => api<Bot>(`/marketing/workspaces/${workspaceId}/telegram-bot`).then(setData).catch(e => setError((e as Error).message)), [workspaceId]);
  useEffect(() => { void load(); }, [load]);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(""); setNotice("");
    const token = String(new FormData(event.currentTarget).get("token") || "");
    try {
      const next = await api<Bot>(`/marketing/workspaces/${workspaceId}/telegram-bot`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
      setData(next); setEditing(false);
      setNotice(`Бот @${next.username} подключён.${next.invited ? ` ${next.invited} сотрудник(ам) отправлена ссылка на переход в него.` : ""} Новые сотрудники подключаются в «Настройки → Мой Telegram».`);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function remove() {
    if (!window.confirm("Отключить бот компании? Сотрудникам, подключённым к нему, нужно будет подключить Telegram заново.")) return;
    setBusy(true); setError(""); setNotice("");
    try { setData(await api<Bot>(`/marketing/workspaces/${workspaceId}/telegram-bot`, { method: "DELETE" })); setNotice("Бот отключён. Уведомления снова идут через бот агентства."); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <section className="tablePanel clientProxyPanel">
    <div className="clientProxyHead"><div><h2>Telegram-бот компании</h2>
      <p>Уведомления о заявках, задачи, отчёт по понедельникам и меню CRM будут приходить от бота с названием компании, а не от бота агентства.</p></div></div>
    {error && <p role="alert" className="clientProxyError">{error}</p>}
    {notice && <p role="status" className="clientProxyNotice">{notice}</p>}
    {data?.connected && !editing ? <div className="clientProxyRow">
      <div className="clientProxyMain"><strong>{data.title || data.username} · <a href={data.url || "#"} target="_blank" rel="noopener noreferrer">@{data.username}</a></strong>
        <span className={`clientProxyStatus ${data.last_error ? "error" : "working"}`}>{data.last_error ? "ошибка" : "работает"}</span>
        {data.last_error && <small className="clientProxyErrorText">{data.last_error}</small>}
        <small>{data.users.length ? data.users.map(u => `${u.name} — ${userStatus[u.status]}`).join(" · ") : "Сотрудников пока нет"}</small></div>
      <div className="clientProxyActions"><button className="button secondary" disabled={busy} onClick={() => setEditing(true)}>Заменить токен</button>
        <button className="button secondary" disabled={busy} onClick={() => void remove()}>Отключить</button></div>
    </div> : <form className="clientProxyForm" onSubmit={save}>
      <p className="clientProxyEmpty">{BOT_STEPS}</p>
      <input name="token" required placeholder="1234567890:AAH…" autoComplete="off" />
      <button className="button primary" disabled={busy}>{busy ? "Проверяем…" : "Подключить бота"}</button>
      {editing && <button type="button" className="button secondary" onClick={() => setEditing(false)}>Отмена</button>}
    </form>}
  </section>;
}
