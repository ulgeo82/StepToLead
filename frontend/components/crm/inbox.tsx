"use client";

import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { request, Team, when } from "./shared";

export type Conversation = { id: number; channel_id: number; channel_kind: string | null; channel_name: string | null; title: string;
  phone: string | null; contact_id: number | null; contact_name: string | null; deal_id: number | null; deal_name: string | null;
  inbound_id: number | null; assigned_user_id: number | null; assigned_name: string | null; status: string; unread_count: number;
  last_message_at: string | null; last_message_preview: string | null; last_direction: string | null; waiting_since: string | null;
  meta: Record<string, unknown> };
type ChatMessage = { id: number; direction: string; text: string | null; author_name: string | null; status: string;
  error: string | null; is_ai: boolean; sent_at: string };
type Detail = Conversation & { messages: ChatMessage[] };
type Channel = { id: number; kind: string; kind_name: string; name: string; active: boolean; status: string; last_error: string | null;
  bot_username?: string | null; instance_id?: string | null; last_polled_at: string | null; unread: number };
type Template = { id: number; title: string; text: string };

export const channelBadge: Record<string, { label: string; cls: string }> = {
  avito: { label: "А", cls: "avito" }, telegram_bot: { label: "TG", cls: "telegram" }, whatsapp: { label: "WA", cls: "whatsapp" } };

const channelNames: Record<string, string> = { avito: "Авито", telegram_bot: "Telegram", whatsapp: "WhatsApp" };
const ago = (value: string | null) => {
  if (!value) return "";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000));
  return minutes < 60 ? `${minutes} мин` : minutes < 1440 ? `${Math.round(minutes / 60)} ч` : `${Math.round(minutes / 1440)} дн`;
};
const shortTime = (value: string | null) => {
  if (!value) return "";
  const d = new Date(value);
  return d.toDateString() === new Date().toDateString() ? d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
};

/** Message thread with composer and quick replies — used in the inbox and inside the deal card. */
export function ChatThread({ conversationId, projectId, compact, canWrite, onChanged }: { conversationId: number; projectId: number;
  compact?: boolean; canWrite: boolean; onChanged?: () => void }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [templates, setTemplates] = useState<Template[]>([]);
  const [showTemplates, setShowTemplates] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const load = useCallback(() => request<Detail>(`/crm/conversations/${conversationId}`, "GET").then(setDetail).catch(e => setError((e as Error).message)), [conversationId]);
  useEffect(() => { setDetail(null); setText(""); setError(""); load(); const timer = setInterval(load, 6000); return () => clearInterval(timer); }, [load]);
  useEffect(() => { request<Template[]>(`/crm/projects/${projectId}/reply-templates`, "GET").then(setTemplates).catch(() => setTemplates([])); }, [projectId]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [detail?.messages.length]);
  async function send(event?: FormEvent) {
    event?.preventDefault(); if (!text.trim() || busy) return;
    setBusy(true); setError("");
    try { await request(`/crm/conversations/${conversationId}/messages`, "POST", { text }); setText(""); await load(); onChanged?.(); }
    catch (e) { setError((e as Error).message); await load(); } finally { setBusy(false); }
  }
  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(); }
  }
  async function saveTemplate() {
    const title = window.prompt("Название шаблона", text.slice(0, 40)); if (!title) return;
    const row = await request<Template>(`/crm/projects/${projectId}/reply-templates`, "POST", { title, text });
    setTemplates(list => [...list, row]);
  }
  const limit = detail?.channel_kind === "avito" ? 1000 : 4096;
  return <div className={`chatThread ${compact ? "compact" : ""}`}>
    <div className="chatMessages">{!detail ? <p className="crmMuted">Загружаем…</p> : detail.messages.map((m, i) => {
      const day = new Date(m.sent_at).toDateString(); const prev = detail.messages[i - 1];
      return <div key={m.id}>{(!prev || new Date(prev.sent_at).toDateString() !== day) && <div className="chatDay">{new Date(m.sent_at).toLocaleDateString("ru-RU", { day: "numeric", month: "long" })}</div>}
        <div className={`chatBubble ${m.direction} ${m.status === "failed" ? "failed" : ""}`}>
          {m.direction === "out" && m.author_name && <small className="chatAuthor">{m.is_ai ? "🤖 " : ""}{m.author_name}</small>}
          <p>{m.text}</p><time>{new Date(m.sent_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}{m.status === "failed" ? " · не доставлено" : ""}</time>
          {m.error && <small className="chatError">{m.error}</small>}</div></div>; })}
      {detail && !detail.messages.length && <p className="crmMuted">Сообщений пока нет</p>}<div ref={bottom}/></div>
    {canWrite && <form className="chatComposer" onSubmit={send}>
      {error && <p className="crmFormError">{error}</p>}
      {showTemplates && <div className="chatTemplates">{templates.map(t => <button type="button" key={t.id} onClick={() => { setText(t.text); setShowTemplates(false); }} title={t.text}><b>{t.title}</b><span>{t.text}</span></button>)}
        {!templates.length && <p className="crmMuted">Шаблонов пока нет: напишите ответ и нажмите «В шаблоны».</p>}</div>}
      <textarea value={text} onChange={e => setText(e.target.value)} onKeyDown={onKey} maxLength={limit} placeholder={`Ответ клиенту${detail?.channel_kind ? ` в ${channelNames[detail.channel_kind] || "чат"}` : ""} · Ctrl+Enter — отправить`}/>
      <div className="chatComposerBar"><button type="button" className="crmLinkButton" onClick={() => setShowTemplates(v => !v)}>⚡ Шаблоны</button>
        {text.trim().length > 5 && <button type="button" className="crmLinkButton" onClick={saveTemplate}>В шаблоны</button>}
        <span className="crmMuted">{text.length}/{limit}</span><button className="crmPrimary" disabled={busy || !text.trim()}>{busy ? "Отправляем…" : "Отправить"}</button></div></form>}
  </div>;
}

