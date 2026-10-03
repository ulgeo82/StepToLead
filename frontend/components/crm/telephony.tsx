"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { request, Team, when } from "./shared";

export type CallRow = { id: number; direction: "in" | "out"; status: string; phone: string | null; line_number: string | null;
  extension: string | null; user_id: number | null; user_name: string | null; started_at: string; answered_at: string | null;
  duration_sec: number; wait_sec: number; contact_id: number | null; contact_name: string | null; deal_id: number | null;
  deal_name: string | null; inbound_id: number | null; has_recording: boolean; recording_deleted: boolean;
  callback_status: string | null; callback_task_id: number | null; stage_name?: string | null; amount?: number | null;
  responsible_name?: string | null };
type PbxUser = { extension: string; name: string; numbers: string[] };
type Connection = { id: number; provider: string; provider_name: string; name: string; active: boolean; status: string;
  last_error: string | null; last_event_at: string | null; retention_days: number; create_leads: boolean; missed_task_minutes: number;
  webhook_path?: string; pbx_users?: PbxUser[]; user_map?: Record<string, number> };
export type TelephonyInfo = { connection: Connection | null; can_manage: boolean; my_extension: string | null;
  members: { id: number; name: string }[] };
type Bucket = { total: number; incoming: number; outgoing: number; answered_in: number; missed_in: number; answered_out: number;
  talk_sec: number; avg_wait_sec: number | null };
type Log = { items: CallRow[]; total: number; page: number;
  stats: { total: Bucket; users: (Bucket & { user_id: number | null; name: string })[]; open_callbacks: number } };

export const fmtPhone = (value: string | null) => {
  const d = (value || "").replace(/\D/g, "");
  return d.length === 11 && d[0] === "7" ? `+7 ${d.slice(1, 4)} ${d.slice(4, 7)}-${d.slice(7, 9)}-${d.slice(9)}` : value || "";
};
const duration = (sec: number) => { const m = Math.floor(sec / 60), s = sec % 60; return m ? `${m}:${String(s).padStart(2, "0")}` : `${s} сек`; };
const talk = (sec: number) => sec >= 3600 ? `${Math.floor(sec / 3600)} ч ${Math.round(sec % 3600 / 60)} мин` : `${Math.round(sec / 60)} мин`;

export function callLabel(c: CallRow) {
  if (c.direction === "in") return c.status === "answered" ? { icon: "↙", text: "Входящий", tone: "ok" } : { icon: "↙", text: "Пропущенный", tone: "missed" };
  return c.status === "answered" ? { icon: "↗", text: "Исходящий", tone: "ok" } : { icon: "↗", text: "Не дозвонились", tone: "noanswer" };
}

/** Telephony settings of the project + whether this user can call from the CRM. */
export function useTelephony(projectId: number | null) {
  const [info, setInfo] = useState<TelephonyInfo | null>(null);
  const load = useCallback(() => projectId ? request<TelephonyInfo>(`/crm/projects/${projectId}/telephony`, "GET").then(setInfo).catch(() => setInfo(null)) : Promise.resolve(),
    [projectId]);
  useEffect(() => { load(); }, [load]);
  return { info, reload: load, canDial: Boolean(info?.connection?.active && info.my_extension) };
}

/** «Позвонить»: through the PBX when it is connected and the manager has an extension, otherwise a tel: link. */
export function CallButton({ projectId, phone, dealId, canDial, onNotice }: { projectId: number; phone: string; dealId?: number;
  canDial: boolean; onNotice?: (text: string, error?: boolean) => void }) {
  const [busy, setBusy] = useState(false);
  if (!canDial) return <a href={`tel:${phone.replace(/[^\d+]/g, "")}`}>📞 Позвонить</a>;
  async function dial() {
    setBusy(true);
    try { const r = await request<{ message: string }>(`/crm/projects/${projectId}/calls/dial`, "POST", { phone, deal_id: dealId ?? null }); onNotice?.(r.message); }
    catch (e) { onNotice?.((e as Error).message, true); } finally { setBusy(false); }
  }
  return <button type="button" className="callDial" disabled={busy} onClick={dial} title="АТС позвонит вам, затем соединит с клиентом">{busy ? "Соединяем…" : "📞 Позвонить"}</button>;
}

