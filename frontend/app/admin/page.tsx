"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api, Campaign, Lead, MarketingSummary } from "@/lib/api";

export default function Dashboard() {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [marketing, setMarketing] = useState<MarketingSummary>({ spend: 0, impressions: 0, clicks: 0, applications: 0, cpc: null, conversion_rate: null, cost_per_application: null, connections: 0, workspaces: 0 });

  useEffect(() => {
    Promise.all([api<Campaign[]>("/campaigns"), api<Lead[]>("/leads"), api<MarketingSummary>("/marketing/summary")])
      .then(([campaignData, leadData, marketingData]) => { setCampaigns(campaignData); setLeads(leadData); setMarketing(marketingData); })
      .catch(() => undefined);
  }, []);

  const metrics = useMemo(() => {
    const replied = leads.filter((lead) => ["replied", "interested", "handoff"].includes(lead.status)).length;
    const interested = leads.filter((lead) => lead.status === "interested").length;
    const sent = leads.filter((lead) => !["new", "queued"].includes(lead.status)).length;
    return { replied, interested, sent, responseRate: sent ? Math.round((replied / sent) * 100) : 0 };
  }, [leads]);

  const pipeline = [
    { label: "В базе", value: leads.length, color: "violet" },
    { label: "Отправлено", value: metrics.sent, color: "blue" },
    { label: "Ответили", value: metrics.replied, color: "mint" },
    { label: "Интерес", value: metrics.interested, color: "lime" },
  ];
  const maxPipeline = Math.max(...pipeline.map((item) => item.value), 1);

  return (
    <div className="page dashboardPage">
      <header className="topbar">
        <div className="crumbs"><span>StepToLead</span><b>/</b><strong>Администрирование</strong></div>
        <div className="topbarActions"><span className="dateChip">Локальная среда</span><button className="iconButton" aria-label="Уведомления">●</button><span className="userAvatar">ЕД</span></div>
      </header>

      <section className="welcome">
        <div><p className="eyebrow">Центр управления агентством</p><h1>Добро пожаловать, Егор.</h1><p className="subtitle">Клиенты, реклама, лиды и Telegram Outreach — в одном защищённом кабинете.</p></div>
        <Link className="button primary" href="/admin/advertising">Подключить рекламу <span>→</span></Link>
      </section>

      <section className="metricsGrid">
        <article className="metricCard featured"><div className="metricTop"><span>Клиенты</span><i>Workspace</i></div><strong>{marketing.workspaces}</strong><p><b>↗</b> Изолированные кабинеты</p><div className="metricGlow" /></article>
        <article className="metricCard"><div className="metricTop"><span>Рекламные кабинеты</span><i>API</i></div><strong>{marketing.connections}</strong><p>Meta Ads и Яндекс Директ</p></article>
        <article className="metricCard"><div className="metricTop"><span>Лиды Outreach</span><i>Telegram</i></div><strong>{leads.length}</strong><p>{metrics.replied} ответили</p></article>
        <article className="metricCard"><div className="metricTop"><span>Кампании</span><i>Автоматизация</i></div><strong>{campaigns.length}</strong><p>{metrics.interested} заинтересованы</p></article>
      </section>

      <section className="dashboardGrid">
        <article className="panel pipelinePanel">
          <div className="panelHead"><div><p className="eyebrow">Воронка</p><h2>Движение лидов</h2></div><Link href="/admin/leads">Все лиды <span>↗</span></Link></div>
          <div className="pipelineList">
            {pipeline.map((item) => <div className="pipelineRow" key={item.label}><span>{item.label}</span><div className="barTrack"><i className={`bar ${item.color}`} style={{ width: `${Math.max((item.value / maxPipeline) * 100, item.value ? 8 : 0)}%` }} /></div><strong>{item.value}</strong></div>)}
          </div>
          <div className="pipelineFoot"><span className="sparkline"><i /><i /><i /><i /><i /><i /><i /><i /></span><span>Воронка обновляется автоматически после изменения статусов</span></div>
        </article>

        <article className="panel launchPanel">
          <div className="launchOrb"><span>01</span><i /></div>
          <div><p className="eyebrow">Первый шаг</p><h2>{marketing.connections ? "Обновите статистику" : "Подключите клиента"}</h2><p>{marketing.connections ? "Запустите синхронизацию и получите вложения, показы и клики за всю историю. Заявки считаются отдельно по CRM." : "Создайте клиентский workspace и добавьте официальный доступ к рекламному кабинету."}</p></div>
          <Link className="button soft" href="/admin/advertising">Открыть рекламу <span>→</span></Link>
        </article>
      </section>

      <section className="bottomGrid">
        <article className="panel recentPanel">
          <div className="panelHead"><div><p className="eyebrow">Последние</p><h2>Кампании</h2></div><Link href="/admin/campaigns">Открыть все</Link></div>
          <div className="recentList">
            {campaigns.slice(0, 3).map((campaign) => <Link href="/admin/campaigns" className="recentRow" key={campaign.id}><span className="campaignGlyph">{campaign.name.slice(0, 1).toUpperCase()}</span><span><strong>{campaign.name}</strong><small>{campaign.description || "Без описания"}</small></span><b>{campaign.leads_count} <small>лидов</small></b><i>→</i></Link>)}
            {!campaigns.length && <div className="miniEmpty"><span>Пока пусто</span><p>Первая кампания появится здесь.</p></div>}
          </div>
        </article>
        <article className="panel readinessPanel"><div className="readinessTop"><span className="readinessRing">100<small>%</small></span><div><p className="eyebrow">Режим запуска</p><h2>Ручная отправка</h2></div></div><div className="checkList"><span className="done"><i>✓</i> Кампании и лиды</span><span className="done"><i>✓</i> Импорт таблиц</span><span className="done"><i>✓</i> Очередь сообщений</span></div></article>
      </section>
    </div>
  );
}
