"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { count, money, MetricChart } from "@/components/reporting";
import "./website-analytics.css";
import TildaIntegration from "@/components/tilda-integration";
import { SiteWidgetEditor } from "@/components/site-widget";

type Site = { id: number; name: string; origin: string; public_key: string; active: boolean; last_event_at: string | null };
type Report = { totals: { sessions: number; visitors: number; page_views: number; engaged: number; bounce_rate: number | null;
  leads: number; qualified: number; sales: number; revenue: number | null; visit_to_lead: number | null;
  visit_to_qualified: number | null; visit_to_sale: number | null; revenue_per_visit: number | null;
  gross_profit_per_visit: number | null };
  events: Record<string, number>; pages: { path: string; views: number }[];
  funnel: { cta_click: number; form_start: number; form_success: number };
  elements: { event: string; name: string; count: number }[];
  actions: { name: string; views: number; clicks: number; leads: number; qualified: number; sales: number; revenue: number | null }[];
  previous: { sessions: number; leads: number; qualified: number; sales: number; revenue: number | null;
    visit_to_lead: number | null; visit_to_qualified: number | null; visit_to_sale: number | null; revenue_per_visit: number | null };
  forms: ({ name: string } & Record<string, number | string>)[]; depths: Record<string, number>;
  daily: ({ date: string; sessions: number; leads: number; qualified: number; sales: number; conversion: number | null })[];
  previous_daily: ({ date: string; sessions: number; leads: number; qualified: number; sales: number; conversion: number | null })[];
  landing_pages: { path: string; sessions: number; leads: number; qualified: number; sales: number; revenue: number | null }[];
  devices: { device: string; sessions: number; leads: number; qualified: number; sales: number; revenue: number | null }[];
  sources: { name: string; sessions: number; leads: number; qualified: number; sales: number; revenue: number | null }[];
  campaigns: { name: string; sessions: number; leads: number; qualified: number; sales: number; revenue: number | null }[] };
