"use client";
// «Отчёты»: конструктор с живым превью. Галочка = учитывать в расчёте (снял — пропадает из всех итогов),
// глазик = только скрыть строку (цифры остаются в итогах). Настройки сохраняются шаблоном проекта.
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { dateInput, ProjectSidebar, ReportingProject } from "@/components/reporting";
import { autoSummary, copyForSheets, HERO, periodTitle, ReportData, ReportView, rub } from "@/components/report/report-view";
import "../result/result.css";
import "./reports.css";

type Source = { id: string; name: string; spend: number | null; leads: number | null; sales: number | null };
type Campaign = { id: string; kind?: string; name: string; connection_id: number | null; hypothesis_id: number | null;
  hypothesis_name: string | null; spend: number | null; leads: number | null };
type Catalog = { current: { sources: Source[] }; analysis: { campaigns: Campaign[] }; viewer: { role: string } };
type Saved = { id: number; title: string; token: string | null; created_at: string; start: string | null; end: string | null; headline: string | null;
  published: boolean; revenue: number | null; leads: number | null; sales: number | null; spend: number | null; expires_at: string | null };
type SavedList = { reports: Saved[]; can_manage: boolean; retention_days: number };
type Opened = { id: number; token: string | null; title: string; published: boolean; report: ReportData; can_manage: boolean };
type PlanItem = { text: string; owner: string };
type FactItem = { text: string; owner: string | null; done: boolean; note: string };
type Cost = { name: string; amount: string };
type Settings = { sections: string[]; exclude: { channels: string[]; hypotheses: number[]; campaigns: string[]; leads: number[] };
  hidden: { channels: string[]; hypotheses: number[]; campaigns: string[] }; metrics: string[]; granularity: string | null;
  bottleneck: string | null; exclude_managers: number[]; extra_costs: { name: string; amount: number }[]; budget_plan: number | null };

const SECTIONS: [string, string, string][] = [
  ["summary", "Главное за период", "вывод одной фразой и 4 большие цифры"],
  ["kpi", "Ключевые показатели", "лиды, цена лида, продажи, чек"],
  ["funnel", "Путь клиента", "воронка и узкое место"],
  ["trend", "Динамика", "расходы и заявки"],
  ["channels", "Каналы", "кто принёс деньги"],
  ["hypotheses", "Гипотезы", "сработало / не сработало / тестируем"],
  ["sales", "Отдел продаж", "скорость ответа, менеджеры, просрочки"],
  ["plan", "План", "что обещали, что дальше, прогноз"],
];
const OWNERS = ["Агентство", "Отдел продаж", "Решение клиента"];
const DEFAULT_METRICS = ["revenue", "romi", "spend", "cac"];

