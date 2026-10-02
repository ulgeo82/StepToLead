"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { money, ProjectSidebar } from "@/components/reporting";
import "../result/result.css";
import "./crm.css";

type Project = { id: number; name: string; organization_name: string };
type Contact = { id: number; name: string; phones: string[]; emails: string[]; telegram: string | null; company: string | null };
type Task = { id: number; deal_id: number | null; type_code: string; title: string; due_at: string; status: string; result: string | null; priority: string;
  deal_name?: string | null; contact_name?: string | null; responsible_name?: string | null };
type Deal = { id: number; name: string; amount: number | null; contact: Contact; lead_id: number | null; stage_id: number; stage_name: string;
  pipeline_id: number; analytics_type: string; source_id: number | null; responsible_user_id: number | null; origin: string;
  source_name?: string | null; responsible_name?: string | null;
  custom_fields: Record<string, unknown>; attribution_snapshot: Record<string, unknown>; task_state: string; next_task: Task | null;
  created_at: string; archived_at?: string | null; form_data?: Record<string, unknown> | null; tasks?: Task[]; activities?: Activity[]; sales?: { id: number; amount: number; occurred_at: string }[] };
type Stage = { id: number; name: string; analytics_type: string; color: string; position?: number; required_fields: string[] };
type Board = { pipeline: { id: number; name: string }; columns: { stage: Stage; total: number; deals: Deal[]; has_more: boolean }[];
  control: Record<string, number>; inbound_count: number; page: number };
type Activity = { id: number; event_type: string; actor_name: string | null; payload: Record<string, unknown>; created_at: string };
type Inbound = { id: number; name: string | null; phone: string | null; email: string | null; status: string; received_at: string;
  raw_payload: Record<string, unknown>; attribution: Record<string, unknown>; potential_duplicates: { id: number; name: string }[] };
