"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, API_URL } from "@/lib/api";
import { money, ProjectSidebar } from "@/components/reporting";
import { Automations } from "@/components/crm/automations";
import { ContactCard } from "@/components/crm/contact-card";
import { DealCard } from "@/components/crm/deal-card";
import { SalesReport } from "@/components/crm/report";
import { CompleteTaskModal, NewTaskModal } from "@/components/crm/task-modals";
import { Board, Contact, CustomField, Deal, days, Inbound, Pipeline, Project, rejectReasons, request, Source,
  Stage, stateLabels, Task, Team, typeLabels, when } from "@/components/crm/shared";
import "../result/result.css";
import "./crm.css";
import "@/components/crm/crm-pro.css";

type Tab = "deals" | "contacts" | "tasks" | "inbound" | "report" | "automation" | "pipeline";
type Modal = "deal" | "stage" | "field" | "lost" | "pipeline" | "task" | null;

export default function CrmPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [role, setRole] = useState("");
  const [permissions, setPermissions] = useState<string[]>([]);
  const [tab, setTab] = useState<Tab>("deals");
  const [view, setView] = useState<"kanban" | "list">("kanban");
  const [archived, setArchived] = useState(false);
  const [board, setBoard] = useState<Board | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [pipelineId, setPipelineId] = useState<number | null>(null);
  const [newDealPipelineId, setNewDealPipelineId] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [filterState, setFilterState] = useState("");
  const [search, setSearch] = useState("");
  const [owner, setOwner] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [mine, setMine] = useState(false);
  const [idleOnly, setIdleOnly] = useState(false);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [contactSearch, setContactSearch] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskPage, setTaskPage] = useState(1);
  const [taskOwner, setTaskOwner] = useState("");
  const [taskType, setTaskType] = useState("");
  const [taskStatus, setTaskStatus] = useState("OPEN");
  const [inbound, setInbound] = useState<Inbound[]>([]);
  const [inboundStatus, setInboundStatus] = useState<"NEW" | "ACCEPTED" | "REJECTED">("NEW");
  const [dealId, setDealId] = useState<number | null>(null);
  const [contactId, setContactId] = useState<number | null>(null);
  const [team, setTeam] = useState<Team[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [fields, setFields] = useState<CustomField[]>([]);
  const [lostReasons, setLostReasons] = useState<{ id: number; label: string }[]>([]);
  const [modal, setModal] = useState<Modal>(null);
  const [editStage, setEditStage] = useState<Stage | null>(null);
  const [editPipeline, setEditPipeline] = useState<Pipeline | null>(null);
  const [completing, setCompleting] = useState<Task | null>(null);
  const [rejecting, setRejecting] = useState<Inbound | null>(null);
  const [linking, setLinking] = useState<Inbound | null>(null);
  const [pendingMove, setPendingMove] = useState<{ deal: Deal; stageId: number } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const can = useCallback((name: string) => permissions.includes(name), [permissions]);
  const project = projects.find(p => p.id === projectId);
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  const currentPipelineId = pipelineId || board?.pipeline.id || pipelines[0]?.id || null;
  const currentPipeline = pipelines.find(p => p.id === currentPipelineId);

  useEffect(() => {
    Promise.all([api<Project[]>("/result/projects"), api<{ role: string; permissions: string[] }>("/portal/auth/me")])
      .then(([rows, user]) => { setProjects(rows); setRole(user.role); setPermissions(user.permissions);
        const params = new URLSearchParams(window.location.search);
        const desired = Number(params.get("project_id"));
        setProjectId(rows.find(p => p.id === desired)?.id || rows[0]?.id || null);
        const wanted = params.get("tab") as Tab | null;
        if (wanted) setTab(wanted); })
      .catch(e => setError(e.message));
  }, []);
  useEffect(() => { if (!projectId) return;
    setPage(1); setDealId(null);
    Promise.all([api<Pipeline[]>(`/crm/projects/${projectId}/pipelines`),
      api<Team[]>(`/portal/crm/team?project_id=${projectId}`),
      api<Source[]>(`/crm/projects/${projectId}/sources`).catch(() => []),
      api<CustomField[]>(`/crm/projects/${projectId}/custom-fields`).catch(() => [])])
      .then(([p, t, s, f]) => { setPipelines(p); setTeam(t); setSources(s); setFields(f);
        setPipelineId(current => current && p.some(x => x.id === current) ? current : null); })
      .catch(e => setError(e.message));
  }, [projectId, revision]);
  useEffect(() => { if (!projectId) return;
    api<{ id: number; label: string }[]>(`/settings/reasons?project_id=${projectId}`).then(setLostReasons).catch(() => setLostReasons([]));
  }, [projectId]);
  const boardQuery = useMemo(() => {
    const q = new URLSearchParams({ page: String(page), per_stage: "25" });
    if (archived) q.set("archived", "true");
    if (pipelineId) q.set("pipeline_id", String(pipelineId));
    if (filterState) q.set("state", filterState);
    if (search.trim()) q.set("search", search.trim());
    if (owner) q.set("responsible_user_id", owner);
    if (sourceFilter) q.set("source_id", sourceFilter);
    if (tagFilter.trim()) q.set("tag", tagFilter.trim());
    if (mine) q.set("mine", "true");
    if (idleOnly) q.set("idle_days", "3");
    return q.toString();
  }, [page, archived, pipelineId, filterState, search, owner, sourceFilter, tagFilter, mine, idleOnly]);
  useEffect(() => { if (!projectId) return; let active = true;
    const timer = setTimeout(() => api<Board>(`/crm/projects/${projectId}/board?${boardQuery}`)
      .then(b => { if (active) { setBoard(b); setSelectedIds([]); } }).catch(e => active && setError(e.message)), 200);
    return () => { active = false; clearTimeout(timer); };
  }, [projectId, boardQuery, revision]);
  useEffect(() => { if (!projectId) return;
    const leadId = Number(new URLSearchParams(window.location.search).get("lead_id"));
    if (leadId > 0) api<{ deal_id: number }>(`/crm/leads/${leadId}/deal`).then(row => setDealId(row.deal_id)).catch(() => {});
  }, [projectId]);
  useEffect(() => { if (!projectId) return; let active = true;
    if (tab === "contacts") { const q = new URLSearchParams({ limit: "100" }); if (contactSearch.trim()) q.set("search", contactSearch.trim());
      const timer = setTimeout(() => api<{ items: Contact[] }>(`/crm/projects/${projectId}/contacts?${q}`).then(v => active && setContacts(v.items)).catch(e => setError(e.message)), 200);
      return () => { active = false; clearTimeout(timer); }; }
    if (tab === "tasks") { const q = new URLSearchParams({ page: String(taskPage), limit: "100" });
      if (taskOwner) q.set("responsible_user_id", taskOwner); if (taskType) q.set("type_code", taskType); if (taskStatus) q.set("status", taskStatus);
      api<{ items: Task[] }>(`/crm/projects/${projectId}/tasks?${q}`).then(v => active && setTasks(v.items)).catch(e => setError(e.message)); }
    if (tab === "inbound") api<{ items: Inbound[] }>(`/crm/projects/${projectId}/inbound?status=${inboundStatus}`).then(v => active && setInbound(v.items)).catch(e => setError(e.message));
    return () => { active = false; };
  }, [projectId, tab, inboundStatus, revision, taskPage, taskOwner, taskType, taskStatus, contactSearch]);

  async function mutate<T>(path: string, method: string, body?: unknown, message?: string): Promise<T | null> {
    setBusy(true); setError(""); setNotice("");
    try { const value = await request<T>(path, method, body ?? {}); refresh(); if (message) setNotice(message); return value; }
    catch (e) { setError((e as Error).message); return null; } finally { setBusy(false); }
  }
  async function move(deal: Deal, stageId: number, lostReasonId?: number, lostComment?: string) {
    if (deal.stage_id === stageId) return;
    const target = board?.columns.find(c => c.stage.id === stageId)?.stage;
    if (target?.analytics_type === "LOST" && !lostReasonId) { setPendingMove({ deal, stageId }); setModal("lost"); return; }
    const before = board;
    if (before) setBoard({ ...before, columns: before.columns.map(column => column.stage.id === deal.stage_id
      ? { ...column, deals: column.deals.filter(item => item.id !== deal.id), total: column.total - 1, amount: column.amount - (deal.amount || 0) }
      : column.stage.id === stageId ? { ...column, deals: [{ ...deal, stage_id: stageId, stage_name: column.stage.name, days_in_stage: 0 }, ...column.deals], total: column.total + 1, amount: column.amount + (deal.amount || 0) }
      : column) });
    const result = await mutate<{ sale_required: boolean; automations?: string[] }>(`/crm/deals/${deal.id}/move`, "POST",
      { stage_id: stageId, lost_reason_id: lostReasonId, lost_comment: lostComment });
    if (!result) setBoard(before);
    if (result?.automations?.length) setNotice(`⚡ ${result.automations.join("; ")}`);
    if (result?.sale_required) setDealId(deal.id);
    return result;
  }
  async function submitDeal(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!projectId) return;
    const f = new FormData(event.currentTarget);
    const custom_fields = Object.fromEntries(fields.map(field => [field.key, parseField(field, String(f.get(`field:${field.key}`) || ""))]).filter(([, v]) => v !== null));
    const payload = { project_id: projectId, name: f.get("name"), contact_id: f.get("contact_id") ? Number(f.get("contact_id")) : null,
      contact_name: f.get("contact_name") || null, phone: f.get("phone") || null, email: f.get("email") || null,
      amount: f.get("amount") ? Number(f.get("amount")) : null, pipeline_id: f.get("pipeline_id") ? Number(f.get("pipeline_id")) : null,
      stage_id: f.get("stage_id") ? Number(f.get("stage_id")) : null,
      created_at: f.get("created_at") ? new Date(String(f.get("created_at"))).toISOString() : null,
      responsible_user_id: f.get("owner") ? Number(f.get("owner")) : null, source_id: f.get("source") ? Number(f.get("source")) : null,
      comment: f.get("comment") || null, custom_fields };
    const result = await mutate<{ id: number; potential_duplicates: Contact[] }>("/crm/deals", "POST", payload);
    if (result) { setModal(null); if (result.potential_duplicates.length) setNotice("Сделка создана. Есть похожий контакт — откройте карточку контакта, чтобы объединить дубли."); setDealId(result.id); }
  }
  async function submitStage(event: FormEvent<HTMLFormElement>) { event.preventDefault();
    const f = new FormData(event.currentTarget);
    const body = { name: f.get("name"), color: f.get("color"),
      required_fields: String(f.get("required_fields") || "").split(",").map(v => v.trim()).filter(Boolean) };
    const result = editStage ? await mutate(`/crm/stages/${editStage.id}`, "PATCH", body, "Этап сохранён")
      : await mutate(`/crm/pipelines/${currentPipelineId}/stages`, "POST", { ...body, analytics_type: f.get("analytics_type"),
        position: Math.max(0, ...(currentPipeline?.stages || []).filter(s => s.analytics_type !== "WON" && s.analytics_type !== "LOST").map(s => s.position || 0)) + 1 }, "Этап добавлен");
    if (result) { setModal(null); setEditStage(null); }
  }
  async function submitPipeline(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!projectId) return;
    const f = new FormData(event.currentTarget);
    const result = editPipeline ? await mutate(`/crm/pipelines/${editPipeline.id}`, "PATCH", { name: f.get("name") }, "Воронка переименована")
      : await mutate<{ id: number }>(`/crm/projects/${projectId}/pipelines`, "POST", { name: f.get("name"),
        copy_from_pipeline_id: f.get("copy") ? Number(f.get("copy")) : null }, "Воронка создана");
    if (result) { setModal(null); if (!editPipeline && typeof result === "object" && "id" in result) setPipelineId((result as { id: number }).id); setEditPipeline(null); }
  }
  async function reorderStages(draggedId: number, targetId: number) {
    if (!currentPipeline || draggedId === targetId) return;
    const ordered = [...currentPipeline.stages].sort((a, b) => (a.position || 0) - (b.position || 0));
    const from = ordered.findIndex(s => s.id === draggedId), to = ordered.findIndex(s => s.id === targetId);
    if (from < 0 || to < 0) return;
    ordered.splice(to, 0, ordered.splice(from, 1)[0]);
    setBusy(true); setError("");
    try { for (const [position, stage] of ordered.entries()) await request(`/crm/stages/${stage.id}`, "PATCH", { position }); refresh(); }
    catch (e) { setError((e as Error).message); refresh(); } finally { setBusy(false); }
  }
  async function submitLost(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!pendingMove) return;
    const f = new FormData(event.currentTarget);
    const result = await move(pendingMove.deal, pendingMove.stageId, Number(f.get("reason")), String(f.get("comment") || ""));
    if (result) { setPendingMove(null); setModal(null); }
  }
  function parseField(field: CustomField, raw: string): unknown {
    if (!raw) return null;
    if (field.field_type === "NUMBER" || field.field_type === "MONEY") return Number(raw);
    if (field.field_type === "BOOLEAN") return raw === "true";
    if (field.field_type === "MULTISELECT") return raw.split(",").map(v => v.trim()).filter(Boolean);
    return raw;
  }
  async function submitField(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!projectId) return;
    const f = new FormData(event.currentTarget);
    const result = await mutate(`/crm/projects/${projectId}/custom-fields`, "POST", { key: f.get("key"), name: f.get("name"),
      field_type: f.get("field_type"), options: String(f.get("options") || "").split(",").map(v => v.trim()).filter(Boolean) }, "Поле добавлено");
    if (result) setModal(null);
  }
  async function bulk(action: string, extra: Record<string, unknown> = {}) {
    if (!selectedIds.length) return;
    const result = await mutate<{ updated: number; skipped: number[] }>("/crm/deals/bulk", "POST", { deal_ids: selectedIds, action, ...extra });
    if (result) setNotice(`Обновлено сделок: ${result.updated}${result.skipped.length ? `. Пропущено ${result.skipped.length}: закрытие и откат этапа — только из карточки` : ""}`);
  }
  async function exportCsv() {
    if (!projectId) return;
    try { const response = await fetch(`${API_URL}/api/crm/projects/${projectId}/deals.csv${currentPipelineId ? `?pipeline_id=${currentPipelineId}` : ""}`, { credentials: "include" });
      if (!response.ok) throw new Error("Не удалось выгрузить сделки");
      const url = URL.createObjectURL(await response.blob()); const a = document.createElement("a");
      a.href = url; a.download = `deals-${projectId}.csv`; a.click(); URL.revokeObjectURL(url);
    } catch (e) { setError((e as Error).message); }
  }

  const allDeals = board?.columns.flatMap(c => c.deals) || [];
  const totalAmount = board?.columns.filter(c => c.stage.analytics_type !== "LOST").reduce((sum, c) => sum + (c.amount || 0), 0) || 0;
  const activeFilters = [owner, sourceFilter, tagFilter, search].filter(Boolean).length + (mine ? 1 : 0) + (idleOnly ? 1 : 0);
  const taskGroups = useMemo(() => {
    const today = new Date().toDateString();
    const open = tasks.filter(t => t.status === "OPEN");
    return [["Просроченные", open.filter(t => new Date(t.due_at).getTime() < Date.now() && new Date(t.due_at).toDateString() !== today), "overdue"],
      ["Сегодня", open.filter(t => new Date(t.due_at).toDateString() === today), "today"],
      ["Предстоящие", open.filter(t => new Date(t.due_at).getTime() > Date.now() && new Date(t.due_at).toDateString() !== today), ""],
      ["Завершённые", tasks.filter(t => t.status === "COMPLETED"), "done"], ["Отменённые", tasks.filter(t => t.status === "CANCELLED"), "done"]] as [string, Task[], string][];
  }, [tasks]);

  const card = (deal: Deal) => <button draggable={!archived && can("move_deal")} onDragStart={e => e.dataTransfer.setData("text/plain", String(deal.id))}
    className={`crmCard ${(deal.idle_days ?? 0) >= 3 && deal.analytics_type !== "WON" && deal.analytics_type !== "LOST" ? "idle" : ""}`} key={deal.id} onClick={() => setDealId(deal.id)}>
    <span className="crmCardTop"><span className="crmCardName">{deal.contact.name}</span><strong>{money(deal.amount)}</strong></span>
    <small>{deal.name}</small>
    {deal.tags?.length > 0 && <span className="crmCardTags">{deal.tags.slice(0, 3).map(t => <i key={t}>#{t}</i>)}</span>}
    <small>{deal.source_name || "Источник не определён"} · {deal.responsible_name || "Не назначен"}</small>
    <span className="crmCardFoot"><span className={`crmTaskBadge ${deal.task_state.toLowerCase()}`}>{stateLabels[deal.task_state]}{deal.next_task ? ` · ${when(deal.next_task.due_at)}` : ""}</span>
      <span className="crmCardAge" title="Дней на этапе">{days(deal.days_in_stage)}</span></span></button>;

  return <div className="resultShell"><ProjectSidebar project={project} projectId={projectId} active="crm" role={role}/>
    <main className="resultMain crmMain"><div className="resultBreadcrumb">StepToLead <span>/</span> CRM</div>
      <header className="crmHeader"><div><h1>CRM</h1><p>Заявки, клиенты, сделки и работа менеджеров — в одной воронке с рекламой и аналитикой.</p></div>
        <div className="crmHeaderActions">{projects.length > 1 && <label>Проект <select value={projectId || ""} onChange={e => setProjectId(Number(e.target.value))}>{projects.map(p => <option key={p.id} value={p.id}>{p.organization_name} · {p.name}</option>)}</select></label>}
          {can("create_deal") && <button className="crmPrimary" onClick={() => { setNewDealPipelineId(currentPipelineId); setModal("deal"); }}>＋ Сделка</button>}</div></header>
      {error && <div className="resultError" role="alert">{error} <button onClick={() => setError("")}>×</button></div>}
      {notice && <div className="crmOkBar" role="status">{notice} <button onClick={() => setNotice("")}>×</button></div>}
      {!projectId ? <div className="resultLoading">{error.includes("401") || error.includes("вход") ? <><p>Для работы с CRM войдите в клиентский кабинет.</p><Link href="/portal/login?next=%2Fcrm">Войти →</Link></> : "Нет доступного проекта"}</div> : <>
      <div className="crmTabs">{([["deals", "▣ Сделки"], ["inbound", "Неразобранное"], ["tasks", "◷ Задачи"], ["contacts", "♙ Контакты"], ["report", "📊 Аналитика продаж"],
        ...(can("manage_pipeline") ? [["automation", "⚡ Автоматизация"], ["pipeline", "⚙ Воронки и поля"]] : [])] as [Tab, string][]).map(([key, label]) =>
        <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{label}{key === "inbound" && board?.inbound_count ? <b>{board.inbound_count}</b> : null}</button>)}</div>

      {tab === "deals" && <>
        <div className="crmFilters crmFiltersPro">
          <select value={currentPipelineId || ""} onChange={e => { setPipelineId(Number(e.target.value)); setPage(1); }} aria-label="Воронка">{pipelines.map(p => <option value={p.id} key={p.id}>{p.name}</option>)}</select>
          <input placeholder="Поиск: сделка, клиент, телефон, email" value={search} onChange={e => { setSearch(e.target.value); setPage(1); }}/>
          <select value={owner} onChange={e => { setOwner(e.target.value); setMine(false); setPage(1); }} aria-label="Ответственный"><option value="">Все менеджеры</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select>
          <select value={sourceFilter} onChange={e => { setSourceFilter(e.target.value); setPage(1); }} aria-label="Источник"><option value="">Все источники</option>{sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select>
          <input className="crmTagInput" placeholder="#тег" value={tagFilter} onChange={e => { setTagFilter(e.target.value.replace(/^#/, "")); setPage(1); }}/>
          <button className={`crmToggle ${mine ? "on" : ""}`} onClick={() => { setMine(!mine); setOwner(""); setPage(1); }}>Мои</button>
          <button className={`crmToggle ${idleOnly ? "on" : ""}`} onClick={() => { setIdleOnly(!idleOnly); setPage(1); }} title="Нет активности 3 дня и больше">Без движения</button>
          <select aria-label="Архив сделок" value={archived ? "archive" : "active"} onChange={e => { setArchived(e.target.value === "archive"); setPage(1); setFilterState(""); }}><option value="active">Активные</option><option value="archive">Архив</option></select>
          {activeFilters > 0 && <button className="crmLinkButton" onClick={() => { setOwner(""); setSourceFilter(""); setTagFilter(""); setSearch(""); setMine(false); setIdleOnly(false); setPage(1); }}>Сбросить ({activeFilters})</button>}
          <div className="crmView"><button className={view === "kanban" ? "active" : ""} onClick={() => setView("kanban")}>▦ Канбан</button><button className={view === "list" ? "active" : ""} onClick={() => setView("list")}>☷ Список</button></div>
          <button className="crmGhost" onClick={exportCsv} title="Выгрузить сделки воронки в CSV (Excel)">⇩ CSV</button></div>
        <div className="crmCounters">{Object.entries(stateLabels).map(([key, label]) => <button key={key} className={`crmCount ${key.toLowerCase()} ${filterState === key ? "active" : ""}`}
          onClick={() => { setFilterState(filterState === key ? "" : key); setPage(1); }}><strong>{board?.control[key] ?? "—"}</strong><span>{label}</span></button>)}
          <div className="crmCount crmCountSum"><strong>{money(totalAmount)}</strong><span>в воронке</span></div></div>
        {view === "kanban" ? <div className="crmBoard">{board?.columns.map(column => <section className="crmColumn" key={column.stage.id}
          onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (archived) return; const id = Number(e.dataTransfer.getData("text/plain")); const deal = allDeals.find(d => d.id === id); if (deal) move(deal, column.stage.id); }}>
          <header style={{ borderTopColor: column.stage.color }}><div><strong>{column.stage.name}</strong><small>{money(column.amount)}</small></div><b>{column.total}</b></header>
          {column.deals.map(card)}
          {!column.deals.length && <p className="crmEmpty">Перетащите сделку сюда</p>}
          {column.has_more && <button className="crmMore" onClick={() => setPage(page + 1)}>Показать ещё</button>}</section>)}</div>
          : <div className="crmList resultPanel">
            {selectedIds.length > 0 && <div className="crmBulk"><b>Выбрано: {selectedIds.length}</b>
              {can("move_deal") && <select value="" onChange={e => e.target.value && bulk("move", { stage_id: Number(e.target.value) })}><option value="">Перевести на этап…</option>{currentPipeline?.stages.filter(s => s.analytics_type !== "WON" && s.analytics_type !== "LOST").map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select>}
              {can("edit_deal") && <select value="" onChange={e => e.target.value && bulk("responsible", { responsible_user_id: Number(e.target.value) })}><option value="">Назначить ответственного…</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select>}
              {can("edit_deal") && <form onSubmit={e => { e.preventDefault(); const tag = String(new FormData(e.currentTarget).get("tag") || "").trim(); if (tag) { bulk("tag_add", { tag }); e.currentTarget.reset(); } }}><input name="tag" placeholder="＋ тег" maxLength={40}/></form>}
              {can("archive_deal") && <button onClick={() => bulk("archive")}>В архив (закрытые)</button>}
              <button className="crmLinkButton" onClick={() => setSelectedIds([])}>Снять выбор</button></div>}
            <table><thead><tr><th><input type="checkbox" aria-label="Выбрать все" checked={allDeals.length > 0 && selectedIds.length === allDeals.length} onChange={e => setSelectedIds(e.target.checked ? allDeals.map(d => d.id) : [])}/></th><th>Сделка</th><th>Клиент</th><th>Этап</th><th>Сумма</th><th>Ответственный</th><th>Источник</th><th>На этапе</th><th>Следующий шаг</th></tr></thead><tbody>
            {allDeals.map(deal => <tr key={deal.id} className={selectedIds.includes(deal.id) ? "selected" : ""} onClick={() => setDealId(deal.id)}>
              <td onClick={e => e.stopPropagation()}><input type="checkbox" checked={selectedIds.includes(deal.id)} onChange={e => setSelectedIds(ids => e.target.checked ? [...ids, deal.id] : ids.filter(id => id !== deal.id))}/></td>
              <td><b>{deal.name}</b>{deal.tags?.length > 0 && <small className="crmInlineTags">{deal.tags.map(t => `#${t}`).join(" ")}</small>}</td><td>{deal.contact.name}</td><td>{deal.stage_name}</td><td>{money(deal.amount)}</td>
              <td>{deal.responsible_name || "—"}</td><td>{deal.source_name || "—"}</td><td className={(deal.idle_days ?? 0) >= 3 ? "crmBad" : ""}>{days(deal.days_in_stage)}</td>
              <td><span className={`crmTaskBadge ${deal.task_state.toLowerCase()}`}>{stateLabels[deal.task_state]}</span>{deal.next_task && <small> {when(deal.next_task.due_at)}</small>}</td></tr>)}</tbody></table>
            {!allDeals.length && <p className="crmEmpty">Сделок по фильтру нет</p>}</div>}
        <div className="crmPager"><button disabled={page <= 1} onClick={() => setPage(page - 1)}>← Назад</button><span>Страница {page}</span><button disabled={!board?.columns.some(c => c.has_more)} onClick={() => setPage(page + 1)}>Далее →</button></div></>}

      {tab === "contacts" && <section className="crmList resultPanel"><div className="crmSectionHead"><div><h2>Контакты</h2><p>Клиент и все его сделки, задачи и покупки. Нажмите на строку, чтобы открыть карточку.</p></div>
        <input className="crmSearch" placeholder="Имя, телефон, email, Telegram" value={contactSearch} onChange={e => setContactSearch(e.target.value)}/></div>
        <table><thead><tr><th>Имя</th><th>Телефон</th><th>Email</th><th>Telegram</th><th>Компания</th><th>Теги</th></tr></thead><tbody>{contacts.map(c => <tr key={c.id} onClick={() => setContactId(c.id)}><td><b>{c.name}</b></td><td>{c.phones.join(", ") || "—"}</td><td>{c.emails.join(", ") || "—"}</td><td>{c.telegram || "—"}</td><td>{c.company || "—"}</td><td>{(c.tags || []).map(t => `#${t}`).join(" ") || "—"}</td></tr>)}</tbody></table>
        {!contacts.length && <p className="crmEmpty">Контакты не найдены</p>}</section>}

      {tab === "tasks" && <section className="crmList resultPanel"><div className="crmSectionHead"><div><h2>Задачи</h2><p>План работы менеджеров. Завершая задачу, сразу ставьте следующую — так ни одна сделка не повиснет.</p></div>{can("manage_tasks") && <button className="crmPrimary" onClick={() => setModal("task")}>＋ Задача</button>}</div>
        <div className="crmTaskFilters"><select value={taskOwner} onChange={e => { setTaskOwner(e.target.value); setTaskPage(1); }}><option value="">Все ответственные</option>{team.map(m => <option value={m.id} key={m.id}>{m.display_name}</option>)}</select>
          <select value={taskType} onChange={e => { setTaskType(e.target.value); setTaskPage(1); }}><option value="">Все типы</option>{Object.entries(typeLabels).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select>
          <select value={taskStatus} onChange={e => { setTaskStatus(e.target.value); setTaskPage(1); }}><option value="">Все статусы</option><option value="OPEN">Открытые</option><option value="COMPLETED">Завершённые</option><option value="CANCELLED">Отменённые</option></select></div>
        <div className="crmTaskGroups">{taskGroups.map(([label, subset, tone]) => (taskStatus === "OPEN" && tone === "done") || (taskStatus && taskStatus !== "OPEN" && tone !== "done") ? null :
          <div key={label} className={tone}><h3>{label} <span>{subset.length}</span></h3>{subset.map(t => <div className="crmTaskRow" key={t.id} onClick={() => t.deal_id && setDealId(t.deal_id)}>
            <span>{when(t.due_at)}</span><b>{typeLabels[t.type_code] || t.type_code}</b><strong>{t.title}<small>{t.deal_name || t.contact_name || "—"} · {t.responsible_name || "Не назначен"}{t.result ? ` · Итог: ${t.result}` : ""}</small></strong>
            <span>{t.priority === "HIGH" ? "❗" : ""}</span>
            {t.status === "OPEN" && can("manage_tasks") ? <button onClick={e => { e.stopPropagation(); setCompleting(t); }}>Завершить</button> : <span/>}</div>)}
            {!subset.length && <p className="crmEmpty">Нет задач</p>}</div>)}</div>
        <div className="crmPager"><button disabled={taskPage <= 1} onClick={() => setTaskPage(taskPage - 1)}>← Назад</button><span>Страница {taskPage}</span><button disabled={tasks.length < 100} onClick={() => setTaskPage(taskPage + 1)}>Далее →</button></div></section>}

      {tab === "inbound" && <section className="crmList resultPanel"><div className="crmSectionHead"><div><h2>Неразобранное</h2><p>Заявки с сайта, Авито и интеграций. Примите в работу, свяжите с существующим клиентом или отклоните. Исходные данные сохраняются.</p></div>
        <div className="crmView">{(["NEW", "ACCEPTED", "REJECTED"] as const).map(status => <button key={status} className={inboundStatus === status ? "active" : ""} onClick={() => setInboundStatus(status)}>{status === "NEW" ? "Новые" : status === "ACCEPTED" ? "Принятые" : "Отклонённые"}</button>)}</div></div>
        {inbound.map(item => { const p = item.raw_payload || {}; const minutesAgo = Math.round((Date.now() - new Date(item.received_at).getTime()) / 60000);
          return <article className="crmInbound crmInboundPro" key={item.id}><div>
            <strong>{item.name || "Без имени"}</strong>
            <small>{[item.phone, item.email, typeof p.contact === "string" && !String(p.contact).startsWith("http") ? p.contact : null].filter(Boolean).join(" · ") || "Контакт не указан"}{p.contact_method ? ` · ${String(p.contact_method)}` : ""}</small>
            <small>{String(p.external_source || p.source || "Интеграция")}{p.item_title ? ` · ${String(p.item_title)}` : ""}{p.utm_source ? ` · ${String(p.utm_source)}${p.utm_campaign ? ` / ${String(p.utm_campaign)}` : ""}` : ""} · {when(item.received_at)}
              {inboundStatus === "NEW" && <b className={minutesAgo > 15 ? "crmBad" : "crmGood"}> · ждёт {minutesAgo < 60 ? `${minutesAgo} мин` : `${Math.round(minutesAgo / 60)} ч`}</b>}</small>
            {Boolean(p.notes || p.comment) && <p className="crmPre">{String(p.notes || p.comment)}</p>}
            {item.potential_duplicates.length > 0 && <em>Возможный дубль: {item.potential_duplicates.map(c => c.name).join(", ")}</em>}</div>
            {inboundStatus === "NEW" ? <div className="crmInboundActions"><button className="crmPrimary" disabled={busy} onClick={() => mutate<{ deal_id: number }>(`/crm/inbound/${item.id}/accept`, "POST", {}).then(r => r && setDealId(r.deal_id))}>Принять в работу</button>
              {item.potential_duplicates.map(c => <button key={c.id} disabled={busy} onClick={() => mutate<{ deal_id: number }>(`/crm/inbound/${item.id}/accept`, "POST", { contact_id: c.id }).then(r => r && setDealId(r.deal_id))}>Связать с «{c.name}»</button>)}
              <button onClick={() => setLinking(item)}>Другой клиент…</button><button className="crmDanger" onClick={() => setRejecting(item)}>Отклонить</button></div>
              : <span className="crmMuted">{inboundStatus === "ACCEPTED" ? "Принято" : "Отклонено"}</span>}</article>; })}
        {!inbound.length && <p className="crmEmpty">{inboundStatus === "NEW" ? "Всё разобрано 👌" : "Обращений с таким статусом нет"}</p>}</section>}

      {tab === "report" && <SalesReport projectId={projectId} pipelines={pipelines} pipelineId={currentPipelineId}/>}
      {tab === "automation" && <Automations projectId={projectId} pipelines={pipelines} pipelineId={currentPipelineId} team={team} sources={sources} canManage={can("manage_pipeline")}/>}

      {tab === "pipeline" && <section className="crmList resultPanel">
        <div className="crmSectionHead"><div><h2>Воронки</h2><p>Отдельные воронки для разных продуктов или процессов (например, «Продажи» и «Повторные продажи»).</p></div><button className="crmPrimary" onClick={() => { setEditPipeline(null); setModal("pipeline"); }}>＋ Воронка</button></div>
        <div className="crmPipelines">{pipelines.map(p => <div key={p.id} className={p.id === currentPipelineId ? "active" : ""}><button className="crmPipelineName" onClick={() => setPipelineId(p.id)}>{p.name}{p.is_default && <small>основная</small>}</button>
          <span>{p.stages.length} этапов</span><button onClick={() => { setEditPipeline(p); setModal("pipeline"); }}>Переименовать</button>
          {!p.is_default && <button onClick={() => mutate(`/crm/pipelines/${p.id}`, "PATCH", { is_default: true }, "Основная воронка изменена")}>Сделать основной</button>}
          {!p.is_default && <button className="crmDanger" onClick={() => window.confirm(`Архивировать воронку «${p.name}»?`) && mutate(`/crm/pipelines/${p.id}`, "PATCH", { archived: true }, "Воронка архивирована")}>Архивировать</button>}</div>)}</div>
        <div className="crmSectionHead"><div><h2>Этапы: {currentPipeline?.name}</h2><p>Перетаскивайте, чтобы поменять порядок. Аналитический тип (Лид / Квал / Продажа / Отказ) сохраняет смысл отчётов при любых названиях.</p></div><button className="crmPrimary" onClick={() => { setEditStage(null); setModal("stage"); }}>＋ Этап</button></div>
        {(currentPipeline?.stages || []).map(s => <div className="crmStageRow" key={s.id} draggable onDragStart={e => e.dataTransfer.setData("text/plain", String(s.id))} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); reorderStages(Number(e.dataTransfer.getData("text/plain")), s.id); }}>
          <span style={{ background: s.color }}/><strong>{s.name}</strong><small>{{ LEAD: "Лид", QUALIFIED: "Квалифицирован", WON: "Продажа", LOST: "Отказ" }[s.analytics_type] || s.analytics_type}{s.required_fields?.length ? ` · обязательно: ${s.required_fields.join(", ")}` : ""}</small>
          <button onClick={() => { setEditStage(s); setModal("stage"); }}>Настроить</button>
          <button onClick={() => window.confirm("Архивировать этап?") && mutate(`/crm/stages/${s.id}`, "PATCH", { archived: true }, "Этап архивирован")}>Архивировать</button></div>)}
        <div className="crmSectionHead crmFieldsHead"><div><h2>Поля сделки</h2><p>Свои поля: бюджет клиента, ЛПР, сроки — их можно сделать обязательными на этапе.</p></div>{can("manage_custom_fields") && <button className="crmPrimary" onClick={() => setModal("field")}>＋ Поле</button>}</div>
        {fields.map(field => <div className="crmStageRow" key={field.id}><strong>{field.name}</strong><small>{field.key} · {field.field_type}{field.options?.length ? ` · ${field.options.join(", ")}` : ""}</small></div>)}
        {!fields.length && <p className="crmEmpty">Пока нет своих полей</p>}
      </section>}
      </>}
    </main>

    {dealId && projectId && <><div className="crmCardBackdrop" onClick={() => setDealId(null)}/><DealCard dealId={dealId} projectId={projectId} pipelines={pipelines} team={team} sources={sources} fields={fields} lostReasons={lostReasons} can={can}
      onClose={() => setDealId(null)} onChanged={refresh} onOpenContact={id => setContactId(id)}/></>}
    {contactId && <ContactCard contactId={contactId} can={can} onClose={() => setContactId(null)} onOpenDeal={id => { setContactId(null); setDealId(id); }} onChanged={refresh}/>}
    {completing && <CompleteTaskModal task={completing} team={team} onClose={() => setCompleting(null)} onDone={() => { setCompleting(null); refresh(); setNotice("Задача завершена"); }}/>}
    {modal === "task" && projectId && <NewTaskModal projectId={projectId} team={team} deals={allDeals} onClose={() => setModal(null)} onDone={() => { setModal(null); refresh(); setNotice("Задача поставлена"); }}/>}
    {rejecting && <InboundReject item={rejecting} onClose={() => setRejecting(null)} onSubmit={async reason => { const ok = await mutate(`/crm/inbound/${rejecting.id}/reject`, "POST", { rejection_reason: reason }, "Заявка отклонена"); if (ok) setRejecting(null); }}/>}
    {linking && projectId && <InboundLink item={linking} projectId={projectId} onClose={() => setLinking(null)} onSubmit={async id => { const r = await mutate<{ deal_id: number }>(`/crm/inbound/${linking.id}/accept`, "POST", { contact_id: id }); if (r) { setLinking(null); setDealId(r.deal_id); } }}/>}
    {modal && modal !== "task" && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) { setModal(null); setEditStage(null); setEditPipeline(null); } }}>
      <form className="resultModal crmModal" onSubmit={modal === "deal" ? submitDeal : modal === "stage" ? submitStage : modal === "field" ? submitField : modal === "pipeline" ? submitPipeline : submitLost}>
      <header><h2>{modal === "deal" ? "Новая сделка" : modal === "stage" ? (editStage ? `Этап «${editStage.name}»` : "Новый этап") : modal === "field" ? "Новое поле" : modal === "pipeline" ? (editPipeline ? "Переименовать воронку" : "Новая воронка") : "Почему сделка проиграна?"}</h2><button type="button" onClick={() => { setModal(null); setEditStage(null); setEditPipeline(null); }}>×</button></header>
      {modal === "deal" && <><label>Название сделки<input name="name" required minLength={2} placeholder="Например, Кухня 3 м — Иванова"/></label>
        <ContactPicker projectId={projectId!}/>
        <div className="resultModalGrid"><label>Телефон<input name="phone"/></label><label>Email<input name="email" type="email"/></label></div><p className="crmModalLead">Для нового клиента нужен телефон или email.</p>
        <div className="resultModalGrid"><label>Воронка<select name="pipeline_id" value={newDealPipelineId || ""} onChange={e => setNewDealPipelineId(Number(e.target.value))}>{pipelines.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
          <label>Этап<select name="stage_id">{pipelines.find(p => p.id === newDealPipelineId)?.stages.filter(s => s.analytics_type === "LEAD").map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label></div>
        <div className="resultModalGrid"><label>Бюджет, ₽<input name="amount" type="number" min="0"/></label><label>Ответственный<select name="owner"><option value="">Я</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label></div>
        <div className="resultModalGrid"><label>Источник<select name="source"><option value="">Не определено</option>{sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label><label>Дата обращения<input name="created_at" type="datetime-local"/></label></div>
        {fields.map(field => <label key={field.id}>{field.name}{field.field_type === "SELECT" ? <select name={`field:${field.key}`}><option value="">—</option>{(field.options || []).map(o => <option key={o}>{o}</option>)}</select> : <input name={`field:${field.key}`} type={field.field_type === "NUMBER" || field.field_type === "MONEY" ? "number" : "text"}/>}</label>)}
        <label>Комментарий<textarea name="comment"/></label></>}
      {modal === "stage" && <><label>Название<input name="name" required defaultValue={editStage?.name || ""}/></label>
        {!editStage && <label>Смысл этапа для аналитики<select name="analytics_type"><option value="LEAD">Лид (до квалификации)</option><option value="QUALIFIED">Квалифицирован (целевой клиент)</option><option value="WON">Продажа</option><option value="LOST">Отказ</option></select></label>}
        <label>Цвет<input name="color" type="color" defaultValue={editStage?.color || "#006bfd"}/></label>
        <label>Обязательные поля для перехода на этап<input name="required_fields" defaultValue={(editStage?.required_fields || []).join(", ")} placeholder={`amount, responsible_user_id${fields.length ? `, ${fields.map(f => f.key).join(", ")}` : ""}`}/></label>
        <p className="crmModalLead">Доступно: amount (бюджет), responsible_user_id (ответственный), source_id (источник){fields.map(f => `, ${f.key} (${f.name})`).join("")}.</p></>}
      {modal === "pipeline" && <><label>Название<input name="name" required minLength={2} defaultValue={editPipeline?.name || ""} placeholder="Например, Повторные продажи"/></label>
        {!editPipeline && <label>Этапы<select name="copy"><option value="">Стандартные: Новый лид → Квалифицирован → Продажа / Отказ</option>{pipelines.map(p => <option key={p.id} value={p.id}>Скопировать из «{p.name}»</option>)}</select></label>}</>}
      {modal === "field" && <><label>Название<input name="name" required placeholder="Например, Бюджет клиента"/></label><label>Ключ (латиницей)<input name="key" pattern="[a-z][a-z0-9_]+" required placeholder="budget"/></label>
        <label>Тип<select name="field_type">{[["TEXT", "Текст"], ["NUMBER", "Число"], ["MONEY", "Деньги"], ["DATE", "Дата"], ["SELECT", "Список"], ["MULTISELECT", "Мультисписок"], ["BOOLEAN", "Да/нет"], ["PHONE", "Телефон"], ["EMAIL", "Email"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Варианты (для списков, через запятую)<input name="options"/></label></>}
      {modal === "lost" && <><p className="crmModalLead">Причины отказов — главный источник роста конверсии. Сделка останется в аналитике.</p><label>Причина<select name="reason" required><option value="">Выберите причину</option>{lostReasons.map(reason => <option key={reason.id} value={reason.id}>{reason.label}</option>)}</select></label><label>Комментарий<textarea name="comment"/></label></>}
      <button className="resultPrimary" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить"}</button></form></div>}
  </div>;
}

function ContactPicker({ projectId }: { projectId: number }) {
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<Contact[]>([]);
  const [chosen, setChosen] = useState<Contact | null>(null);
  useEffect(() => { if (query.trim().length < 2 || chosen) { setOptions([]); return; } let active = true;
    const timer = setTimeout(() => api<{ items: Contact[] }>(`/crm/projects/${projectId}/contacts?${new URLSearchParams({ search: query.trim(), limit: "8" })}`)
      .then(v => active && setOptions(v.items)).catch(() => undefined), 250);
    return () => { active = false; clearTimeout(timer); }; }, [query, projectId, chosen]);
  return <div className="crmPicker"><label>Клиент<input name="contact_name" required={!chosen} value={chosen ? chosen.name : query} readOnly={!!chosen}
    onChange={e => setQuery(e.target.value)} placeholder="Имя нового клиента или поиск существующего"/></label>
    <input type="hidden" name="contact_id" value={chosen?.id || ""}/>
    {chosen ? <button type="button" className="crmLinkButton" onClick={() => { setChosen(null); setQuery(""); }}>Выбран существующий клиент · изменить</button>
      : options.length > 0 && <div className="crmPickerList">{options.map(c => <button type="button" key={c.id} onClick={() => setChosen(c)}>{c.name}<small>{c.phones[0] || c.emails[0] || ""}</small></button>)}</div>}</div>;
}

function InboundReject({ item, onClose, onSubmit }: { item: Inbound; onClose: () => void; onSubmit: (reason: string) => void }) {
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><form className="resultModal crmModal" onSubmit={e => { e.preventDefault(); onSubmit(String(new FormData(e.currentTarget).get("reason"))); }}>
    <header><h2>Отклонить заявку</h2><button type="button" onClick={onClose}>×</button></header><p className="crmModalLead">{item.name || "Без имени"} · {when(item.received_at)}. Отклонённые заявки не считаются лидами в аналитике.</p>
    <label>Причина<select name="reason" required>{Object.entries(rejectReasons).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
    <button className="resultPrimary">Отклонить</button></form></div>;
}

function InboundLink({ item, projectId, onClose, onSubmit }: { item: Inbound; projectId: number; onClose: () => void; onSubmit: (contactId: number) => void }) {
  const [query, setQuery] = useState(item.phone || item.email || item.name || "");
  const [options, setOptions] = useState<Contact[]>([]);
  useEffect(() => { if (query.trim().length < 2) { setOptions([]); return; } let active = true;
    const timer = setTimeout(() => api<{ items: Contact[] }>(`/crm/projects/${projectId}/contacts?${new URLSearchParams({ search: query.trim(), limit: "10" })}`)
      .then(v => active && setOptions(v.items)).catch(() => undefined), 250);
    return () => { active = false; clearTimeout(timer); }; }, [query, projectId]);
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><div className="resultModal crmModal">
    <header><h2>Связать с клиентом</h2><button type="button" onClick={onClose}>×</button></header><p className="crmModalLead">Заявка станет новой сделкой существующего клиента — история клиента сохранится в одной карточке.</p>
    <label>Поиск клиента<input value={query} onChange={e => setQuery(e.target.value)} autoFocus/></label>
    <div className="crmPickerList static">{options.map(c => <button key={c.id} onClick={() => onSubmit(c.id)}>{c.name}<small>{[c.phones[0], c.emails[0], c.company].filter(Boolean).join(" · ")}</small></button>)}{!options.length && <p className="crmMuted">Никого не нашли</p>}</div></div></div>;
}
