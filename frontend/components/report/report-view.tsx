"use client";
// Отчёт клиенту: один компонент для конструктора, сохранённого отчёта и ссылки без входа.
// Данные приходят снимком с бэкенда (services/reports.py); здесь только вид и выгрузка в таблицы.
import "./report.css";

export type ReportTotals = { spend: number | null; leads: number | null; qualified: number | null; meetings?: number | null;
  sales: number | null; revenue: number | null; cpl: number | null; cac: number | null; average_check: number | null;
  romi: number | null; target_share: number | null };
export type ReportData = {
  title?: string; project: { name: string }; period: { start: string; end: string; previous_start: string; previous_end: string };
  sections: string[]; headline: string; summary: string | null; totals: ReportTotals; previous: ReportTotals;
  changes: Record<string, number | null>; funnel: { label: string; value: number | null; conversion: number | null }[];
  trend: ({ label: string } & Partial<ReportTotals>)[]; granularity: string; clicks: number | null;
  channels: { id: string; name: string; spend: number | null; leads: number | null; sales: number | null; revenue: number | null;
    cpl: number | null; romi: number | null; revenue_share: number | null }[];
  hypotheses: { id: number; name: string; verdict: "worked" | "failed" | "testing"; spend: number; leads: number; sales: number;
    revenue: number; campaigns: number; cpl: number | null }[];
  sales_cycle_days: number | null; lost_reasons: { label: string; count: number }[];
  sales_team: { deals: number; response_minutes: number | null; unanswered: number;
    managers: { name: string; deals: number; won: number; response_minutes: number | null; conversion: number | null }[] };
  plan: { text: string; owner: string | null }[];
  excluded: { crm_leads: number; channels: number; hypotheses: number; campaigns: number }; built_at: string;
};