function Channels({ projectId, onClose, onChanged }: { projectId: number; onClose: () => void; onChanged: () => void }) {
  const [data, setData] = useState<{ channels: Channel[]; can_manage: boolean; avito_cabinets: { id: number; name: string; status: string }[] } | null>(null);
  const [kind, setKind] = useState("telegram_bot");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => request<typeof data>(`/crm/projects/${projectId}/channels`, "GET").then(setData).catch(e => setError(e.message)), [projectId]);
  useEffect(() => { load(); }, [load]);
  async function run(action: () => Promise<unknown>, message: string) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); setNotice(message); await load(); onChanged(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget); const form = event.currentTarget;
    await run(async () => { const row = await request<Channel & { details?: unknown }>(`/crm/projects/${projectId}/channels`, "POST", {
      kind, name: f.get("name"), token: f.get("token") || null, instance_id: f.get("instance_id") || null,
      api_url: f.get("api_url") || null, connection_id: f.get("connection_id") ? Number(f.get("connection_id")) : null });
      if (row.status === "error") throw new Error(`Канал сохранён, но не работает: ${row.last_error}`); form.reset(); }, "Канал подключён. Новые сообщения появятся в «Чатах» в течение минуты.");
  }
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><div className="resultModal crmModal chatChannels">
    <header><h2>Каналы переписки</h2><button type="button" onClick={onClose}>×</button></header>
    <p className="crmModalLead">Все переписки с клиентами в одном окне. Первое сообщение от нового человека автоматически становится заявкой в «Неразобранном».</p>
    {error && <p className="crmFormError">{error}</p>}{notice && <p className="crmOk">{notice}</p>}
    <div className="chatChannelList">{data?.channels.map(c => <div key={c.id} className={c.active ? "" : "off"}>
      <span className={`chatBadge ${channelBadge[c.kind]?.cls}`}>{channelBadge[c.kind]?.label}</span>
      <div><b>{c.name}</b><small>{c.kind_name}{c.bot_username ? ` · @${c.bot_username}` : ""} · {c.status === "connected" ? "работает" : c.status === "error" ? "ошибка" : "не проверен"}{c.last_polled_at ? ` · проверка ${when(c.last_polled_at)}` : ""}</small>
        {c.last_error && <small className="chatError">{c.last_error}</small>}</div>
      {data.can_manage && <div className="chatChannelActions"><button disabled={busy} onClick={() => run(() => request(`/crm/channels/${c.id}/sync`, "POST", {}), "Проверено")}>Проверить</button>
        <button disabled={busy} onClick={() => run(() => request(`/crm/channels/${c.id}`, "PATCH", { active: !c.active }), c.active ? "Канал выключен" : "Канал включён")}>{c.active ? "Выключить" : "Включить"}</button></div>}</div>)}
      {data && !data.channels.length && <p className="crmMuted">Каналы ещё не подключены.</p>}</div>
    {data?.can_manage && <form className="chatAddChannel" onSubmit={add}><h3 className="crmSub">Подключить канал</h3>
      <div className="crmChips">{[["telegram_bot", "Telegram-бот"], ["whatsapp", "WhatsApp"], ["avito", "Чаты Авито"]].map(([k, l]) => <button type="button" key={k} className={kind === k ? "active" : ""} onClick={() => setKind(k)}>{l}</button>)}</div>
      <label>Название<input name="name" required minLength={2} placeholder={kind === "telegram_bot" ? "Бот компании" : kind === "whatsapp" ? "WhatsApp отдела продаж" : "Авито — кухни"}/></label>
      {kind === "telegram_bot" && <><p className="crmModalLead">Создайте бота в @BotFather (/newbot) и вставьте токен. Ссылку на бота (t.me/…) разместите на сайте, в объявлениях и в подписи. Это должен быть отдельный бот, не бот уведомлений.</p>
        <label>Токен бота<input name="token" required autoComplete="off" placeholder="123456789:AA…"/></label></>}
      {kind === "whatsapp" && <><p className="crmModalLead">WhatsApp подключается через сервис GREEN-API (green-api.com): создайте инстанс, отсканируйте QR-код телефоном с номером отдела продаж и скопируйте idInstance, apiTokenInstance и apiUrl.</p>
        <div className="resultModalGrid"><label>idInstance<input name="instance_id" required inputMode="numeric" pattern="[0-9]+"/></label><label>apiUrl<input name="api_url" placeholder="https://api.green-api.com"/></label></div>
        <label>apiTokenInstance<input name="token" required autoComplete="off"/></label></>}
      {kind === "avito" && <><p className="crmModalLead">Используются ключи кабинета «Авито · Объявления» из раздела «Реклама». Ответы уходят прямо в чат Авито (до 1000 символов).</p>
        <label>Кабинет<select name="connection_id" required>{(data.avito_cabinets || []).map(c => <option key={c.id} value={c.id}>{c.name}{c.status !== "connected" ? " (не проверен)" : ""}</option>)}</select></label>
        {!data.avito_cabinets.length && <p className="crmWarnLine">Сначала подключите «Авито · Объявления» в разделе «Реклама».</p>}</>}
      <button className="resultPrimary" disabled={busy}>{busy ? "Проверяем…" : "Подключить"}</button></form>}
  </div></div>;
}