export function CallPlayer({ call }: { call: CallRow }) {
  const [open, setOpen] = useState(false);
  if (call.recording_deleted) return <span className="crmMuted" title="Записи хранятся 3 месяца">запись удалена</span>;
  if (!call.has_recording) return null;
  return open ? <audio className="callAudio" controls autoPlay preload="none" src={`/api/crm/calls/${call.id}/recording`}/>
    : <button type="button" className="crmLinkButton" onClick={() => setOpen(true)}>▶ Запись</button>;
}

/** Calls block inside the deal card. */
export function DealCalls({ dealId, refreshKey }: { dealId: number; refreshKey?: unknown }) {
  const [rows, setRows] = useState<CallRow[] | null>(null);
  useEffect(() => { request<CallRow[]>(`/crm/deals/${dealId}/calls`, "GET").then(setRows).catch(() => setRows([])); }, [dealId, refreshKey]);
  if (!rows?.length) return null;
  return <div className="crmBlock"><h3>Звонки</h3><div className="callList">{rows.map(c => { const l = callLabel(c);
    return <div key={c.id} className={`callRow ${l.tone}`}><i>{l.icon}</i><div><b>{l.text}{c.status === "answered" ? ` · ${duration(c.duration_sec)}` : ""}</b>
      <small>{when(c.started_at)}{c.user_name ? ` · ${c.user_name}` : ""}</small></div><CallPlayer call={c}/></div>; })}</div></div>;
}

/** Pop-up card while a client is calling: who it is and the deal, before the manager picks up. */
export function IncomingCall({ projectId, enabled, onOpenDeal, onOpenInbound }: { projectId: number; enabled: boolean;
  onOpenDeal: (id: number) => void; onOpenInbound: () => void }) {
  const [calls, setCalls] = useState<CallRow[]>([]);
  const [hidden, setHidden] = useState<number[]>([]);
  useEffect(() => {
    if (!enabled) { setCalls([]); return; }
    let active = true;
    const tick = () => request<CallRow[]>(`/crm/projects/${projectId}/calls/active`, "GET").then(v => { if (active) setCalls(v); }).catch(() => undefined);
    tick(); const timer = setInterval(tick, 3000);
    return () => { active = false; clearInterval(timer); };
  }, [projectId, enabled]);
  const visible = calls.filter(c => !hidden.includes(c.id));
  if (!visible.length) return null;
  return <div className="callPopups" role="status" aria-live="polite">{visible.map(c => <div key={c.id} className="callPopup">
    <header><span className="callPulse"/>Входящий звонок<button onClick={() => setHidden(h => [...h, c.id])} aria-label="Скрыть">×</button></header>
    <b>{c.contact_name || fmtPhone(c.phone) || "Номер скрыт"}</b>
    {c.contact_name && <small>{fmtPhone(c.phone)}</small>}
    {c.deal_id ? <p>Сделка «{c.deal_name}»{c.stage_name ? ` · ${c.stage_name}` : ""}{c.responsible_name ? ` · ${c.responsible_name}` : ""}</p>
      : <p>Новый клиент — после звонка появится заявка в «Неразобранном»</p>}
    {c.line_number && <small>на линию {fmtPhone(c.line_number)}{c.user_name ? ` · звонит ${c.user_name}` : ""}</small>}
    {c.deal_id ? <button className="crmPrimary" onClick={() => onOpenDeal(c.deal_id!)}>Открыть сделку</button>
      : c.inbound_id ? <button className="crmGhost" onClick={onOpenInbound}>Открыть заявку</button> : null}</div>)}</div>;
}

