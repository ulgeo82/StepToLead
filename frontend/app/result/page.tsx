"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { count, dateInput, MetricChart, money, percent, PeriodControls, ProjectSidebar } from "@/components/reporting";
import { LaunchPanel } from "@/components/launch-panel";
import {BaselineCard} from "@/components/brief-baseline";
import "./result.css";

type Metric = "spend" | "leads" | "qualified" | "sales" | "revenue" | "romi";
type Totals = Record<Metric, number | null> & {
  cpl: number | null; cpql: number | null; cac: number | null; average_check: number | null;
  gross_profit: number | null; spend_change?: number | null; leads_change?: number | null;
  qualified_change?: number | null; sales_change?: number | null; revenue_change?: number | null; romi_change?: number | null;
};
type Source = Totals & { id: string; name: string; kind: string; method: string | null; platform: string | null; status: string | null; data_mode: string };
type Point = Record<Metric, number | null> & { label: string };
type Result = {
  project: { id: number; name: string; workspace_id: number };
  period: { start: string; end: string; previous_start: string; previous_end: string };
  current: { totals: Totals; sources: Source[]; trend: Point[] };
  previous: { totals: Totals; sources: Source[]; trend: Point[] };
  funnel: { label: string; value: number | null; conversion: number | null }[];
  economics: { average_order_value: number | null; margin: number | null; allowable_cac: number | null; break_even_sales: number | null; active: boolean };
  recent_leads: { id: number; name: string; source: string; created_at: string; status: string; amount: number | null }[];
  alerts: { title: string; detail: string; href: string; tone: string }[];
  viewer: { name: string; role: string; can_manage_sources: boolean; can_manage_economics: boolean };
};
type Project = { id: number; name: string; organization_id: number; organization_name: string };
type GrowthModel = { id: number; name: string; created_at: string; average_order_value: number | null; gross_margin: number | null };
type EconomicsValues = { inputs: Record<string, number> | null; allowable_cac: number | null };
const METRICS: { key: Metric; label: string; icon: string; note?: keyof Totals }[] = [
  { key: "spend", label: "Расходы на маркетинг", icon: "◫" },
  { key: "leads", label: "Лиды", icon: "♧", note: "cpl" },
  { key: "qualified", label: "Квалифицированные лиды", icon: "✓", note: "cpql" },
  { key: "sales", label: "Продажи", icon: "▣", note: "cac" },
  { key: "revenue", label: "Выручка", icon: "◉", note: "average_check" },
  { key: "romi", label: "ROMI", icon: "▥", note: "gross_profit" },
];
const comparison = (now: number | null, before: number | null) =>
  now == null || before == null || before === 0 ? "—" : percent((now - before) / Math.abs(before) * 100);
const metricValue = (key: Metric, n: number | null | undefined) => key === "spend" || key === "revenue" ? money(n) : key === "romi" ? percent(n) : count(n);