export function Inbox({ projectId, team, can, onOpenDeal, onOpenInbound }: { projectId: number; team: Team[];
  can: (name: string) => boolean; onOpenDeal: (id: number) => void; onOpenInbound: () => void }) {
  const [items, setItems] = useState<Conversation[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState("all");
  const [status, setStatus] = useState("open");
  const [search, setSearch] = useState("");
  const [active, setActive] = useState<number | null>(null);
  const [channels, setChannels] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    const q = new URLSearchParams({ filter, status }); if (search.trim()) q.set("search", search.trim());
    return request<{ items: Conversation[]; counts: Record<string, number> }>(`/crm/projects/${projectId}/conversations?${q}`, "GET")
      .then(v => { setItems(v.items); setCounts(v.counts); }).catch(e => setError((e as Error).message));
  }, [projectId, filter, status, search]);
  useEffect(() => { const t = setTimeout(load, 150); const i = setInterval(load, 10000); return () => { clearTimeout(t); clearInterval(i); }; }, [load]);
  const current = items.find(c => c.id === active);
  async function patch(body: Record<string, unknown>) {
    if (!active) return; try { await request(`/crm/conversations/${active}`, "PATCH", body); await load(); } catch (e) { setError((e as Error).message); }
  }
  const filters: [string, string, number | undefined][] = [["all", "Все", undefined], ["waiting", "Ждут ответа", counts.waiting], ["unread", "Непрочитанные", counts.unread],
    ["unassigned", "Без ответственного", counts.unassigned], ["mine", "Мои", undefined]];
  return <section className="chatInbox">
    <aside className="chatList"><div className="chatListHead"><input placeholder="Поиск: имя, телефон, текст" value={search} onChange={e => setSearch(e.target.value)}/>
      <button className="crmGhost" onClick={() => setChannels(true)} title="Каналы переписки">⚙ Каналы</button></div>
      <div className="chatFilters">{filters.map(([key, label, n]) => <button key={key} className={filter === key ? "active" : ""} onClick={() => setFilter(key)}>{label}{n ? <b>{n}</b> : null}</button>)}
        <button className={status === "closed" ? "active" : ""} onClick={() => setStatus(status === "open" ? "closed" : "open")}>{status === "open" ? "Закрытые" : "← Открытые"}</button></div>
      {error && <p className="crmFormError">{error}</p>}
      <div className="chatItems">{items.map(c => { const badge = channelBadge[c.channel_kind || ""]; const late = c.waiting_since && Date.now() - new Date(c.waiting_since).getTime() > 15 * 60000;
        return <button key={c.id} className={`chatItem ${active === c.id ? "active" : ""} ${c.unread_count ? "unread" : ""}`} onClick={() => setActive(c.id)}>
          <span className={`chatBadge ${badge?.cls || ""}`}>{badge?.label || "?"}</span>
          <span className="chatItemBody"><span className="chatItemTop"><b>{c.contact_name || c.title}</b><time>{shortTime(c.last_message_at)}</time></span>
            <span className="chatPreview">{c.last_direction === "out" ? "Вы: " : ""}{c.last_message_preview}</span>
            <span className="chatItemMeta">{c.deal_name ? <i>сделка: {c.deal_name}</i> : c.inbound_id ? <i className="new">новая заявка</i> : null}
              {c.waiting_since && <i className={late ? "late" : "wait"}>ждёт {ago(c.waiting_since)}</i>}{c.assigned_name && <i>{c.assigned_name}</i>}</span></span>
          {c.unread_count > 0 && <b className="chatUnread">{c.unread_count}</b>}</button>; })}
        {!items.length && <div className="chatEmptyList"><p>Диалогов нет.</p><button className="crmPrimary" onClick={() => setChannels(true)}>Подключить каналы</button></div>}</div></aside>
    <div className="chatPane">{current ? <>
      <header className="chatPaneHead"><div><h3>{current.contact_name || current.title}</h3>
        <small>{current.channel_name} · {current.phone || (current.meta.username ? `@${String(current.meta.username)}` : "")}{current.meta.item_title ? ` · ${String(current.meta.item_title)}` : ""}</small></div>
        <div className="chatPaneActions">{current.deal_id ? <button className="crmGhost" onClick={() => onOpenDeal(current.deal_id!)}>Сделка: {current.deal_name} →</button>
          : current.inbound_id ? <button className="crmGhost" onClick={onOpenInbound}>Заявка в «Неразобранном» →</button> : null}
          {can("edit_deal") && <select value={current.assigned_user_id || ""} onChange={e => patch({ assigned_user_id: Number(e.target.value) || null })}><option value="">Без ответственного</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select>}
          {can("edit_deal") && <button className="crmGhost" onClick={() => patch({ status: current.status === "open" ? "closed" : "open" })}>{current.status === "open" ? "✓ Закрыть диалог" : "Открыть снова"}</button>}</div></header>
      <ChatThread conversationId={current.id} projectId={projectId} canWrite={can("edit_deal")} onChanged={load}/></>
      : <div className="chatPlaceholder"><b>Выберите диалог</b><p>Сообщения из Авито, Telegram и WhatsApp приходят сюда. Ответ уходит клиенту в тот же мессенджер, а в сделке остаётся история.</p></div>}</div>
    {channels && <Channels projectId={projectId} onClose={() => setChannels(false)} onChanged={load}/>}
  </section>;
}

