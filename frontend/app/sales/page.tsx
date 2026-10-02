"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { count, dateInput, money, PeriodControls, ProjectSidebar } from "@/components/reporting";
import "../result/result.css";
import "./sales.css";

type Project = { id: number; name: string; organization_name: string };
type Facts = { viewer: { role: string } };
type Sale = { id: number; lead_id: number; client: string; phone: string | null; email: string | null;
  source_id: string; source: string; campaign: string | null; occurred_at: string; amount: number | null;
  status: string; owner_id: number | null; owner: string | null; comment: string | null };
type SalesData = { rows: Sale[]; total: number; page: number; page_size: number; cycle_days: number | null;
  previous_cycle_days: number | null; sources: { id: string; name: string }[]; owners: { id: number; name: string }[] };
type LeadOption = { id: number; name: string; phone: string | null; email: string | null; telegram: string | null; status: string };
const when = (value: string) => new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });

export default function SalesPage() {
  const router = useRouter();
  const today = useMemo(() => new Date(), []);
  const [start, setStart] = useState(dateInput(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)));
  const [end, setEnd] = useState(dateInput(today));
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [facts, setFacts] = useState<Facts | null>(null);
  const [sales, setSales] = useState<SalesData>({ rows: [], total: 0, page: 1, page_size: 20, cycle_days: null, previous_cycle_days: null, sources: [], owners: [] });
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");
  const [owner, setOwner] = useState("");
  const [minAmount, setMinAmount] = useState("");
  const [maxAmount, setMaxAmount] = useState("");
  const [amountFilters, setAmountFilters] = useState({ min: "", max: "" });
  const [showAmounts, setShowAmounts] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [modal, setModal] = useState(false);
  const [leadSearch, setLeadSearch] = useState("");
  const [leadOptions, setLeadOptions] = useState<LeadOption[]>([]);
  const [selectedLead, setSelectedLead] = useState("");
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
  useEffect(() => { setPage(1); }, [projectId, start, end, search, source, owner, amountFilters]);
  useEffect(() => { if (!projectId) return;
    let active = true; setLoading(true); setError("");
    const report = new URLSearchParams({ project_id: String(projectId), start, end });
    const query = new URLSearchParams({ project_id: String(projectId), start, end, page: String(page), page_size: String(pageSize) });
    if (search) query.set("search", search); if (source) query.set("source", source);
    if (owner) query.set("owner_id", owner);
    if (amountFilters.min) query.set("min_amount", amountFilters.min);
    if (amountFilters.max) query.set("max_amount", amountFilters.max);
    Promise.all([api<Facts>(`/result?${report}`), api<SalesData>(`/result/sales?${query}`)])
      .then(([reportData, saleData]) => { if (active) { setFacts(reportData); setSales(saleData); } })
      .catch(e => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, start, end, page, pageSize, search, source, owner, amountFilters, revision]);
  useEffect(() => { if (!modal || !projectId) return;
    let active = true;
    const query = new URLSearchParams({ project_id: String(projectId), page_size: "100", search: leadSearch });
    api<{ rows: LeadOption[] }>(`/result/leads?${query}`).then(data => {
      if (active) setLeadOptions(data.rows.filter(row => row.status !== "lost"));
    }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [modal, projectId, leadSearch]);

  const project = projects.find(row => row.id === projectId);
  const canAdd = !!facts && ["client_owner", "sales_head", "sales_manager"].includes(facts.viewer.role);
  const submitSale = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!selectedLead) { setError("Выберите лида для продажи"); return; }
    const form = new FormData(event.currentTarget); setBusy(true); setError("");
    try { await api(`/portal/crm/leads/${selectedLead}/sales`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount: Number(form.get("amount")), occurred_at: new Date(`${form.get("date")}T12:00:00`).toISOString(),
        comment: form.get("comment") || null }) });
      setModal(false); setSelectedLead(""); setLeadSearch(""); setRevision(value => value + 1);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const openSale = () => {
    const linked = new URLSearchParams(window.location.search).get("lead_id") || "";
    setSelectedLead(linked); setModal(true);
  };
  const openLead = (leadId: number) => router.push(`/crm?project_id=${projectId}&lead_id=${leadId}`);

  return <div className="resultShell"><ProjectSidebar project={project} projectId={projectId} active="sales" role={facts?.viewer.role}/>
    <main className="resultMain salesMain"><div className="resultBreadcrumb">StepToLead <span>/</span> Продажи</div>
      <header className="resultHeader salesHeader"><div><h1>Продажи</h1><p>Реестр подтверждённых сделок. Маркетинговые и финансовые показатели собраны в «Аналитике».</p></div>
        <div className="salesHeaderActions"><PeriodControls projects={projects} projectId={projectId} setProjectId={setProjectId} start={start} setStart={setStart} end={end} setEnd={setEnd}/>
          {canAdd && <button className="salesBlueButton" onClick={openSale}>＋ Добавить продажу</button>}</div></header>
      {error && <div className="resultError" role="alert">{error} <button onClick={() => setError("")}>×</button></div>}
      <div className="salesRegistryIntro"><span>Продажи проекта</span><Link href={`/analytics?tab=sales${projectId ? `&project_id=${projectId}` : ""}`}>Смотреть аналитику продаж →</Link></div>
      <section className="resultPanel salesList"><div className="salesListHead"><h2>Список продаж</h2><div className="salesFilters"><input aria-label="Поиск" placeholder="Клиент, телефон, email…" value={search} onChange={e => setSearch(e.target.value)}/><select aria-label="Источник" value={source} onChange={e => setSource(e.target.value)}><option value="">Все источники</option>{sales.sources.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><select aria-label="Ответственный" value={owner} onChange={e => setOwner(e.target.value)}><option value="">Все ответственные</option>{sales.owners.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><button onClick={() => setShowAmounts(!showAmounts)}>Сумма ▾</button></div></div>
        {showAmounts && <form className="salesAmountFilters" onSubmit={e => { e.preventDefault(); setAmountFilters({ min: minAmount, max: maxAmount }); }}><label>От, ₽<input type="number" min="0" step="0.01" value={minAmount} onChange={e => setMinAmount(e.target.value)}/></label><label>До, ₽<input type="number" min="0" step="0.01" value={maxAmount} onChange={e => setMaxAmount(e.target.value)}/></label><button>Применить</button><button type="button" onClick={() => { setMinAmount(""); setMaxAmount(""); setAmountFilters({ min: "", max: "" }); }}>Сбросить</button></form>}
        <div className="resultTableScroll"><table><thead><tr>{["Клиент","Источник","Кампания","Дата продажи","Сумма","Статус","Ответственный","Комментарий"].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{sales.rows.map(row => <tr key={row.id} tabIndex={0} onClick={() => openLead(row.lead_id)} onKeyDown={e => { if (e.key === "Enter") openLead(row.lead_id); }}><td><strong>{row.client}</strong><small>{row.phone || row.email || "—"}</small></td><td>{row.source}</td><td>{row.campaign || "—"}</td><td>{when(row.occurred_at)}</td><td>{money(row.amount)}</td><td><span className="resultStatus sale">Продажа</span></td><td>{row.owner || "—"}</td><td title={row.comment || ""}>{row.comment || "—"}</td></tr>)}</tbody></table>{!loading && !sales.rows.length && <p className="resultTableEmpty">Продаж по выбранным фильтрам нет.</p>}{loading && <p className="resultLoading">Загружаем продажи…</p>}</div><footer className="salesPagination"><span>Показано {sales.rows.length ? (page - 1) * pageSize + 1 : 0}–{Math.min(page * pageSize, sales.total)} из {count(sales.total)} продаж</span><div><button disabled={page <= 1} onClick={() => setPage(page - 1)}>‹</button><span>{page} / {Math.max(1, Math.ceil(sales.total / pageSize))}</span><button disabled={page * pageSize >= sales.total} onClick={() => setPage(page + 1)}>›</button></div><label>Показывать по <select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(1); }}><option>20</option><option>50</option><option>100</option></select></label></footer></section>
      {modal && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) setModal(false); }}><form className="resultModal" onSubmit={submitSale}><header><h2>Добавить продажу</h2><button type="button" onClick={() => setModal(false)}>×</button></header><p>Выберите существующего лида. Продажа попадёт во все отчёты проекта.</p><label>Найти лида<input value={leadSearch} onChange={e => setLeadSearch(e.target.value)} placeholder="Имя, телефон, email или Telegram"/></label><label>Лид<select required value={selectedLead} onChange={e => setSelectedLead(e.target.value)}><option value="">Выберите лида</option>{leadOptions.map(lead => <option key={lead.id} value={lead.id}>{lead.name} · {lead.phone || lead.email || lead.telegram || `#${lead.id}`}</option>)}{selectedLead && !leadOptions.some(lead => String(lead.id) === selectedLead) && <option value={selectedLead}>Лид #{selectedLead}</option>}</select></label><label>Сумма продажи, ₽<input name="amount" type="number" min="0.01" step="0.01" required/></label><label>Дата продажи<input name="date" type="date" defaultValue={dateInput(new Date())} required/></label><label>Комментарий<textarea name="comment" maxLength={4000}/></label><button className="resultPrimary" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить продажу"}</button></form></div>}
    </main></div>;
}
