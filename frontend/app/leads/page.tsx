"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { count, dateInput, money, percent, PeriodControls, ProjectSidebar } from "@/components/reporting";
import "../result/result.css";
import "./leads.css";

type Project = { id: number; name: string; organization_name: string };
type Status = "lead" | "qualified" | "sale" | "lost";
type Row = { id: number; name: string; phone: string | null; email: string | null; telegram: string | null;
  source: string; campaign: string | null; campaign_id: string | null; hypothesis: string | null;
  created_at: string; status: Status; sales_count: number; revenue: number | null;
  assigned_to_id: number | null; assigned_to_name: string | null; notes: string | null; lost_reason: string | null;
  meeting_at?: string | null };
type List = { rows: Row[]; total: number; page: number; page_size: number };
type LeadEvent = { id: number; event_type: string; description: string; actor_name: string | null; created_at: string };
type Detail = { lead: Row; events: LeadEvent[]; sales: { id: number; amount: number | null; occurred_at: string }[] };
type Totals = { leads: number | null; qualified: number | null; sales: number | null; revenue: number | null;
  leads_change: number | null; qualified_change: number | null; sales_change: number | null; revenue_change: number | null };
type Result = { project: { meeting_enabled: boolean }; current: { totals: Totals }; previous: { totals: Totals }; viewer: { role: string } };
type TeamMember = { id: number; display_name: string };
type FilterOptions = { sources: string[]; campaigns: { id: string; name: string }[] };
const labels: Record<Status, string> = { lead: "Лид", qualified: "Квалифицирован", sale: "Продажа", lost: "Потерян" };
const when = (value: string) => new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
const mutate = (path: string, method: string, body: unknown) => api<unknown>(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export default function ClientLeadsPage() {
  const today = useMemo(() => new Date(), []);
  const [start, setStart] = useState(dateInput(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)));
  const [end, setEnd] = useState(dateInput(today));
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [list, setList] = useState<List>({ rows: [], total: 0, page: 1, page_size: 20 });
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [campaign, setCampaign] = useState("");
  const [owner, setOwner] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [extra, setExtra] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [tab, setTab] = useState<"info" | "history" | "comments">("info");
  const [modal, setModal] = useState<"add" | "sale" | "lost" | null>(null);
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [filterOptions, setFilterOptions] = useState<FilterOptions>({ sources: [], campaigns: [] });
  const [lostReasons, setLostReasons] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => { api<Project[]>("/result/projects").then(rows => {
    setProjects(rows);
    const desired = Number(new URLSearchParams(window.location.search).get("project_id"));
    setProjectId(rows.find(row => row.id === desired)?.id || rows[0]?.id || null);
    if (!rows.length) setLoading(false);
  }).catch(e => { setError(e.message); setLoading(false); }); }, []);
  useEffect(() => { setPage(1); setSelectedId(null); setDetail(null); }, [projectId, start, end, search, status, source, campaign, owner]);
  useEffect(() => { if (!projectId) return;
    const linked = Number(new URLSearchParams(window.location.search).get("lead_id"));
    if (linked > 0) setSelectedId(linked);
  }, [projectId]);
  useEffect(() => {
    if (!projectId) return;
    let active = true; setLoading(true); setError("");
    const params = new URLSearchParams({ project_id: String(projectId), start, end, page: String(page), page_size: String(pageSize) });
    if (search) params.set("search", search); if (status) params.set("status", status);
    if (source) params.set("source", source); if (campaign) params.set("campaign", campaign);
    if (owner) params.set("owner_id", owner);
    Promise.all([api<List>(`/result/leads?${params}`), api<Result>(`/result?project_id=${projectId}&start=${start}&end=${end}`)])
      .then(([leads, facts]) => { if (active) { setList(leads); setResult(facts); } })
      .catch(e => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, start, end, page, pageSize, search, status, source, campaign, owner, revision]);
  useEffect(() => { if (!projectId || result?.viewer.role === "admin") return;
    api<TeamMember[]>(`/portal/crm/team?project_id=${projectId}`).then(setTeam).catch(() => setTeam([]));
  }, [projectId, result?.viewer.role]);
  useEffect(() => { if (!projectId) return;
    api<FilterOptions>(`/result/lead-filters?project_id=${projectId}`).then(setFilterOptions)
      .catch(() => setFilterOptions({ sources: [], campaigns: [] }));
  }, [projectId, revision]);
  useEffect(() => { if (!projectId) return;
    api<{ label: string }[]>(`/settings/reasons?project_id=${projectId}`).then(rows => setLostReasons(rows.map(row => row.label)))
      .catch(() => setLostReasons([]));
  }, [projectId, revision]);
  useEffect(() => { if (!selectedId || !projectId) return;
    let active = true;
    api<Detail>(`/result/leads/${selectedId}?project_id=${projectId}`).then(value => { if (active) setDetail(value); })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [selectedId, projectId, revision]);

  const project = projects.find(p => p.id === projectId);
  const selected = list.rows.find(row => row.id === selectedId) || (detail?.lead.id === selectedId ? detail.lead : null);
  const role = result?.viewer.role;
  const canEdit = !!role && ["client_owner", "sales_head", "sales_manager"].includes(role);
  const canCreate = !!role && ["client_owner", "sales_head"].includes(role);
  const canAssign = canCreate;
  const current = result?.current.totals, previous = result?.previous.totals;
  const conversion = current?.leads && current.sales != null ? current.sales / current.leads * 100 : null;
  const previousConversion = previous?.leads && previous.sales != null ? previous.sales / previous.leads * 100 : null;
  const refresh = () => { setRevision(value => value + 1); setDetail(null); };
  const run = async (action: () => Promise<unknown>, closeModal = false) => { setBusy(true); setError(""); try { await action(); if (closeModal) setModal(null); refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const changeStatus = (value: string) => { if (!selected || value === selected.status) return;
    if (value === "sale" || value === "lost") { setModal(value); return; }
    if (value === "qualified") run(() => mutate(`/portal/crm/leads/${selected.id}`, "PATCH", { status: "qualified" }));
  };
  const submitAdd = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); if (!projectId) return;
    const form = new FormData(event.currentTarget);
    run(() => mutate("/portal/crm/leads", "POST", { project_id: projectId, full_name: form.get("name"), phone: form.get("phone") || null,
      email: form.get("email") || null, telegram: form.get("telegram") || null, source: form.get("source") || "Вручную",
      assigned_to_id: form.get("owner") ? Number(form.get("owner")) : null, notes: form.get("comment") || null }), true);
  };
  const submitSale = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); if (!selected) return;
    const form = new FormData(event.currentTarget);
    run(() => mutate(`/portal/crm/leads/${selected.id}/sales`, "POST", { amount: Number(form.get("amount")),
      occurred_at: new Date(`${form.get("date")}T12:00:00`).toISOString(), comment: form.get("comment") || null }), true);
  };
  const submitLost = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); if (!selected) return;
    const form = new FormData(event.currentTarget);
    run(() => mutate(`/portal/crm/leads/${selected.id}/lost`, "POST", { reason: form.get("reason"), detail: form.get("detail") || null }), true);
  };
  const submitComment = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); if (!selected) return;
    const form = event.currentTarget, data = new FormData(form);
    run(async () => { await mutate(`/portal/crm/leads/${selected.id}/comments`, "POST", { text: data.get("text") }); form.reset(); });
  };
  const kpis: { label: string; value: number | null | undefined; prior: number | null | undefined; change: number | null | undefined; format: (n: number | null | undefined) => string; icon: string }[] = [
    { label: "Всего лидов", value: current?.leads, prior: previous?.leads, change: current?.leads_change, format: count, icon: "♙" },
    { label: "Квалифицированные", value: current?.qualified, prior: previous?.qualified, change: current?.qualified_change, format: count, icon: "✓" },
    { label: "Продажи", value: current?.sales, prior: previous?.sales, change: current?.sales_change, format: count, icon: "▣" },
    { label: "CR лид → продажа", value: conversion, prior: previousConversion, change: conversion != null && previousConversion != null ? conversion - previousConversion : null, format: n => n == null ? "—" : `${n.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`, icon: "◎" },
    { label: "Выручка", value: current?.revenue, prior: previous?.revenue, change: current?.revenue_change, format: money, icon: "◉" },
  ];

  return <div className="resultShell"><ProjectSidebar project={project} projectId={projectId} active="leads" role={role}/>
    <main className="resultMain leadsMain"><div className="resultBreadcrumb">StepToLead <span>/</span> Лиды</div>
      <header className="resultHeader leadsHeader"><div><h1>Лиды</h1><p>Реальные лиды из рекламы, сайта и других источников. Статус подтверждает отдел продаж.</p></div>
        <div className="leadsHeaderActions"><PeriodControls projects={projects} projectId={projectId} setProjectId={setProjectId} start={start} setStart={setStart} end={end} setEnd={setEnd}/>
          {canCreate && <button className="leadsBlueButton" onClick={() => setModal("add")}>＋ Добавить лид</button>}</div></header>
      {error && <div className="resultError" role="alert">{error} <button onClick={() => setError("")}>×</button></div>}
      <section className="resultKpis leadsKpis">{kpis.map(kpi => <article className="resultKpi" key={kpi.label}><div className="resultKpiTitle"><span className="resultKpiIcon">{kpi.icon}</span>{kpi.label}</div>
        <div className="resultKpiValue">{kpi.format(kpi.value)}{kpi.change != null && <em className={kpi.change < 0 ? "negative" : "positive"}>{kpi.change > 0 ? "↑" : "↓"} {kpi.label.startsWith("CR") ? `${Math.abs(kpi.change).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} п.п.` : percent(Math.abs(kpi.change))}</em>}</div>
        <p>{kpi.prior == null ? "Нет данных за прошлый период" : `${kpi.format(kpi.prior)} за прошлый период`}</p></article>)}</section>
      <div className={`leadsWorkspace ${selected ? "withDrawer" : ""}`}><div className="leadsRegister">
        <div className="leadsFilters"><input aria-label="Поиск лидов" placeholder="Поиск по имени, телефону, email…" value={search} onChange={e => setSearch(e.target.value)}/>
          <select aria-label="Статус" value={status} onChange={e => setStatus(e.target.value)}><option value="">Все статусы</option>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
          <select aria-label="Источник" value={source} onChange={e => setSource(e.target.value)}><option value="">Все источники</option>{filterOptions.sources.map(value => <option key={value}>{value}</option>)}</select>
          <select aria-label="Кампания" value={campaign} onChange={e => setCampaign(e.target.value)}><option value="">Все кампании</option>{filterOptions.campaigns.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}</select>
          <button className="leadsFilterButton" onClick={() => setExtra(!extra)}>⚑ Ещё фильтры</button>
          {extra && <select aria-label="Ответственный" value={owner} onChange={e => setOwner(e.target.value)}><option value="">Все ответственные</option>{team.map(member => <option value={member.id} key={member.id}>{member.display_name}</option>)}</select>}</div>
        <section className="resultPanel leadsTable"><div className="resultTableScroll"><table><thead><tr><th>Лид</th><th>Источник</th><th>Кампания</th><th>Дата лида</th><th>Статус</th><th>Сумма</th><th>Ответственный</th></tr></thead><tbody>{list.rows.map(row => <tr key={row.id} className={selectedId === row.id ? "selected" : ""} onClick={() => { setSelectedId(row.id); setTab("info"); }} tabIndex={0} onKeyDown={e => { if (e.key === "Enter") setSelectedId(row.id); }}><td><strong>{row.name}</strong><small>{row.phone || row.email || row.telegram || "Контакт не указан"}</small></td><td>{row.source || "Не определено"}</td><td>{row.campaign || "—"}</td><td>{when(row.created_at)}</td><td><span className={`resultStatus ${row.status}`}>{labels[row.status]}</span></td><td>{money(row.revenue)}</td><td>{row.assigned_to_name || "—"}</td></tr>)}</tbody></table>{!loading && !list.rows.length && <p className="resultTableEmpty">Лидов по выбранным фильтрам нет.</p>}{loading && <p className="resultLoading">Загружаем лиды…</p>}</div>
          <footer className="leadsPagination"><span>Показано {list.rows.length ? (page - 1) * pageSize + 1 : 0}–{Math.min(page * pageSize, list.total)} из {count(list.total)} лидов</span><div><button disabled={page <= 1} onClick={() => setPage(page - 1)}>‹</button><span>{page} / {Math.max(1, Math.ceil(list.total / pageSize))}</span><button disabled={page * pageSize >= list.total} onClick={() => setPage(page + 1)}>›</button></div><label>Показывать по <select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }}><option>20</option><option>50</option><option>100</option></select></label></footer></section></div>
        {selected && <aside className="resultPanel leadsDrawer"><div className="leadsDrawerTop"><button onClick={() => { setSelectedId(null); setDetail(null); }} aria-label="Закрыть">×</button><span>Карточка лида</span></div>
          <div className="leadsIdentity"><span className="leadsAvatar">{selected.name.slice(0, 1).toUpperCase()}</span><div><h2>{selected.name}</h2><span className={`resultStatus ${selected.status}`}>{labels[selected.status]}</span></div></div>
          <div className="leadsContacts">{selected.phone && <div>☎ {selected.phone}</div>}{selected.email && <div>✉ {selected.email}</div>}{selected.telegram && <div>◉ {selected.telegram}</div>}</div>
          <div className="leadsDrawerTabs">{(["info", "history", "comments"] as const).map(value => <button key={value} className={tab === value ? "active" : ""} onClick={() => setTab(value)}>{value === "info" ? "Информация" : value === "history" ? "История" : "Комментарии"}</button>)}</div>
          {tab === "info" && <div className="leadsInfo"><h3>Основное</h3><dl><div><dt>Источник</dt><dd>{selected.source || "Не определено"}</dd></div><div><dt>Кампания</dt><dd>{selected.campaign || "—"}</dd></div><div><dt>Гипотеза</dt><dd>{selected.hypothesis || "—"}</dd></div><div><dt>Дата лида</dt><dd>{when(selected.created_at)}</dd></div><div><dt>Ответственный</dt><dd>{canAssign ? <select value={selected.assigned_to_id || ""} disabled={busy} onChange={e => run(() => mutate(`/portal/crm/leads/${selected.id}`, "PATCH", { assigned_to_id: e.target.value ? Number(e.target.value) : null }))}><option value="">Не назначен</option>{team.map(member => <option key={member.id} value={member.id}>{member.display_name}</option>)}</select> : selected.assigned_to_name || "—"}</dd></div><div><dt>Статус</dt><dd>{canEdit && selected.status !== "lost" ? <select value={selected.status} disabled={busy} onChange={e => changeStatus(e.target.value)}><option value={selected.status}>{labels[selected.status]}</option>{selected.status === "lead" && <option value="qualified">Квалифицирован</option>}{selected.status !== "sale" && <option value="sale">Продажа</option>}{selected.status !== "sale" && <option value="lost">Потерян</option>}</select> : labels[selected.status]}</dd></div><div><dt>Сумма продаж</dt><dd>{money(selected.revenue)}</dd></div><div><dt>Продаж</dt><dd>{selected.sales_count}</dd></div></dl>
            <h3>Дополнительно</h3><dl><div><dt>Данные квалификации</dt><dd>—</dd></div>{result?.project.meeting_enabled && <div><dt>Встреча</dt><dd>{canEdit ? <input type="datetime-local" value={detail?.lead.meeting_at ? detail.lead.meeting_at.slice(0,16) : ""} onChange={e => run(() => mutate(`/portal/crm/leads/${selected.id}`, "PATCH", { meeting_at: e.target.value ? new Date(e.target.value).toISOString() : null }))}/> : detail?.lead.meeting_at ? when(detail.lead.meeting_at) : "—"}</dd></div>}<div><dt>Причина потери</dt><dd>{selected.lost_reason || "—"}</dd></div></dl><h3>Комментарий</h3><p>{selected.notes || "—"}</p></div>}
          {tab === "history" && <div className="leadsTimeline">{detail?.events.map(event => <div key={event.id}><i/><strong>{event.event_type.replaceAll("_", " ")}</strong><p>{event.description}</p><small>{when(event.created_at)} · {event.actor_name || "Система"}</small></div>)}{detail && !detail.events.length && <p>История пока пуста.</p>}</div>}
          {tab === "comments" && <div className="leadsTimeline">{detail?.events.filter(event => ["COMMENT_ADDED", "comment"].includes(event.event_type)).map(event => <div key={event.id}><i/><p>{event.description}</p><small>{when(event.created_at)} · {event.actor_name || "Система"}</small></div>)}{canEdit && <form className="leadsCommentForm" onSubmit={submitComment}><textarea name="text" required maxLength={4000} placeholder="Добавить комментарий"/><button disabled={busy}>Добавить комментарий</button></form>}</div>}
        </aside>}</div>
      {modal && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) setModal(null); }}><form className="resultModal" onSubmit={modal === "add" ? submitAdd : modal === "sale" ? submitSale : submitLost}><header><h2>{modal === "add" ? "Добавить лид" : modal === "sale" ? "Подтвердить продажу" : "Отметить потерянным"}</h2><button type="button" onClick={() => setModal(null)}>×</button></header>
        {modal === "add" && <><p>Добавляйте только людей, которые оставили контакт или согласились на связь.</p><label>Имя<input name="name" required minLength={2} maxLength={180}/></label><div className="resultModalGrid"><label>Телефон<input name="phone" type="tel" maxLength={64}/></label><label>Email<input name="email" type="email" maxLength={254}/></label></div><label>Telegram<input name="telegram" maxLength={120} placeholder="@username или ссылка"/></label><label>Источник<select name="source"><option>Вручную</option><option>Рекомендация</option><option>Другое</option></select></label><label>Ответственный<select name="owner"><option value="">Не назначен</option>{team.map(member => <option key={member.id} value={member.id}>{member.display_name}</option>)}</select></label><label>Комментарий<textarea name="comment" maxLength={4000}/></label></>}
        {modal === "sale" && <><p>Продажа будет учтена в общей аналитике проекта. У лида может быть несколько продаж.</p><label>Сумма продажи, ₽<input name="amount" type="number" min="0.01" step="0.01" required/></label><label>Дата продажи<input name="date" type="date" defaultValue={dateInput(new Date())} required/></label><label>Комментарий<textarea name="comment" maxLength={4000}/></label></>}
        {modal === "lost" && <><p>Причина потери сохранится в истории лида.</p><label>Причина<select name="reason" required>{lostReasons.map(reason => <option key={reason}>{reason}</option>)}</select></label>{!lostReasons.length && <p>Не удалось загрузить причины потери. Проверьте настройки проекта.</p>}<label>Пояснение для «Другое»<textarea name="detail" maxLength={4000}/></label></>}
        <button className="resultPrimary" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить"}</button></form></div>}
    </main></div>;
}