export default function ResultPage() {
  const today = useMemo(() => new Date(), []);
  const [start, setStart] = useState(dateInput(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)));
  const [end, setEnd] = useState(dateInput(today));
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [granularity, setGranularity] = useState("day");
  const [metric, setMetric] = useState<Metric>("spend");
  const [data, setData] = useState<Result | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [sourceForm, setSourceForm] = useState(false);
  const [metricSource, setMetricSource] = useState<Source | null>(null);
  const [economicsForm, setEconomicsForm] = useState(false);
  const [projectEconomicsForm, setProjectEconomicsForm] = useState(false);
  const [economicsValues, setEconomicsValues] = useState<EconomicsValues | null>(null);
  const [growthModels, setGrowthModels] = useState<GrowthModel[]>([]);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);

  useEffect(() => { api<Project[]>("/result/projects").then(rows => {
    setProjects(rows);
    const desired = Number(new URLSearchParams(window.location.search).get("project_id"));
    if (rows.length) setProjectId(rows.find(row => row.id === desired)?.id || rows[0].id);
    else setLoading(false);
  }).catch(e => { setLoading(false); setError(e.message); }); }, []);
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true); setError(""); setData(null);
    const query = new URLSearchParams({ project_id: String(projectId), start, end, granularity });
    api<Result>(`/result?${query}`)
      .then(value => { if (!cancelled) setData(value); })
      .catch(exc => { if (!cancelled) setError(exc.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [projectId, start, end, granularity, revision]);
  // Live: new leads and fresh ad spend show up without reloading (quiet refresh while the tab is visible).
  useEffect(() => { if (!projectId) return;
    const query = new URLSearchParams({ project_id: String(projectId), start, end, granularity });
    const timer = setInterval(() => { if (document.visibilityState === "visible") api<Result>(`/result?${query}`).then(setData).catch(() => {}); }, 60000);
    return () => clearInterval(timer); }, [projectId, start, end, granularity]);

  async function createSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!projectId) return;
    const form = new FormData(event.currentTarget); setBusy(true); setError("");
    try {
      await api("/result/sources", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, name: form.get("name"), kind: form.get("kind"), method: form.get("method") }) });
      setSourceForm(false); setRevision(v => v + 1);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось добавить источник"); }
    finally { setBusy(false); }
  }
  async function saveMetric(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!metricSource) return;
    const form = new FormData(event.currentTarget); setBusy(true); setError("");
    const payload: Record<string, string | number | null> = { date: String(form.get("date")) };
    for (const field of ["spend", "impressions", "clicks", "aggregated_leads", "aggregated_qualified", "aggregated_sales", "aggregated_revenue"])
      if (form.get(field) !== "") payload[field] = Number(form.get(field));
    try {
      await api(`/result/sources/${metricSource.id.split(":")[1]}/daily`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      setMetricSource(null); setRevision(v => v + 1);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось сохранить данные"); }
    finally { setBusy(false); }
  }
  async function openEconomics() {
    setError("");
    try { setGrowthModels(await api<GrowthModel[]>("/result/growth-models")); setEconomicsForm(true); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось загрузить модели /growth"); }
  }
  async function openProjectEconomics() {
    if (!projectId) return;
    setError("");
    try {
      setEconomicsValues(await api<EconomicsValues>(`/result/projects/${projectId}/economics/model`));
      setProjectEconomicsForm(true);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось загрузить модель проекта"); }
  }
  async function saveProjectEconomics(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!projectId) return;
    const form = new FormData(event.currentTarget); setBusy(true); setError("");
    const payload: Record<string, number | null> = {};
    for (const field of ["average_order_value", "purchases_per_customer", "gross_margin", "fixed_costs", "current_customers", "target_customers", "growth_budget"])
      payload[field] = Number(form.get(field));
    payload.allowable_cac = form.get("allowable_cac") === "" ? null : Number(form.get("allowable_cac"));
    try {
      await api(`/result/projects/${projectId}/economics/model`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      setProjectEconomicsForm(false); setRevision(v => v + 1);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось сохранить модель проекта"); }
    finally { setBusy(false); }
  }
  async function activateEconomics(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!projectId) return;
    const form = new FormData(event.currentTarget); setBusy(true); setError("");
    try {
      await api(`/result/projects/${projectId}/economics`, { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ growth_calculation_id: Number(form.get("growth_calculation_id")),
          allowable_cac: form.get("allowable_cac") === "" ? null : Number(form.get("allowable_cac")) }) });
      setEconomicsForm(false); setRevision(v => v + 1);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось подключить модель"); }
    finally { setBusy(false); }
  }

  const selectedProject = projects.find(p => p.id === projectId);
  const leadsHref = projectId ? `/crm?project_id=${projectId}` : "/crm";
  const current = data?.current.totals;
  const previous = data?.previous.totals;
  return <div className="resultShell">
    <ProjectSidebar project={selectedProject} projectId={projectId} active="result" role={data?.viewer.role}/>
    <main className="resultMain"><div className="resultBreadcrumb">StepToLead <span>/</span> Результат</div>
      <header className="resultHeader"><div><h1>Результат</h1><p>Главные показатели маркетинга и продаж за выбранный период.</p></div><PeriodControls projects={projects} projectId={projectId} setProjectId={setProjectId} start={start} setStart={setStart} end={end} setEnd={setEnd}/></header>
      {error && <div className="resultError" role="alert">{error}{!projects.length && <div><Link href="/portal/login?next=/result">Войти в кабинет</Link> · <Link href="/login?next=/result">Войти как администратор</Link></div>}</div>}
      {loading && !data && <div className="resultLoading">Загружаем результат…</div>}
      {!loading && !data && !error && <div className="resultLoading">Проекты пока не созданы.</div>}
      {data && data.viewer.role !== "admin" && <LaunchPanel projectId={projectId}/>}
      {data && <>
        {projectId&&<BaselineCard projectId={projectId} period={`${start} — ${end}`} current={{...current,budget:current?.spend,conversion:current?.leads&&current.sales!=null?current.sales/current.leads*100:null}}/>}
        <section className="resultKpis" aria-label="Главные показатели">{METRICS.map(({ key, label, icon, note }) => {
          const change = current?.[`${key}_change` as keyof Totals] as number | null | undefined;
          const prior = previous?.[key];
          return <article className="resultKpi" key={key}><div className="resultKpiTitle"><span className={`resultKpiIcon ${key}`}>{icon}</span><span>{label}</span></div><div className="resultKpiValue">{metricValue(key, current?.[key])}{change != null && <em className={(key === "spend" ? change > 0 : change < 0) ? "negative" : "positive"}>{change > 0 ? "↑" : change < 0 ? "↓" : "="} {percent(Math.abs(change))}</em>}</div><p>{prior == null ? "Нет данных за прошлый период" : `${metricValue(key, prior)} за прошлый период`}</p>{note && <small>{note === "cpl" ? "CPL" : note === "cpql" ? "CPQL" : note === "cac" ? "CAC" : note === "average_check" ? "Средний чек" : "Валовая прибыль"}: {money(current?.[note] as number | null)}</small>}</article>;
        })}</section>
        <section className="resultMiddle"><article className="resultPanel resultDynamics"><div className="resultPanelHead"><h2>Динамика основных показателей</h2><select aria-label="Группировка графика" value={granularity} onChange={e => setGranularity(e.target.value)}><option value="day">По дням</option><option value="week">По неделям</option><option value="month">По месяцам</option></select></div><div className="resultTabs">{METRICS.map(m => <button key={m.key} className={metric === m.key ? "active" : ""} onClick={() => setMetric(m.key)}>{m.label.replace(" на маркетинг", "")}</button>)}</div><MetricChart current={data.current.trend} previous={data.previous.trend} metric={metric} label={METRICS.find(item => item.key === metric)?.label || metric}/><div className="resultLegend"><span><i className="now"/>Текущий период</span><span><i className="prior"/>Прошлый период</span></div></article>
          <article className="resultPanel resultFunnel"><div className="resultPanelHead"><h2>Воронка продаж</h2><Link href={leadsHref}>Все лиды ↗</Link></div><div className="resultFunnelSteps">{data.funnel.map((stage, i) => <div className={`resultFunnelStep stage${i}`} key={stage.label}><div><span>{stage.label}</span><strong>{count(stage.value)}</strong></div>{i > 0 && <small>{stage.conversion == null ? "—" : `${stage.conversion.toFixed(0)}%`} от предыдущего этапа</small>}</div>)}</div><p className="resultHint">Квалификацию и продажу подтверждает отдел продаж.</p></article>
          <article className="resultPanel resultEconomics"><div className="resultPanelHead"><h2>Экономика бизнеса</h2><span>Модель проекта</span></div>{data.economics.active ? <><dl><div><dt>Средний чек модели</dt><dd>{money(data.economics.average_order_value)}</dd></div><div><dt>Маржинальность</dt><dd>{data.economics.margin == null ? "—" : `${data.economics.margin}%`}</dd></div><div><dt>Допустимый CAC</dt><dd>{money(data.economics.allowable_cac)}</dd></div><div><dt>Точка безубыточности</dt><dd>{data.economics.break_even_sales == null ? "—" : `${count(data.economics.break_even_sales)} клиентов / мес`}</dd></div></dl><hr/><dl><div><dt>Фактический CAC</dt><dd>{money(current?.cac)}</dd></div><div><dt>Фактический ROMI</dt><dd>{percent(current?.romi)}</dd></div></dl></> : <p className="resultEmptyEconomy">Экономическая модель проекта пока не заполнена. ROMI появится после её сохранения.</p>}{data.viewer.can_manage_economics && <button className="resultOutlineLink resultEconomicsAction" onClick={openProjectEconomics}>{data.economics.active ? "Изменить модель проекта" : "Заполнить экономическую модель"}</button>}{data.viewer.role === "admin" && <button className="resultOutlineLink" onClick={openEconomics}>Подключить готовый расчёт /growth</button>}</article></section>
        <section className="resultPanel resultChannels" id="channels"><div className="resultPanelHead"><h2>Результат по каналам</h2>{data.viewer.can_manage_sources && <button className="resultAdd" onClick={() => setSourceForm(true)}>＋ Источник</button>}</div><div className="resultTableScroll"><table><thead><tr>{["Канал", "Расходы", "Лиды", "Квал. лиды", "Продажи", "Выручка", "CPL", "CPQL", "CAC", "ROMI", "Динамика"].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>{data.current.sources.map(s => { const priorSource = data.previous.sources?.find(p => p.id === s.id); const trendMetric = s.revenue != null ? "revenue" : "leads"; return <tr key={s.id}><td><strong>{s.name}</strong><small>{s.kind === "UNKNOWN" ? "Без атрибуции" : `${s.kind} · ${s.method}`}{s.data_mode === "aggregated/manual" ? " · агрегировано" : ""}</small>{data.viewer.can_manage_sources && s.id.startsWith("source:") && <button className="resultRowAction" onClick={() => setMetricSource(s)}>Добавить данные</button>}</td><td>{money(s.spend)}</td><td>{count(s.leads)}</td><td>{count(s.qualified)}</td><td>{count(s.sales)}</td><td>{money(s.revenue)}</td><td>{money(s.cpl)}</td><td>{money(s.cpql)}</td><td>{money(s.cac)}</td><td>{percent(s.romi)}</td><td title={`Изменение ${trendMetric === "revenue" ? "выручки" : "лидов"} к прошлому периоду`}>{comparison(s[trendMetric], priorSource?.[trendMetric] ?? null)}</td></tr>; })}</tbody></table>{!data.current.sources.length && <p className="resultTableEmpty">Нет подключённых источников и данных за выбранный период.</p>}</div></section>
        <div className="resultBottom"><section className="resultPanel resultAttention"><div className="resultPanelHead"><h2>Что требует внимания</h2></div><div className="resultAlerts">{data.alerts.length ? data.alerts.map((a, i) => <Link className={`resultAlert ${a.tone}`} href={a.href === "/leads" ? leadsHref : a.href} key={i}><strong>{a.title}</strong><span>{a.detail}</span><b>Открыть →</b></Link>) : <p className="resultCalm">По доступным данным явных отклонений нет. При небольшом объёме данных предупреждения не показываются.</p>}</div></section><section className="resultPanel resultRecent"><div className="resultPanelHead"><h2>Последние лиды</h2><Link href={leadsHref}>Все лиды →</Link></div><div className="resultTableScroll"><table><thead><tr><th>Лид</th><th>Источник</th><th>Дата</th><th>Статус</th><th>Сумма</th></tr></thead><tbody>{data.recent_leads.map(lead => <tr key={lead.id}><td>{lead.name}</td><td>{lead.source}</td><td>{new Date(lead.created_at).toLocaleDateString("ru-RU")}</td><td><span className={`resultStatus ${lead.status}`}>{lead.status === "sale" ? "Продажа" : lead.status === "qualified" ? "Квалифицирован" : lead.status === "lost" ? "Потерян" : "Лид"}</span></td><td>{money(lead.amount)}</td></tr>)}</tbody></table>{!data.recent_leads.length && <p className="resultTableEmpty">Лидов за выбранный период пока нет.</p>}</div></section></div>
      </>}
    </main>
    {sourceForm && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) setSourceForm(false); }}><form className="resultModal" onSubmit={createSource}><header><h2>Новый источник</h2><button type="button" onClick={() => setSourceForm(false)}>×</button></header><p>Добавьте источник проекта. Его данные появятся в общей аналитике после подключения или ввода.</p><label>Название<input name="name" required minLength={2}/></label><label>Тип<select name="kind"><option value="CUSTOM">Кастомный</option><option value="MANUAL">Ручной</option><option value="INTERNAL">Внутренний</option></select></label><label>Способ получения<select name="method"><option value="MANUAL">Ручной ввод</option><option value="FILE">Файл</option><option value="API">API</option><option value="WEBHOOK">Webhook</option><option value="BOT">Бот</option><option value="INTERNAL">Внутренний</option></select></label><button className="resultPrimary" disabled={busy}>Создать источник</button></form></div>}
    {metricSource && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) setMetricSource(null); }}><form className="resultModal" onSubmit={saveMetric}><header><h2>Данные: {metricSource.name}</h2><button type="button" onClick={() => setMetricSource(null)}>×</button></header><p>Агрегированные показатели используются только если за этот период для источника нет отдельных лидов и продаж.</p><label>Дата<input name="date" type="date" defaultValue={end} required/></label><div className="resultModalGrid">{[["spend", "Расходы"], ["impressions", "Показы"], ["clicks", "Клики"], ["aggregated_leads", "Лиды, агрегировано"], ["aggregated_qualified", "Квал. лиды, агрегировано"], ["aggregated_sales", "Продажи, агрегировано"], ["aggregated_revenue", "Выручка, агрегировано"]].map(([name, label]) => <label key={name}>{label}<input name={name} type="number" min="0" step={name.includes("revenue") || name === "spend" ? ".01" : "1"} placeholder="Нет данных"/></label>)}</div><button className="resultPrimary" disabled={busy}>Сохранить данные</button></form></div>}
    {economicsForm && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) setEconomicsForm(false); }}><form className="resultModal" onSubmit={activateEconomics}><header><h2>Модель экономики проекта</h2><button type="button" onClick={() => setEconomicsForm(false)}>×</button></header><p>Выберите сохранённый расчёт /growth, который действительно относится к этому проекту. Его данные определяют маржинальность и ROMI.</p>{growthModels.length ? <><label>Расчёт /growth<select name="growth_calculation_id" required>{growthModels.map(model => <option key={model.id} value={model.id}>{model.name} · {money(model.average_order_value)} · маржа {model.gross_margin ?? "—"}%</option>)}</select></label><label>Допустимый CAC, ₽<input name="allowable_cac" type="number" min="0" step=".01" placeholder="Нет данных"/></label><button className="resultPrimary" disabled={busy}>Подключить к проекту</button></> : <p>Сохранённых расчётов /growth пока нет.</p>}</form></div>}
    {projectEconomicsForm && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) setProjectEconomicsForm(false); }}><form className="resultModal" onSubmit={saveProjectEconomics}><header><h2>Экономика проекта</h2><button type="button" onClick={() => setProjectEconomicsForm(false)}>×</button></header><p>Заполните один раз: расчёт сохранится за текущим проектом и появится в «Результате», аналитике и настройках. Позже его можно изменить здесь.</p><div className="resultModalGrid">{[["average_order_value", "Средний чек, ₽"], ["purchases_per_customer", "Покупок на клиента"], ["gross_margin", "Валовая маржа, %"], ["fixed_costs", "Постоянные расходы в месяц, ₽"], ["current_customers", "Клиентов сейчас в месяц"], ["target_customers", "Цель клиентов в месяц"], ["growth_budget", "Бюджет роста в месяц, ₽"], ["allowable_cac", "Допустимый CAC, ₽ (необязательно)"]].map(([name, label]) => <label key={name}>{label}<input name={name} type="number" min="0" max={name === "gross_margin" ? "100" : undefined} step={["current_customers", "target_customers"].includes(name) ? "1" : ".01"} required={name !== "allowable_cac"} defaultValue={name === "allowable_cac" ? economicsValues?.allowable_cac ?? "" : economicsValues?.inputs?.[name] ?? ""} placeholder="Введите значение"/></label>)}</div><button className="resultPrimary" disabled={busy}>Сохранить модель проекта</button></form></div>}
  </div>;
}
