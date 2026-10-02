"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { count, dateInput, money, ProjectSidebar } from "@/components/reporting";
import { avitoStatus, share } from "@/components/avito-panel";
import "../../../../result/result.css";
import "../../../ads.css";

type Project = { id: number; name: string; organization_name: string };
type Stats = { timestamp?: string; views?: number; clicks?: number; ctr?: number; spend?: number; spendKopeks?: number;
  spendBonus?: number; spendBonusKopeks?: number; cpm?: number; cpc?: number; videoViews100?: number; vtr?: number };
type Entity = { id: number; name?: string; groupId?: number; data?: Stats[]; totalData?: Stats;
  preview_url?: string | null; origin_url?: string | null; status?: string | null };
type Detail = { project_id: number; connection_id: number; connection_name: string;
  meta: { name?: string; status?: string; payment_model?: string; campaign_type?: string; budget?: number;
    start_date?: string; end_date?: string } | null;
  campaign: { id?: number; name?: string; paymentModel?: string; campaignType?: string; data?: Stats[]; totalData?: Stats };
  groups: Entity[]; creatives: Entity[] };

const rub = (s?: Stats) => s ? (s.spendKopeks != null ? s.spendKopeks / 100 : s.spend ?? null) : null;
const bonus = (s?: Stats) => s ? (s.spendBonusKopeks != null ? s.spendBonusKopeks / 100 : s.spendBonus ?? null) : null;

function Totals({ s }: { s?: Stats }) {
  const items: [string, string][] = [["Показы", count(s?.views)], ["Клики", count(s?.clicks)], ["CTR", share(s?.ctr)],
    ["Расход", money(rub(s))], ["Бонусами", money(bonus(s))], ["CPC", money(s?.cpc)], ["CPM", money(s?.cpm)]];
  if (s?.videoViews100 != null) items.push(["Досмотры", count(s.videoViews100)], ["VTR", share(s.vtr)]);
  return <div className="adsMetricStrip avitoTotals">{items.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>;
}

function Daily({ data }: { data: Stats[] }) {
  const max = Math.max(1, ...data.map(point => rub(point) || 0));
  if (!data.length) return <p className="resultTableEmpty">Нет показов за период.</p>;
  return <div className="avitoDaily" role="img" aria-label="Расход по дням">{data.map(point => {
    const value = rub(point) || 0; const day = (point.timestamp || "").slice(0, 10);
    return <div key={day} title={`${day}: ${money(value)}, ${count(point.views)} показов, ${count(point.clicks)} кликов`}><i style={{ height: `${Math.max(2, value / max * 100)}%` }}/></div>;
  })}</div>;
}

function Table({ rows, kind }: { rows: Entity[]; kind: "groups" | "creatives" }) {
  if (!rows.length) return <p className="resultTableEmpty">{kind === "groups" ? "Групп нет." : "Креативов нет."}</p>;
  return <div className="avitoTableScroll"><table><thead><tr><th>{kind === "groups" ? "Группа" : "Креатив"}</th><th>Показы</th><th>Клики</th><th>CTR</th><th>Расход</th><th>CPC</th><th>CPM</th></tr></thead>
    <tbody>{rows.map(row => { const s = row.totalData; return <tr key={row.id}><td><div className="avitoListing"><strong>{row.name || `ID ${row.id}`}</strong><small>ID {row.id}{row.status ? ` · ${avitoStatus[row.status] || row.status}` : ""}{row.preview_url && <> · <a href={row.preview_url} target="_blank" rel="noopener noreferrer">превью</a></>}{row.origin_url && <> · <a href={row.origin_url} target="_blank" rel="noopener noreferrer">ссылка</a></>}</small></div></td>
      <td>{count(s?.views)}</td><td>{count(s?.clicks)}</td><td>{share(s?.ctr)}</td><td>{money(rub(s))}</td><td>{money(s?.cpc)}</td><td>{money(s?.cpm)}</td></tr>; })}</tbody></table></div>;
}

function CampaignPage() {
  const params = useParams<{ connectionId: string; campaignId: string }>();
  const search = useSearchParams();
  const today = useMemo(() => new Date(), []);
  const [start, setStart] = useState(search.get("start") || dateInput(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)));
  const [end, setEnd] = useState(search.get("end") || dateInput(today));
  const [detail, setDetail] = useState<Detail | null>(null);
  const [project, setProject] = useState<Project | undefined>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => { let active = true; setLoading(true); setError("");
    api<Detail>(`/ads/connections/${params.connectionId}/avito/campaigns/${params.campaignId}?${new URLSearchParams({ start, end })}`, { timeoutMs: 120_000 })
      .then(value => { if (!active) return; setDetail(value);
        api<Project[]>("/result/projects").then(rows => active && setProject(rows.find(row => row.id === value.project_id))).catch(() => undefined); })
      .catch(e => { if (active) setError((e as Error).message); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [params.connectionId, params.campaignId, start, end]);
  const name = detail?.campaign.name || detail?.meta?.name || `Кампания ${params.campaignId}`;
  return <div className="resultShell"><ProjectSidebar project={project} projectId={detail?.project_id ?? null} active="ads" role={undefined}/>
    <main className="resultMain adsMain"><div className="resultBreadcrumb">StepToLead <span>/</span> <Link href={`/ads${detail ? `?project_id=${detail.project_id}` : ""}`}>Реклама</Link> <span>/</span> Авито Реклама</div>
      <header className="resultHeader adsHeader"><div><h1>{name}</h1><p>{detail?.connection_name || "Авито Реклама"} · ID {params.campaignId}{detail?.meta?.status ? ` · ${avitoStatus[detail.meta.status] || detail.meta.status}` : ""}{detail?.campaign.paymentModel ? ` · ${detail.campaign.paymentModel}` : ""}</p></div>
        <div className="avitoPeriod"><label>С<input type="date" value={start} max={end} onChange={e => setStart(e.target.value)}/></label><label>По<input type="date" value={end} min={start} onChange={e => setEnd(e.target.value)}/></label><small>Авито отдаёт детальную статистику максимум за 100 дней</small></div></header>
      {error && <div className="resultError" role="alert">{error}</div>}
      {loading && !detail && <div className="resultLoading">Запрашиваем статистику у Авито…</div>}
      {detail && <div className="avitoPanel">
        <section className="resultPanel avitoCard"><div className="avitoCardHead"><div><h3>Итоги за период</h3><p>Деньги — в рублях с НДС; бонусы показаны отдельно и не входят в расход для CPL/ROMI.</p></div>{loading && <span className="avitoMuted">Обновляем…</span>}</div>
          <Totals s={detail.campaign.totalData}/><Daily data={detail.campaign.data || []}/></section>
        <section className="resultPanel avitoCard"><div className="avitoCardHead"><div><h3>Группы</h3><p>Бюджет, ставка и таргетинг задаются на уровне группы в кабинете Авито Рекламы.</p></div></div><Table rows={detail.groups} kind="groups"/></section>
        <section className="resultPanel avitoCard"><div className="avitoCardHead"><div><h3>Креативы</h3><p>Конкретные баннеры и видео. Ссылка — посадочная страница креатива.</p></div></div><Table rows={detail.creatives} kind="creatives"/></section>
      </div>}
    </main></div>;
}

export default function Page() {
  return <Suspense fallback={<div className="resultLoading">Загружаем…</div>}><CampaignPage/></Suspense>;
}
