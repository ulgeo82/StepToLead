"use client";
// Отчёт клиенту: один компонент для конструктора, сохранённого отчёта и ссылки без входа.
// Данные приходят снимком с бэкенда (services/reports.py); здесь только вид и выгрузка в таблицы.
import "./report.css";

export type ReportTotals = { spend: number | null; leads: number | null; qualified: number | null; meetings?: number | null;
  sales: number | null; revenue: number | null; cpl: number | null; cac: number | null; average_check: number | null;
  romi: number | null; target_share: number | null; drr?: number | null; profit?: number | null };
type Outside = { name: string; spend?: number | null; leads?: number | null; revenue?: number | null };
export type ReportData = {
  title?: string; project: { name: string }; period: { start: string; end: string; previous_start: string; previous_end: string };
  sections: string[]; headline: string; summary: string | null; totals: ReportTotals; previous: ReportTotals;
  changes: Record<string, number | null>; hero?: string[]; extra_costs?: { name: string; amount: number }[];
  budget_plan?: { amount: number; used: number } | null; bottleneck?: string;
  funnel: { label: string; value: number | null; conversion: number | null }[];
  trend: ({ label: string } & Partial<ReportTotals>)[]; granularity: string; clicks: number | null;
  channels: { id: string; name: string; spend: number | null; leads: number | null; sales: number | null; revenue: number | null;
    cpl: number | null; romi: number | null; revenue_share: number | null }[];
  hypotheses: { id: number; name: string; verdict: "worked" | "failed" | "testing"; in_totals?: boolean; spend: number; leads: number;
    sales: number; revenue: number; campaigns: number; cpl: number | null }[];
  hidden?: { channels: string[]; hypotheses: number[]; campaigns: string[] };
  sales_cycle_days: number | null; lost_reasons: { label: string; count: number }[];
  sales_team: { deals: number; response_minutes: number | null; unanswered: number; overdue?: number;
    managers: { id?: number; name: string; deals: number; won: number; overdue?: number; response_minutes: number | null; conversion: number | null }[];
    all_managers?: { id: number; name: string }[] };
  sales_list?: { lead_id: number; name: string; amount: number | null; date: string }[];
  plan: { text: string; owner: string | null }[];
  plan_fact?: { text: string; owner: string | null; done: boolean; note: string | null }[];
  forecast?: { spend: number | null; leads: number | null; sales: number | null; revenue: number | null } | null;
  comments?: Record<string, string>;
  excluded: { crm_leads: number; channels?: Outside[]; hypotheses?: Outside[]; campaigns?: Outside[]; deals?: Outside[] };
  built_at: string;
};