/** «Звонки» tab: stats per manager, missed calls to call back, the call log with recordings. */
export function CallsTab({ projectId, team, info, reloadInfo, onOpenDeal, onOpenInbound }: { projectId: number; team: Team[];
  info: TelephonyInfo | null; reloadInfo: () => void; onOpenDeal: (id: number) => void; onOpenInbound: () => void }) {
  const [log, setLog] = useState<Log | null>(null);
  const [days, setDays] = useState("7");
  const [direction, setDirection] = useState("");
  const [status, setStatus] = useState("");
  const [userId, setUserId] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [settings, setSettings] = useState(false);
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    const q = new URLSearchParams({ days, page: String(page), limit: "50" });
    if (direction) q.set("direction", direction); if (status) q.set("status", status); if (userId) q.set("user_id", userId);
    if (search.trim()) q.set("search", search.trim());
    return request<Log>(`/crm/projects/${projectId}/calls?${q}`, "GET").then(setLog).catch(e => setError((e as Error).message));
  }, [projectId, days, direction, status, userId, search, page]);
  useEffect(() => { const t = setTimeout(load, 150); const i = setInterval(load, 20000); return () => { clearTimeout(t); clearInterval(i); }; }, [load]);
  useEffect(() => { setPage(1); }, [days, direction, status, userId, search]);
  const conn = info?.connection;
  const t = log?.stats.total;
  const canDial = Boolean(conn?.active && info?.my_extension);
  const answeredShare = t && t.incoming ? Math.round(t.answered_in / t.incoming * 100) : null;
  return <section className="crmList resultPanel callsTab">
    <div className="crmSectionHead"><div><h2>Звонки</h2><p>{conn ? <>Все звонки {conn.provider_name} попадают в сделки сами: с записью, длительностью и менеджером. Пропущенный — задача «Перезвонить».</>
      : <>Подключите облачную АТС — звонки будут сами попадать в сделки, а пропущенные превращаться в задачи.</>}</p></div>
      {(info?.can_manage || conn) && <button className="crmGhost" onClick={() => setSettings(true)}>⚙ {conn ? "Настройки телефонии" : "Подключить телефонию"}</button>}</div>
    {conn && conn.status === "error" && <p className="crmWarnLine">Телефония: {conn.last_error}</p>}
    {conn && !conn.last_event_at && info?.can_manage && <p className="crmWarnLine">От АТС ещё не пришло ни одного события. Проверьте, что адрес для событий вставлен в кабинете Mango (⚙ Настройки телефонии).</p>}
    {conn && !info?.my_extension && <p className="crmMuted">Вам не назначен внутренний номер — звонить из CRM пока нельзя. Попросите руководителя указать его в настройках телефонии.</p>}
    {notice && <p className={notice.error ? "crmFormError" : "crmOk"}>{notice.text}</p>}
    {error && <p className="crmFormError">{error}</p>}
    {t && <div className="crmKpis">
      <div className="crmKpi"><span>Всего звонков</span><strong>{t.total}</strong><small>входящих {t.incoming} · исходящих {t.outgoing}</small></div>
      <div className={`crmKpi ${t.missed_in ? "bad" : "good"}`}><span>Пропущено входящих</span><strong>{t.missed_in}</strong><small>{answeredShare == null ? "входящих не было" : `принято ${answeredShare}%`}</small></div>
      <div className={`crmKpi ${log!.stats.open_callbacks ? "warn" : ""}`}><span>Ждут перезвона</span><strong>{log!.stats.open_callbacks}</strong><small>открытые задачи «Перезвонить»</small></div>
      <div className="crmKpi"><span>Время разговоров</span><strong>{talk(t.talk_sec)}</strong><small>{t.avg_wait_sec == null ? "" : `ожидание ответа ~${t.avg_wait_sec} сек`}</small></div></div>}
    {log && log.stats.users.length > 1 && <div className="crmTableScroll callStats"><table><thead><tr><th>Менеджер</th><th>Входящие</th><th>Пропущено</th><th>Исходящие</th><th>Дозвонились</th><th>Разговоры</th><th>Ожидание</th></tr></thead>
      <tbody>{log.stats.users.map(u => <tr key={u.user_id ?? 0}><td>{u.name}</td><td>{u.incoming}</td><td className={u.missed_in ? "bad" : ""}>{u.missed_in}</td><td>{u.outgoing}</td>
        <td>{u.outgoing ? `${Math.round(u.answered_out / u.outgoing * 100)}%` : "—"}</td><td>{talk(u.talk_sec)}</td><td>{u.avg_wait_sec == null ? "—" : `${u.avg_wait_sec} сек`}</td></tr>)}</tbody></table></div>}
    <div className="crmFilters crmFiltersPro callFilters">
      <select value={days} onChange={e => setDays(e.target.value)}><option value="1">Сегодня и вчера</option><option value="7">7 дней</option><option value="30">30 дней</option><option value="90">3 месяца</option></select>
      <select value={direction} onChange={e => setDirection(e.target.value)}><option value="">Все направления</option><option value="in">Входящие</option><option value="out">Исходящие</option></select>
      <select value={status} onChange={e => setStatus(e.target.value)}><option value="">Любой результат</option><option value="answered">Состоялся</option><option value="missed">Пропущен / не дозвонились</option><option value="callback">Ждут перезвона</option></select>
      {team.length > 1 && <select value={userId} onChange={e => setUserId(e.target.value)}><option value="">Все менеджеры</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select>}
      <input placeholder="Поиск по номеру" value={search} onChange={e => setSearch(e.target.value)}/></div>
    <div className="crmTableScroll"><table className="callTable"><thead><tr><th>Звонок</th><th>Клиент</th><th>Сделка</th><th>Менеджер</th><th>Когда</th><th>Длительность</th><th>Запись</th><th/></tr></thead>
      <tbody>{log?.items.map(c => { const l = callLabel(c);
        return <tr key={c.id} className={l.tone}><td><span className={`callIcon ${l.tone}`}>{l.icon}</span>{l.text}</td>
          <td><b>{c.contact_name || fmtPhone(c.phone) || "Номер скрыт"}</b>{c.contact_name && c.phone ? <small>{fmtPhone(c.phone)}</small> : null}</td>
          <td>{c.deal_id ? <button className="crmLinkButton" onClick={() => onOpenDeal(c.deal_id!)}>{c.deal_name}</button>
            : c.inbound_id ? <button className="crmLinkButton" onClick={onOpenInbound}>заявка</button> : "—"}</td>
          <td>{c.user_name || (c.extension ? `доб. ${c.extension}` : "—")}</td><td>{when(c.started_at)}</td>
          <td>{c.status === "answered" ? duration(c.duration_sec) : c.direction === "in" && c.wait_sec ? `ждал ${duration(c.wait_sec)}` : "—"}</td>
          <td><CallPlayer call={c}/></td>
          <td>{c.callback_status === "OPEN" ? <span className="callBack">ждёт перезвона</span> : c.callback_status === "COMPLETED" ? <span className="crmMuted">перезвонили</span> : null}
            {c.phone && c.direction === "in" && c.status !== "answered" && c.callback_status === "OPEN" &&
              <CallButton projectId={projectId} phone={c.phone} dealId={c.deal_id ?? undefined} canDial={canDial} onNotice={(text, err) => setNotice({ text, error: err })}/>}</td></tr>; })}</tbody></table>
      {log && !log.items.length && <div className="callEmpty"><p>{conn ? "Звонков за период нет." : "Звонков пока нет."}</p>
        {!conn && info?.can_manage && <button className="crmPrimary" onClick={() => setSettings(true)}>Подключить Mango Office</button>}</div>}</div>
    {log && log.total > 50 && <div className="crmPager"><button disabled={page <= 1} onClick={() => setPage(p => p - 1)}>← Назад</button><span>{page} из {Math.ceil(log.total / 50)}</span>
      <button disabled={page * 50 >= log.total} onClick={() => setPage(p => p + 1)}>Дальше →</button></div>}
    {settings && <TelephonySettings projectId={projectId} info={info} onClose={() => setSettings(false)} onChanged={() => { reloadInfo(); load(); }}/>}
  </section>;
}