type Team = { id: number; display_name: string };
type Source = { id: number; name: string };
type CustomField = { id: number; key: string; name: string; field_type: string; options: string[] | null };
const stateLabels: Record<string, string> = { OVERDUE: "Просрочено", NO_TASK: "Без задачи", TODAY: "На сегодня", PLANNED: "Запланировано" };
const typeLabels: Record<string, string> = { CALL: "Позвонить", MEETING: "Встреча", MESSAGE: "Написать", SEND: "Отправить", FOLLOW_UP: "Связаться повторно", OTHER: "Другое" };
const when = (value?: string) => value ? new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
const request = <T,>(path: string, method: string, body: unknown) => api<T>(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function clientFields(data: Record<string, unknown>) {
  const fields: [string, string[]][] = [
    ["Имя", ["name", "full_name"]], ["Способ связи", ["contact_method"]],
    ["Контакт", ["contact"]], ["Телефон", ["phone"]], ["Email", ["email"]],
    ["Сайт клиента", ["website"]], ["Комментарий", ["comment", "notes"]],
  ];
  const seen = new Set<string>();
  return fields.flatMap(([label, keys]) => {
    const value = keys.map(key => String(data[key] ?? "").trim()).find(Boolean);
    if (!value || seen.has(value)) return [];
    seen.add(value); return [{ label, value }];
  });
}

export default function CrmPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [role, setRole] = useState("");
  const [permissions, setPermissions] = useState<string[]>([]);
  const [tab, setTab] = useState<"deals" | "contacts" | "tasks" | "inbound" | "pipeline">("deals");
  const [view, setView] = useState<"kanban" | "list">("kanban");
  const [archived, setArchived] = useState(false);
  const [board, setBoard] = useState<Board | null>(null);
  const [pipelines, setPipelines] = useState<{ id: number; name: string; stages: Stage[] }[]>([]);
  const [pipelineId, setPipelineId] = useState<number | null>(null);
  const [newDealPipelineId, setNewDealPipelineId] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [filterState, setFilterState] = useState("");
  const [search, setSearch] = useState("");
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskPage, setTaskPage] = useState(1);
  const [taskOwner, setTaskOwner] = useState("");
  const [taskType, setTaskType] = useState("");
  const [taskStatus, setTaskStatus] = useState("OPEN");
  const [taskStage, setTaskStage] = useState("");
  const [taskSource, setTaskSource] = useState("");
  const [taskPipeline, setTaskPipeline] = useState("");
  const [taskDueStart, setTaskDueStart] = useState("");
  const [taskDueEnd, setTaskDueEnd] = useState("");
  const [inbound, setInbound] = useState<Inbound[]>([]);
  const [inboundStatus, setInboundStatus] = useState<"NEW" | "ACCEPTED" | "REJECTED">("NEW");
  const [selected, setSelected] = useState<Deal | null>(null);
  const [team, setTeam] = useState<Team[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [fields, setFields] = useState<CustomField[]>([]);
  const [lostReasons, setLostReasons] = useState<{ id: number; label: string }[]>([]);
  const [modal, setModal] = useState<"deal" | "task" | "stage" | "sale" | "field" | "lost" | null>(null);
  const [pendingMove, setPendingMove] = useState<{ deal: Deal; stageId: number } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const can = (name: string) => permissions.includes(name);
  const project = projects.find(p => p.id === projectId);
  const refresh = () => setRevision(v => v + 1);
  useEffect(() => {
    Promise.all([api<Project[]>("/result/projects"), api<{ role: string; permissions: string[] }>("/portal/auth/me")])
      .then(([rows, user]) => { setProjects(rows); setRole(user.role); setPermissions(user.permissions);
        const desired = Number(new URLSearchParams(window.location.search).get("project_id"));
        setProjectId(rows.find(p => p.id === desired)?.id || rows[0]?.id || null); })
      .catch(e => setError(e.message));
  }, []);
  useEffect(() => { if (!projectId) return;
    setPipelineId(null); setPage(1); setSelected(null);
    Promise.all([api<{ id: number; name: string; stages: Stage[] }[]>(`/crm/projects/${projectId}/pipelines`),
      api<Team[]>(`/portal/crm/team?project_id=${projectId}`),
      api<Source[]>(`/crm/projects/${projectId}/sources`).catch(() => []),
      api<CustomField[]>(`/crm/projects/${projectId}/custom-fields`).catch(() => []),
      api<{ items: Contact[] }>(`/crm/projects/${projectId}/contacts?limit=100`).catch(() => ({ items: [] }))])
      .then(([p, t, s, f, c]) => { setPipelines(p); setTeam(t); setSources(s); setFields(f); setContacts(c.items); })
      .catch(e => setError(e.message));
  }, [projectId, revision]);
  const loadBoard = useCallback(() => { if (!projectId) return;
    const q = new URLSearchParams({ page: String(page), per_stage: "20" });
    if (archived) q.set("archived", "true");
    if (pipelineId) q.set("pipeline_id", String(pipelineId)); if (filterState) q.set("state", filterState); if (search) q.set("search", search);
    api<Board>(`/crm/projects/${projectId}/board?${q}`).then(setBoard).catch(e => setError(e.message));
  }, [projectId, pipelineId, page, filterState, search, archived]);
  useEffect(() => { loadBoard(); }, [loadBoard, revision]);
  useEffect(() => { if (!projectId) return;
    const leadId = Number(new URLSearchParams(window.location.search).get("lead_id"));
    if (leadId > 0) api<{ deal_id: number }>(`/crm/leads/${leadId}/deal`).then(row => openDeal(row.deal_id)).catch(() => {});
  }, [projectId]);
  useEffect(() => { if (!projectId) return;
    if (tab === "contacts") api<{ items: Contact[] }>(`/crm/projects/${projectId}/contacts`).then(v => setContacts(v.items)).catch(e => setError(e.message));
    if (tab === "tasks") { const q = new URLSearchParams({ page: String(taskPage), limit: "50" });
      if (taskOwner) q.set("responsible_user_id", taskOwner); if (taskType) q.set("type_code", taskType);
      if (taskStatus) q.set("status", taskStatus); if (taskStage) q.set("stage_id", taskStage); if (taskSource) q.set("source_id", taskSource);
      if (taskPipeline) q.set("pipeline_id", taskPipeline);
      if (taskDueStart) q.set("due_start", `${taskDueStart}T00:00:00Z`);
      if (taskDueEnd) q.set("due_end", `${taskDueEnd}T23:59:59Z`);
      api<{ items: Task[] }>(`/crm/projects/${projectId}/tasks?${q}`).then(v => setTasks(v.items)).catch(e => setError(e.message)); }
    if (tab === "inbound") api<{ items: Inbound[] }>(`/crm/projects/${projectId}/inbound?status=${inboundStatus}`).then(v => setInbound(v.items)).catch(e => setError(e.message));
  }, [projectId, tab, inboundStatus, revision, taskPage, taskOwner, taskType, taskStatus, taskStage, taskSource, taskPipeline, taskDueStart, taskDueEnd]);
  useEffect(() => { if (!projectId) return;
    api<{ id: number; label: string }[]>(`/settings/reasons?project_id=${projectId}`).then(setLostReasons).catch(() => setLostReasons([]));
  }, [projectId]);
  async function openDeal(id: number) { try { setSelected(await api<Deal>(`/crm/deals/${id}`)); } catch (e) { setError((e as Error).message); } }
  async function mutate<T>(path: string, method: string, body: unknown): Promise<T | null> { setBusy(true); setError("");
    try { const value = await request<T>(path, method, body); refresh(); return value; }
    catch (e) { setError((e as Error).message); return null; } finally { setBusy(false); } }
  async function move(deal: Deal, stageId: number, lostReasonId?: number, lostComment?: string) {
    if (deal.stage_id === stageId) return;
    const target = board?.columns.find(c => c.stage.id === stageId)?.stage;
    if (target?.analytics_type === "LOST" && !lostReasonId) { setPendingMove({ deal, stageId }); setModal("lost"); return; }
    const before = board;
    if (before) setBoard({ ...before, columns: before.columns.map(column => column.stage.id === deal.stage_id
      ? { ...column, deals: column.deals.filter(item => item.id !== deal.id), total: column.total - 1 }
      : column.stage.id === stageId ? { ...column, deals: [{ ...deal, stage_id: stageId, stage_name: column.stage.name }, ...column.deals], total: column.total + 1 }
      : column) });
    const result = await mutate<{ sale_required: boolean }>(`/crm/deals/${deal.id}/move`, "POST", { stage_id: stageId, lost_reason_id: lostReasonId, lost_comment: lostComment });
    if (!result) setBoard(before);
    if (result?.sale_required) { await openDeal(deal.id); setModal("sale"); }
    return result;
  }
  async function submitDeal(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!projectId) return;
    const f = new FormData(event.currentTarget); const custom_fields = Object.fromEntries(fields.map(field => [field.key, parseField(field, String(f.get(`field:${field.key}`) || ""))]).filter(([, v]) => v !== null));
    const payload = { project_id: projectId, name: f.get("name"), contact_id: f.get("contact_id") ? Number(f.get("contact_id")) : null,
      contact_name: f.get("contact_name") || null,
      phone: f.get("phone") || null, email: f.get("email") || null, amount: f.get("amount") ? Number(f.get("amount")) : null,
      pipeline_id: f.get("pipeline_id") ? Number(f.get("pipeline_id")) : null, stage_id: f.get("stage_id") ? Number(f.get("stage_id")) : null,
      created_at: f.get("created_at") ? new Date(String(f.get("created_at"))).toISOString() : null,
      responsible_user_id: f.get("owner") ? Number(f.get("owner")) : null, source_id: f.get("source") ? Number(f.get("source")) : null,
      comment: f.get("comment") || null, custom_fields };
    const result = await mutate<{ id: number; potential_duplicates: Contact[] }>("/crm/deals", "POST", payload);
    if (result) { setModal(null); if (result.potential_duplicates.length) setError("Есть похожий контакт. Сделка создана отдельно; проверьте возможный дубль.");
      await openDeal(result.id); if (f.get("next_task")) setModal("task"); }
  }
  async function submitTask(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!projectId) return;
    const f = new FormData(event.currentTarget);
    const result = await mutate("/crm/tasks", "POST", { project_id: projectId, deal_id: selected?.id || Number(f.get("deal_id")) || null,
      type_code: f.get("type_code"), title: f.get("title"), due_at: new Date(String(f.get("due_at"))).toISOString(),
      responsible_user_id: Number(f.get("owner")) || null, priority: f.get("priority"),
      description: f.get("description") || null, duration_minutes: f.get("duration_minutes") ? Number(f.get("duration_minutes")) : null });
    if (result) { setModal(null); if (selected) openDeal(selected.id); }
  }
  async function submitStage(event: FormEvent<HTMLFormElement>) { event.preventDefault();
    const f = new FormData(event.currentTarget); const result = await mutate(`/crm/pipelines/${pipelineId || board?.pipeline.id}/stages`, "POST",
      { name: f.get("name"), analytics_type: f.get("analytics_type"), color: f.get("color"), position: Number(f.get("position")) || 0,
        required_fields: String(f.get("required_fields") || "").split(",").map(v => v.trim()).filter(Boolean) });
    if (result) setModal(null);
  }
  async function reorderStages(draggedId: number, targetId: number) {
    const pipeline = pipelines.find(p => p.id === (pipelineId || board?.pipeline.id));
    if (!pipeline || draggedId === targetId) return;
    const ordered = [...pipeline.stages].sort((a, b) => (a.position || 0) - (b.position || 0));
    const from = ordered.findIndex(s => s.id === draggedId), to = ordered.findIndex(s => s.id === targetId);
    if (from < 0 || to < 0) return;
    ordered.splice(to, 0, ordered.splice(from, 1)[0]);
    setBusy(true); setError("");
    try { for (const [position, stage] of ordered.entries()) await request(`/crm/stages/${stage.id}`, "PATCH", { position }); refresh(); }
    catch (e) { setError((e as Error).message); refresh(); } finally { setBusy(false); }
  }
  async function submitSale(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!selected) return;
    const f = new FormData(event.currentTarget); const result = await mutate(`/crm/deals/${selected.id}/sales`, "POST",
      { amount: Number(f.get("amount")), occurred_at: f.get("occurred_at") ? new Date(String(f.get("occurred_at"))).toISOString() : null,
        comment: f.get("comment") || null });
    if (result) { setModal(null); openDeal(selected.id); }
  }
  async function submitLost(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!pendingMove) return;
    const f = new FormData(event.currentTarget); const dealId = pendingMove.deal.id;
    const result = await move(pendingMove.deal, pendingMove.stageId, Number(f.get("reason")), String(f.get("comment") || ""));
    if (result) { setPendingMove(null); setModal(null); if (selected?.id === dealId) openDeal(dealId); }
  }
  function parseField(field: CustomField, raw: string): unknown {
    if (!raw) return null;
    if (field.field_type === "NUMBER" || field.field_type === "MONEY") return Number(raw);
    if (field.field_type === "BOOLEAN") return raw === "true";
    if (field.field_type === "MULTISELECT") return raw.split(",").map(v => v.trim()).filter(Boolean);
    return raw;
  }
  async function submitField(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!projectId) return;
    const f = new FormData(event.currentTarget); const result = await mutate(`/crm/projects/${projectId}/custom-fields`, "POST",
      { key: f.get("key"), name: f.get("name"), field_type: f.get("field_type"),
        options: String(f.get("options") || "").split(",").map(v => v.trim()).filter(Boolean) });
    if (result) setModal(null);
  }
  return <div className="resultShell"><ProjectSidebar project={project} projectId={projectId} active="crm" role={role}/>
    <main className="resultMain crmMain"><div className="resultBreadcrumb">StepToLead <span>/</span> CRM <span>/</span> Сделки</div>
      <header className="crmHeader"><div><h1>Сделки</h1><p>Заявки, клиенты и работа менеджеров в одной воронке.</p></div>
        <div className="crmHeaderActions"><label>Проект <select value={projectId || ""} onChange={e => setProjectId(Number(e.target.value))}>{projects.map(p => <option key={p.id} value={p.id}>{p.organization_name} · {p.name}</option>)}</select></label>
        {can("create_deal") && <button className="crmPrimary" onClick={() => { setSelected(null); setNewDealPipelineId(pipelineId || board?.pipeline.id || pipelines[0]?.id || null); setModal("deal"); }}>＋ Создать сделку</button>}</div></header>
      {error && <div className="resultError" role="alert">{error} <button onClick={() => setError("")}>×</button></div>}
      {!projectId ? <div className="resultLoading">{error.includes("401") ? <><p>Для работы с CRM войдите в клиентский кабинет.</p><Link href="/portal/login?next=%2Fcrm">Войти →</Link></> : "Нет доступного проекта"}</div> : <>
      <div className="crmTabs"><button className={tab === "deals" ? "active" : ""} onClick={() => setTab("deals")}>▣ Сделки</button>
        <button className={tab === "contacts" ? "active" : ""} onClick={() => setTab("contacts")}>♙ Контакты</button>
        <button className={tab === "tasks" ? "active" : ""} onClick={() => setTab("tasks")}>◷ Задачи</button>
        <button className={tab === "inbound" ? "active" : ""} onClick={() => setTab("inbound")}>Неразобранное {board?.inbound_count ? <b>{board.inbound_count}</b> : null}</button>
        {can("manage_pipeline") && <button className={tab === "pipeline" ? "active" : ""} onClick={() => setTab("pipeline")}>⚙ Воронка</button>}</div>
      {tab === "deals" && <><div className="crmFilters"><select value={pipelineId || ""} onChange={e => { setPipelineId(Number(e.target.value)); setPage(1); }}>
        {pipelines.map(p => <option value={p.id} key={p.id}>{p.name}</option>)}</select>
        <input placeholder="Поиск по сделкам" value={search} onChange={e => { setSearch(e.target.value); setPage(1); }}/>
        <select aria-label="Архив сделок" value={archived ? "archive" : "active"} onChange={e => { setArchived(e.target.value === "archive"); setPage(1); setFilterState(""); setSelected(null); }}><option value="active">Активные сделки</option><option value="archive">Архив</option></select>
        <div className="crmView"><button className={view === "kanban" ? "active" : ""} onClick={() => setView("kanban")}>▦ Kanban</button><button className={view === "list" ? "active" : ""} onClick={() => setView("list")}>☷ Список</button></div></div>
        <div className="crmCounters">{Object.entries(stateLabels).map(([key, label]) => <button key={key} className={`crmCount ${key.toLowerCase()} ${filterState === key ? "active" : ""}`}
          onClick={() => { setFilterState(filterState === key ? "" : key); setPage(1); }}><strong>{board?.control[key] ?? "—"}</strong><span>{label}</span></button>)}</div>
        {view === "kanban" ? <div className="crmBoard">{board?.columns.map(column => <section className="crmColumn" key={column.stage.id}
          onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (archived) return; const id = Number(e.dataTransfer.getData("text/plain")); const deal = board.columns.flatMap(c => c.deals).find(d => d.id === id); if (deal) move(deal, column.stage.id); }}>
          <header style={{ borderTopColor: column.stage.color }}><strong>{column.stage.name}</strong><b>{column.total}</b></header>
          {column.deals.map(deal => <button draggable={!archived && can("move_deal")} onDragStart={e => e.dataTransfer.setData("text/plain", String(deal.id))}
            className="crmCard" key={deal.id} onClick={() => openDeal(deal.id)}><span className="crmCardName">{deal.contact.name}</span><small>{deal.name}</small>
            <strong>{money(deal.amount)}</strong><small>{deal.source_name || "Не определено"} · {deal.responsible_name || "Не назначен"}</small>
            <span className={`crmTaskBadge ${deal.task_state.toLowerCase()}`}>{stateLabels[deal.task_state]}{deal.next_task ? ` · ${when(deal.next_task.due_at)}` : ""}</span></button>)}
          {!column.deals.length && <p className="crmEmpty">Сделок нет</p>}
          {column.has_more && <button className="crmMore" onClick={() => setPage(page + 1)}>Показать ещё</button>}</section>)}</div>
          : <div className="crmList resultPanel"><table><thead><tr><th>Сделка</th><th>Контакт</th><th>Этап</th><th>Сумма</th><th>Следующее действие</th></tr></thead><tbody>
          {board?.columns.flatMap(c => c.deals).map(deal => <tr key={deal.id} onClick={() => openDeal(deal.id)}><td>{deal.name}</td><td>{deal.contact.name}</td><td>{deal.stage_name}</td><td>{money(deal.amount)}</td><td>{stateLabels[deal.task_state]}</td></tr>)}</tbody></table></div>}
        <div className="crmPager"><button disabled={page <= 1} onClick={() => setPage(page - 1)}>← Назад</button><span>Страница {page}</span><button onClick={() => setPage(page + 1)}>Далее →</button></div></>}
      {tab === "contacts" && <section className="crmList resultPanel"><h2>Контакты</h2><p>Один контакт может быть связан с несколькими сделками.</p>
        <table><thead><tr><th>Имя</th><th>Телефон</th><th>Email</th><th>Telegram</th><th>Компания</th></tr></thead><tbody>{contacts.map(c => <tr key={c.id}><td>{c.name}</td><td>{c.phones.join(", ") || "—"}</td><td>{c.emails.join(", ") || "—"}</td><td>{c.telegram || "—"}</td><td>{c.company || "—"}</td></tr>)}</tbody></table>
        {!contacts.length && <p className="crmEmpty">Пока нет контактов</p>}</section>}
      {tab === "tasks" && <section className="crmList resultPanel"><div className="crmSectionHead"><div><h2>Задачи</h2><p>Работа по текущим сделкам</p></div>{can("manage_tasks") && <button className="crmPrimary" onClick={() => { setSelected(null); setModal("task"); }}>＋ Создать задачу</button>}</div>
        <div className="crmTaskFilters"><select value={taskOwner} onChange={e => { setTaskOwner(e.target.value); setTaskPage(1); }}><option value="">Все ответственные</option>{team.map(m => <option value={m.id} key={m.id}>{m.display_name}</option>)}</select>
          <select value={taskType} onChange={e => { setTaskType(e.target.value); setTaskPage(1); }}><option value="">Все типы</option>{Object.entries(typeLabels).map(([key,label]) => <option value={key} key={key}>{label}</option>)}</select>
          <select value={taskStatus} onChange={e => { setTaskStatus(e.target.value); setTaskPage(1); }}><option value="">Все статусы</option><option value="OPEN">Открытые</option><option value="COMPLETED">Завершённые</option><option value="CANCELLED">Отменённые</option></select>
          <select value={taskPipeline} onChange={e => { setTaskPipeline(e.target.value); setTaskPage(1); }}><option value="">Все воронки</option>{pipelines.map(p => <option value={p.id} key={p.id}>{p.name}</option>)}</select>
          <select value={taskStage} onChange={e => { setTaskStage(e.target.value); setTaskPage(1); }}><option value="">Все этапы</option>{pipelines.flatMap(p => p.stages).map(s => <option value={s.id} key={s.id}>{s.name}</option>)}</select>
          <select value={taskSource} onChange={e => { setTaskSource(e.target.value); setTaskPage(1); }}><option value="">Все источники</option>{sources.map(s => <option value={s.id} key={s.id}>{s.name}</option>)}</select>
          <label>С <input type="date" value={taskDueStart} onChange={e => { setTaskDueStart(e.target.value); setTaskPage(1); }}/></label>
          <label>По <input type="date" value={taskDueEnd} onChange={e => { setTaskDueEnd(e.target.value); setTaskPage(1); }}/></label></div>
        <div className="crmTaskGroups">{["Просроченные", "Сегодня", "Предстоящие", "Завершенные", "Отменённые"].map((label, index) => { const subset = tasks.filter(t => index === 4 ? t.status === "CANCELLED" : index === 3 ? t.status === "COMPLETED" : t.status === "OPEN" &&
          (index === 0 ? new Date(t.due_at).getTime() < Date.now() && new Date(t.due_at).toDateString() !== new Date().toDateString() : index === 1 ? new Date(t.due_at).toDateString() === new Date().toDateString() : new Date(t.due_at).getTime() > Date.now() && new Date(t.due_at).toDateString() !== new Date().toDateString()));
          if (taskStatus && ((taskStatus === "OPEN" && index >= 3) || (taskStatus === "COMPLETED" && index !== 3) || (taskStatus === "CANCELLED" && index !== 4))) return null;
          return <div key={label}><h3>{label} <span>{subset.length}</span></h3>{subset.map(t => <div className="crmTaskRow" key={t.id} onClick={() => t.deal_id && openDeal(t.deal_id!)}><span>{when(t.due_at)}</span><b>{typeLabels[t.type_code] || t.type_code}</b><strong>{t.title}<small>{t.deal_name || t.contact_name || "—"} · {t.responsible_name || "Не назначен"}</small></strong><span>{t.status}</span>
            {t.status === "OPEN" && can("manage_tasks") && <button onClick={async e => { e.stopPropagation(); const result = window.prompt("Результат выполнения задачи"); if (result) { const done = await mutate(`/crm/tasks/${t.id}/complete`, "POST", { result }); if (done && window.confirm("Создать следующую задачу?")) { if (t.deal_id) await openDeal(t.deal_id); setModal("task"); } } }}>Завершить</button>}</div>)}
            {!subset.length && <p className="crmEmpty">Нет задач</p>}</div>; })}</div><div className="crmPager"><button disabled={taskPage <= 1} onClick={() => setTaskPage(taskPage - 1)}>← Назад</button><span>Страница {taskPage}</span><button disabled={tasks.length < 50} onClick={() => setTaskPage(taskPage + 1)}>Далее →</button></div></section>}
      {tab === "inbound" && <section className="crmList resultPanel"><h2>Входящие обращения</h2><p>Исходные данные заявок сохраняются независимо от последующих изменений CRM.</p>
        <div className="crmView">{(["NEW", "ACCEPTED", "REJECTED"] as const).map(status => <button key={status} className={inboundStatus === status ? "active" : ""} onClick={() => setInboundStatus(status)}>{status === "NEW" ? "Неразобранные" : status === "ACCEPTED" ? "Принятые" : "Отклонённые"}</button>)}</div>
        {inbound.map(item => <article className="crmInbound" key={item.id}><div><strong>{item.name || "Без имени"}</strong><small>{item.phone || item.email || String(item.raw_payload.contact || "Контакт не указан")} · {when(item.received_at)}</small>
          {item.potential_duplicates.length > 0 && <em>Возможный дубль: {item.potential_duplicates.map(c => c.name).join(", ")}</em>}</div>
          {inboundStatus === "NEW" ? <div><button onClick={async () => { const result = await mutate(`/crm/inbound/${item.id}/accept`, "POST", {}); if (result) setTab("deals"); }}>Принять</button>
          {item.potential_duplicates.map(c => <button key={c.id} onClick={async () => { const result = await mutate(`/crm/inbound/${item.id}/accept`, "POST", { contact_id: c.id }); if (result) setTab("deals"); }}>Связать с {c.name}</button>)}
          <button onClick={async () => { const contactId = window.prompt("ID другого существующего контакта"); if (contactId) { const result = await mutate(`/crm/inbound/${item.id}/accept`, "POST", { contact_id: Number(contactId) }); if (result) setTab("deals"); } }}>Другой контакт</button>
          <button onClick={() => { const reason = window.prompt("Причина: SPAM / DUPLICATE / TEST / INVALID / NOT_TARGET / OTHER"); if (reason) mutate(`/crm/inbound/${item.id}/reject`, "POST", { rejection_reason: reason }); }}>Отклонить</button></div> : <span>{inboundStatus === "ACCEPTED" ? "Принято" : "Отклонено"}</span>}</article>)}
        {!inbound.length && <p className="crmEmpty">Обращений с таким статусом нет</p>}</section>}
      {tab === "pipeline" && <section className="crmList resultPanel"><div className="crmSectionHead"><div><h2>Этапы воронки</h2><p>Рабочие названия можно менять; аналитический тип сохраняет смысл показателей.</p></div><button className="crmPrimary" onClick={() => setModal("stage")}>＋ Добавить этап</button></div>
        {(pipelines.find(p => p.id === (pipelineId || board?.pipeline.id))?.stages || []).map(s => <div className="crmStageRow" key={s.id} draggable onDragStart={e => e.dataTransfer.setData("text/plain", String(s.id))} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); reorderStages(Number(e.dataTransfer.getData("text/plain")), s.id); }}><span style={{ background: s.color }}/><strong>{s.name}</strong><small>{s.analytics_type}</small><button onClick={() => { const name = window.prompt("Новое название", s.name); if (name) mutate(`/crm/stages/${s.id}`, "PATCH", { name }); }}>Переименовать</button>
          <button onClick={() => { const position = window.prompt("Порядок", String(s.position || 0)); if (position !== null) mutate(`/crm/stages/${s.id}`, "PATCH", { position: Number(position) }); }}>Порядок</button>
          <button onClick={() => { const keys = window.prompt("Обязательные поля через запятую. Доступные: amount, name, responsible_user_id, source_id, пользовательские ключи.", (s.required_fields || []).join(", ")); if (keys !== null) mutate(`/crm/stages/${s.id}`, "PATCH", { required_fields: keys.split(",").map(v => v.trim()).filter(Boolean) }); }}>Обязательные поля</button>
          <button onClick={() => { if (window.confirm("Архивировать этап?")) mutate(`/crm/stages/${s.id}`, "PATCH", { archived: true }); }}>Архивировать</button></div>)}
        <div className="crmSectionHead crmFieldsHead"><h2>Пользовательские поля</h2>{can("manage_custom_fields") && <button onClick={() => setModal("field")}>＋ Добавить поле</button>}</div>
        {fields.map(field => <div className="crmStageRow" key={field.id}><strong>{field.name}</strong><small>{field.key} · {field.field_type}</small></div>)}
      </section>}
      </>}
    </main>
    {selected && <aside className="crmDrawer"><header><div><small>Сделка #{selected.id}</small><h2>{selected.name}</h2><strong>{money(selected.amount)}</strong></div><button onClick={() => setSelected(null)}>×</button></header>
      <div className="crmDrawerScroll"><div className="crmDrawerSection"><h3>Контакт</h3><strong>{selected.contact.name}</strong>{selected.contact.phones.length > 0 && <p>{selected.contact.phones.join(", ")}</p>}{selected.contact.emails.length > 0 && <p>{selected.contact.emails.join(", ")}</p>}{selected.contact.telegram && <p>Telegram: {selected.contact.telegram}</p>}</div>
      <div className="crmDrawerSection"><h3>Данные сделки</h3><p>Этап: <b>{selected.stage_name}</b></p><p>Происхождение: {selected.origin}</p><p>Дата создания: {when(selected.created_at)}</p>
        {can("edit_deal") && <><label>Ответственный <select value={selected.responsible_user_id || ""} onChange={async e => { const r = await mutate(`/crm/deals/${selected.id}`, "PATCH", { responsible_user_id: Number(e.target.value) || null }); if (r) openDeal(selected.id); }}><option value="">Не назначен</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
          <label>Сумма <input type="number" min="0" defaultValue={selected.amount ?? ""} onBlur={async e => { const value = e.target.value ? Number(e.target.value) : null; if (value !== selected.amount) { const r = await mutate(`/crm/deals/${selected.id}`, "PATCH", { amount: value }); if (r) openDeal(selected.id); } }}/></label></>}
        {can("move_deal") && <label>Этап <select value={selected.stage_id} onChange={async e => { await move(selected, Number(e.target.value)); openDeal(selected.id); }}>{pipelines.find(p => p.id === selected.pipeline_id)?.stages.map(s => <option value={s.id} key={s.id}>{s.name}</option>)}</select></label>}
        {fields.map(field => <label key={field.id}>{field.name}<input defaultValue={Array.isArray(selected.custom_fields[field.key]) ? (selected.custom_fields[field.key] as string[]).join(", ") : String(selected.custom_fields[field.key] ?? "")}
          onBlur={async e => { const value = parseField(field, e.target.value); if (value !== selected.custom_fields[field.key]) {
            const r = await mutate(`/crm/deals/${selected.id}`, "PATCH", { custom_fields: { ...selected.custom_fields, [field.key]: value } }); if (r) openDeal(selected.id); } }}/></label>)}
        {can("archive_deal") && (selected.archived_at || selected.analytics_type === "LOST") && <button disabled={busy} onClick={async () => { if (!window.confirm(selected.archived_at ? "Вернуть сделку из архива?" : "Перенести отказ в архив? Заявка и история сохранятся.")) return; const result = await mutate(`/crm/deals/${selected.id}/${selected.archived_at ? "restore" : "archive"}`, "POST", {}); if (result) setSelected(null); }}>{selected.archived_at ? "Вернуть из архива" : "В архив"}</button>}
      </div><div className="crmDrawerSection"><h3>Откуда пришёл клиент</h3><p>{selected.source_name || "Источник не определён"}</p>
        {can("change_attribution") && <label>Корректировка источника<select value={selected.source_id || ""} onChange={async e => { const r = await mutate(`/crm/deals/${selected.id}`, "PATCH", { source_id: Number(e.target.value) || null }); if (r) openDeal(selected.id); }}><option value="">Не определено</option>{sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
        {[["utm_source", "Источник рекламы"], ["utm_medium", "Канал"], ["utm_campaign", "Кампания"], ["landing_url", "Страница заявки"], ["referer", "Откуда перешёл"]].map(([key, label]) => {
          const value = selected.attribution_snapshot[key] || selected.form_data?.[key] || (key === "landing_url" ? selected.form_data?.page_url : null);
          return value ? <p key={key}>{label}: {String(value)}</p> : null;
        })}</div>
      {selected.form_data && <div className="crmDrawerSection"><h3>Что написал клиент</h3>{clientFields(selected.form_data).map(({label, value}) => <p style={{whiteSpace: "pre-wrap", overflowWrap: "anywhere"}} key={label}><b>{label}:</b> {value}</p>)}</div>}
      <div className="crmDrawerSection"><h3>Задачи</h3>{selected.tasks?.map(t => <div className="crmDrawerTask" key={t.id}><b>{t.title}</b><span>{when(t.due_at)} · {t.status}</span>{t.status === "OPEN" && can("manage_tasks") && <button onClick={async () => { const result = window.prompt("Результат выполнения задачи"); if (result) { const done = await mutate(`/crm/tasks/${t.id}/complete`, "POST", { result }); if (done) { openDeal(selected.id); if (window.confirm("Создать следующую задачу?")) setModal("task"); } } }}>Завершить</button>}</div>)}
        {can("manage_tasks") && <button onClick={() => setModal("task")}>＋ Добавить задачу</button>}</div>
      <div className="crmDrawerSection"><h3>Продажи</h3>{selected.sales?.map(s => <p key={s.id}>{money(s.amount)} · {when(s.occurred_at)}</p>)}{can("create_sale") && <button onClick={() => setModal("sale")}>＋ Подтвердить продажу</button>}</div>
      <div className="crmDrawerSection"><h3>История и комментарии</h3>{can("edit_deal") && <form onSubmit={async e => { e.preventDefault(); const form = e.currentTarget; const text = new FormData(form).get("text"); const r = await mutate(`/crm/deals/${selected.id}/comments`, "POST", { text }); if (r) { form.reset(); openDeal(selected.id); } }}><textarea name="text" required placeholder="Написать комментарий"/><button>Добавить</button></form>}
        {selected.activities?.map(a => <div className="crmActivity" key={a.id}><small>{when(a.created_at)} · {a.actor_name || "Система"}</small><strong>{a.event_type}</strong><p>{String(a.payload.text || a.payload.result || a.payload.title || "")}</p></div>)}</div></div></aside>}
    {modal && <div className="resultModalBackdrop" onClick={() => setModal(null)}><form className="resultModal" onClick={e => e.stopPropagation()} onSubmit={modal === "deal" ? submitDeal : modal === "task" ? submitTask : modal === "stage" ? submitStage : modal === "field" ? submitField : modal === "lost" ? submitLost : submitSale}>
      <header><h2>{modal === "deal" ? "Создать сделку" : modal === "task" ? "Создать задачу" : modal === "stage" ? "Добавить этап" : modal === "field" ? "Добавить поле" : modal === "lost" ? "Причина потери" : "Подтвердить продажу"}</h2><button type="button" onClick={() => setModal(null)}>×</button></header>
      {modal === "deal" && <><label>Название сделки<input name="name" required/></label><label>Существующий контакт<select name="contact_id"><option value="">Создать новый</option>{contacts.map(c => <option key={c.id} value={c.id}>{c.name} · {c.phones[0] || c.emails[0] || "—"}</option>)}</select></label><label>Имя нового контакта<input name="contact_name"/></label>
        <div className="resultModalGrid"><label>Телефон<input name="phone"/></label><label>Email<input name="email" type="email"/></label></div><p>Нужен телефон или email — иначе это ещё не лид.</p>
        <div className="resultModalGrid"><label>Воронка<select name="pipeline_id" value={newDealPipelineId || ""} onChange={e => setNewDealPipelineId(Number(e.target.value))}>{pipelines.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
          <label>Начальный этап<select name="stage_id">{pipelines.find(p => p.id === newDealPipelineId)?.stages.filter(s => s.analytics_type === "LEAD").map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label></div>
        <label>Дата сделки<input name="created_at" type="datetime-local"/></label>
        <label>Сумма<input name="amount" type="number" min="0"/></label><label>Ответственный<select name="owner"><option value="">Выберите</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
        <label>Источник<select name="source"><option value="">Не определено</option>{sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        {fields.map(field => <label key={field.id}>{field.name}<input name={`field:${field.key}`} type={field.field_type === "NUMBER" || field.field_type === "MONEY" ? "number" : "text"}/></label>)}<label>Комментарий<textarea name="comment"/></label>
        <label><input name="next_task" type="checkbox"/> Сразу создать задачу после сделки</label></>}
      {modal === "task" && <><label>Сделка<select name="deal_id" defaultValue={selected?.id || ""} required={!selected}><option value="">Выберите</option>{board?.columns.flatMap(c => c.deals).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
        <label>Тип<select name="type_code">{Object.entries(typeLabels).map(([k,v]) => <option value={k} key={k}>{v}</option>)}</select></label><label>Название<input name="title" required/></label>
        <label>Дата и время<input name="due_at" type="datetime-local" required/></label><label>Ответственный<select name="owner"><option value="">Ответственный сделки</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
        <label>Приоритет<select name="priority"><option value="NORMAL">Обычный</option><option value="HIGH">Высокий</option><option value="LOW">Низкий</option></select></label>
        <label>Длительность, мин<input name="duration_minutes" type="number" min="1"/></label><label>Описание<textarea name="description"/></label></>}
      {modal === "stage" && <><label>Название<input name="name" required/></label><label>Тип для аналитики<select name="analytics_type"><option value="LEAD">Lead</option><option value="QUALIFIED">Qualified</option><option value="WON">Won</option><option value="LOST">Lost</option></select></label>
        <label>Цвет<input name="color" type="color" defaultValue="#006bfd"/></label><label>Порядок<input name="position" type="number" defaultValue="1"/></label>
        <label>Обязательные поля (ключи через запятую)<input name="required_fields" placeholder="amount, budget"/></label></>}
      {modal === "field" && <><label>Ключ (латиницей)<input name="key" pattern="[a-z][a-z0-9_]+" required/></label><label>Название<input name="name" required/></label>
        <label>Тип<select name="field_type">{["TEXT", "NUMBER", "MONEY", "DATE", "DATETIME", "SELECT", "MULTISELECT", "BOOLEAN", "PHONE", "EMAIL"].map(x => <option key={x}>{x}</option>)}</select></label>
        <label>Варианты выбора (через запятую)<input name="options"/></label></>}
      {modal === "sale" && <><label>Сумма продажи<input name="amount" type="number" min="0.01" step="0.01" required/></label><label>Дата продажи<input name="occurred_at" type="datetime-local"/></label><label>Комментарий<textarea name="comment"/></label></>}
      {modal === "lost" && <><p>Сделка останется в истории и аналитике.</p><label>Причина<select name="reason" required><option value="">Выберите причину</option>{lostReasons.map(reason => <option key={reason.id} value={reason.id}>{reason.label}</option>)}</select></label><label>Комментарий<textarea name="comment"/></label></>}
      <button className="resultPrimary" disabled={busy}>Сохранить</button></form></div>}
  </div>;
}
