"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, CrmLead, CrmLeadStatus, PortalUser } from "@/lib/api";

const columns: { value: CrmLeadStatus; label: string; tone: string }[] = [
  { value: "new", label: "Новые", tone: "violet" },
  { value: "contacted", label: "Связались", tone: "blue" },
  { value: "qualified", label: "Квалификация", tone: "yellow" },
  { value: "proposal", label: "Предложение", tone: "orange" },
  { value: "won", label: "Сделка", tone: "green" },
  { value: "lost", label: "Отказ", tone: "gray" },
];
const money = (value: number) => new Intl.NumberFormat("ru-RU", { style: "currency", currency: "RUB", maximumFractionDigits: 0 }).format(value);
type LeadEvent = { id:number; event_type:string; description:string; actor_name:string|null; created_at:string };
type CrmNotification = { id:number; title:string; body:string|null; level:string; created_at:string; read_at:string|null };
type LeadAttribution = { external_campaign_id:string|null; external_ad_id:string|null; utm_source:string|null; utm_medium:string|null; utm_campaign:string|null; utm_content:string|null; utm_term:string|null; landing_url:string|null; hypothesis_id:number|null };

export default function PortalCrmPage() {
  const [me, setMe] = useState<PortalUser | null>(null);
  const [leads, setLeads] = useState<CrmLead[]>([]);
  const [team, setTeam] = useState<PortalUser[]>([]);
  const [notifications, setNotifications] = useState<CrmNotification[]>([]);
  const [events, setEvents] = useState<LeadEvent[]>([]);
  const [attribution, setAttribution] = useState<LeadAttribution|null>(null);
  const [search, setSearch] = useState(""); const [assigneeFilter, setAssigneeFilter] = useState(""); const [dueFilter, setDueFilter] = useState("");
  const [selected, setSelected] = useState<CrmLead | null>(null);
  const [modal, setModal] = useState<"create" | "lead" | null>(null);
  const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      const [dashboard, leadRows, members, alerts] = await Promise.all([
        api<{ user: PortalUser }>("/portal/dashboard"), api<CrmLead[]>("/portal/crm/leads"), api<PortalUser[]>("/portal/crm/team"), api<CrmNotification[]>("/portal/notifications"),
      ]);
      setMe(dashboard.user); setLeads(leadRows); setTeam(members); setNotifications(alerts); setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось загрузить CRM"); }
  }, []);
  useEffect(() => { load(); const timer = window.setInterval(load, 15_000); return () => window.clearInterval(timer); }, [load]);
  const canCreate = me?.role === "client_owner" || me?.role === "sales_head";
  const canEdit = me && !["viewer", "client_marketer"].includes(me.role);
  const totals = useMemo(() => ({ active: leads.filter(x => !["won", "lost"].includes(x.status)).length, won: leads.filter(x => x.status === "won").length, value: leads.filter(x => x.status === "won").reduce((sum, x) => sum + x.value, 0) }), [leads]);
  const filteredLeads = useMemo(() => leads.filter(lead => {
    const needle=search.trim().toLowerCase(); const matchesSearch=!needle||[lead.full_name,lead.phone,lead.email,lead.source].some(value=>value?.toLowerCase().includes(needle));
    const matchesAssignee=!assigneeFilter||(assigneeFilter==="none"?!lead.assigned_to_id:String(lead.assigned_to_id)===assigneeFilter);
    const overdue=lead.next_action_at&&!(["won","lost"] as string[]).includes(lead.status)&&new Date(lead.next_action_at)<new Date();
    const today=lead.next_action_at&&new Date(lead.next_action_at).toDateString()===new Date().toDateString();
    const matchesDue=!dueFilter||(dueFilter==="overdue"?overdue:dueFilter==="today"?today:!lead.next_action_at);
    return matchesSearch&&matchesAssignee&&matchesDue;
  }),[leads,search,assigneeFilter,dueFilter]);

  async function createLead(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const data = new FormData(event.currentTarget); const next = String(data.get("next_action_at") || "");
    try {
      await api("/portal/crm/leads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ full_name: data.get("full_name"), phone: data.get("phone") || null, email: data.get("email") || null, source: data.get("source") || "Вручную", assigned_to_id: data.get("assigned_to_id") ? Number(data.get("assigned_to_id")) : null, value: Number(data.get("value") || 0), notes: data.get("notes") || null, next_action_at: next ? new Date(next).toISOString() : null }) });
      setModal(null); setNotice("Лид добавлен в воронку."); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось добавить лид"); } finally { setBusy(false); }
  }
  async function updateLead(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selected) return; setBusy(true); setError("");
    const data = new FormData(event.currentTarget); const next = String(data.get("next_action_at") || "");
    const body: Record<string, unknown> = { full_name:data.get("full_name"), phone:data.get("phone")||null, email:data.get("email")||null, source:data.get("source"), status: data.get("status"), value: Number(data.get("value") || 0), notes: data.get("notes") || null, next_action_at: next ? new Date(next).toISOString() : null };
    if (canCreate) body.assigned_to_id = data.get("assigned_to_id") ? Number(data.get("assigned_to_id")) : null;
    try { await api(`/portal/crm/leads/${selected.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); setModal(null); setSelected(null); setNotice("Карточка лида обновлена."); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось обновить лид"); } finally { setBusy(false); }
  }
  async function openLead(lead: CrmLead) { setSelected(lead); setModal("lead"); setNotice(""); setEvents([]); setAttribution(null); try{const [history,mark]=await Promise.all([api<LeadEvent[]>(`/portal/crm/leads/${lead.id}/events`),api<LeadAttribution|null>(`/portal/crm/leads/${lead.id}/attribution`)]);setEvents(history);setAttribution(mark);}catch(e){setError(e instanceof Error?e.message:"Не удалось загрузить историю");} }
  async function dismissNotification(id:number){try{await api(`/portal/notifications/${id}/read`,{method:"POST"});setNotifications(rows=>rows.filter(row=>row.id!==id));}catch{/* уведомление останется видимым */}}
  if (!me) return <main className="portalSurface"><p className={error ? "notice error" : "portalMuted"}>{error || "Загружаем CRM…"}</p></main>;

  return <div className="portalShell"><aside className="portalSidebar"><div className="portalBrand"><span>↗</span><b>StepToLead</b></div><div className="portalCompany"><small>Компания</small><strong>{me.workspace_name}</strong></div><nav><Link href="/portal">Общая картина</Link><Link className="active" href="/portal/crm">Продажи</Link><Link className="active" href="/portal/crm">{me.role === "sales_manager" ? "Мои лиды" : "Воронка"}</Link><a>Уведомления</a></nav><button onClick={async()=>{await api("/portal/auth/logout",{method:"POST"});location.assign("/portal/login");}}>Выйти</button></aside>
    <main className="portalMain crmMain"><header><div><p>{me.role_name} · CRM</p><h1>{me.role === "sales_manager" ? "Мои лиды" : "Воронка продаж"}</h1><span>{me.role === "sales_manager" ? "Обрабатывайте обращения и фиксируйте результат каждого контакта." : "Все обращения компании, ответственные и следующие действия."}</span></div><div className="crmHeaderActions">{canCreate && <button className="button primary" onClick={() => setModal("create")}>＋ Добавить лид</button>}<div className="portalUser">{me.display_name.slice(0,2).toUpperCase()}</div></div></header>
      {error && <p className="notice error">{error}</p>}{notice && <p className="notice success">{notice}</p>}
      {notifications.filter(item=>!item.read_at).length > 0 && <section className="crmAlerts">{notifications.filter(item=>!item.read_at).slice(0,3).map(item => <article key={item.id} onClick={()=>dismissNotification(item.id)} title="Нажмите, чтобы отметить прочитанным"><i></i><div><strong>{item.title}</strong><span>{item.body}</span></div><time>{new Date(item.created_at).toLocaleString("ru-RU", { day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit" })}</time></article>)}</section>}
      <section className="crmKpis"><article><span>Всего лидов</span><strong>{leads.length}</strong></article><article><span>В активной работе</span><strong>{totals.active}</strong></article><article><span>Успешные сделки</span><strong>{totals.won}</strong></article><article><span>Сумма сделок</span><strong>{money(totals.value)}</strong></article></section>
      <section className="crmFilters"><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Поиск по имени, телефону, email или источнику"/><select value={assigneeFilter} onChange={e=>setAssigneeFilter(e.target.value)}><option value="">Все ответственные</option><option value="none">Не назначены</option>{team.map(user=><option value={user.id} key={user.id}>{user.display_name}</option>)}</select><select value={dueFilter} onChange={e=>setDueFilter(e.target.value)}><option value="">Все сроки</option><option value="overdue">Просроченные</option><option value="today">На сегодня</option><option value="none">Без следующего действия</option></select><span>Найдено: {filteredLeads.length}</span></section>
      <section className="crmBoard">{columns.map(column => { const rows = filteredLeads.filter(lead => lead.status === column.value); return <div className="crmColumn" key={column.value}><header><span className={column.tone}></span><strong>{column.label}</strong><b>{rows.length}</b></header><div>{rows.map(lead => {const overdue=lead.next_action_at&&!(["won","lost"] as string[]).includes(lead.status)&&new Date(lead.next_action_at)<new Date();return <button className={`crmLeadCard ${overdue?"overdue":""}`} key={lead.id} onClick={() => openLead(lead)}><div><strong>{lead.full_name}</strong><span>{lead.source}</span></div>{lead.value > 0 && <b>{money(lead.value)}</b>}<p>{lead.phone || lead.email || "Контакт не указан"}</p><footer><span>{lead.assigned_to_name || "Не назначен"}</span><time>{lead.next_action_at ? `${overdue?"Просрочено · ":""}${new Date(lead.next_action_at).toLocaleDateString("ru-RU")}` : "Без срока"}</time></footer></button>})}{!rows.length && <p className="crmEmpty">На этом этапе пока пусто</p>}</div></div>})}</section>
    </main>
    {modal === "create" && <div className="modalBackdrop"><form className="modal crmModal" onSubmit={createLead}><div className="modalHead"><div><p className="eyebrow">Новое обращение</p><h2>Добавить лид</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div><div className="crmFields"><label>Имя клиента<input name="full_name" required minLength={2} autoFocus /></label><label>Источник<input name="source" defaultValue="Вручную" required /></label><label>Телефон<input name="phone" type="tel" /></label><label>Email<input name="email" type="email" /></label><label>Ответственный<select name="assigned_to_id" defaultValue=""><option value="">Пока не назначен</option>{team.map(user => <option key={user.id} value={user.id}>{user.display_name} · {user.role_name}</option>)}</select></label><label>Потенциальная сумма<input name="value" type="number" min="0" defaultValue="0" /></label><label>Следующее действие<input name="next_action_at" type="datetime-local" /></label><label className="wide">Комментарий<textarea name="notes" rows={4}></textarea></label></div><button className="button primary full" disabled={busy}>{busy ? "Сохраняем…" : "Добавить в воронку"}</button></form></div>}
    {modal === "lead" && selected && <div className="modalBackdrop"><form className="modal crmModal" onSubmit={updateLead}><div className="modalHead"><div><p className="eyebrow">Карточка лида #{selected.id}</p><h2>{selected.full_name}</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div>{attribution&&<section className="attributionBox"><div><small>UTM-источник</small><strong>{[attribution.utm_source,attribution.utm_medium].filter(Boolean).join(" / ")||"Не передан"}</strong></div><div><small>UTM-кампания</small><strong>{attribution.utm_campaign||"Не передана"}</strong></div><div><small>ID кампании</small><strong>{attribution.external_campaign_id||"Не передан"}</strong></div><div><small>Гипотеза</small><strong>{attribution.hypothesis_id?`#${attribution.hypothesis_id}`:"Не определена"}</strong></div>{attribution.landing_url&&<a href={attribution.landing_url} target="_blank">Открыть посадочную страницу ↗</a>}</section>}<div className="crmFields"><label>Имя контакта<input name="full_name" required minLength={2} defaultValue={selected.full_name} disabled={!canEdit}/></label><label>Источник<input name="source" required defaultValue={selected.source} disabled={!canEdit}/></label><label>Телефон<input name="phone" type="tel" defaultValue={selected.phone||""} disabled={!canEdit}/></label><label>Email<input name="email" type="email" defaultValue={selected.email||""} disabled={!canEdit}/></label><label>Этап<select name="status" defaultValue={selected.status} disabled={!canEdit}>{columns.map(column => <option key={column.value} value={column.value}>{column.label}</option>)}</select></label>{canCreate && <label>Ответственный<select name="assigned_to_id" defaultValue={selected.assigned_to_id || ""}><option value="">Не назначен</option>{team.map(user => <option key={user.id} value={user.id}>{user.display_name} · {user.role_name}</option>)}</select></label>}<label>Сумма сделки<input name="value" type="number" min="0" defaultValue={selected.value} disabled={!canEdit} /></label><label>Следующее действие<input name="next_action_at" type="datetime-local" defaultValue={selected.next_action_at ? selected.next_action_at.slice(0,16) : ""} disabled={!canEdit} /></label><label className="wide">Комментарий<textarea name="notes" rows={4} defaultValue={selected.notes || ""} disabled={!canEdit}></textarea></label></div>{canEdit ? <button className="button primary full" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить изменения"}</button> : <p className="formHint">Для вашей роли карточка доступна только для просмотра.</p>}<section className="leadTimeline"><h3>История работы</h3>{events.map(event=><article key={event.id}><i></i><div><strong>{event.description}</strong><span>{event.actor_name||"Автоматически"}</span></div><time>{new Date(event.created_at).toLocaleString("ru-RU")}</time></article>)}{!events.length&&<p className="portalMuted">История загружается или пока пуста.</p>}</section></form></div>}
  </div>;
}
