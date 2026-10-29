"use client";

import { useEffect, useMemo, useState } from "react";
import { count, dateInput } from "@/components/reporting";
import { request } from "./shared";

type Row = { id: number; name: string; status: string; written: number; replied: number; handed: number; calls: number; won: number; reply_rate: number | null };
type Report = { period: { start: string; end: string }; enabled: boolean;
  totals?: { written: number; replied: number; handed: number; calls: number; won: number; reply_rate: number | null };
  campaigns?: Row[]; funnel?: { stage_id: number; name: string; color: string; count: number }[] };

const pct = (v: number | null | undefined) => v == null ? "—" : `${v.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
const STATUS: Record<string, string> = { draft: "Готовится", active: "Идёт", paused: "На паузе", done: "Завершена" };

function Tile({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) {
  return <div className={`crmKpi ${tone || ""}`}><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</div>;
}

/** Отчёт «Аутрич»: рассылку для клиента ведёт агентство, клиент видит результат — ответы и переданные лиды. */
export function OutreachReport({ projectId, onOpenPipeline }: { projectId: number; onOpenPipeline?: () => void }) {
  const today = useMemo(() => new Date(), []);
  const [start, setStart] = useState(dateInput(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)));
  const [end, setEnd] = useState(dateInput(today));
  const [data, setData] = useState<Report | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { let active = true; setError("");
    request<Report>(`/crm/projects/${projectId}/outreach-report?${new URLSearchParams({ start, end })}`, "GET")
      .then(v => { if (active) setData(v); }).catch(e => { if (active) setError((e as Error).message); });
    return () => { active = false; }; }, [projectId, start, end]);
  const t = data?.totals;
  const max = Math.max(1, ...(data?.funnel?.map(s => s.count) || [1]));
  return <section className="crmReport">
    <div className="crmReportHead"><div><h2>Аутрич</h2><p>Холодные письма от вашего имени ведёт агентство. Ответившие с интересом сразу попадают в воронку «Аутрич» с задачей «Связаться» — дальше их ведёт ваш менеджер.</p></div>
      <div className="crmReportFilters"><label>С<input type="date" value={start} max={end} onChange={e => setStart(e.target.value)}/></label><label>По<input type="date" value={end} min={start} onChange={e => setEnd(e.target.value)}/></label></div></div>
    {error && <p className="crmFormError">{error}</p>}
    {!data ? <div className="resultLoading">Считаем…</div> : !data.enabled ? <p className="crmMuted">Рассылок от вашего имени пока нет. Подключить аутрич можно через вашего менеджера в агентстве.</p> : <>
      <div className="crmKpis">
        <Tile label="Написали компаниям" value={count(t!.written)} hint="первое письмо или продолжение"/>
        <Tile label="Ответили" value={count(t!.replied)} hint={`отклик ${pct(t!.reply_rate)}`}/>
        <Tile label="Передали вам" value={count(t!.handed)} hint="сделки в воронке «Аутрич»" tone={t!.handed ? "good" : ""}/>
        <Tile label="Созвоны и встречи" value={count(t!.calls)} hint="дошли до этапа «Созвон назначен» и дальше"/>
        <Tile label="Стали клиентами" value={count(t!.won)} tone={t!.won ? "good" : ""}/>
      </div>
      <div className="crmReportGrid">
        <div className="crmReportCard"><h3>Воронка «Аутрич»</h3>{data.funnel?.length ? data.funnel.map(s => <div className="crmFunnelRow" key={s.stage_id}>
          <span>{s.name}</span><div><i style={{ width: `${s.count / max * 100}%`, background: s.color }}/></div><b>{count(s.count)}</b><small/></div>) : <p className="crmMuted">Переданных лидов за период нет.</p>}
          {onOpenPipeline && <p className="crmMuted"><button type="button" className="crmLinkButton" onClick={onOpenPipeline}>Открыть сделки →</button></p>}</div>
        <div className="crmReportCard"><h3>Рассылки</h3><div className="crmTableScroll"><table><thead><tr><th>Рассылка</th><th>Статус</th><th>Написали</th><th>Ответили</th><th>Передали</th><th>Созвоны</th></tr></thead>
          <tbody>{data.campaigns!.map(r => <tr key={r.id}><td>{r.name}</td><td>{STATUS[r.status] || r.status}</td><td>{count(r.written)}</td><td>{count(r.replied)}<small> {pct(r.reply_rate)}</small></td><td>{count(r.handed)}</td><td>{count(r.calls)}</td></tr>)}</tbody></table></div></div>
      </div>
    </>}
  </section>;
}
