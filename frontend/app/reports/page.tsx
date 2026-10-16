"use client";
// «Отчёты»: конструктор (даты, разделы, матрёшка исключений, текст и план) с живым превью,
// сохранённые отчёты со ссылкой для клиента, копирование в Google Таблицы и PDF через печать.
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { dateInput, ProjectSidebar, ReportingProject } from "@/components/reporting";
import { autoSummary, copyForSheets, periodTitle, ReportData, ReportView } from "@/components/report/report-view";
import "../result/result.css";
import "./reports.css";

type Source = { id: string; name: string; spend: number | null; leads: number | null; sales: number | null };
type Campaign = { id: string; kind?: string; name: string; connection_id: number | null; hypothesis_id: number | null;
  hypothesis_name: string | null; spend: number | null; leads: number | null };
type Catalog = { current: { sources: Source[] }; analysis: { campaigns: Campaign[] }; viewer: { role: string } };
type Saved = { id: number; title: string; token: string; created_at: string; start: string | null; end: string | null; headline: string | null };
type PlanItem = { text: string; owner: string };

const SECTIONS: [string, string, string][] = [
  ["summary", "Главное за период", "вывод одной фразой и 4 большие цифры"],
  ["kpi", "Ключевые показатели", "лиды, цена лида, продажи, чек"],
  ["funnel", "Путь клиента", "воронка и узкое место"],
  ["trend", "Динамика", "расходы и заявки по дням или неделям"],
  ["channels", "Каналы", "кто принёс деньги"],
  ["hypotheses", "Гипотезы", "сработало / не сработало / тестируем"],
  ["sales", "Отдел продаж", "скорость ответа, менеджеры"],
  ["plan", "План на следующий период", "шаги и кто отвечает"],
];
const OWNERS = ["Агентство", "Отдел продаж", "Решение клиента"];