const nf = (digits = 0) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits });
export const rub = (n: number | null | undefined) => n == null ? "—" : `${nf().format(n)} ₽`;
const num = (n: number | null | undefined) => n == null ? "—" : nf().format(n);
const pct = (n: number | null | undefined) => n == null ? "—" : `${nf().format(n)}%`;
const day = (s: string) => new Date(`${String(s).slice(0, 10)}T12:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
const dayShort = (s: string) => new Date(`${String(s).slice(0, 10)}T12:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
const monthShort = (s: string) => new Date(`${String(s).slice(0, 10)}T12:00:00`).toLocaleDateString("ru-RU", { month: "short", year: "2-digit" });
const minutes = (m: number | null | undefined) => m == null ? "—" : m < 60 ? `${Math.round(m)} мин` : m < 1440 ? `${nf(1).format(m / 60)} ч` : `${nf(1).format(m / 1440)} дн`;
const LOWER_IS_BETTER = new Set(["cpl", "cac", "drr"]);

/** Пул главных цифр: в конструкторе выбирают 4 (e-com — ДРР, B2B — квал. лиды и ROMI). */
export const HERO: Record<string, { label: string; value: (t: ReportTotals) => string }> = {
  revenue: { label: "Выручка", value: t => rub(t.revenue) },
  profit: { label: "Прибыль после рекламы", value: t => rub(t.profit) },
  romi: { label: "ROMI", value: t => pct(t.romi) },
  drr: { label: "ДРР", value: t => pct(t.drr) },
  spend: { label: "Вложено в рекламу", value: t => rub(t.spend) },
  leads: { label: "Лиды", value: t => num(t.leads) },
  qualified: { label: "Квалифицированные лиды", value: t => num(t.qualified) },
  cpl: { label: "Цена лида", value: t => rub(t.cpl) },
  cac: { label: "Стоимость продажи", value: t => rub(t.cac) },
  average_check: { label: "Средний чек", value: t => rub(t.average_check) },
};

export function periodTitle(r: ReportData) {
  const s = new Date(`${r.period.start}T12:00:00`), e = new Date(`${r.period.end}T12:00:00`);
  const wholeMonth = s.getDate() === 1 && new Date(e.getFullYear(), e.getMonth(), e.getDate() + 1).getDate() === 1 && s.getMonth() === e.getMonth();
  return wholeMonth ? `Отчёт за ${s.toLocaleDateString("ru-RU", { month: "long", year: "numeric" }).replace(" г.", "")}` : `Отчёт за ${day(r.period.start)} — ${day(r.period.end)}`;
}

function Delta({ field, r }: { field: string; r: ReportData }) {
  const v = r.changes[field];
  if (v == null || !Number.isFinite(v)) return <span className="rpDelta">нет сравнения</span>;
  const good = LOWER_IS_BETTER.has(field) ? v < 0 : v > 0;
  return <span className={`rpDelta ${Math.abs(v) < 1 || field === "spend" ? "" : good ? "good" : "bad"}`}>{v > 0 ? "▲" : v < 0 ? "▼" : "•"} {nf().format(Math.abs(v))}% к прошлому периоду</span>;
}

/** Автотекст под заголовком, если менеджер не написал свой. */
export function autoSummary(r: ReportData) {
  const parts: string[] = [];
  const top = r.channels[0];
  if (top && top.revenue && top.revenue_share) parts.push(`Больше всего выручки дал канал «${top.name}» — ${nf().format(top.revenue_share)}%.`);
  else if (top && top.leads) parts.push(`Больше всего заявок пришло из «${top.name}» — ${num(top.leads)}.`);
  const counted = r.hypotheses.filter(h => h.in_totals !== false);
  const worked = counted.find(h => h.verdict === "worked"), failed = counted.find(h => h.verdict === "failed");
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
  const label = r.granularity === "month" ? monthShort : dayShort;
  return <svg viewBox={`0 0 ${w} ${h}`} className="rpChart" role="img" aria-label="Расходы и лиды за период">
    <line x1={left} y1={h - bottom} x2={w - right} y2={h - bottom} stroke="#E3E8F2" />
    {points.map((p, i) => <rect key={`b${i}`} x={left + step * i + (step - bw) / 2} y={y(Number(p.spend || 0), maxSpend)} width={bw}
      height={h - bottom - y(Number(p.spend || 0), maxSpend)} rx={Math.min(8, bw / 4)} fill="#C9D8F7" />)}
    <polyline points={line} fill="none" stroke="#1E5FE0" strokeWidth={4} strokeLinecap="round" strokeLinejoin="round" />
    {points.map((p, i) => <circle key={`c${i}`} cx={left + step * i + step / 2} cy={y(Number(p.leads || 0), maxLeads)} r={points.length > 20 ? 3 : 6} fill="#1E5FE0" />)}
    <text x={left + step * (points.length - 1) + step / 2} y={y(Number(last.leads || 0), maxLeads) - 14} textAnchor="middle" fontSize="16" fontWeight="800" fill="#14213A">{num(last.leads || 0)} лид.</text>
    {points.map((p, i) => i % every === 0 ? <text key={`t${i}`} x={left + step * i + step / 2} y={h - 14} textAnchor="middle" fontSize="14" fill="#5B6478">{label(p.label)}</text> : null)}
  </svg>;
}

const VERDICT = { worked: ["СРАБОТАЛО", "good"], failed: ["НЕ СРАБОТАЛО", "bad"], testing: ["ТЕСТИРУЕМ", "info"] } as const;
const OWNER_TONE: Record<string, string> = { "Агентство": "info", "Отдел продаж": "good", "Решение клиента": "bad" };
const Comment = ({ r, k }: { r: ReportData; k: string }) => r.comments?.[k] ? <p className="rpComment"><b>Комментарий:</b> {r.comments[k]}</p> : null;

function outsideRows(r: ReportData) {
  const rows: { kind: string; name: string; detail: string }[] = [];
  const detail = (o: Outside) => [o.spend ? rub(o.spend) : "", o.leads ? `${num(o.leads)} лид.` : "", o.revenue ? `выручка ${rub(o.revenue)}` : ""].filter(Boolean).join(" · ");
  for (const o of r.excluded.channels || []) rows.push({ kind: "Канал", name: o.name, detail: detail(o) });
  for (const o of r.excluded.hypotheses || []) rows.push({ kind: "Гипотеза", name: o.name, detail: detail(o) });
  for (const o of r.excluded.campaigns || []) rows.push({ kind: "Кампания", name: o.name, detail: detail(o) });
  for (const o of r.excluded.deals || []) rows.push({ kind: "Сделка", name: o.name, detail: detail(o) || "без продажи" });
  if (r.excluded.crm_leads) rows.push({ kind: "CRM", name: `Заявки с отметкой «не считать в аналитику»`, detail: `${num(r.excluded.crm_leads)} шт.` });
  return rows;
}

export function ReportView({ report: r }: { report: ReportData }) {
  const show = (key: string) => r.sections.includes(key);
  const t = r.totals;
  const hidden = r.hidden || { channels: [], hypotheses: [], campaigns: [] };
  const funnel = [...(r.clicks ? [{ label: "Клики", value: r.clicks, conversion: null as number | null }] : []), ...r.funnel];
  const funnelMax = Math.max(1, ...funnel.map(f => f.value || 0));
  let worst = -1;
  if (r.bottleneck && r.bottleneck !== "auto" && r.bottleneck !== "hide") worst = funnel.findIndex(f => f.label === r.bottleneck);
  else if (r.bottleneck !== "hide") {
    let worstRate = 101;
    funnel.forEach((f, i) => { if (i > 0 && f.conversion != null && funnel[i - 1].label !== "Клики" && f.conversion < worstRate) { worstRate = f.conversion; worst = i; } });
  }
  const hero = (r.hero && r.hero.length ? r.hero : ["revenue", "romi", "spend", "cac"]).filter(k => HERO[k]);
  const channels = r.channels.filter(c => !hidden.channels.includes(c.id));
  const hiddenChannels = r.channels.length - channels.length;
  const hyps = r.hypotheses.filter(h => !hidden.hypotheses.includes(h.id));
  const outside = outsideRows(r);
  const extra = r.extra_costs || [];
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
        {r.budget_plan && <div className="rpBudget"><div><span>Бюджет: {rub(t.spend)} из {rub(r.budget_plan.amount)}</span><b>{nf().format(r.budget_plan.used)}%</b></div>
          <div className="rpBudgetBar"><i style={{ width: `${Math.min(100, r.budget_plan.used)}%` }} /></div></div>}
      </div>
      <div className="rpHeroGrid">{hero.map(key => <div key={key}><span>{HERO[key].label}</span><strong>{HERO[key].value(t)}</strong>
        {key === "romi" && t.romi == null ? <span className="rpDelta">укажите маржу в экономике проекта</span> : <Delta field={key} r={r} />}</div>)}</div>
    </section>}
    {show("summary") && <Comment r={r} k="summary" />}

    {show("kpi") && <section className="rpKpis" aria-label="Ключевые показатели">
      {([["Лиды", num(t.leads), "leads"], ["Цена лида", rub(t.cpl), "cpl"], ["Целевые", pct(t.target_share), "target_share"],
        ["Квалифицированы", num(t.qualified), "qualified"], ["Продажи", num(t.sales), "sales"], ["Средний чек", rub(t.average_check), "average_check"]] as const)
        .map(([label, value, field]) => <div key={field} className="rpCard rpKpi"><span>{label}</span><strong>{value}</strong><Delta field={field} r={r} /></div>)}
    </section>}
    {show("kpi") && <Comment r={r} k="kpi" />}

    {show("funnel") && funnel.length > 0 && <section className="rpCard rpBlock" aria-label="Путь клиента">
      <div className="rpBlockHead"><h2>Путь клиента: от рекламы до оплаты</h2>{worst > 0 && <span>Узкое место — {funnel[worst - 1].label.toLowerCase()} → {funnel[worst].label.toLowerCase()}</span>}</div>
      <div className="rpFunnel">{funnel.map((f, i) => <div key={f.label}>
        {i > 0 && <div className={`rpFunnelStep ${i === worst ? "bad" : ""}`}>↓ {f.conversion != null ? `${nf(1).format(f.conversion)}%` : funnel[i - 1].value ? `${nf(1).format((f.value || 0) / (funnel[i - 1].value || 1) * 100)}%` : "—"}{i === worst ? " — здесь теряем больше всего" : ""}</div>}
        <div className="rpFunnelRow"><span>{f.label}</span><div className="rpBar"><i style={{ width: `${Math.max(2, (f.value || 0) / funnelMax * 100)}%` }} className={i === funnel.length - 1 ? "dark" : ""} /></div><strong>{num(f.value)}</strong></div>
      </div>)}</div>
      <Comment r={r} k="funnel" />
    </section>}

    {show("trend") && <section className="rpCard rpBlock" aria-label="Динамика">
      <div className="rpBlockHead"><h2>Расходы и заявки по {r.granularity === "month" ? "месяцам" : r.granularity === "week" ? "неделям" : "дням"}</h2>
        <div className="rpLegend"><span><i className="bar" />Расходы</span><span><i className="line" />Лиды</span></div></div>
      <TrendChart r={r} />
      <Comment r={r} k="trend" />
    </section>}

    {show("channels") && (channels.length > 0 || extra.length > 0) && <section className="rpCard rpBlock" aria-label="Каналы">
      <div className="rpBlockHead"><h2>Какие каналы принесли деньги</h2>{hiddenChannels > 0 && <span>Ещё {hiddenChannels} канал(а) скрыты из таблицы, их цифры учтены в итогах</span>}</div>
      <div className="rpTableBox"><table className="rpTable"><thead><tr><th>Канал</th><th>Доля выручки</th><th>Расходы</th><th>Лиды</th><th>Цена лида</th><th>Продажи</th><th>ROMI</th></tr></thead>
        <tbody>{channels.map(c => <tr key={c.id}><td><strong>{c.name}</strong></td>
          <td><div className="rpShare"><div><i style={{ width: `${Math.min(100, c.revenue_share || 0)}%` }} /></div><span>{c.revenue_share != null ? pct(c.revenue_share) : "—"}</span></div></td>
          <td>{rub(c.spend)}</td><td>{num(c.leads)}</td><td>{rub(c.cpl)}</td><td>{num(c.sales)}</td>
          <td className={c.romi == null ? "" : c.romi >= 0 ? "good" : "bad"}>{pct(c.romi)}</td></tr>)}
          {extra.map(c => <tr key={`x${c.name}`} className="rpExtra"><td><strong>{c.name}</strong><small>доп. расходы, учтены в итогах</small></td><td>—</td><td>{rub(c.amount)}</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>)}
        </tbody></table></div>
      <Comment r={r} k="channels" />
    </section>}

    {show("hypotheses") && hyps.length > 0 && <section className="rpHyps" aria-label="Гипотезы">
      {hyps.slice(0, 9).map(h => <div key={h.id} className={`rpCard rpHyp ${h.in_totals === false ? "outside" : ""}`}>
        <div className="rpHypTags"><span className={`rpTag ${VERDICT[h.verdict][1]}`}>{VERDICT[h.verdict][0]}</span>{h.in_totals === false && <span className="rpTag muted">НЕ В ИТОГАХ</span>}</div>
        <h3>{h.name}</h3>
        <p>{h.verdict === "worked" ? `${num(h.sales)} продаж на ${rub(h.revenue)} при цене лида ${rub(h.cpl)}.`
          : h.verdict === "failed" ? `${num(h.leads)} заявок и ${rub(h.spend)} расходов, но ни одной продажи.`
          : `${num(h.leads)} заявок, ${num(h.sales)} продаж — данных пока мало для вывода.`}
          {h.in_totals === false ? " Тестовый бюджет не входит в общие итоги отчёта." : ""}</p>
      </div>)}
    </section>}
    {show("hypotheses") && <Comment r={r} k="hypotheses" />}

    {show("sales") && r.sales_team.deals > 0 && <section className="rpCard rpBlock" aria-label="Отдел продаж">
      <div className="rpBlockHead"><h2>Как работал отдел продаж</h2><span>Данные CRM портала</span></div>
      <div className="rpStats">
        <div><span>Первый ответ клиенту</span><strong>{minutes(r.sales_team.response_minutes)}</strong></div>
        <div><span>Заявок без ответа</span><strong>{num(r.sales_team.unanswered)}</strong></div>
        <div><span>Просроченные задачи</span><strong>{num(r.sales_team.overdue ?? 0)}</strong></div>
        <div><span>Цикл сделки</span><strong>{r.sales_cycle_days != null ? `${nf().format(r.sales_cycle_days)} дн.` : "—"}</strong></div>
      </div>
      {r.sales_team.managers.length > 0 && <div className="rpTableBox"><table className="rpTable"><thead><tr><th>Менеджер</th><th>Сделок</th><th>Ответ</th><th>Продажи</th><th>Конверсия</th><th>Просрочено</th></tr></thead>
        <tbody>{r.sales_team.managers.map(m => <tr key={`${m.id}-${m.name}`}><td><strong>{m.name}</strong></td><td>{num(m.deals)}</td><td>{minutes(m.response_minutes)}</td><td>{num(m.won)}</td>
          <td><strong>{m.conversion != null ? `${nf(1).format(m.conversion)}%` : "—"}</strong></td><td className={m.overdue ? "bad" : ""}>{num(m.overdue ?? 0)}</td></tr>)}</tbody></table></div>}
      <Comment r={r} k="sales" />
    </section>}

    {show("plan") && (r.plan_fact?.length || r.plan.length || r.forecast) ? <section className="rpCard rpBlock" aria-label="План">
      {r.plan_fact && r.plan_fact.length > 0 && <>
        <div className="rpBlockHead"><h2>Что обещали в прошлый раз</h2><span>Выполнено {r.plan_fact.filter(p => p.done).length} из {r.plan_fact.length}</span></div>
        <ul className="rpFact">{r.plan_fact.map((p, i) => <li key={i} className={p.done ? "done" : "open"}><b aria-hidden="true">{p.done ? "✓" : "—"}</b>
          <span>{p.text}{p.note ? <small>{p.note}</small> : null}</span>{p.owner && <em className={`rpTag ${OWNER_TONE[p.owner] || "info"}`}>{p.owner}</em>}</li>)}</ul>
      </>}
      {r.plan.length > 0 && <><div className="rpBlockHead"><h2>План на следующий период</h2></div>
        <ol className="rpPlan">{r.plan.map((p, i) => <li key={i}><b>{i + 1}</b><span>{p.text}</span>{p.owner && <em className={`rpTag ${OWNER_TONE[p.owner] || "info"}`}>{p.owner}</em>}</li>)}</ol></>}
      {r.forecast && <div className="rpForecast"><span>Прогноз на следующий период</span><div>
        {r.forecast.spend != null && <div><small>Бюджет</small><strong>{rub(r.forecast.spend)}</strong></div>}
        {r.forecast.leads != null && <div><small>Лиды</small><strong>{num(r.forecast.leads)}</strong></div>}
        {r.forecast.sales != null && <div><small>Продажи</small><strong>{num(r.forecast.sales)}</strong></div>}
        {r.forecast.revenue != null && <div><small>Выручка</small><strong>{rub(r.forecast.revenue)}</strong></div>}
      </div></div>}
      <Comment r={r} k="plan" />
    </section> : null}

    {outside.length > 0 && <section className="rpCard rpBlock rpOutside" aria-label="Что не вошло в расчёт">
      <div className="rpBlockHead"><h2>Что не вошло в расчёт</h2><span>Эти данные не входят в итоги отчёта — показываем их, чтобы ничего не было скрыто</span></div>
      <ul>{outside.map((o, i) => <li key={i}><em>{o.kind}</em><strong>{o.name}</strong><span>{o.detail}</span></li>)}</ul>
    </section>}

    <footer className="rpFoot"><span>Отчёт подготовлен StepToLead по данным портала: реклама, заявки и CRM в одном месте.</span>
      {extra.length > 0 && <span>Доп. расходы учтены в обоих периодах для честного сравнения.</span>}</footer>
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
    ["Продажи", "sales"], ["Выручка, ₽", "revenue"], ["Средний чек, ₽", "average_check"], ["Стоимость продажи, ₽", "cac"], ["ROMI, %", "romi"],
    ["ДРР, %", "drr"], ["Прибыль после рекламы, ₽", "profit"], ["Доля целевых, %", "target_share"]];
  for (const [label, key] of metrics) rows.push([label, r.totals[key], r.previous[key], r.changes[key]]);
  if (r.budget_plan) rows.push(["План бюджета, ₽", r.budget_plan.amount, "", ""], ["Использовано плана, %", r.budget_plan.used, "", ""]);
  rows.push([], ["Воронка", "Значение", "Конверсия, %"]);
  if (r.clicks) rows.push(["Клики", r.clicks, ""]);
  for (const f of r.funnel) rows.push([f.label, f.value, f.conversion]);
  rows.push([], ["Динамика", "Расходы, ₽", "Лиды", "Продажи", "Выручка, ₽"]);
  for (const p of r.trend) rows.push([p.label, p.spend, p.leads, p.sales, p.revenue]);
  rows.push([], ["Канал", "Расходы, ₽", "Лиды", "Цена лида, ₽", "Продажи", "Выручка, ₽", "Доля выручки, %", "ROMI, %"]);
  for (const c of r.channels) rows.push([c.name, c.spend, c.leads, c.cpl, c.sales, c.revenue, c.revenue_share, c.romi]);
  for (const c of r.extra_costs || []) rows.push([`${c.name} (доп. расходы)`, c.amount]);
  if (r.hypotheses.length) {
    rows.push([], ["Гипотеза", "Итог", "В итогах", "Расходы, ₽", "Лиды", "Продажи", "Выручка, ₽"]);
    for (const h of r.hypotheses) rows.push([h.name, VERDICT[h.verdict][0].toLowerCase(), h.in_totals === false ? "нет" : "да", h.spend, h.leads, h.sales, h.revenue]);
  }
  if (r.sales_team.managers.length) {
    rows.push([], ["Менеджер", "Сделок", "Первый ответ, мин", "Продажи", "Конверсия, %", "Просрочено задач"]);
    for (const m of r.sales_team.managers) rows.push([m.name, m.deals, m.response_minutes, m.won, m.conversion, m.overdue ?? 0]);
  }
  if (r.plan_fact?.length) { rows.push([], ["Что обещали", "Выполнено", "Ответственный", "Комментарий"]); for (const p of r.plan_fact) rows.push([p.text, p.done ? "да" : "нет", p.owner, p.note]); }
  if (r.plan.length) { rows.push([], ["План", "Ответственный"]); for (const p of r.plan) rows.push([p.text, p.owner]); }
  if (r.forecast) rows.push([], ["Прогноз", "Бюджет, ₽", "Лиды", "Продажи", "Выручка, ₽"], ["", r.forecast.spend, r.forecast.leads, r.forecast.sales, r.forecast.revenue]);
  const outside = outsideRows(r);
  if (outside.length) { rows.push([], ["Не вошло в расчёт", "Название", "Сколько"]); for (const o of outside) rows.push([o.kind, o.name, o.detail]); }
  return rows.map(row => row.map(cell).join("\t")).join("\n");
}

export async function copyForSheets(r: ReportData) {
  await navigator.clipboard.writeText(reportToTsv(r));
}
