"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import WebsiteAnalytics from "@/components/website-analytics";
import { count, dateInput, MetricChart, money, percent, PeriodControls, ProjectSidebar, ReportingProject } from "@/components/reporting";
import "../result/result.css";
import "./analytics.css";

type Metric = "spend" | "leads" | "qualified" | "sales" | "revenue" | "romi";
type Totals = Record<Metric, number | null> & { meetings: number | null; cpl: number | null; cpql: number | null;
  cac: number | null; average_check: number | null; gross_profit: number | null;
  target?: number; non_target?: number; target_share?: number | null; cost_per_target?: number | null };
type Source = Totals & {
  id: string; name: string; kind: string; method: string | null; platform: string | null;
  impressions: number | null; clicks: number | null; cpc: number | null; ctr: number | null;
  click_to_lead: number | null; lead_to_qualified: number | null; qualified_to_sale: number | null;
  group?: string | null;
};
type Exposure = { impressions: number | null; clicks: number | null; cpc: number | null;
  ctr: number | null; click_to_lead: number | null; lead_to_qualified: number | null;
  qualified_to_sale: number | null; lead_to_sale: number | null; sources: Source[] };
type Campaign = Totals & { id: string; kind?: "ad" | "channel"; external_campaign_id: string; name: string; connection_id: number | null;
  connection_name: string; platform: string; hypothesis_id: number | null; hypothesis_name: string | null;
  status: string | null; impressions: number | null; clicks: number | null; cpc: number | null };
type ReportingPoint = { label: string } & Record<Metric, number | null>;
type Analytics = {
  project: { id: number; name: string; workspace_id: number; meeting_enabled: boolean };
  period: { start: string; end: string; previous_start: string; previous_end: string };
  current: { totals: Totals; trend: ReportingPoint[] }; previous: { totals: Totals; trend: ReportingPoint[] };
  economics: { margin: number | null; allowable_cac: number | null; break_even_sales: number | null };
  analysis: { current: Exposure; previous: Exposure;
    sales_cycle_days: number | null;
    connections: { id: number; name: string; platform: string; status: string; last_synced_at: string | null }[];
    hypotheses: { id: number; name: string; status: string }[]; campaigns: Campaign[];
    lost_reasons: { label: string; count: number }[]; non_target_reasons?: { label: string; count: number }[] };
  viewer: { role: string };
};

/** Share of target leads among the ones sales marked; "—" until someone marks. */
const quality = (row: { target?: number; non_target?: number; target_share?: number | null }) =>
  row.target_share == null ? "—" : `${Math.round(row.target_share)}% (${row.target}/${(row.target || 0) + (row.non_target || 0)})`;