function lastMonth() {
  const now = new Date();
  return [dateInput(new Date(now.getFullYear(), now.getMonth() - 1, 1)), dateInput(new Date(now.getFullYear(), now.getMonth(), 0))];
}
const fmtDate = (s: string | null) => s ? new Date(`${s}T12:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "numeric" }) : "";
const short = (n: number | null) => n == null ? "" : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(n)}`;
const toNumber = (v: string) => { const n = Number(String(v).replace(/\s/g, "").replace(",", ".")); return Number.isFinite(n) && v.trim() ? n : null; };

function Eye({ off, onClick, label }: { off: boolean; onClick: () => void; label: string }) {
  return <button type="button" className={`reportsEye ${off ? "off" : ""}`} aria-pressed={off} aria-label={off ? `Показать «${label}» в отчёте` : `Скрыть «${label}» из отчёта (цифры останутся в итогах)`}
    title={off ? "Скрыто из отчёта, цифры в итогах" : "Скрыть строку, цифры оставить в итогах"} onClick={e => { e.preventDefault(); e.stopPropagation(); onClick(); }}>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" />{off && <path d="M3 3l18 18" />}</svg>
  </button>;
}

export default function ReportsPage() {
  const [projects, setProjects] = useState<ReportingProject[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [[start, end], setPeriod] = useState<string[]>(lastMonth);
  const [sections, setSections] = useState<string[]>(SECTIONS.map(s => s[0]));
  const [channels, setChannels] = useState<string[]>([]);
  const [hypotheses, setHypotheses] = useState<number[]>([]);
  const [campaigns, setCampaigns] = useState<string[]>([]);
  const [deals, setDeals] = useState<number[]>([]);
  const [hiddenChannels, setHiddenChannels] = useState<string[]>([]);
  const [hiddenHyps, setHiddenHyps] = useState<number[]>([]);
  const [managersOff, setManagersOff] = useState<number[]>([]);
  const [metrics, setMetrics] = useState<string[]>(DEFAULT_METRICS);
  const [granularity, setGranularity] = useState("");
  const [bottleneck, setBottleneck] = useState("auto");
  const [costs, setCosts] = useState<Cost[]>([]);
  const [budget, setBudget] = useState("");
  const [comments, setComments] = useState<Record<string, string>>({});
  const [openComment, setOpenComment] = useState<string | null>(null);
  const [summary, setSummary] = useState("");
  const [plan, setPlan] = useState<PlanItem[]>([{ text: "", owner: "Агентство" }]);
  const [fact, setFact] = useState<FactItem[]>([]);
  const [lastTitle, setLastTitle] = useState<string | null>(null);
  const [forecast, setForecast] = useState({ spend: "", leads: "", sales: "", revenue: "" });
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [report, setReport] = useState<ReportData | null>(null);
  const [saved, setSaved] = useState<Saved[]>([]);
  const [canManage, setCanManage] = useState<boolean | null>(null);
  const [retention, setRetention] = useState(90);
  const [publishNow, setPublishNow] = useState(true);
  const [opened, setOpened] = useState<Opened | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    api<ReportingProject[]>("/result/projects").then(rows => {
      setProjects(rows);
      const desired = Number(new URLSearchParams(window.location.search).get("project_id"));
      setProjectId(rows.find(row => row.id === desired)?.id || rows[0]?.id || null);
    }).catch(exc => setError(exc.message));
  }, []);
  const loadSaved = (id: number) => api<SavedList>(`/reports?project_id=${id}`)
    .then(data => { setSaved(data.reports); setCanManage(data.can_manage); setRetention(data.retention_days); })
    .catch(exc => { setSaved([]); setCanManage(false); setError(exc.message); });
  function applySettings(t: Partial<Settings> | null) {
    setSections(t?.sections?.length ? t.sections : SECTIONS.map(s => s[0]));
    setChannels(t?.exclude?.channels || []); setHypotheses(t?.exclude?.hypotheses || []); setCampaigns(t?.exclude?.campaigns || []); setDeals([]);
    setHiddenChannels(t?.hidden?.channels || []); setHiddenHyps(t?.hidden?.hypotheses || []);
    setMetrics(t?.metrics?.length ? t.metrics : DEFAULT_METRICS); setGranularity(t?.granularity || ""); setBottleneck(t?.bottleneck || "auto");
    setManagersOff(t?.exclude_managers || []); setCosts((t?.extra_costs || []).map(c => ({ name: c.name, amount: String(c.amount) })));
    setBudget(t?.budget_plan ? String(t.budget_plan) : "");
  }
  // Новый проект: шаблон (если сохраняли) и план из прошлого отчёта для «Что обещали в прошлый раз».
  useEffect(() => { if (projectId) { setCanManage(null); loadSaved(projectId); } }, [projectId]);
  useEffect(() => {
    if (!projectId || !canManage) return;
    api<{ template: Partial<Settings> | null; last_plan: { text: string; owner: string | null }[]; last_title: string | null }>(`/reports/template?project_id=${projectId}`)
      .then(data => { applySettings(data.template); setFact(data.last_plan.map(p => ({ text: p.text, owner: p.owner, done: false, note: "" }))); setLastTitle(data.last_title); })
      .catch(() => applySettings(null));
  }, [projectId, canManage]);
  useEffect(() => {
    if (!projectId || !canManage || !start || !end || start > end) return;
    const query = new URLSearchParams({ project_id: String(projectId), start, end });
    api<Catalog>(`/analytics?${query}`).then(setCatalog).catch(exc => setError(exc.message));
  }, [projectId, start, end, canManage]);

  const settings: Settings = useMemo(() => ({ sections, exclude: { channels, hypotheses, campaigns, leads: deals },
    hidden: { channels: hiddenChannels, hypotheses: hiddenHyps, campaigns: [] }, metrics, granularity: granularity || null,
    bottleneck: bottleneck === "auto" ? null : bottleneck, exclude_managers: managersOff,
    extra_costs: costs.map(c => ({ name: c.name.trim(), amount: toNumber(c.amount) || 0 })).filter(c => c.name && c.amount > 0),
    budget_plan: toNumber(budget) }), [sections, channels, hypotheses, campaigns, deals, hiddenChannels, hiddenHyps, metrics, granularity, bottleneck, managersOff, costs, budget]);
  const body = useMemo(() => {
    const fc = { spend: toNumber(forecast.spend), leads: toNumber(forecast.leads), sales: toNumber(forecast.sales), revenue: toNumber(forecast.revenue) };
    return { project_id: projectId, start, end, ...settings, summary: summary.trim() || null,
      comments: Object.fromEntries(Object.entries(comments).filter(([, v]) => v.trim())),
      forecast: Object.values(fc).some(v => v != null) ? fc : null,
      plan: plan.filter(p => p.text.trim()).map(p => ({ text: p.text.trim(), owner: p.owner || null })),
      plan_fact: fact.filter(p => p.text.trim()).map(p => ({ text: p.text, owner: p.owner, done: p.done, note: p.note.trim() || null })) };
  }, [projectId, start, end, settings, summary, comments, forecast, plan, fact]);
  useEffect(() => {
    if (!projectId || !canManage || !start || !end || start > end) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      api<ReportData>("/reports/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), timeoutMs: 60_000 })
        .then(value => { setReport(value); setError(""); }).catch(exc => setError(exc.message));
    }, 500);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [body, projectId, start, end, canManage]);

  const tree = useMemo(() => {
    if (!catalog) return [];
    const ads = catalog.analysis.campaigns.filter(c => c.kind !== "channel");
    return catalog.current.sources.filter(s => s.spend || s.leads || s.sales).map(source => {
      const connectionId = source.id.startsWith("ad:") ? Number(source.id.slice(3)) : null;
      const groups = new Map<string, { id: number | null; name: string; items: Campaign[] }>();
      for (const c of connectionId ? ads.filter(c => c.connection_id === connectionId) : []) {
        const key = c.hypothesis_id ? String(c.hypothesis_id) : "none";
        const g = groups.get(key) || { id: c.hypothesis_id, name: c.hypothesis_name || "Без гипотезы", items: [] };
        g.items.push(c); groups.set(key, g);
      }
      return { source, groups: [...groups.values()].sort((a, b) => (a.id ? 0 : 1) - (b.id ? 0 : 1)) };
    });
  }, [catalog]);
  const toggle = <T,>(list: T[], value: T, set: (v: T[]) => void) => set(list.includes(value) ? list.filter(v => v !== value) : [...list, value]);
  const excludedCount = channels.length + hypotheses.length + campaigns.length + deals.length;
  const paidOnly = () => setChannels(tree.filter(({ source }) => !source.id.startsWith("ad:")).map(({ source }) => source.id));
  const pickMetric = (key: string) => setMetrics(metrics.includes(key) ? metrics.filter(k => k !== key) : metrics.length >= 4 ? [...metrics.slice(1), key] : [...metrics, key]);

  async function save() {
    if (!projectId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const created = await api<{ id: number; token: string; title: string }>("/reports", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), timeoutMs: 60_000 });
      if (publishNow) await api(`/reports/${created.id}/publish`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ published: true }) });
      await loadSaved(projectId);
      const link = `${window.location.origin}/r/${created.token}`;
      await navigator.clipboard.writeText(link).catch(() => undefined);
      setNotice(`${publishNow ? "Отчёт опубликован в портале клиента." : "Отчёт сохранён черновиком — клиент его пока не видит."} Ссылка скопирована: ${link}`);
    } catch (exc) { setError((exc as Error).message); } finally { setBusy(false); }
  }
  async function saveTemplate() {
    if (!projectId) return;
    try { await api("/reports/template", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId, ...settings, exclude: { ...settings.exclude, leads: [] } }) });
      setNotice("Шаблон сохранён: в следующий раз отчёт по этому проекту соберётся с этими настройками."); }
    catch (exc) { setError((exc as Error).message); }
  }
  async function open(id: number) {
    setError("");
    try { const data = await api<Opened>(`/reports/${id}`); setOpened(data); window.scrollTo({ top: 0 }); }
    catch (exc) { setError((exc as Error).message); }
  }
  async function togglePublish(item: Opened) {
    try {
      await api(`/reports/${item.id}/publish`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ published: !item.published }) });
      setOpened({ ...item, published: !item.published }); if (projectId) loadSaved(projectId);
      setNotice(item.published ? "Отчёт снят с публикации — клиент его больше не видит в портале." : "Отчёт опубликован в портале клиента.");
    } catch (exc) { setError((exc as Error).message); }
  }
  async function remove(id: number) {
    if (!window.confirm("Удалить отчёт? Ссылка у клиента перестанет открываться.")) return;
    try { await api(`/reports/${id}`, { method: "DELETE" }); setOpened(null); if (projectId) loadSaved(projectId); }
    catch (exc) { setError((exc as Error).message); }
  }
  async function copySheets(r: ReportData) {
    try { await copyForSheets(r); setNotice("Скопировано. Откройте Google Таблицу, встаньте в ячейку A1 и нажмите Ctrl+V (⌘V)."); }
    catch { setError("Браузер не дал доступ к буферу обмена — разрешите копирование и попробуйте ещё раз."); }
  }
  const selectedProject = projects.find(p => p.id === projectId);
  const shown = opened?.report || report;
  // Клиент: последний отчёт крупно, остальные — по месяцам периода.
  const months = useMemo(() => {
    const groups = new Map<string, Saved[]>();
    for (const s of saved.slice(1)) {
      const anchor = s.end || s.created_at.slice(0, 10);
      const label = new Date(`${anchor.slice(0, 10)}T12:00:00`).toLocaleDateString("ru-RU", { month: "long", year: "numeric" }).replace(" г.", "");
      groups.set(label, [...(groups.get(label) || []), s]);
    }
    return [...groups.entries()];
  }, [saved]);
  const fmtNum = (n: number | null) => n == null ? "—" : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(n);
  const latest = saved[0];
  const clientView = opened ? <div className="reportsOpened">
      <div className="reportsActions"><button type="button" onClick={() => setOpened(null)}>← Все отчёты</button>
        <button type="button" onClick={() => copySheets(opened.report)}>Скопировать для Google Таблиц</button>
        <button type="button" onClick={() => window.print()}>Скачать PDF</button></div>
      <ReportView report={{ ...opened.report, title: opened.title }} />
    </div>
    : !latest ? <section className="reportsEmpty"><h2>Отчётов пока нет</h2><p>Команда StepToLead публикует отчёт по итогам периода — он появится здесь. Цифры в реальном времени — в «Результате» и «Аналитике».</p></section>
    : <div className="reportsClient">
      <button type="button" className="reportsLatest" onClick={() => open(latest.id)}>
        <span className="reportsLatestTag">ПОСЛЕДНИЙ ОТЧЁТ · {fmtDate(latest.start)} — {fmtDate(latest.end)}</span>
        <strong className="reportsLatestTitle">{latest.title}</strong>
        {latest.headline && <span className="reportsLatestHead">{latest.headline}</span>}
        <span className="reportsLatestNums">
          <span><small>Выручка</small><b>{latest.revenue != null ? `${fmtNum(latest.revenue)} ₽` : "—"}</b></span>
          <span><small>Лиды</small><b>{fmtNum(latest.leads)}</b></span>
          <span><small>Продажи</small><b>{fmtNum(latest.sales)}</b></span>
          <span><small>Расходы</small><b>{latest.spend != null ? `${fmtNum(latest.spend)} ₽` : "—"}</b></span>
        </span>
        <span className="reportsLatestOpen">Открыть отчёт →</span>
      </button>
      {months.map(([label, items]) => <section key={label} className="reportsMonth" aria-label={label}><h2>{label}</h2>
        <div className="reportsGrid">{items.map(s => <button key={s.id} type="button" className="reportsCard" onClick={() => open(s.id)}>
          <strong>{s.title}</strong>{s.headline && <span>{s.headline}</span>}
          <small>{fmtDate(s.start)} — {fmtDate(s.end)}{s.expires_at ? ` · доступен до ${fmtDate(s.expires_at.slice(0, 10))}` : ""}</small>
        </button>)}</div></section>)}
      <p className="reportsMuted">Отчёты хранятся {retention} дней. Нужен более старый — напишите менеджеру StepToLead.</p>
    </div>;
  const funnelLabels = report ? [...(report.clicks ? ["Клики"] : []), ...report.funnel.map(f => f.label)].slice(1) : [];

  const sectionExtra = (key: string) => {
    if (key === "summary") return <div className="reportsInline"><span>Главные 4 цифры:</span><div className="reportsChips">{Object.entries(HERO).map(([k, h]) =>
      <button key={k} type="button" aria-pressed={metrics.includes(k)} className={metrics.includes(k) ? "on" : ""} onClick={() => pickMetric(k)}>{h.label}</button>)}</div></div>;
    if (key === "trend") return <label className="reportsInline">Шаг графика<select value={granularity} onChange={e => setGranularity(e.target.value)}><option value="">Автоматически</option><option value="day">Дни</option><option value="week">Недели</option><option value="month">Месяцы</option></select></label>;
    if (key === "funnel") return <label className="reportsInline">Узкое место<select value={bottleneck} onChange={e => setBottleneck(e.target.value)}><option value="auto">Определить автоматически</option><option value="hide">Не показывать</option>{funnelLabels.map(l => <option key={l} value={l}>Перед этапом «{l}»</option>)}</select></label>;
    return null;
  };

  return <div className="resultShell reportsShell"><ProjectSidebar project={selectedProject} projectId={projectId} active="reports" role={catalog?.viewer.role} />
    <main className="resultMain">
      <div className="resultBreadcrumb">StepToLead <span>/</span> Отчёты</div>
      <header className="resultHeader reportsHeader"><div><h1>Отчёты</h1><p>{canManage ? "Цифры подтягиваются из рекламы, заявок и CRM. Галочка — учитывать в расчёте, глазик — только скрыть строку. Всё, что не вошло в расчёт, отчёт честно перечислит в конце."
          : "Отчёты команды StepToLead по итогам периодов: что получилось, что дальше и всё в цифрах."}</p></div>
        {projects.length > 1 && <label className="resultSelect"><span>Проект</span><select value={projectId || ""} onChange={e => { setProjectId(Number(e.target.value)); setOpened(null); }}>{projects.map(p => <option key={p.id} value={p.id}>{p.organization_name} · {p.name}</option>)}</select></label>}
      </header>
      {error && <div className="resultError" role="alert">{error}</div>}
      {notice && <div className="reportsNotice" role="status">{notice}</div>}

      {canManage === false ? clientView : canManage === null ? <p className="reportsMuted">Загружаем отчёты…</p> : <>
      {saved.length > 0 && <section className="reportsSaved" aria-label="Сохранённые отчёты">
        {saved.map(s => <div key={s.id} className={`reportsSavedItem ${opened?.id === s.id ? "active" : ""}`}>
          <button type="button" onClick={() => open(s.id)}><em className={s.published ? "on" : ""}>{s.published ? "опубликован" : "черновик"}</em><strong>{s.title}</strong><span>{s.headline || ""}</span><small>{fmtDate(s.start)} — {fmtDate(s.end)}</small></button>
        </div>)}
      </section>}

      {opened ? <div className="reportsOpened">
        <div className="reportsActions">
          <button type="button" onClick={() => setOpened(null)}>← К конструктору</button>
          <button type="button" className={opened.published ? "" : "primary"} onClick={() => togglePublish(opened)}>{opened.published ? "Снять с публикации" : "Опубликовать клиенту"}</button>
          <button type="button" onClick={() => { navigator.clipboard.writeText(`${window.location.origin}/r/${opened.token}`); setNotice("Ссылка для клиента скопирована."); }}>Скопировать ссылку</button>
          <Link href={`/r/${opened.token}`} target="_blank">Открыть как клиент ↗</Link>
          <button type="button" onClick={() => copySheets(opened.report)}>Скопировать для Google Таблиц</button>
          <button type="button" onClick={() => window.print()}>Скачать PDF</button>
          <button type="button" className="danger" onClick={() => remove(opened.id)}>Удалить</button>
        </div>
        <ReportView report={{ ...opened.report, title: opened.title }} />
      </div> : <div className="reportsLayout">
        <aside className="reportsBuilder" aria-label="Настройки отчёта">
          <section><h2>1. Период</h2>
            <div className="reportsDates">
              <label>С<input type="date" value={start} max={end} onChange={e => setPeriod([e.target.value, end])} /></label>
              <label>По<input type="date" value={end} min={start} onChange={e => setPeriod([start, e.target.value])} /></label>
            </div>
            <small>Сравнение — с таким же периодом до этого.</small>
          </section>

          <section><h2>2. Что показать</h2>
            {SECTIONS.map(([key, label, hint]) => <div key={key} className="reportsSection">
              <label className="reportsCheck"><input type="checkbox" checked={sections.includes(key)} onChange={() => toggle(sections, key, setSections)} /><span><strong>{label}</strong> — {hint}</span>
                <button type="button" className={`reportsCommentBtn ${comments[key] ? "has" : ""}`} onClick={e => { e.preventDefault(); setOpenComment(openComment === key ? null : key); }}>{comments[key] ? "комментарий ✓" : "+ комментарий"}</button></label>
              {sections.includes(key) && sectionExtra(key)}
              {openComment === key && <textarea className="reportsComment" rows={2} maxLength={600} placeholder="Например: 15-го лежал сайт — заявок не было, бюджет остановили" value={comments[key] || ""} onChange={e => setComments({ ...comments, [key]: e.target.value })} aria-label={`Комментарий к блоку «${label}»`} />}
            </div>)}
          </section>

          <section><h2>3. Что учесть {excludedCount > 0 && <em>убрано из расчёта: {excludedCount}</em>}</h2>
            <small>Галочка — учитывать в расчёте. Глазик — скрыть строку, но оставить цифры в итогах.</small>
            <div className="reportsPresets"><button type="button" onClick={paidOnly}>Только платная реклама</button><button type="button" onClick={() => { setChannels([]); setHypotheses([]); setCampaigns([]); setDeals([]); setHiddenChannels([]); setHiddenHyps([]); }}>Учитывать всё</button></div>
            {!catalog ? <p className="reportsMuted">Загружаем каналы…</p> : !tree.length ? <p className="reportsMuted">За период нет данных.</p> :
              <div className="reportsTree">{tree.map(({ source, groups }) => {
                const offChannel = channels.includes(source.id);
                return <details key={source.id} open={groups.length > 0 && !offChannel}>
                  <summary><label className="reportsNode" onClick={e => e.stopPropagation()}><input type="checkbox" checked={!offChannel} onChange={() => toggle(channels, source.id, setChannels)} /><strong>{source.name}</strong></label>
                    <span className="reportsNodeMeta">{source.spend ? `${short(source.spend)} ₽ · ` : ""}{short(source.leads) || 0} лид.</span>
                    {!offChannel && <Eye off={hiddenChannels.includes(source.id)} label={source.name} onClick={() => toggle(hiddenChannels, source.id, setHiddenChannels)} />}</summary>
                  {groups.map(g => {
                    const offHyp = g.id != null && hypotheses.includes(g.id);
                    return <div key={g.id ?? "none"} className="reportsLevel">
                      {g.id != null ? <div className="reportsRow"><label className="reportsNode"><input type="checkbox" disabled={offChannel} checked={!offChannel && !offHyp} onChange={() => toggle(hypotheses, g.id as number, setHypotheses)} /><span>Гипотеза «{g.name}»</span></label>
                        <Eye off={hiddenHyps.includes(g.id)} label={g.name} onClick={() => toggle(hiddenHyps, g.id as number, setHiddenHyps)} /></div>
                        : <div className="reportsNode reportsMuted">Кампании без гипотезы</div>}
                      {offHyp && <small className="reportsHint">Карточка гипотезы останется в отчёте с пометкой «не в итогах».</small>}
                      <div className="reportsLevel">{g.items.map(c => <label key={c.id} className="reportsNode small"><input type="checkbox" disabled={offChannel || offHyp} checked={!offChannel && !offHyp && !campaigns.includes(c.id)} onChange={() => toggle(campaigns, c.id, setCampaigns)} />
                        <span>{c.name}</span><span className="reportsNodeMeta">{c.spend ? `${short(c.spend)} ₽` : ""}{c.leads ? ` · ${c.leads} лид.` : ""}</span></label>)}</div>
                    </div>;
                  })}
                </details>;
              })}</div>}
            {!!report?.sales_list?.length && <div className="reportsSub"><strong>Крупные продажи</strong><small>Снимите случайную сделку, которая искажает средний чек и ROMI.</small>
              {report.sales_list.map(s => <label key={s.lead_id} className="reportsNode"><input type="checkbox" checked={!deals.includes(s.lead_id)} onChange={() => toggle(deals, s.lead_id, setDeals)} />
                <span>{s.name}</span><span className="reportsNodeMeta">{rub(s.amount)}</span></label>)}</div>}
            {!!report?.sales_team.all_managers?.length && <div className="reportsSub"><strong>Менеджеры в блоке «Отдел продаж»</strong><small>Снимите РОПа с тестовыми лидами или уволенного сотрудника.</small>
              {report.sales_team.all_managers.map(m => <label key={m.id} className="reportsNode"><input type="checkbox" checked={!managersOff.includes(m.id)} onChange={() => toggle(managersOff, m.id, setManagersOff)} /><span>{m.name}</span></label>)}</div>}
          </section>

          <section><h2>4. Деньги</h2>
            <div className="reportsField">Доп. расходы — входят в ROMI и стоимость продажи
              {costs.map((c, i) => <div key={i} className="reportsCostRow">
                <input value={c.name} maxLength={120} placeholder="Гонорар агентства" onChange={e => setCosts(costs.map((x, j) => j === i ? { ...x, name: e.target.value } : x))} aria-label="Статья расходов" />
                <input value={c.amount} inputMode="decimal" placeholder="₽" onChange={e => setCosts(costs.map((x, j) => j === i ? { ...x, amount: e.target.value } : x))} aria-label="Сумма" />
                <button type="button" aria-label="Убрать" onClick={() => setCosts(costs.filter((_, j) => j !== i))}>×</button></div>)}
              {costs.length < 20 && <button type="button" className="reportsAdd" onClick={() => setCosts([...costs, { name: "", amount: "" }])}>+ Расход</button>}
            </div>
            <label className="reportsField">План бюджета на период, ₽<input value={budget} inputMode="decimal" placeholder="Например, 200 000" onChange={e => setBudget(e.target.value)} /></label>
          </section>

          <section><h2>5. Текст для клиента</h2>
            <label className="reportsField">Главное — можно оставить пустым, напишем по цифрам
              <textarea rows={3} maxLength={1500} value={summary} onChange={e => setSummary(e.target.value)} placeholder={report ? autoSummary(report) : ""} /></label>
            {fact.length > 0 && <div className="reportsField">Что обещали в прошлый раз{lastTitle ? ` («${lastTitle}»)` : ""}
              {fact.map((p, i) => <div key={i} className="reportsFactRow">
                <label className="reportsNode"><input type="checkbox" checked={p.done} onChange={() => setFact(fact.map((x, j) => j === i ? { ...x, done: !x.done } : x))} /><span>{p.text}</span></label>
                <input value={p.note} maxLength={300} placeholder="Комментарий: что получилось" onChange={e => setFact(fact.map((x, j) => j === i ? { ...x, note: e.target.value } : x))} aria-label={`Комментарий к «${p.text}»`} />
              </div>)}</div>}
            <div className="reportsField">План на следующий период
              {plan.map((item, i) => <div key={i} className="reportsPlanRow">
                <input value={item.text} maxLength={300} placeholder={i === 0 ? "Например: +30% бюджета на лучшую кампанию" : "Следующий шаг"} onChange={e => setPlan(plan.map((p, j) => j === i ? { ...p, text: e.target.value } : p))} aria-label={`Шаг ${i + 1}`} />
                <select value={item.owner} onChange={e => setPlan(plan.map((p, j) => j === i ? { ...p, owner: e.target.value } : p))} aria-label="Кто отвечает">{OWNERS.map(o => <option key={o}>{o}</option>)}</select>
                <button type="button" aria-label="Убрать шаг" onClick={() => setPlan(plan.length > 1 ? plan.filter((_, j) => j !== i) : [{ text: "", owner: "Агентство" }])}>×</button>
              </div>)}
              {plan.length < 12 && <button type="button" className="reportsAdd" onClick={() => setPlan([...plan, { text: "", owner: "Агентство" }])}>+ Шаг</button>}
            </div>
            <div className="reportsField">Прогноз на следующий период
              <div className="reportsForecast">{([["spend", "Бюджет, ₽"], ["leads", "Лиды"], ["sales", "Продажи"], ["revenue", "Выручка, ₽"]] as const).map(([k, l]) =>
                <label key={k}>{l}<input value={forecast[k]} inputMode="decimal" onChange={e => setForecast({ ...forecast, [k]: e.target.value })} /></label>)}</div>
            </div>
          </section>

          <div className="reportsBuilderActions">
            <label className="reportsPublishNow"><input type="checkbox" checked={publishNow} onChange={() => setPublishNow(!publishNow)} />Сразу опубликовать в портале клиента</label>
            <button type="button" className="primary" disabled={busy || !report} onClick={save}>{busy ? "Сохраняем…" : publishNow ? "Сохранить и опубликовать" : "Сохранить черновик"}</button>
            <button type="button" disabled={!report} onClick={() => report && copySheets(report)}>Скопировать для Google Таблиц</button>
            <button type="button" disabled={!report} onClick={() => window.print()}>Скачать PDF</button>
            <button type="button" onClick={saveTemplate}>Сохранить настройки как шаблон проекта</button>
          </div>
        </aside>
        <section className="reportsPreview" aria-label="Превью отчёта">
          <div className="reportsPreviewLabel">ТАК УВИДИТ КЛИЕНТ{report ? ` · ${periodTitle(report)}` : ""}</div>
          {shown ? <ReportView report={shown} /> : <p className="reportsMuted">Собираем отчёт…</p>}
        </section>
      </div>}
      </>}
    </main>
  </div>;
}