function TelephonySettings({ projectId, info, onClose, onChanged }: { projectId: number; info: TelephonyInfo | null;
  onClose: () => void; onChanged: () => void }) {
  const conn = info?.connection;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [map, setMap] = useState<Record<string, number>>(conn?.user_map || {});
  const [copied, setCopied] = useState(false);
  useEffect(() => { setMap(conn?.user_map || {}); }, [conn?.user_map]);
  const manage = Boolean(info?.can_manage);
  const webhook = conn?.webhook_path && typeof window !== "undefined" ? `${window.location.origin}${conn.webhook_path}` : "";
  async function run(action: () => Promise<unknown>, message: string) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); setNotice(message); onChanged(); return true; } catch (e) { setError((e as Error).message); return false; } finally { setBusy(false); }
  }
  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget);
    await run(() => request(`/crm/projects/${projectId}/telephony`, "POST", { provider: "mango", name: f.get("name") || "Mango Office",
      api_key: String(f.get("api_key") || "").trim(), api_salt: String(f.get("api_salt") || "").trim() }),
      "Ключи верны. Остался шаг 3: вставьте адрес для событий в кабинет Mango.");
  }
  async function saveKeys(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget); const form = event.currentTarget;
    if (await run(() => request(`/crm/telephony/${conn!.id}`, "PATCH", { api_key: String(f.get("api_key")).trim(), api_salt: String(f.get("api_salt")).trim() }), "Ключи обновлены")) form.reset();
  }
  async function copy() { if (!webhook) return; await navigator.clipboard?.writeText(webhook).catch(() => undefined); setCopied(true); setTimeout(() => setCopied(false), 2000); }
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><div className="resultModal crmModal callSettings">
    <header><h2>Телефония · Mango Office</h2><button type="button" onClick={onClose}>×</button></header>
    {error && <p className="crmFormError">{error}</p>}{notice && <p className="crmOk">{notice}</p>}
    {!conn && !manage && <p className="crmMuted">Телефонию подключает руководитель или владелец аккаунта.</p>}
    {!conn && manage && <form onSubmit={connect}>
      <ol className="callSteps">
        <li>В личном кабинете Mango Office откройте <b>Интеграции → API-коннектор</b> (раздел «Интеграции» в меню АТС) и подключите его.</li>
        <li>Скопируйте оттуда <b>уникальный код АТС</b> и <b>ключ для создания подписи</b> и вставьте ниже.</li>
        <li>После проверки ключей здесь появится адрес для событий — его нужно вставить в поле «Адрес внешней системы» там же, в Mango.</li></ol>
      <label>Название<input name="name" defaultValue="Mango Office" minLength={2} required/></label>
      <label>Уникальный код АТС (vpbx_api_key)<input name="api_key" required autoComplete="off"/></label>
      <label>Ключ для создания подписи<input name="api_salt" required autoComplete="off" type="password"/></label>
      <button className="resultPrimary" disabled={busy}>{busy ? "Проверяем ключи…" : "Подключить"}</button></form>}
    {conn && <>
      <div className="callConnState"><span className={`callDot ${conn.active ? conn.status : "off"}`}/>
        <div><b>{conn.name}</b><small>{!conn.active ? "выключена" : conn.status === "connected" ? "подключена" : conn.status === "error" ? "ошибка" : "проверяется"}
          {conn.last_event_at ? ` · последнее событие ${when(conn.last_event_at)}` : " · событий от АТС ещё не было"}</small>
          {conn.last_error && <small className="chatError">{conn.last_error}</small>}</div>
        {manage && <button className="crmGhost" disabled={busy} onClick={() => run(() => request(`/crm/telephony/${conn.id}`, "PATCH", { active: !conn.active }), conn.active ? "Телефония выключена" : "Телефония включена")}>{conn.active ? "Выключить" : "Включить"}</button>}</div>
      {info?.my_extension && <p className="crmModalLead">Ваш внутренний номер: <b>{info.my_extension}</b>. Кнопка «Позвонить» в сделке сначала позвонит вам, затем соединит с клиентом.</p>}
      {manage && webhook && <div className="callWebhook"><h3 className="crmSub">Адрес для событий</h3>
        <p className="crmModalLead">Вставьте его в кабинете Mango: <b>Интеграции → API-коннектор → «Адрес внешней системы»</b> и сохраните. Без этого звонки не будут попадать в CRM.</p>
        <div className="callWebhookRow"><code>{webhook}</code><button type="button" className="crmGhost" onClick={copy}>{copied ? "Скопировано ✓" : "Копировать"}</button></div>
        {webhook.startsWith("http://") && <p className="crmWarnLine">Это локальный адрес — Mango до него не достучится. Настройте подключение на рабочем портале (https://…).</p>}</div>}
      {manage && <><div className="crmBlockHead"><h3 className="crmSub">Сотрудники и внутренние номера</h3>
        <button className="crmLinkButton" disabled={busy} onClick={() => run(() => request(`/crm/telephony/${conn.id}/sync-users`, "POST", {}), "Список сотрудников АТС обновлён")}>↻ Обновить из Mango</button></div>
        <p className="crmModalLead">Кому принадлежит какой номер: так звонки попадают к нужному менеджеру, и он может звонить из CRM.</p>
        <div className="callMap">{(conn.pbx_users || []).map(u => <label key={u.extension}><span><b>доб. {u.extension}</b> {u.name}</span>
          <select value={map[u.extension] || ""} onChange={e => setMap(m => { const next = { ...m }; if (e.target.value) next[u.extension] = Number(e.target.value); else delete next[u.extension]; return next; })}>
            <option value="">— не сотрудник CRM —</option>{info!.members.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>)}
          {!conn.pbx_users?.length && <p className="crmMuted">В АТС нет сотрудников с внутренними номерами.</p>}</div>
        <button className="crmPrimary" disabled={busy} onClick={() => run(() => request(`/crm/telephony/${conn.id}`, "PATCH", { user_map: map }), "Сотрудники сохранены")}>Сохранить сотрудников</button>
        <h3 className="crmSub">Правила</h3>
        <label className="crmCheck"><input type="checkbox" checked={conn.create_leads} disabled={busy} onChange={e => run(() => request(`/crm/telephony/${conn.id}`, "PATCH", { create_leads: e.target.checked }), "Сохранено")}/>
          Звонок с незнакомого номера создаёт заявку в «Неразобранном»</label>
        <label>Перезвонить по пропущенному в течение, мин<input type="number" min={1} max={1440} defaultValue={conn.missed_task_minutes} key={conn.missed_task_minutes}
          onBlur={e => { const v = Number(e.target.value); if (v >= 1 && v <= 1440 && v !== conn.missed_task_minutes) run(() => request(`/crm/telephony/${conn.id}`, "PATCH", { missed_task_minutes: v }), "Сохранено"); }}/></label>
        <p className="crmMuted">Записи разговоров хранятся {Math.round(conn.retention_days / 30)} месяца, затем удаляются автоматически.</p>
        <details><summary>Заменить ключи API</summary><form onSubmit={saveKeys}>
          <label>Уникальный код АТС<input name="api_key" required autoComplete="off"/></label>
          <label>Ключ для создания подписи<input name="api_salt" required autoComplete="off" type="password"/></label>
          <button className="crmGhost" disabled={busy}>Проверить и сохранить</button></form></details></>}
    </>}
  </div></div>;
}