/** Conversion rate without the "+" sign used for period-over-period differences. */
const rate = (n: number | null | undefined) => n == null ? "—" : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(n)}%`;
const pct = (a: number, b: number) => b > 0 ? a / b * 100 : null;
const hasActivity = (s: Source) => Boolean(s.leads || s.clicks || s.qualified || s.sales || s.target || s.non_target);
type ConversionRow = { key: string; name: string; values: Pick<Source, "click_to_lead" | "lead_to_qualified" | "qualified_to_sale" | "target" | "non_target" | "target_share">; members?: Source[] };

/** Channels of one kind («Telegram-профиль: …», «Tilda: …») fold into one row with summed conversions. */
function conversionRows(list: Source[]): ConversionRow[] {
  const active = list.filter(hasActivity);
  const groups = new Map<string, Source[]>();
  active.forEach(s => { if (s.group) groups.set(s.group, [...(groups.get(s.group) || []), s]); });
  const done = new Set<string>();
  const rows: ConversionRow[] = [];
  for (const s of active) {
    const members = s.group ? groups.get(s.group) || [] : [];
    if (members.length < 2) { rows.push({ key: s.id, name: s.name, values: s }); continue; }
    if (done.has(s.group!)) continue;
    done.add(s.group!);
    const sum = (f: (m: Source) => number | null | undefined, only = members) => only.reduce((t, m) => t + Number(f(m) || 0), 0);
    const clickable = members.filter(m => m.clicks != null);
    const target = sum(m => m.target), nonTarget = sum(m => m.non_target);
    rows.push({ key: `group:${s.group}`, name: s.group!, members, values: {
      click_to_lead: clickable.length ? pct(sum(m => m.leads, clickable), sum(m => m.clicks, clickable)) : null,
      lead_to_qualified: pct(sum(m => m.qualified), sum(m => m.leads)),
      qualified_to_sale: pct(sum(m => m.sales), sum(m => m.qualified)),
      target, non_target: nonTarget, target_share: pct(target, target + nonTarget) } });
  }
  return rows;
}

const channelsWord = (n: number) => n % 10 === 1 && n % 100 !== 11 ? "канал" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "канала" : "каналов";

const PLATFORM_NAMES: Record<string, string> = { yandex: "Яндекс Директ", yandex_direct: "Яндекс Директ", vk_ads: "VK Реклама",
  avito_ads: "Авито Реклама", avito_items: "Авито · Объявления", yandex_maps: "Яндекс Карты", "2gis": "2ГИС", meta: "Meta Ads" };
type CampaignNode = { key: string; name: string; note?: string; rows: Campaign[]; children: CampaignNode[]; depth: number };
type CampaignSum = Pick<Campaign, "spend" | "clicks" | "cpc" | "leads" | "qualified" | "sales" | "revenue" | "cpl" | "cpql" | "cac" | "romi" | "target" | "non_target" | "target_share">;

function sumCampaigns(rows: Campaign[], margin: number | null): CampaignSum {
  const total = (f: (r: Campaign) => number | null | undefined) => { const v = rows.map(f).filter((x): x is number => x != null); return v.length ? v.reduce((a, b) => a + Number(b), 0) : null; };
  const spend = total(r => r.spend), clicks = total(r => r.clicks), leads = total(r => r.leads), qualified = total(r => r.qualified);
  const sales = total(r => r.sales), revenue = total(r => r.revenue), target = total(r => r.target) || 0, non_target = total(r => r.non_target) || 0;
  const div = (a: number | null, b: number | null) => a != null && b ? a / b : null;
  const profit = revenue != null && margin != null ? revenue * margin / 100 : null;
  return { spend, clicks, leads, qualified, sales, revenue, target, non_target, cpc: div(spend, clicks), cpl: div(spend, leads),
    cpql: div(spend, qualified), cac: div(spend, sales), romi: profit != null && spend ? (profit - spend) / spend * 100 : null,
    target_share: target + non_target ? target / (target + non_target) * 100 : null };
}

/** Канал → гипотеза (или профиль/аккаунт) → кампании. Уровень пропускается, если в нём нечего различать. */
function campaignTree(rows: Campaign[]): CampaignNode[] {
  const top = new Map<string, { name: string; rows: Campaign[] }>();
  for (const row of rows) {
    const name = row.kind === "channel" ? (row.platform.split(": ")[0] || row.platform) : PLATFORM_NAMES[row.platform] || row.platform;
    const key = `${row.kind || "ad"}:${name}`;
    top.set(key, { name, rows: [...(top.get(key)?.rows || []), row] });
  }
  return [...top.entries()].map(([key, group]) => {
    const second = new Map<string, Campaign[]>();
    const isAd = group.rows[0].kind !== "channel";
    for (const row of group.rows) {
      const name = isAd ? row.hypothesis_name || "Без гипотезы" : row.platform.slice(group.name.length).replace(/^:\s*/, "") || row.platform;
      second.set(name, [...(second.get(name) || []), row]);
    }
    const useSecond = second.size > 1 || (isAd && group.rows.some(row => row.hypothesis_id != null));
    const children = useSecond ? [...second.entries()].map(([name, list]) => ({ key: `${key}/${name}`, name, rows: list, children: [], depth: 1,
      note: isAd ? (name === "Без гипотезы" ? "кампании без гипотезы" : "гипотеза") : undefined })) : [];
    return { key, name: group.name, rows: group.rows, children, depth: 0, note: isAd ? "реклама" : undefined };
  }).sort((a, b) => (sumCampaigns(b.rows, null).spend || 0) - (sumCampaigns(a.rows, null).spend || 0) || (sumCampaigns(b.rows, null).leads || 0) - (sumCampaigns(a.rows, null).leads || 0));
}

const campaignWord = (n: number) => n % 10 === 1 && n % 100 !== 11 ? "кампания" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "кампании" : "кампаний";

type Tab = "overview" | "marketing" | "sales" | "accounts" | "hypotheses" | "campaigns" | "website";
const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Общий обзор" }, { key: "marketing", label: "Маркетинг" },
  { key: "sales", label: "Продажи" },
  { key: "accounts", label: "Рекламные кабинеты" }, { key: "hypotheses", label: "Гипотезы" },
  { key: "campaigns", label: "Кампании" },
  { key: "website", label: "Сайт" },
];
const SOURCE_METRICS: { key: Metric; label: string }[] = [
  { key: "spend", label: "Расходы" }, { key: "leads", label: "Лиды" },
  { key: "qualified", label: "Квал. лиды" }, { key: "sales", label: "Продажи" },
  { key: "revenue", label: "Выручка" }, { key: "romi", label: "ROMI" },
];
const CHART_METRICS: { key: Metric; label: string }[] = [
  { key: "leads", label: "Лиды" }, { key: "qualified", label: "Квал. лиды" },
  { key: "sales", label: "Продажи" }, { key: "revenue", label: "Выручка" },
  { key: "spend", label: "Расходы" },
];
const COLORS = ["#006bfd", "#39a3ff", "#6847ea", "#13a8bc", "#fa9052", "#8eb8d2", "#5bc98b"];
const ratio = (top: number | null, bottom: number | null) => top != null && bottom != null && bottom !== 0 ? top / bottom * 100 : null;
const change = (now: number | null, prior: number | null) => now != null && prior != null && prior !== 0 ? (now - prior) / Math.abs(prior) * 100 : null;
const metricText = (key: Metric, value: number | null) => key === "spend" || key === "revenue" ? money(value) : key === "romi" ? percent(value) : count(value);

function KpiCard({ label, icon, value, prior, note, inverse = false, display = count }: {
  label: string; icon: string; value: number | null; prior: number | null; note?: string;
  inverse?: boolean; display?: (value: number | null) => string;
}) {
  const difference = change(value, prior);
  const negative = difference != null && (inverse ? difference > 0 : difference < 0);
  return <article className="resultKpi"><div className="resultKpiTitle"><span className="resultKpiIcon">{icon}</span><span>{label}</span></div>
    <div className="resultKpiValue">{display(value)}{difference != null && <em className={negative ? "negative" : "positive"}>{difference > 0 ? "↑" : difference < 0 ? "↓" : "="} {percent(Math.abs(difference))}</em>}</div>
    <p>{prior == null ? "Нет данных за прошлый период" : `${display(prior)} за прошлый период`}</p>
    {note && <small>{note}</small>}</article>;
}

export default function AnalyticsPage() {
  const today = useMemo(() => new Date(), []);
  const [openGroups, setOpenGroups] = useState<string[]>([]);
  const [openCampaignNodes, setOpenCampaignNodes] = useState<string[]>([]);
  const toggleGroup = (key: string) => setOpenGroups(list => list.includes(key) ? list.filter(k => k !== key) : [...list, key]);
  const [start, setStart] = useState(dateInput(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)));
  const [end, setEnd] = useState(dateInput(today));
  const [projects, setProjects] = useState<ReportingProject[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [data, setData] = useState<Analytics | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [granularity, setGranularity] = useState("day");
  const [tab, setTab] = useState<Tab>("overview");
  const [chartMetric, setChartMetric] = useState<Metric>("leads");
  const [salesMetric, setSalesMetric] = useState<"revenue" | "sales" | "average_check">("revenue");
  const [sourceMetric, setSourceMetric] = useState<Metric>("leads");
  const [funnelChannel, setFunnelChannel] = useState("");
  const [filterPlatform, setFilterPlatform] = useState("");
  const [filterAccount, setFilterAccount] = useState("");
  const [filterHypothesis, setFilterHypothesis] = useState("");
  const [search, setSearch] = useState("");
  const [selectedCampaign, setSelectedCampaign] = useState<Campaign | null>(null);

  useEffect(() => {
    api<ReportingProject[]>("/result/projects").then(rows => {
      setProjects(rows);
      const desired = Number(new URLSearchParams(window.location.search).get("project_id"));
      setProjectId(rows.find(row => row.id === desired)?.id || rows[0]?.id || null);
      if (!rows.length) setLoading(false);
    }).catch(exc => { setError(exc.message); setLoading(false); });
  }, []);
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true); setError(""); setData(null);
    const query = new URLSearchParams({ project_id: String(projectId), start, end, granularity });
    api<Analytics>(`/analytics?${query}`)
      .then(value => { if (!cancelled) setData(value); })
      .catch(exc => { if (!cancelled) setError(exc.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [projectId, start, end, granularity]);
  // Live: new leads and fresh ad spend show up without reloading (quiet refresh while the tab is visible).
  useEffect(() => { if (!projectId) return;
    const query = new URLSearchParams({ project_id: String(projectId), start, end, granularity });
    const timer = setInterval(() => { if (document.visibilityState === "visible") api<Analytics>(`/analytics?${query}`).then(setData).catch(() => {}); }, 60000);
    return () => clearInterval(timer); }, [projectId, start, end, granularity]);
  useEffect(() => { const query = new URLSearchParams(window.location.search);
    setFunnelChannel(""); setFilterPlatform(query.get("platform") || "");
    setFilterAccount(query.get("account") || ""); setFilterHypothesis(""); setSelectedCampaign(null);
    const requested = query.get("tab");
    if (TABS.some(item => item.key === requested)) setTab(requested as Tab);
  }, [projectId]);

  const current = data?.current.totals;
  const previous = data?.previous.totals;
  const exposure = data?.analysis.current;
  const priorExposure = data?.analysis.previous;
  const sources = exposure?.sources || [];
  const selectedSource = sources.find(source => source.id === funnelChannel);
  const funnel = selectedSource || (exposure && current ? { ...current, ...exposure } : null);
  const costSources = sources.filter(source => source.cpl != null || source.cpql != null || source.cac != null);
  const maxCost = Math.max(1, ...costSources.flatMap(source => [source.cpl || 0, source.cpql || 0, source.cac || 0]));
  const filteredCampaigns = (data?.analysis.campaigns || []).filter(campaign =>
    (!filterPlatform || campaign.platform === filterPlatform) &&
    (!filterAccount || campaign.connection_id === Number(filterAccount)) &&
    (!filterHypothesis || (filterHypothesis === "none" ? campaign.hypothesis_id == null : campaign.hypothesis_id === Number(filterHypothesis))) &&
    (!search.trim() || `${campaign.name} ${campaign.external_campaign_id}`.toLowerCase().includes(search.trim().toLowerCase())));
  const sourceValues = sources.filter(source => source[sourceMetric] != null && (sourceMetric === "romi" || Number(source[sourceMetric]) >= 0));
  const sourceTotal = sourceMetric === "romi" ? null : sourceValues.reduce((total, source) => total + Number(source[sourceMetric] || 0), 0);
  const distribution = sourceTotal && sourceTotal > 0 ? sourceValues.reduce<{ pieces: string[]; cursor: number }>((acc, source, index) => {
    const next = acc.cursor + Number(source[sourceMetric] || 0) / sourceTotal * 100;
    acc.pieces.push(`${COLORS[index % COLORS.length]} ${acc.cursor}% ${next}%`); acc.cursor = next; return acc;
  }, { pieces: [], cursor: 0 }).pieces.join(", ") : "";
  const selectedProject = projects.find(project => project.id === projectId);

  const expandAllCampaigns = Boolean(search.trim() || filterAccount || filterHypothesis);
  const toggleCampaignNode = (key: string) => setOpenCampaignNodes(list => list.includes(key) ? list.filter(k => k !== key) : [...list, key]);
  const campaignCells = (v: CampaignSum, ad: boolean) => <><td>{ad ? money(v.spend) : "—"}</td><td>{ad ? count(v.clicks) : "—"}</td><td>{ad ? money(v.cpc) : "—"}</td><td>{count(v.leads)}</td><td title="Отметки отдела продаж: целевые / всего отмеченных">{quality(v)}</td><td>{count(v.qualified)}</td><td>{count(v.sales)}</td><td>{money(v.revenue)}</td><td>{ad ? money(v.cpl) : "—"}</td><td>{ad ? money(v.cpql) : "—"}</td><td>{ad ? money(v.cac) : "—"}</td><td>{ad ? percent(v.romi) : "—"}</td></>;
  const campaignRow = (row: Campaign, depth: number) => row.kind === "channel"
    ? <tr key={row.id} className={`analyticsChannelCampaign analyticsDepth${depth}`}><td><strong>{row.name}</strong><small>{row.external_campaign_id ? "utm_campaign" : "Заявки без рекламной метки"}</small></td><td>{row.platform}</td><td>—</td>{campaignCells(row, false)}<td>—</td></tr>
    : <tr key={row.id} className={`analyticsClickableRow analyticsDepth${depth}`} onClick={() => setSelectedCampaign(row)} tabIndex={0} onKeyDown={event => { if (event.key === "Enter") setSelectedCampaign(row); }}><td><strong>{row.name}</strong><small>ID {row.external_campaign_id} · {row.connection_name}</small></td><td>{PLATFORM_NAMES[row.platform] || row.platform}</td><td>{row.hypothesis_name || "—"}</td>{campaignCells(row, true)}<td>{row.status || "—"}</td></tr>;
  function campaignNodeRows(node: CampaignNode): React.ReactNode[] {
    if (node.depth === 0 && node.rows.length === 1 && !node.children.length) return [campaignRow(node.rows[0], 0)];
    const open = expandAllCampaigns || openCampaignNodes.includes(node.key);
    const ad = node.rows[0].kind !== "channel";
    const head = <tr key={node.key} className={`analyticsClickableRow analyticsGroupRow analyticsDepth${node.depth}`} tabIndex={0} aria-expanded={open}
      onClick={() => toggleCampaignNode(node.key)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleCampaignNode(node.key); } }}>
      <td><strong><span className="analyticsGroupArrow">{open ? "▾" : "▸"}</span>{node.name}</strong><small>{node.rows.length} {campaignWord(node.rows.length)}{node.note ? ` · ${node.note}` : ""}</small></td>
      <td>{node.depth === 0 ? "Итого по каналу" : ""}</td><td>{node.depth === 1 && ad && node.name !== "Без гипотезы" ? node.name : "—"}</td>
      {campaignCells(sumCampaigns(node.rows, data?.economics.margin ?? null), ad)}<td>—</td></tr>;
    if (!open) return [head];
    return [head, ...(node.children.length ? node.children.flatMap(child => campaignNodeRows(child)) : node.rows.map(row => campaignRow(row, node.depth + 1)))];
  }

  function CampaignTable() {
    return <section className="resultPanel analyticsCampaigns" id="campaigns"><div className="resultPanelHead"><h2>Результат по кампаниям</h2><span className="analyticsQuiet">Рекламные кампании из кабинетов и заявки с сайта, мессенджеров и других каналов (по utm_campaign)</span></div>
      <div className="analyticsCampaignFilters"><select aria-label="Канал" value={filterPlatform} onChange={event => setFilterPlatform(event.target.value)}><option value="">Все каналы</option>{[...new Set([...(data?.analysis.connections || []).map(row => row.platform), ...(data?.analysis.campaigns || []).filter(row => row.kind === "channel").map(row => row.platform)])].map(value => <option key={value} value={value}>{value}</option>)}</select>
        <select aria-label="Рекламный кабинет" value={filterAccount} onChange={event => setFilterAccount(event.target.value)}><option value="">Все кабинеты</option>{data?.analysis.connections.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>
        <select aria-label="Гипотеза" value={filterHypothesis} onChange={event => setFilterHypothesis(event.target.value)}><option value="">Все гипотезы</option><option value="none">Без гипотезы</option>{data?.analysis.hypotheses.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>
        <input aria-label="Поиск кампании" value={search} onChange={event => setSearch(event.target.value)} placeholder="Поиск по кампании…" /></div>
      <div className="resultTableScroll"><table><thead><tr>{["Кампания", "Канал", "Гипотеза", "Расходы", "Клики", "CPC", "Лиды", "Целевые", "Квал.", "Продажи", "Выручка", "CPL", "CPQL", "CAC", "ROMI", "Статус"].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{campaignTree(filteredCampaigns).flatMap(node => campaignNodeRows(node))}</tbody></table>{!filteredCampaigns.length && <p className="resultTableEmpty">За этот период нет заявок и кампаний с данными.</p>}</div>
    </section>;
  }

  function SourceSection() {
    return <div className="analyticsSourceGrid"><section className="resultPanel analyticsSources"><div className="resultPanelHead"><h2>Источники лидов</h2></div><div className="resultTabs analyticsTabs">{SOURCE_METRICS.map(item => <button key={item.key} className={sourceMetric === item.key ? "active" : ""} onClick={() => setSourceMetric(item.key)}>{item.label}</button>)}</div>
      {sourceValues.length ? <div className="analyticsDistribution"><div className="analyticsDonut" style={{ background: distribution ? `conic-gradient(${distribution})` : "#eaf0f8" }}><div><strong>{sourceMetric === "romi" ? "ROMI" : metricText(sourceMetric, sourceTotal)}</strong><span>за период</span></div></div><div className="analyticsDistributionRows">{sourceValues.map((source, index) => <div key={source.id}><i style={{ background: COLORS[index % COLORS.length] }}/><span>{source.name}</span><strong>{metricText(sourceMetric, source[sourceMetric])}</strong><small>{sourceTotal && sourceMetric !== "romi" ? `${(Number(source[sourceMetric]) / sourceTotal * 100).toFixed(1)}%` : "—"}</small></div>)}</div></div> : <p className="resultTableEmpty">Для этой метрики пока нет данных по источникам.</p>}
    </section><section className="resultPanel analyticsCosts"><div className="resultPanelHead"><h2>Стоимость этапов</h2><span className="analyticsLegend"><i className="cpl"/> CPL <i className="cpql"/> CPQL <i className="cac"/> CAC</span></div>{costSources.length ? <div className="analyticsCostRows">{costSources.map(source => { return <div className="analyticsCostRow" key={source.id}><strong>{source.name}</strong><div><div title={`CPL: ${money(source.cpl)}`}><i className="cpl" style={{ width: `${(source.cpl || 0) / maxCost * 100}%` }}/><span>{money(source.cpl)}</span></div><div title={`CPQL: ${money(source.cpql)}`}><i className="cpql" style={{ width: `${(source.cpql || 0) / maxCost * 100}%` }}/><span>{money(source.cpql)}</span></div><div title={`CAC: ${money(source.cac)}`}><i className="cac" style={{ width: `${(source.cac || 0) / maxCost * 100}%` }}/><span>{money(source.cac)}</span></div></div></div>; })}</div> : <p className="resultTableEmpty">Для сравнения пока нет расходов и подтверждённых этапов.</p>}</section>
      <section className="resultPanel analyticsConversions"><div className="resultPanelHead"><h2>Конверсии по каналам</h2></div><div className="resultTableScroll"><table><thead><tr><th>Канал</th><th>Клик → Лид</th><th>Целевые</th><th>Лид → Квал.</th><th>Квал. → Прод.</th></tr></thead><tbody>{conversionRows(sources).flatMap(row => {
        const open = openGroups.includes(row.key);
        const cells = (v: ConversionRow["values"]) => <><td>{rate(v.click_to_lead)}</td><td>{quality(v)}</td><td>{rate(v.lead_to_qualified)}</td><td>{rate(v.qualified_to_sale)}</td></>;
        if (!row.members) return [<tr key={row.key}><td><strong>{row.name}</strong></td>{cells(row.values)}</tr>];
        return [<tr key={row.key} className="analyticsClickableRow analyticsGroupRow" tabIndex={0} aria-expanded={open}
          onClick={() => toggleGroup(row.key)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleGroup(row.key); } }}>
          <td><strong><span className="analyticsGroupArrow">{open ? "▾" : "▸"}</span>{row.name}</strong><small>{row.members.length} {channelsWord(row.members.length)}</small></td>{cells(row.values)}</tr>,
          ...(open ? row.members.map(member => <tr key={member.id} className="analyticsGroupChild"><td>{member.name.slice((member.group || "").length).replace(/^:\s*/, "")}</td>{cells(member)}</tr>) : [])];
      })}</tbody></table>{!conversionRows(sources).length && <p className="resultTableEmpty">За выбранный период по каналам нет лидов и кликов.</p>}</div></section></div>;
  }

  return <div className="resultShell analyticsShell"><ProjectSidebar project={selectedProject} projectId={projectId} active="analytics" role={data?.viewer.role}/><main className="resultMain"><div className="resultBreadcrumb">StepToLead <span>/</span> Аналитика</div>
    <header className="resultHeader"><div><h1>Аналитика</h1><p>Подробные причины результата по этапам воронки, каналам и кампаниям.</p></div><PeriodControls projects={projects} projectId={projectId} setProjectId={setProjectId} start={start} setStart={setStart} end={end} setEnd={setEnd}/></header>
    <div className="analyticsTopTabs" role="tablist">{TABS.map(item => <button key={item.key} role="tab" aria-selected={tab === item.key} className={tab === item.key ? "active" : ""} onClick={() => { setTab(item.key); const url = new URL(window.location.href); url.searchParams.set("tab", item.key); window.history.replaceState(null, "", url); }}>{item.label}</button>)}</div>
    {error && <div className="resultError" role="alert">{error}{!projects.length && <div><Link href="/login?next=/analytics">Войти в кабинет</Link> · <Link href="/login?next=/analytics">Войти как администратор</Link></div>}</div>}
    {loading && !data && <div className="resultLoading">Загружаем аналитику…</div>}
    {!loading && !data && !error && <div className="resultLoading">Проекты пока не созданы.</div>}
    {data && <>
      {tab === "overview" && <>
        <section className="resultKpis analyticsKpis" aria-label="Ключевые показатели">
          <KpiCard label="Расходы" icon="▣" value={current?.spend ?? null} prior={previous?.spend ?? null} display={money} inverse/>
          <KpiCard label="Клики" icon="↗" value={exposure?.clicks ?? null} prior={priorExposure?.clicks ?? null} note={`CPC: ${money(exposure?.cpc)}`}/>
          <KpiCard label="Лиды" icon="♧" value={current?.leads ?? null} prior={previous?.leads ?? null} note={`CPL: ${money(current?.cpl)}${current?.target_share != null ? ` · целевых ${Math.round(current.target_share)}%` : ""}`}/>
          <KpiCard label="Квал. лиды" icon="✓" value={current?.qualified ?? null} prior={previous?.qualified ?? null} note={`CPQL: ${money(current?.cpql)}`}/>
          <KpiCard label="Продажи" icon="▣" value={current?.sales ?? null} prior={previous?.sales ?? null} note={`CAC: ${money(current?.cac)}`}/>
          <KpiCard label="Выручка" icon="◉" value={current?.revenue ?? null} prior={previous?.revenue ?? null} display={money} note={`Средний чек: ${money(current?.average_check)}`}/>
        </section>
        <div className="analyticsHero"><section className="resultPanel analyticsFunnel"><div className="resultPanelHead"><h2>Воронка и конверсии</h2><select aria-label="Канал воронки" value={funnelChannel} onChange={event => setFunnelChannel(event.target.value)}><option value="">Все каналы</option>{sources.map(source => <option key={source.id} value={source.id}>{source.name}</option>)}</select></div><div className="analyticsFunnelBody"><div className="analyticsFunnelStages">{[["Показы", funnel?.impressions], ["Клики", funnel?.clicks], ["Лиды", funnel?.leads], ["Квал. лиды", funnel?.qualified], ...(data.project.meeting_enabled ? [["Встречи", funnel?.meetings]] : []), ["Продажи", funnel?.sales]].map(([label, value], index) => <div className="analyticsFunnelStage" key={label as string}><span>{label}</span><div className={`analyticsStageBar stage${index}`}><strong>{count(value as number | null)}</strong></div></div>)}</div><div className="analyticsCrCards">{[["CTR", selectedSource ? selectedSource.ctr : exposure?.ctr, "Клики / показы"], ["Клик → лид", selectedSource ? selectedSource.click_to_lead : exposure?.click_to_lead, "Лиды с кликовыми источниками / клики"], ["Лид → квалификация", selectedSource ? selectedSource.lead_to_qualified : exposure?.lead_to_qualified, "Квал. лиды / лиды"], ["Квалификация → продажа", selectedSource ? selectedSource.qualified_to_sale : exposure?.qualified_to_sale, "Продажи / квал. лиды"], ["Лид → продажа", ratio(funnel?.sales ?? null, funnel?.leads ?? null), "Продажи / лиды"]].map(([label, value, hint]) => <div key={label as string}><span>{label}</span><strong>{percent(value as number | null)}</strong><small>{hint}</small></div>)}</div></div><p className="analyticsFootnote">Показы и клики есть только у источников, которые передают эти данные. Этапы считают события периода: продажа может относиться к лиду прошлого периода. Лиды без атрибуции не распределяются по кампаниям.</p></section>
          <section className="resultPanel analyticsDynamics"><div className="resultPanelHead"><h2>Динамика по этапам воронки</h2><select aria-label="Группировка графика" value={granularity} onChange={event => setGranularity(event.target.value)}><option value="day">По дням</option><option value="week">По неделям</option><option value="month">По месяцам</option></select></div><div className="resultTabs">{CHART_METRICS.map(item => <button key={item.key} className={chartMetric === item.key ? "active" : ""} onClick={() => setChartMetric(item.key)}>{item.label}</button>)}</div><MetricChart current={data.current.trend} previous={data.previous.trend} metric={chartMetric} label={CHART_METRICS.find(item => item.key === chartMetric)?.label || chartMetric}/><div className="resultLegend"><span><i className="now"/>Текущий период</span><span><i className="prior"/>Прошлый период</span></div></section></div>
        <div className="analyticsDrillLinks"><button onClick={() => setTab("marketing")}>Маркетинг: источники, стоимость и кампании →</button><button onClick={() => setTab("sales")}>Продажи: выручка, цикл и экономика →</button></div>
      </>}
      {tab === "marketing" && <><div className="analyticsTabIntro"><h2>Маркетинг</h2><p>Каналы, затраты, конверсии и подтверждённые результаты рекламных кампаний выбранного проекта.</p></div>{SourceSection()}{CampaignTable()}</>}
      {tab === "sales" && <><div className="analyticsTabIntro"><h2>Аналитика продаж</h2><p>Выручка, конверсия и эффективность подтверждённых сделок. Реестр сделок остаётся в разделе «Продажи».</p></div>
        <section className="resultKpis analyticsSalesKpis" aria-label="Показатели продаж">
          <KpiCard label="Продажи" icon="▣" value={current?.sales ?? null} prior={previous?.sales ?? null}/>
          <KpiCard label="Выручка" icon="◉" value={current?.revenue ?? null} prior={previous?.revenue ?? null} display={money}/>
          <KpiCard label="Средний чек" icon="◇" value={current?.average_check ?? null} prior={previous?.average_check ?? null} display={money}/>
          <article className="resultKpi"><div className="resultKpiTitle"><span className="resultKpiIcon">%</span>Лид → продажа</div><div className="resultKpiValue">{ratio(current?.sales ?? null, current?.leads ?? null) == null ? "—" : `${ratio(current?.sales ?? null, current?.leads ?? null)!.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`}</div><p>Продажи / лиды за период</p></article>
          <article className="resultKpi"><div className="resultKpiTitle"><span className="resultKpiIcon">◷</span>Средний цикл</div><div className="resultKpiValue">{data.analysis.sales_cycle_days == null ? "—" : `${data.analysis.sales_cycle_days.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} дн.`}</div><p>От создания лида до продажи</p></article>
        </section>
        <div className="analyticsSalesGrid"><section className="resultPanel analyticsSalesTrend"><div className="resultPanelHead"><h2>Динамика продаж</h2><select aria-label="Группировка графика" value={granularity} onChange={event => setGranularity(event.target.value)}><option value="day">По дням</option><option value="week">По неделям</option><option value="month">По месяцам</option></select></div><div className="resultTabs">{([['revenue','Выручка'],['sales','Количество продаж'],['average_check','Средний чек']] as const).map(([key,label]) => <button key={key} className={salesMetric === key ? "active" : ""} onClick={() => setSalesMetric(key)}>{label}</button>)}</div><MetricChart current={data.current.trend} previous={data.previous.trend} metric={salesMetric} label={salesMetric}/><div className="resultLegend"><span><i className="now"/>Текущий период</span><span><i className="prior"/>Прошлый период</span></div></section>
          <section className="resultPanel analyticsSalesEconomics"><h2>Экономика продаж</h2><dl><div><dt>Выручка</dt><dd>{money(current?.revenue)}</dd></div><div><dt>Средний чек</dt><dd>{money(current?.average_check)}</dd></div><div><dt>Маржинальность</dt><dd>{data.economics.margin == null ? "—" : `${data.economics.margin}%`}</dd></div><div><dt>Валовая прибыль</dt><dd>{money(current?.gross_profit)}</dd></div><div><dt>CAC</dt><dd>{money(current?.cac)}</dd></div><div><dt>Допустимый CAC</dt><dd>{money(data.economics.allowable_cac)}</dd></div><div><dt>ROMI</dt><dd>{percent(current?.romi)}</dd></div><div><dt>Точка безубыточности</dt><dd>{data.economics.break_even_sales == null ? "—" : `${count(data.economics.break_even_sales)} продаж / мес`}</dd></div></dl><Link href="/growth">Открыть экономическую модель →</Link></section></div>
        <div className="analyticsSalesChannels">{([['Выручка по каналам','revenue'],['Продажи по каналам','sales']] as const).map(([title,field]) => <section key={field} className="resultPanel"><h2>{title}</h2>{sources.filter(source => source[field] != null).length ? <div className="analyticsSalesRows">{sources.filter(source => source[field] != null).map(source => <div key={source.id}><span>{source.name}</span><strong>{field === "revenue" ? money(source.revenue) : count(source.sales)}</strong><small>{current?.[field] ? `${((source[field] || 0) / current[field]! * 100).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%` : "—"}</small></div>)}</div> : <p className="resultTableEmpty">Для этого периода нет данных по каналам.</p>}</section>)}</div>
        <section className="resultPanel analyticsLostReasons"><div className="resultPanelHead"><h2>Причины потери</h2></div>
          {data.analysis.lost_reasons.length ? <div className="analyticsLostList">{data.analysis.lost_reasons.map(reason =>
            <div key={reason.label}><span>{reason.label}</span><strong>{count(reason.count)}</strong></div>)}</div>
            : <p className="resultTableEmpty">За выбранный период событий потери лидов нет.</p>}
          <p>По событиям потери. Архивирование причины не меняет историю.</p></section>
        <section className="resultPanel analyticsLostReasons"><div className="resultPanelHead"><h2>Почему заявки нецелевые</h2></div>
          {data.analysis.non_target_reasons?.length ? <div className="analyticsLostList">{data.analysis.non_target_reasons.map(reason =>
            <div key={reason.label}><span>{reason.label}</span><strong>{count(reason.count)}</strong></div>)}</div>
            : <p className="resultTableEmpty">За период нет заявок, отмеченных как нецелевые.</p>}
          <p>Отметку ставит менеджер в карточке сделки — по ней маркетолог отключает неработающие кампании и ключи.</p></section>
      </>}
      {tab === "accounts" && <section className="resultPanel analyticsListPanel"><div className="resultPanelHead"><h2>Рекламные кабинеты</h2></div>{data.analysis.connections.length ? <div className="analyticsEntityGrid">{data.analysis.connections.map(account => <article key={account.id}><span>{account.platform}</span><strong>{account.name}</strong><small>Статус: {account.status} · синхронизация: {account.last_synced_at ? new Date(account.last_synced_at).toLocaleDateString("ru-RU") : "—"}</small>{data.viewer.role === "admin" && <Link href={`/admin/advertising/${data.project.workspace_id}`}>Открыть рекламную аналитику →</Link>}</article>)}</div> : <p className="resultTableEmpty">Рекламные кабинеты для проекта не подключены.</p>}</section>}
      {tab === "hypotheses" && <section className="resultPanel analyticsListPanel"><div className="resultPanelHead"><h2>Гипотезы проекта</h2></div>{data.analysis.hypotheses.length ? <div className="analyticsEntityGrid">{data.analysis.hypotheses.map(hypothesis => <article key={hypothesis.id}><span>Гипотеза #{hypothesis.id}</span><strong>{hypothesis.name}</strong><small>Статус: {hypothesis.status}</small><button onClick={() => { setFilterHypothesis(String(hypothesis.id)); setTab("campaigns"); }}>Кампании: {data.analysis.campaigns.filter(campaign => campaign.hypothesis_id === hypothesis.id).length} →</button></article>)}</div> : <p className="resultTableEmpty">Гипотезы для проекта пока не созданы.</p>}</section>}
      {tab === "campaigns" && CampaignTable()}
      {tab === "website" && projectId && <WebsiteAnalytics projectId={projectId} start={start} end={end} role={data.viewer.role} />}
    </>}
  </main>
  {selectedCampaign && <div className="resultModalBackdrop" onMouseDown={event => { if (event.target === event.currentTarget) setSelectedCampaign(null); }}><section className="resultModal analyticsCampaignDetail" role="dialog" aria-modal="true" aria-label="Детали кампании"><header><h2>{selectedCampaign.name}</h2><button type="button" onClick={() => setSelectedCampaign(null)}>×</button></header><p>{selectedCampaign.connection_name} · {selectedCampaign.platform} · ID {selectedCampaign.external_campaign_id}</p><div className="analyticsDetailGrid">{[["Гипотеза", selectedCampaign.hypothesis_name || "—"], ["Расходы", money(selectedCampaign.spend)], ["Показы", count(selectedCampaign.impressions)], ["Клики", count(selectedCampaign.clicks)], ["Лиды", count(selectedCampaign.leads)], ["Квалифицированные", count(selectedCampaign.qualified)], ["Продажи", count(selectedCampaign.sales)], ["Выручка", money(selectedCampaign.revenue)], ["ROMI", percent(selectedCampaign.romi)]].map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div><p>Лиды и продажи показаны только при подтверждённой связи с этой кампанией. Данные по расходам и кликам синхронизированы из рекламного кабинета.</p>{selectedCampaign.platform === "vk_ads" && <Link className="analyticsVkDeepLink" href={`/ads/vk/${selectedCampaign.connection_id}/${selectedCampaign.external_campaign_id}`}>Открыть настройки кампании VK →</Link>}</section></div>}
  </div>;
}