/** Conversations of a deal (and its contact) inside the deal card, with a way to start WhatsApp. */
export function DealChats({ dealId, projectId, canWrite, phone }: { dealId: number; projectId: number; canWrite: boolean; phone: string | null }) {
  const [items, setItems] = useState<Conversation[] | null>(null);
  const [active, setActive] = useState<number | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [error, setError] = useState("");
  const load = useCallback(() => request<Conversation[]>(`/crm/deals/${dealId}/conversations`, "GET").then(rows => {
    setItems(rows); setActive(current => current && rows.some(r => r.id === current) ? current : rows[0]?.id ?? null); }).catch(e => setError((e as Error).message)), [dealId]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { request<{ channels: Channel[] }>(`/crm/projects/${projectId}/channels`, "GET").then(v => setChannels(v.channels.filter(c => c.kind === "whatsapp" && c.active))).catch(() => setChannels([])); }, [projectId]);
  async function startWhatsapp(channelId: number) {
    setError("");
    try { const row = await request<Conversation>(`/crm/deals/${dealId}/conversations`, "POST", { channel_id: channelId }); await load(); setActive(row.id); }
    catch (e) { setError((e as Error).message); }
  }
  if (items === null) return null;
  return <div className="crmBlock dealChats"><div className="crmBlockHead"><h3>Переписка</h3>
    {canWrite && phone && channels.length > 0 && !items.some(c => c.channel_kind === "whatsapp") && <button className="crmLinkButton" onClick={() => startWhatsapp(channels[0].id)}>＋ Написать в WhatsApp</button>}</div>
    {error && <p className="crmFormError">{error}</p>}
    {items.length > 1 && <div className="crmChips">{items.map(c => <button key={c.id} className={active === c.id ? "active" : ""} onClick={() => setActive(c.id)}>{channelBadge[c.channel_kind || ""]?.label} · {c.channel_name}</button>)}</div>}
    {active ? <ChatThread conversationId={active} projectId={projectId} compact canWrite={canWrite} onChanged={load}/>
      : <p className="crmMuted">Переписки с клиентом пока нет. Сообщения из Авито, Telegram и WhatsApp появятся здесь автоматически{phone && channels.length ? ", а в WhatsApp можно написать первым" : ""}.</p>}</div>;
}