type TrendKey = "sessions" | "leads" | "qualified" | "sales" | "conversion";
const rate = (n: number | null | undefined) => n == null ? "—" : `${n.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
const eventName: Record<string, string> = { page_view: "Просмотр страницы", section_view: "Просмотр секции",
  scroll_depth: "Глубина прокрутки", cta_view: "Показ кнопки", cta_click: "Нажатие кнопки",
  phone_click: "Нажатие на телефон", messenger_click: "Переход в мессенджер",
  service_view: "Просмотр услуги", case_view: "Просмотр кейса", pricing_view: "Просмотр цены",
  service_click: "Нажатие на услугу", case_open: "Открытие кейса", pricing_open: "Открытие цены",
  form_view: "Показ формы", form_start: "Начало заполнения", form_error: "Ошибка формы",
  form_submit: "Отправка формы", form_success: "Успешная заявка" };

export default function WebsiteAnalytics({ projectId, start, end, role }: { projectId: number; start: string; end: string; role: string }) {
  const [sites, setSites] = useState<Site[]>([]);
  const [widgetSite, setWidgetSite] = useState<Site | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [siteId, setSiteId] = useState("");
  const [page, setPage] = useState("");
  const [device, setDevice] = useState("");
  const [visitor, setVisitor] = useState("");
  const [utmSource, setUtmSource] = useState("");
  const [utmCampaign, setUtmCampaign] = useState("");
  const [name, setName] = useState("");
  const [origin, setOrigin] = useState("");
  const [showConnect, setShowConnect] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [trendKey, setTrendKey] = useState<TrendKey>("sessions");
  const [selectedForm, setSelectedForm] = useState<string | null>(null);
  const [permissions, setPermissions] = useState<string[]>([]);
  const canManage = role === "admin" || role === "client_owner" || permissions.includes("manage_integrations");

  const refreshSites = () => api<Site[]>(`/website/sites?project_id=${projectId}`).then(setSites).catch(e => setError(e.message));
  useEffect(() => { if (role !== "admin") api<{ permissions: string[] }>("/portal/auth/me").then(value => setPermissions(value.permissions)).catch(() => setPermissions([])); }, [role]);
  useEffect(() => { setSiteId(""); setPage(""); setReport(null); refreshSites(); }, [projectId]);
  useEffect(() => {
    let live = true;
    const query = new URLSearchParams({ project_id: String(projectId), start, end });
    if (siteId) query.set("site_id", siteId);
    if (page) query.set("page", page);
    if (device) query.set("device", device);
    if (visitor) query.set("visitor", visitor);
    if (utmSource) query.set("utm_source", utmSource);
    if (utmCampaign) query.set("utm_campaign", utmCampaign);
    api<Report>(`/website/analytics?${query}`).then(value => { if (live) { setReport(value); setError(""); } })
      .catch(e => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [projectId, start, end, siteId, page, device, visitor, utmSource, utmCampaign]);

  async function connect() {
    setBusy(true); setError("");
    try {
      await api<Site>("/website/sites", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, name, origin }) });
      await refreshSites(); setShowConnect(false); setName(""); setOrigin("");
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось подключить сайт"); }
    finally { setBusy(false); }
  }
  async function toggle(site: Site) {
    try {
      await api(`/website/sites/${site.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: !site.active }) });
      await refreshSites();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось изменить сайт"); }
  }
  const t = report?.totals;
  return <div className="websiteAnalytics">
    <div className="websiteTitle"><div><h2>Аналитика сайта</h2><p>Поведение посетителей и связь с подтверждёнными лидами и продажами проекта.</p></div>
      {canManage && <button className="websitePrimary" onClick={() => setShowConnect(!showConnect)}>+ Подключить сайт</button>}</div>
    {error && <div className="resultError" role="alert">{error}</div>}
    {showConnect && <section className="resultPanel websiteConnect"><h3>Подключить сайт</h3><div className="websiteFilters">
      <input aria-label="Название сайта" placeholder="Название сайта" value={name} onChange={e => setName(e.target.value)}/>
      <input aria-label="Адрес сайта" placeholder="https://example.ru" value={origin} onChange={e => setOrigin(e.target.value)}/>
      <button className="websitePrimary" disabled={busy || !name.trim() || !origin.trim()} onClick={connect}>Сохранить</button></div></section>}
    {sites.length > 0 && <section className="resultPanel websiteSites"><h3>Подключённые сайты</h3><div className="websiteSiteGrid">{sites.map(site => <article key={site.id}>
      <strong>{site.name}</strong><span>{site.origin}</span><small>{site.active ? "Сбор включён" : "Сбор остановлен"} · Последнее событие: {site.last_event_at ? new Date(site.last_event_at).toLocaleString("ru-RU") : "—"}</small>
      <details><summary>Код установки</summary><p>Разместите перед закрывающим тегом страницы. Передавайте согласие после выбора посетителя.</p>
        <code>{`<script async src="${typeof window === "undefined" ? "https://YOUR_STEPTOLEAD_DOMAIN" : window.location.origin}/stl.js" data-stl-key="${site.public_key}"></script>`}</code>
        <code>{`StepToLead.consent(true)`}</code><p>При отправке заявки передайте <code>StepToLead.context()</code> в существующий входящий поток. Успех формы фиксируйте вызовом <code>{`StepToLead.track("form_success", { element_name: "Название формы" })`}</code> только после ответа сервера. Точно такое же название укажите в <code>data-stl-name</code> формы.</p></details>
      <div className="websiteSiteActions"><button onClick={() => setWidgetSite(site)}>☎ Виджет «Перезвоните мне»</button>{canManage && <button onClick={() => toggle(site)}>{site.active ? "Приостановить" : "Включить"}</button>}</div></article>)}</div></section>}
    {widgetSite && <SiteWidgetEditor siteId={widgetSite.id} siteName={widgetSite.name} canManage={canManage} onClose={() => setWidgetSite(null)}/>}
    {!sites.length && <section className="resultPanel"><p className="resultTableEmpty">Сайт не подключён. Добавьте домен и установите код на сайт после настройки согласия на аналитику.</p></section>}
    <TildaIntegration projectId={projectId} sites={sites} canManage={canManage} />
    <div className="websiteFilters"><select aria-label="Сайт" value={siteId} onChange={e => setSiteId(e.target.value)}><option value="">Все сайты</option>{sites.map(site => <option key={site.id} value={site.id}>{site.name}</option>)}</select>
      <select aria-label="Устройство" value={device} onChange={e => setDevice(e.target.value)}><option value="">Все устройства</option><option value="desktop">Компьютер</option><option value="mobile">Телефон</option><option value="tablet">Планшет</option></select>
      <select aria-label="Посетители" value={visitor} onChange={e => setVisitor(e.target.value)}><option value="">Все посетители</option><option value="new">Новые</option><option value="returning">Повторные</option></select>
      <input aria-label="Источник" placeholder="Источник по метке" value={utmSource} onChange={e => setUtmSource(e.target.value)}/>
      <input aria-label="Кампания" placeholder="Кампания по метке" value={utmCampaign} onChange={e => setUtmCampaign(e.target.value)}/>
      <input aria-label="Страница" placeholder="Путь страницы" value={page} onChange={e => setPage(e.target.value)}/></div>
    {sites.length > 0 && t && t.sessions === 0 && <section className="resultPanel"><p className="resultTableEmpty">За выбранный период посещений нет. Заявки Tilda поступают отдельно и сами по себе не создают визит. Установите счётчик из раздела «Код установки», проверьте домен сайта и согласие на аналитику. Устройство, браузер и переходы появятся после первого записанного посещения.</p></section>}
    {t && t.sessions > 0 && <><section className="websiteKpis">
      {[["Посещения", count(t.sessions)], ["Посетители", count(t.visitors)], ["Просмотры страниц", count(t.page_views)], ["Вовлечённые посещения", count(t.engaged)], ["Отказы", rate(t.bounce_rate)],
        ["Лиды", count(t.leads)], ["Квалифицированные", count(t.qualified)], ["Продажи", count(t.sales)], ["Выручка", money(t.revenue)],
        ["Конверсия в заявку", rate(t.visit_to_lead)], ["Конверсия в квал. заявку", rate(t.visit_to_qualified)],
        ["Конверсия в продажу", rate(t.visit_to_sale)], ["Выручка на посещение", money(t.revenue_per_visit)],
        ["Валовая прибыль на посещение", money(t.gross_profit_per_visit)]].map(([label, value]) => <article className="resultPanel" key={label}><small>{label}</small><strong>{value}</strong></article>)}
    </section><section className="resultPanel"><h3>Сравнение с прошлым периодом</h3><div className="websiteComparison">
      {[["Посещения", t.sessions, report.previous.sessions, false], ["Конверсия в заявку", t.visit_to_lead, report.previous.visit_to_lead, true], ["Продажи", t.sales, report.previous.sales, false], ["Выручка на посещение", t.revenue_per_visit, report.previous.revenue_per_visit, false]].map(([label, current, previous, points]) => {
        const a = current as number | null, b = previous as number | null;
        const difference = a != null && b != null && (points || b !== 0) ? (points ? a - b : (a - b) / Math.abs(b) * 100) : null;
        return <div key={String(label)}><span>{label}</span><strong>{difference == null ? "—" : `${difference > 0 ? "+" : ""}${difference.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} ${points ? "п.п." : "%"}`}</strong></div>;
      })}</div></section>
      <section className="resultPanel"><div className="websiteTrendHead"><h3>Динамика</h3><div className="websiteTrendTabs">{([['sessions','Посещения'],['leads','Заявки'],['qualified','Квал. заявки'],['sales','Продажи'],['conversion','Конверсия']] as const).map(([key,label]) => <button key={key} className={trendKey === key ? "active" : ""} onClick={() => setTrendKey(key)}>{label}</button>)}</div></div>
        <MetricChart current={report.daily.map(point => ({ ...point, label: point.date }))} previous={report.previous_daily.map(point => ({ ...point, label: point.date }))} metric={trendKey} label="Показатели сайта"/><p className="websiteNote">Синий — выбранный период, серый — предыдущий период.</p></section>
      <div className="websiteGrid"><section className="resultPanel"><h3>Воронка сайта</h3><div className="websiteRows">{[["Посещения", t.sessions], ["Нажали целевую кнопку", report.funnel.cta_click], ["Начали заполнять", report.funnel.form_start], ["Подтверждённые лиды", t.leads], ["Квалифицированы", t.qualified], ["Продажи", t.sales]].map(([label, value], index, rows) => { const prior = index ? Number(rows[index - 1][1]) : null; const current = Number(value); return <div key={String(label)}><span>{label}{prior && current <= prior ? <small> · {rate(current / prior * 100)} от предыдущего этапа</small> : null}</span><strong>{value == null ? "—" : count(current)}</strong></div>; })}</div><p className="websiteNote">Действия могут происходить разными путями: переходы между этапами не приписываются автоматически одному посетителю.</p></section>
      <section className="resultPanel"><h3>Действия посетителей</h3><div className="websiteRows">{Object.entries(report.events).map(([key, value]) => <div key={key}><span>{eventName[key] || key}</span><strong>{count(value)}</strong></div>)}{!Object.keys(report.events).length && <p className="resultTableEmpty">Событий за период нет.</p>}</div></section>
      <section className="resultPanel"><h3>Страницы</h3><div className="websiteRows">{report.pages.map(row => <div key={row.path}><span>{row.path}</span><strong>{count(row.views)}</strong></div>)}{!report.pages.length && <p className="resultTableEmpty">Просмотров страниц нет.</p>}</div></section>
      <section className="resultPanel"><h3>Кнопки и действия</h3><div className="websiteTable"><table><thead><tr><th>Действие</th><th>Показы</th><th>Нажатия</th><th>Конверсия</th><th>Связанные лиды</th><th>Квал.</th><th>Продажи</th><th>Выручка</th></tr></thead><tbody>{report.actions.map(row => <tr key={row.name}><td>{row.name}</td><td>{row.views ? count(row.views) : "—"}</td><td>{count(row.clicks)}</td><td>{row.views ? rate(row.clicks / row.views * 100) : "—"}</td><td>{count(row.leads)}</td><td>{count(row.qualified)}</td><td>{count(row.sales)}</td><td>{money(row.revenue)}</td></tr>)}</tbody></table>{!report.actions.length && <p className="resultTableEmpty">Отмеченных действий нет.</p>}</div><p className="websiteNote">Лид и продажа связаны с посещением, в котором было действие. Это не доказательство, что именно кнопка вызвала продажу.</p></section></div>
      <div className="websiteGrid"><section className="resultPanel"><h3>Страницы входа</h3><div className="websiteTable"><table><thead><tr><th>Страница</th><th>Посещения</th><th>Лиды</th><th>Конверсия</th><th>Квал.</th><th>Продажи</th><th>Выручка</th></tr></thead><tbody>{report.landing_pages.map(row => <tr key={row.path}><td>{row.path}</td><td>{count(row.sessions)}</td><td>{count(row.leads)}</td><td>{row.sessions ? rate(row.leads / row.sessions * 100) : "—"}</td><td>{count(row.qualified)}</td><td>{count(row.sales)}</td><td>{money(row.revenue)}</td></tr>)}</tbody></table>{!report.landing_pages.length && <p className="resultTableEmpty">Данных о страницах входа нет.</p>}</div></section>
      <section className="resultPanel"><h3>Устройства</h3><div className="websiteTable"><table><thead><tr><th>Устройство</th><th>Посещения</th><th>Лиды</th><th>Конверсия</th><th>Квал.</th><th>Продажи</th><th>Выручка</th></tr></thead><tbody>{report.devices.map(row => <tr key={row.device}><td>{{desktop:"Компьютер",mobile:"Телефон",tablet:"Планшет"}[row.device] || "Не определено"}</td><td>{count(row.sessions)}</td><td>{count(row.leads)}</td><td>{row.sessions ? rate(row.leads / row.sessions * 100) : "—"}</td><td>{count(row.qualified)}</td><td>{count(row.sales)}</td><td>{money(row.revenue)}</td></tr>)}</tbody></table>{!report.devices.length && <p className="resultTableEmpty">Данных об устройствах нет.</p>}</div></section>
      <section className="resultPanel"><h3>Источники посещений по меткам</h3><div className="websiteTable"><table><thead><tr><th>Источник</th><th>Посещения</th><th>Лиды</th><th>Продажи</th><th>Выручка</th></tr></thead><tbody>{report.sources.map(row => <tr key={row.name}><td>{row.name}</td><td>{count(row.sessions)}</td><td>{count(row.leads)}</td><td>{count(row.sales)}</td><td>{money(row.revenue)}</td></tr>)}</tbody></table></div></section>
      <section className="resultPanel"><h3>Кампании посещений по меткам</h3><div className="websiteTable"><table><thead><tr><th>Кампания</th><th>Посещения</th><th>Лиды</th><th>Продажи</th><th>Выручка</th></tr></thead><tbody>{report.campaigns.map(row => <tr key={row.name}><td>{row.name}</td><td>{count(row.sessions)}</td><td>{count(row.leads)}</td><td>{count(row.sales)}</td><td>{money(row.revenue)}</td></tr>)}</tbody></table></div></section>
      <section className="resultPanel"><h3>Формы</h3><div className="websiteTable"><table><thead><tr><th>Форма</th><th>Увидели</th><th>Начали</th><th>Отправили</th><th>Конверсия</th><th>Ошибки</th></tr></thead><tbody>{report.forms.map(row => <tr key={row.name} className="websiteClickableRow" onClick={() => setSelectedForm(row.name)} onKeyDown={event => { if (event.key === "Enter") setSelectedForm(row.name); }} tabIndex={0}><td>{row.name}</td><td>{row.form_view == null ? "—" : count(Number(row.form_view))}</td><td>{row.form_start == null ? "—" : count(Number(row.form_start))}</td><td>{row.form_success == null ? "—" : count(Number(row.form_success))}</td><td>{Number(row.form_view) && row.form_success != null ? rate(Number(row.form_success) / Number(row.form_view) * 100) : "—"}</td><td>{row.form_error == null ? "—" : count(Number(row.form_error))}</td></tr>)}</tbody></table>{!report.forms.length && <p className="resultTableEmpty">Отмеченных форм нет.</p>}</div>{selectedForm && report.forms.find(row => row.name === selectedForm) && <div className="websiteFormDetail"><strong>{selectedForm}</strong>{[["Увидели форму", "form_view"], ["Начали заполнять", "form_start"], ["Успешно отправили", "form_success"]].map(([label, key]) => <div key={key}><span>{label}</span><b>{report.forms.find(row => row.name === selectedForm)?.[key] == null ? "—" : count(Number(report.forms.find(row => row.name === selectedForm)?.[key]))}</b></div>)}<button onClick={() => setSelectedForm(null)}>Закрыть</button></div>}</section>
      <section className="resultPanel"><h3>Глубина просмотра</h3><div className="websiteRows">{[25,50,75,90,100].map(mark => <div key={mark}><span>{mark}% страницы</span><strong>{report.depths[String(mark)] == null || !t.sessions ? "—" : rate(report.depths[String(mark)] / t.sessions * 100)}</strong></div>)}</div></section>
      <section className="resultPanel"><h3>Посещения по дням</h3><div className="websiteRows">{report.daily.map(row => <div key={row.date}><span>{new Date(`${row.date}T00:00:00`).toLocaleDateString("ru-RU")}</span><strong>{count(row.sessions)}</strong></div>)}{!report.daily.length && <p className="resultTableEmpty">Посещений за период нет.</p>}</div></section></div>
      <p className="websiteNote">Лиды и продажи — только подтверждённые сущности общей CRM, связанные с посещениями выбранного периода. Отправка формы сама по себе не считается лидом. Значения за другие периоды и неподтверждённая атрибуция не подставляются.</p></>}
  </div>;
}
