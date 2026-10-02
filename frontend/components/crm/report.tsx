"use client";

import { useEffect, useMemo, useState } from "react";
import { count, dateInput, money } from "@/components/reporting";
import { Pipeline, request } from "./shared";

type Row = { id: number | null; name: string; deals: number; won: number; lost: number; revenue: number;
  conversion: number | null; average_check: number | null; median_response_minutes: number | null; overdue_tasks?: number };
type Report = { period: { start: string; end: string }; pipeline: { id: number; name: string };
  totals: { deals: number; won: number; lost: number; open: number; revenue: number; win_rate: number | null;
    conversion: number | null; average_check: number | null; average_cycle_days: number | null;
    median_response_minutes: number | null; responded_in_15_min: number | null; open_without_response: number;
    pipeline_value: number; forecast: number; idle_3_days: number };
  funnel: { stage_id: number; name: string; color: string; count: number; conversion_from_previous: number | null; conversion_from_start: number | null }[];
  managers: Row[]; sources: Row[]; lost_reasons: { name: string; count: number }[];
  stage_probability: { stage_id: number; name: string; probability: number }[] };

const pct = (v: number | null | undefined) => v == null ? "—" : `${v.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
const minutes = (v: number | null | undefined) => v == null ? "—" : v < 60 ? `${Math.round(v)} мин` : v < 1440 ? `${(v / 60).toFixed(1)} ч` : `${(v / 1440).toFixed(1)} дн`;

function Tile({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) {
  return <div className={`crmKpi ${tone || ""}`}><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</div>;
}

export function SalesReport({ projectId, pipelines, pipelineId }: { projectId: number; pipelines: Pipeline[]; pipelineId: number | null }) {
  const today = useMemo(() => new Date(), []);
  const [start, setStart] = useState(dateInput(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)));
  const [end, setEnd] = useState(dateInput(today));
  const [pipeline, setPipeline] = useState<number | null>(pipelineId);
  const [data, setData] = useState<Report | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { if (!pipeline && pipelines[0]) setPipeline(pipelineId || pipelines[0].id); }, [pipelines, pipelineId, pipeline]);
  useEffect(() => { if (!pipeline) return; let active = true; setError("");
    request<Report>(`/crm/projects/${projectId}/report?${new URLSearchParams({ start, end, pipeline_id: String(pipeline) })}`, "GET")
      .then(v => { if (active) setData(v); }).catch(e => { if (active) setError((e as Error).message); });
    return () => { active = false; }; }, [projectId, pipeline, start, end]);
  const t = data?.totals;
  const max = Math.max(1, ...(data?.funnel.map(s => s.count) || [1]));
  const lostMax = Math.max(1, ...(data?.lost_reasons.map(r => r.count) || [1]));
  const responseTone = t?.median_response_minutes == null ? "" : t.median_response_minutes <= 15 ? "good" : t.median_response_minutes <= 60 ? "warn" : "bad";
  return <section className="crmReport">
    <div className="crmReportHead"><div><h2>Аналитика продаж</h2><p>Когорта сделок, созданных за период: что с ними стало, кто и как быстро их обрабатывает.</p></div>
      <div className="crmReportFilters">{pipelines.length > 1 && <select value={pipeline || ""} onChange={e => setPipeline(Number(e.target.value))}>{pipelines.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>}
        <label>С<input type="date" value={start} max={end} onChange={e => setStart(e.target.value)}/></label><label>По<input type="date" value={end} min={start} onChange={e => setEnd(e.target.value)}/></label></div></div>
    {error && <p className="crmFormError">{error}</p>}
    {!data ? <div className="resultLoading">Считаем…</div> : <>
      <div className="crmKpis">
        <Tile label="Новых сделок" value={count(t!.deals)} hint={`в работе ${count(t!.open)}`}/>
        <Tile label="Выиграно" value={count(t!.won)} hint={`конверсия ${pct(t!.conversion)}`} tone="good"/>
        <Tile label="Win rate" value={pct(t!.win_rate)} hint={`выиграно из закрытых · проиграно ${count(t!.lost)}`}/>
        <Tile label="Выручка" value={money(t!.revenue)} hint={`средний чек ${money(t!.average_check)}`}/>
        <Tile label="Цикл сделки" value={t!.average_cycle_days == null ? "—" : `${t!.average_cycle_days} дн`} hint="от создания до продажи"/>
        <Tile label="Скорость реакции" value={minutes(t!.median_response_minutes)} hint={`за 15 мин: ${pct(t!.responded_in_15_min)}`} tone={responseTone}/>
        <Tile label="Без ответа" value={count(t!.open_without_response)} hint="открытые сделки без касания" tone={t!.open_without_response ? "bad" : ""}/>
        <Tile label="Прогноз выручки" value={money(t!.forecast)} hint={`в воронке ${money(t!.pipeline_value)}`}/>
      </div>
      {t!.idle_3_days > 0 && <p className="crmWarnLine">{count(t!.idle_3_days)} открытых сделок без движения 3+ дня. Отфильтруйте их на канбане («Без движения») или включите правило дожима в «Автоматизации».</p>}
      <div className="crmReportGrid">
        <div className="crmReportCard"><h3>Воронка</h3>{data.funnel.map(s => <div className="crmFunnelRow" key={s.stage_id}>
          <span>{s.name}</span><div><i style={{ width: `${s.count / max * 100}%`, background: s.color }}/></div><b>{count(s.count)}</b>
          <small>{s.conversion_from_previous == null ? "" : `${pct(s.conversion_from_previous)} от пред.`}</small></div>)}
          <p className="crmMuted">Вероятность выигрыша по этапам (по истории): {data.stage_probability.map(s => `${s.name} — ${pct(s.probability)}`).join(" · ")}</p></div>
        <div className="crmReportCard"><h3>Причины отказов</h3>{data.lost_reasons.length ? data.lost_reasons.map(r => <div className="crmFunnelRow lost" key={r.name}>
          <span>{r.name}</span><div><i style={{ width: `${r.count / lostMax * 100}%` }}/></div><b>{r.count}</b><small/></div>) : <p className="crmMuted">Отказов за период нет.</p>}</div>
      </div>
      <div className="crmReportCard"><h3>Менеджеры</h3><Table rows={data.managers} withTasks/></div>
      <div className="crmReportCard"><h3>Источники</h3><Table rows={data.sources}/></div>
    </>}
  </section>;
}

function Table({ rows, withTasks }: { rows: Row[]; withTasks?: boolean }) {
  if (!rows.length) return <p className="crmMuted">Нет данных за период.</p>;
  return <div className="crmTableScroll"><table><thead><tr><th>Имя</th><th>Сделок</th><th>Выиграно</th><th>Проиграно</th><th>Конверсия</th><th>Выручка</th><th>Средний чек</th><th>Реакция (медиана)</th>{withTasks && <th>Просрочено задач</th>}</tr></thead>
    <tbody>{rows.map(r => <tr key={String(r.id)}><td>{r.name}</td><td>{count(r.deals)}</td><td>{count(r.won)}</td><td>{count(r.lost)}</td><td>{pct(r.conversion)}</td><td>{money(r.revenue)}</td><td>{money(r.average_check)}</td>
      <td className={r.median_response_minutes != null && r.median_response_minutes > 60 ? "bad" : ""}>{minutes(r.median_response_minutes)}</td>{withTasks && <td className={r.overdue_tasks ? "bad" : ""}>{count(r.overdue_tasks ?? 0)}</td>}</tr>)}</tbody></table></div>;
}