function lastMonth() {
  const now = new Date();
  return [dateInput(new Date(now.getFullYear(), now.getMonth() - 1, 1)), dateInput(new Date(now.getFullYear(), now.getMonth(), 0))];
}
const fmtDate = (s: string | null) => s ? new Date(`${s}T12:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "numeric" }) : "";
const short = (n: number | null) => n == null ? "" : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(n)}`;

export default function ReportsPage() {
  const [projects, setProjects] = useState<ReportingProject[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [[start, end], setPeriod] = useState<string[]>(lastMonth);
  const [sections, setSections] = useState<string[]>(SECTIONS.map(s => s[0]));
  const [channels, setChannels] = useState<string[]>([]);
  const [hypotheses, setHypotheses] = useState<number[]>([]);
  const [campaigns, setCampaigns] = useState<string[]>([]);
  const [summary, setSummary] = useState("");
  const [plan, setPlan] = useState<PlanItem[]>([{ text: "", owner: "Агентство" }]);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [report, setReport] = useState<ReportData | null>(null);
  const [saved, setSaved] = useState<Saved[]>([]);
  const [opened, setOpened] = useState<{ id: number; token: string; title: string; report: ReportData } | null>(null);
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
  const loadSaved = (id: number) => api<Saved[]>(`/reports?project_id=${id}`).then(setSaved).catch(() => setSaved([]));
  useEffect(() => { if (projectId) loadSaved(projectId); }, [projectId]);
  // Каталог для «матрёшки»: каналы, гипотезы и кампании за выбранный период (без исключений).
  useEffect(() => {
    if (!projectId || !start || !end || start > end) return;
    const query = new URLSearchParams({ project_id: String(projectId), start, end });
    api<Catalog>(`/analytics?${query}`).then(setCatalog).catch(exc => setError(exc.message));
  }, [projectId, start, end]);

  const body = useMemo(() => ({ project_id: projectId, start, end, sections, summary: summary.trim() || null,
    exclude: { channels, hypotheses, campaigns }, plan: plan.filter(p => p.text.trim()).map(p => ({ text: p.text.trim(), owner: p.owner || null })) }),
    [projectId, start, end, sections, summary, channels, hypotheses, campaigns, plan]);
  // Живое превью: пересобираем через полсекунды после последней правки.
  useEffect(() => {
    if (!projectId || !start || !end || start > end) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      api<ReportData>("/reports/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), timeoutMs: 60_000 })
        .then(value => { setReport(value); setError(""); }).catch(exc => setError(exc.message));
    }, 500);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [body, projectId, start, end]);

  const tree = useMemo(() => {
    if (!catalog) return [];
    const ads = catalog.analysis.campaigns.filter(c => c.kind !== "channel");
    return catalog.current.sources.filter(s => s.spend || s.leads || s.sales).map(source => {
      const connectionId = source.id.startsWith("ad:") ? Number(source.id.slice(3)) : null;
      const own = connectionId ? ads.filter(c => c.connection_id === connectionId) : [];
      const groups = new Map<string, { id: number | null; name: string; items: Campaign[] }>();
      for (const c of own) {
        const key = c.hypothesis_id ? String(c.hypothesis_id) : "none";
        const g = groups.get(key) || { id: c.hypothesis_id, name: c.hypothesis_name || "Без гипотезы", items: [] };
        g.items.push(c); groups.set(key, g);
      }
      return { source, groups: [...groups.values()].sort((a, b) => (a.id ? 0 : 1) - (b.id ? 0 : 1)) };
    });
  }, [catalog]);
  const toggle = <T,>(list: T[], value: T, set: (v: T[]) => void) => set(list.includes(value) ? list.filter(v => v !== value) : [...list, value]);
  const excludedCount = channels.length + hypotheses.length + campaigns.length;

  async function save() {
    if (!projectId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const created = await api<{ id: number; token: string; title: string }>("/reports", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), timeoutMs: 60_000 });
      await loadSaved(projectId);
      const link = `${window.location.origin}/r/${created.token}`;
      await navigator.clipboard.writeText(link).catch(() => undefined);
      setNotice(`Отчёт сохранён. Ссылка для клиента скопирована: ${link}`);
    } catch (exc) { setError((exc as Error).message); } finally { setBusy(false); }
  }
  async function open(id: number) {
    setError("");
    try { const data = await api<{ id: number; token: string; title: string; report: ReportData }>(`/reports/${id}`); setOpened(data); window.scrollTo({ top: 0 }); }
    catch (exc) { setError((exc as Error).message); }
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

  return <div className="resultShell reportsShell"><ProjectSidebar project={selectedProject} projectId={projectId} active="reports" role={catalog?.viewer.role} />
    <main className="resultMain">
      <div className="resultBreadcrumb">StepToLead <span>/</span> Отчёты</div>
      <header className="resultHeader reportsHeader"><div><h1>Отчёты</h1><p>Цифры подтягиваются из рекламы, заявок и CRM. Уберите лишнее, допишите план — и отправьте клиенту ссылкой, PDF или в Google Таблицы.</p></div>
        {projects.length > 1 && <label className="resultSelect"><span>Проект</span><select value={projectId || ""} onChange={e => { setProjectId(Number(e.target.value)); setOpened(null); setChannels([]); setHypotheses([]); setCampaigns([]); }}>{projects.map(p => <option key={p.id} value={p.id}>{p.organization_name} · {p.name}</option>)}</select></label>}
      </header>
      {error && <div className="resultError" role="alert">{error}</div>}
      {notice && <div className="reportsNotice" role="status">{notice}</div>}

      {saved.length > 0 && <section className="reportsSaved" aria-label="Сохранённые отчёты">
        {saved.map(s => <div key={s.id} className={`reportsSavedItem ${opened?.id === s.id ? "active" : ""}`}>
          <button type="button" onClick={() => open(s.id)}><strong>{s.title}</strong><span>{s.headline || ""}</span><small>{fmtDate(s.start)} — {fmtDate(s.end)}</small></button>
        </div>)}
      </section>}

      {opened ? <div className="reportsOpened">
        <div className="reportsActions">
          <button type="button" onClick={() => setOpened(null)}>← К конструктору</button>
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
            {SECTIONS.map(([key, label, hint]) => <label key={key} className="reportsCheck"><input type="checkbox" checked={sections.includes(key)} onChange={() => toggle(sections, key, setSections)} /><span><strong>{label}</strong> — {hint}</span></label>)}
          </section>

          <section><h2>3. Что учесть {excludedCount > 0 && <em>убрано: {excludedCount}</em>}</h2>
            <small>Снимите галочку — канал, гипотеза или кампания не попадёт в этот отчёт: ни лиды, ни продажи, ни расходы. Аналитика портала не меняется.</small>
            {!catalog ? <p className="reportsMuted">Загружаем каналы…</p> : !tree.length ? <p className="reportsMuted">За период нет данных.</p> :
              <div className="reportsTree">{tree.map(({ source, groups }) => {
                const offChannel = channels.includes(source.id);
                return <details key={source.id} open={groups.length > 0 && !offChannel}>
                  <summary><label className="reportsNode" onClick={e => e.stopPropagation()}><input type="checkbox" checked={!offChannel} onChange={() => toggle(channels, source.id, setChannels)} /><strong>{source.name}</strong></label>
                    <span className="reportsNodeMeta">{source.spend ? `${short(source.spend)} ₽ · ` : ""}{short(source.leads) || 0} лид.</span></summary>
                  {groups.map(g => {
                    const offHyp = g.id != null && hypotheses.includes(g.id);
                    return <div key={g.id ?? "none"} className="reportsLevel">
                      {g.id != null ? <label className="reportsNode"><input type="checkbox" disabled={offChannel} checked={!offChannel && !offHyp} onChange={() => toggle(hypotheses, g.id as number, setHypotheses)} /><span>Гипотеза «{g.name}»</span></label>
                        : <div className="reportsNode reportsMuted">Кампании без гипотезы</div>}
                      <div className="reportsLevel">{g.items.map(c => <label key={c.id} className="reportsNode small"><input type="checkbox" disabled={offChannel || offHyp} checked={!offChannel && !offHyp && !campaigns.includes(c.id)} onChange={() => toggle(campaigns, c.id, setCampaigns)} />
                        <span>{c.name}</span><span className="reportsNodeMeta">{c.spend ? `${short(c.spend)} ₽` : ""}{c.leads ? ` · ${c.leads} лид.` : ""}</span></label>)}</div>
                    </div>;
                  })}
                </details>;
              })}</div>}
          </section>

          <section><h2>4. Текст для клиента</h2>
            <label className="reportsField">Главное (можно оставить пустым — напишем сами по цифрам)
              <textarea rows={3} maxLength={1500} value={summary} onChange={e => setSummary(e.target.value)} placeholder={report ? autoSummary(report) : ""} /></label>
            <div className="reportsField">План на следующий период
              {plan.map((item, i) => <div key={i} className="reportsPlanRow">
                <input value={item.text} maxLength={300} placeholder={i === 0 ? "Например: +30% бюджета на лучшую кампанию" : "Следующий шаг"} onChange={e => setPlan(plan.map((p, j) => j === i ? { ...p, text: e.target.value } : p))} aria-label={`Шаг ${i + 1}`} />
                <select value={item.owner} onChange={e => setPlan(plan.map((p, j) => j === i ? { ...p, owner: e.target.value } : p))} aria-label="Кто отвечает">{OWNERS.map(o => <option key={o}>{o}</option>)}</select>
                <button type="button" aria-label="Убрать шаг" onClick={() => setPlan(plan.length > 1 ? plan.filter((_, j) => j !== i) : [{ text: "", owner: "Агентство" }])}>×</button>
              </div>)}
              {plan.length < 12 && <button type="button" className="reportsAdd" onClick={() => setPlan([...plan, { text: "", owner: "Агентство" }])}>+ Шаг</button>}
            </div>
          </section>

          <div className="reportsBuilderActions">
            <button type="button" className="primary" disabled={busy || !report} onClick={save}>{busy ? "Сохраняем…" : "Сохранить и получить ссылку"}</button>
            <button type="button" disabled={!report} onClick={() => report && copySheets(report)}>Скопировать для Google Таблиц</button>
            <button type="button" disabled={!report} onClick={() => window.print()}>Скачать PDF</button>
          </div>
        </aside>
        <section className="reportsPreview" aria-label="Превью отчёта">
          <div className="reportsPreviewLabel">ТАК УВИДИТ КЛИЕНТ{report ? ` · ${periodTitle(report)}` : ""}</div>
          {shown ? <ReportView report={shown} /> : <p className="reportsMuted">Собираем отчёт…</p>}
        </section>
      </div>}
    </main>
  </div>;
}