const nf = (digits = 0) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits });
export const rub = (n: number | null | undefined) => n == null ? "—" : `${nf().format(n)} ₽`;
const num = (n: number | null | undefined) => n == null ? "—" : nf().format(n);
const pct = (n: number | null | undefined) => n == null ? "—" : `${nf().format(n)}%`;
const day = (s: string) => new Date(`${String(s).slice(0, 10)}T12:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
const dayShort = (s: string) => new Date(`${String(s).slice(0, 10)}T12:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
const minutes = (m: number | null) => m == null ? "—" : m < 60 ? `${Math.round(m)} мин` : m < 1440 ? `${nf(1).format(m / 60)} ч` : `${nf(1).format(m / 1440)} дн`;
const LOWER_IS_BETTER = new Set(["cpl", "cac"]);

export function periodTitle(r: ReportData) {
  const s = new Date(`${r.period.start}T12:00:00`), e = new Date(`${r.period.end}T12:00:00`);
  const wholeMonth = s.getDate() === 1 && new Date(e.getFullYear(), e.getMonth(), e.getDate() + 1).getDate() === 1 && s.getMonth() === e.getMonth();
  return wholeMonth ? `Отчёт за ${s.toLocaleDateString("ru-RU", { month: "long", year: "numeric" }).replace(" г.", "")}` : `Отчёт за ${day(r.period.start)} — ${day(r.period.end)}`;
}

function Delta({ field, r, unit = "%" }: { field: string; r: ReportData; unit?: string }) {
  const v = r.changes[field];
  if (v == null || !Number.isFinite(v)) return <span className="rpDelta">нет сравнения</span>;
  const good = LOWER_IS_BETTER.has(field) ? v < 0 : v > 0;
  return <span className={`rpDelta ${Math.abs(v) < 1 ? "" : good ? "good" : "bad"}`}>{v > 0 ? "▲" : v < 0 ? "▼" : "•"} {nf().format(Math.abs(v))}{unit} к прошлому периоду</span>;
}

/** Автотекст под заголовком, если менеджер не написал свой. */
export function autoSummary(r: ReportData) {
  const parts: string[] = [];
  const top = r.channels[0];
  if (top && top.revenue && top.revenue_share) parts.push(`Больше всего выручки дал канал «${top.name}» — ${nf().format(top.revenue_share)}%.`);
  else if (top && top.leads) parts.push(`Больше всего заявок пришло из «${top.name}» — ${num(top.leads)}.`);
  const worked = r.hypotheses.find(h => h.verdict === "worked"), failed = r.hypotheses.find(h => h.verdict === "failed");
  if (worked) parts.push(`Сработала гипотеза «${worked.name}»: ${num(worked.sales)} продаж.`);
  if (failed) parts.push(`«${failed.name}» не окупилась — ${rub(failed.spend)} без продаж.`);
  if (r.sales_team.response_minutes != null) parts.push(`Отдел продаж отвечает клиентам в среднем за ${minutes(r.sales_team.response_minutes)}.`);
  return parts.join(" ") || "Ключевые цифры периода и сравнение с прошлым периодом.";
}

function TrendChart({ r }: { r: ReportData }) {
  const points = r.trend;
  if (!points.length) return <p className="rpEmpty">Нет данных за период.</p>;
  const w = 1000, h = 260, left = 40, right = 20, bottom = 40, top = 30;
  const maxSpend = Math.max(1, ...points.map(p => Number(p.spend || 0)));
  const maxLeads = Math.max(1, ...points.map(p => Number(p.leads || 0)));
  const step = (w - left - right) / points.length;
  const bw = Math.min(90, step * 0.55);
  const y = (v: number, max: number) => h - bottom - (v / max) * (h - bottom - top);
  const line = points.map((p, i) => `${left + step * i + step / 2},${y(Number(p.leads || 0), maxLeads)}`).join(" ");
  const last = points[points.length - 1];
  const every = Math.ceil(points.length / 10);
  return <svg viewBox={`0 0 ${w} ${h}`} className="rpChart" role="img" aria-label={`Расходы и лиды по ${r.granularity === "week" ? "неделям" : "дням"}`}>
    <line x1={left} y1={h - bottom} x2={w - right} y2={h - bottom} stroke="#E3E8F2" />
    {points.map((p, i) => <rect key={`b${i}`} x={left + step * i + (step - bw) / 2} y={y(Number(p.spend || 0), maxSpend)} width={bw}
      height={h - bottom - y(Number(p.spend || 0), maxSpend)} rx={Math.min(8, bw / 4)} fill="#C9D8F7" />)}
    <polyline points={line} fill="none" stroke="#1E5FE0" strokeWidth={4} strokeLinecap="round" strokeLinejoin="round" />
    {points.map((p, i) => <circle key={`c${i}`} cx={left + step * i + step / 2} cy={y(Number(p.leads || 0), maxLeads)} r={points.length > 20 ? 3 : 6} fill="#1E5FE0" />)}
    <text x={left + step * (points.length - 1) + step / 2} y={y(Number(last.leads || 0), maxLeads) - 14} textAnchor="middle" fontSize="16" fontWeight="800" fill="#14213A">{num(last.leads || 0)} лид.</text>
    {points.map((p, i) => i % every === 0 ? <text key={`t${i}`} x={left + step * i + step / 2} y={h - 14} textAnchor="middle" fontSize="14" fill="#5B6478">{dayShort(p.label)}</text> : null)}
  </svg>;
}

const VERDICT = { worked: ["СРАБОТАЛО", "good"], failed: ["НЕ СРАБОТАЛО", "bad"], testing: ["ТЕСТИРУЕМ", "info"] } as const;
const OWNER_TONE: Record<string, string> = { "Агентство": "info", "Отдел продаж": "good", "Решение клиента": "bad" };

export function ReportView({ report: r }: { report: ReportData }) {
  const show = (key: string) => r.sections.includes(key);
  const t = r.totals;
  const funnel = [...(r.clicks ? [{ label: "Клики", value: r.clicks, conversion: null as number | null }] : []), ...r.funnel];
  const funnelMax = Math.max(1, ...funnel.map(f => f.value || 0));
  let worst = -1, worstRate = 101;
  funnel.forEach((f, i) => { if (i > 0 && f.conversion != null && funnel[i - 1].label !== "Клики" && f.conversion < worstRate) { worstRate = f.conversion; worst = i; } });
  const excludedText = [r.excluded.crm_leads ? `${r.excluded.crm_leads} лид. из CRM` : "", r.excluded.channels ? `каналов: ${r.excluded.channels}` : "",
    r.excluded.hypotheses ? `гипотез: ${r.excluded.hypotheses}` : "", r.excluded.campaigns ? `кампаний: ${r.excluded.campaigns}` : ""].filter(Boolean).join(", ");
  return <article className="rp">
    <header className="rpHead">
      <div className="rpKicker">{r.project.name}</div>
      <h1>{r.title && !r.title.startsWith("Отчёт ") ? r.title : periodTitle(r)}</h1>
      <div className="rpSub">{day(r.period.start)} — {day(r.period.end)} · сравнение с {day(r.period.previous_start)} — {day(r.period.previous_end)}</div>
    </header>

    {show("summary") && <section className="rpHero" aria-label="Главное за период">
      <div className="rpHeroText">
        <span className="rpPill">ГЛАВНОЕ ЗА ПЕРИОД</span>
        <h2>{r.headline}</h2>
        <p>{r.summary || autoSummary(r)}</p>
      </div>
      <div className="rpHeroGrid">
        <div><span>Выручка</span><strong>{rub(t.revenue)}</strong><Delta field="revenue" r={r} /></div>
        {t.romi != null ? <div><span>ROMI</span><strong>{pct(t.romi)}</strong><Delta field="romi" r={r} /></div>
          : <div><span>Выручка на 1 ₽ рекламы</span><strong>{t.revenue != null && t.spend ? `${nf(1).format(t.revenue / t.spend)} ₽` : "—"}</strong><span className="rpDelta">укажите маржу — посчитаем ROMI</span></div>}
        <div><span>Вложено в рекламу</span><strong>{rub(t.spend)}</strong><Delta field="spend" r={r} /></div>
        <div><span>Стоимость продажи</span><strong>{rub(t.cac)}</strong><Delta field="cac" r={r} /></div>
      </div>
    </section>}

    {show("kpi") && <section className="rpKpis" aria-label="Ключевые показатели">
      {([["Лиды", num(t.leads), "leads"], ["Цена лида", rub(t.cpl), "cpl"], ["Целевые", pct(t.target_share), "target_share"],
        ["Квалифицированы", num(t.qualified), "qualified"], ["Продажи", num(t.sales), "sales"], ["Средний чек", rub(t.average_check), "average_check"]] as const)
        .map(([label, value, field]) => <div key={field} className="rpCard rpKpi"><span>{label}</span><strong>{value}</strong><Delta field={field} r={r} /></div>)}
    </section>}

    {show("funnel") && funnel.length > 0 && <section className="rpCard rpBlock" aria-label="Путь клиента">
      <div className="rpBlockHead"><h2>Путь клиента: от рекламы до оплаты</h2>{worst > 0 && <span>Узкое место — {funnel[worst - 1].label.toLowerCase()} → {funnel[worst].label.toLowerCase()}</span>}</div>
      <div className="rpFunnel">{funnel.map((f, i) => <div key={f.label}>
        {i > 0 && <div className={`rpFunnelStep ${i === worst ? "bad" : ""}`}>↓ {f.conversion != null ? `${nf(1).format(f.conversion)}%` : funnel[i - 1].value ? `${nf(1).format((f.value || 0) / (funnel[i - 1].value || 1) * 100)}%` : "—"}{i === worst ? " — здесь теряем больше всего" : ""}</div>}
        <div className="rpFunnelRow"><span>{f.label}</span><div className="rpBar"><i style={{ width: `${Math.max(2, (f.value || 0) / funnelMax * 100)}%` }} className={i === funnel.length - 1 ? "dark" : ""} /></div><strong>{num(f.value)}</strong></div>
      </div>)}</div>
    </section>}

    {show("trend") && <section className="rpCard rpBlock" aria-label="Динамика">
      <div className="rpBlockHead"><h2>Расходы и заявки по {r.granularity === "week" ? "неделям" : "дням"}</h2>
        <div className="rpLegend"><span><i className="bar" />Расходы</span><span><i className="line" />Лиды</span></div></div>
      <TrendChart r={r} />
    </section>}

    {show("channels") && r.channels.length > 0 && <section className="rpCard rpBlock" aria-label="Каналы">
      <div className="rpBlockHead"><h2>Какие каналы принесли деньги</h2></div>
      <div className="rpTableBox"><table className="rpTable"><thead><tr><th>Канал</th><th>Доля выручки</th><th>Расходы</th><th>Лиды</th><th>Цена лида</th><th>Продажи</th><th>ROMI</th></tr></thead>
        <tbody>{r.channels.map(c => <tr key={c.id}><td><strong>{c.name}</strong></td>
          <td><div className="rpShare"><div><i style={{ width: `${Math.min(100, c.revenue_share || 0)}%` }} /></div><span>{c.revenue_share != null ? pct(c.revenue_share) : "—"}</span></div></td>
          <td>{rub(c.spend)}</td><td>{num(c.leads)}</td><td>{rub(c.cpl)}</td><td>{num(c.sales)}</td>
          <td className={c.romi == null ? "" : c.romi >= 0 ? "good" : "bad"}>{pct(c.romi)}</td></tr>)}</tbody></table></div>
    </section>}

    {show("hypotheses") && r.hypotheses.length > 0 && <section className="rpHyps" aria-label="Гипотезы">
      {r.hypotheses.slice(0, 6).map(h => <div key={h.id} className="rpCard rpHyp">
        <span className={`rpTag ${VERDICT[h.verdict][1]}`}>{VERDICT[h.verdict][0]}</span>
        <h3>{h.name}</h3>
        <p>{h.verdict === "worked" ? `${num(h.sales)} продаж на ${rub(h.revenue)} при цене лида ${rub(h.cpl)}.`
          : h.verdict === "failed" ? `${num(h.leads)} заявок и ${rub(h.spend)} расходов, но ни одной продажи.`
          : `${num(h.leads)} заявок, ${num(h.sales)} продаж — данных пока мало для вывода.`}</p>
      </div>)}
    </section>}

    {show("sales") && r.sales_team.deals > 0 && <section className="rpCard rpBlock" aria-label="Отдел продаж">
      <div className="rpBlockHead"><h2>Как работал отдел продаж</h2><span>Данные CRM портала</span></div>
      <div className="rpStats">
        <div><span>Первый ответ клиенту</span><strong>{minutes(r.sales_team.response_minutes)}</strong></div>
        <div><span>Заявок без ответа</span><strong>{num(r.sales_team.unanswered)}</strong></div>
        <div><span>Цикл сделки</span><strong>{r.sales_cycle_days != null ? `${nf().format(r.sales_cycle_days)} дн.` : "—"}</strong></div>
      </div>
      {r.sales_team.managers.length > 0 && <div className="rpTableBox"><table className="rpTable"><thead><tr><th>Менеджер</th><th>Сделок</th><th>Ответ</th><th>Продажи</th><th>Конверсия</th></tr></thead>
        <tbody>{r.sales_team.managers.map(m => <tr key={m.name}><td><strong>{m.name}</strong></td><td>{num(m.deals)}</td><td>{minutes(m.response_minutes)}</td><td>{num(m.won)}</td><td><strong>{m.conversion != null ? `${nf(1).format(m.conversion)}%` : "—"}</strong></td></tr>)}</tbody></table></div>}
    </section>}

    {show("plan") && r.plan.length > 0 && <section className="rpCard rpBlock" aria-label="План">
      <div className="rpBlockHead"><h2>План на следующий период</h2></div>
      <ol className="rpPlan">{r.plan.map((p, i) => <li key={i}><b>{i + 1}</b><span>{p.text}</span>{p.owner && <em className={`rpTag ${OWNER_TONE[p.owner] || "info"}`}>{p.owner}</em>}</li>)}</ol>
    </section>}

    <footer className="rpFoot"><span>Отчёт подготовлен StepToLead по данным портала: реклама, заявки и CRM в одном месте.</span>
      {excludedText && <span>Не учтены в отчёте: {excludedText}</span>}</footer>
  </article>;
}

// ── Выгрузка для Google Таблиц: TSV вставляется в ячейки как есть (Ctrl+V в A1). ──
const cell = (v: unknown) => v == null ? "" : typeof v === "number" ? String(Math.round(v * 100) / 100).replace(".", ",") : String(v).replace(/[\t\n\r]+/g, " ");
export function reportToTsv(r: ReportData) {
  const rows: unknown[][] = [];
  rows.push([periodTitle(r), r.project.name], [`${r.period.start} — ${r.period.end}`, `сравнение: ${r.period.previous_start} — ${r.period.previous_end}`], []);
  rows.push(["Главное", r.headline], ["", r.summary || autoSummary(r)], []);
  rows.push(["Показатель", "Период", "Прошлый период", "Изменение, %"]);
  const metrics: [string, keyof ReportTotals][] = [["Расходы, ₽", "spend"], ["Лиды", "leads"], ["Цена лида, ₽", "cpl"], ["Квалифицированы", "qualified"],
    ["Продажи", "sales"], ["Выручка, ₽", "revenue"], ["Средний чек, ₽", "average_check"], ["Стоимость продажи, ₽", "cac"], ["ROMI, %", "romi"], ["Доля целевых, %", "target_share"]];
  for (const [label, key] of metrics) rows.push([label, r.totals[key], r.previous[key], r.changes[key]]);
  rows.push([], ["Воронка", "Значение", "Конверсия, %"]);
  if (r.clicks) rows.push(["Клики", r.clicks, ""]);
  for (const f of r.funnel) rows.push([f.label, f.value, f.conversion]);
  rows.push([], ["Динамика", "Расходы, ₽", "Лиды", "Продажи", "Выручка, ₽"]);
  for (const p of r.trend) rows.push([p.label, p.spend, p.leads, p.sales, p.revenue]);
  rows.push([], ["Канал", "Расходы, ₽", "Лиды", "Цена лида, ₽", "Продажи", "Выручка, ₽", "Доля выручки, %", "ROMI, %"]);
  for (const c of r.channels) rows.push([c.name, c.spend, c.leads, c.cpl, c.sales, c.revenue, c.revenue_share, c.romi]);
  if (r.hypotheses.length) {
    rows.push([], ["Гипотеза", "Итог", "Расходы, ₽", "Лиды", "Продажи", "Выручка, ₽"]);
    for (const h of r.hypotheses) rows.push([h.name, VERDICT[h.verdict][0].toLowerCase(), h.spend, h.leads, h.sales, h.revenue]);
  }
  if (r.sales_team.managers.length) {
    rows.push([], ["Менеджер", "Сделок", "Первый ответ, мин", "Продажи", "Конверсия, %"]);
    for (const m of r.sales_team.managers) rows.push([m.name, m.deals, m.response_minutes, m.won, m.conversion]);
  }
  if (r.plan.length) { rows.push([], ["План", "Ответственный"]); for (const p of r.plan) rows.push([p.text, p.owner]); }
  return rows.map(row => row.map(cell).join("\t")).join("\n");
}

export async function copyForSheets(r: ReportData) {
  await navigator.clipboard.writeText(reportToTsv(r));
}
