"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { money, ProjectSidebar } from "@/components/reporting";
import "../../../../result/result.css";
import "../../../ads.css";
import "./vk-campaign.css";

type Project = { id: number; name: string; organization_name: string };
type VkBanner = { id: number; name?: string | null; status?: string | null; delivery?: string | null;
  moderation_status?: string | null; issues?: unknown[] | null; textblocks?: Record<string, unknown> | null;
  urls?: Record<string, unknown> | null };
type VkGroup = { id: number; name?: string | null; status?: string | null; delivery?: string | null;
  issues?: unknown[] | null; objective?: string | null; package_id?: number | null;
  budget_limit?: string | number | null; budget_limit_day?: string | number | null;
  date_start?: string | null; date_end?: string | null; price?: string | number | null;
  max_price?: string | number | null; age_restrictions?: string | null;
  enable_utm?: boolean | null; utm?: string | null; targetings?: Record<string, unknown> | null;
  banners: VkBanner[] };
type VkCampaign = { id: number; name: string; status?: string | null; vkads_status?: unknown;
  objective?: string | null; autobidding_mode?: string | null; budget_limit?: string | number | null;
  budget_limit_day?: string | number | null; date_start?: string | null; date_end?: string | null;
  created?: string | null; updated?: string | null; max_price?: string | number | null;
  priced_goal?: unknown };
type Detail = { project_id: number; connection_id: number; connection_name: string;
  campaign: VkCampaign; groups: VkGroup[] };

const statusLabels: Record<string, string> = { active: "Активна", blocked: "Остановлена", deleted: "Удалена",
  pending: "Ожидает", delivering: "Показывается", not_delivering: "Не показывается",
  allowed: "Одобрено", banned: "Отклонено", max_goals: "Максимум целевых действий" };
const targetingLabels: Record<string, string> = { geo: "География", regions: "Регионы", local_geo: "Локальная география",
  age: "Возраст", sex: "Пол", interests: "Интересы", interests_soc_dem: "Социально-демографические интересы",
  segments: "Сегменты", pads: "Площадки", fulltime: "Расписание показов", browser: "Браузеры",
  mobile_types: "Устройства", mobile_operators: "Операторы", mobile_operation_systems: "ОС",
  search_phrases: "Поисковые фразы", group_members: "Участники сообществ" };
const issueLabels: Record<string, string> = { NO_MONEY: "Недостаточно средств на балансе",
  OUT_OF_DAY_LIMIT: "Достигнут дневной лимит", OUT_OF_FULL_LIMIT: "Достигнут общий лимит",
  BANNER_ON_MODERATION: "Объявление на модерации", NO_ALLOWED_BANNERS: "Нет одобренных объявлений",
  AD_PLAN_STOPPED: "Кампания остановлена", STOPPED: "Показы остановлены" };

function present(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (typeof value === "number" || typeof value === "string") return statusLabels[String(value)] || String(value);
  if (Array.isArray(value)) return value.length ? value.map(present).join(", ") : "—";
  return Object.entries(value as Record<string, unknown>).map(([key, nested]) =>
    `${targetingLabels[key] || key}: ${present(nested)}`).join(" · ") || "—";
}

function budget(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  return amount === -1 ? "Без лимита" : money(amount);
}

function Field({ label, value }: { label: string; value: unknown }) {
  return <div className="vkDetailField"><span>{label}</span><strong>{present(value)}</strong></div>;
}

function Issues({ values }: { values?: unknown[] | null }) {
  if (!values?.length) return null;
  return <div className="vkIssues"><strong>Причины непоказа</strong><div>{values.map((value, index) => {
    const code = typeof value === "string" ? value : typeof value === "object" && value ?
      String((value as Record<string, unknown>).code || (value as Record<string, unknown>).type || "") : "";
    return <span key={index}>{issueLabels[code] || present(value)}</span>;
  })}</div></div>;
}

export default function VkCampaignPage() {
  const { connectionId, campaignId } = useParams<{ connectionId: string; campaignId: string }>();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true); setError("");
    Promise.all([
      api<Detail>(`/ads/connections/${connectionId}/vk-campaigns/${campaignId}`),
      api<Project[]>("/result/projects"),
    ]).then(([campaign, rows]) => { if (active) { setDetail(campaign); setProjects(rows); } })
      .catch(cause => { if (active) setError((cause as Error).message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [connectionId, campaignId, revision]);

  const project = projects.find(row => row.id === detail?.project_id);
  const campaign = detail?.campaign;
  const back = detail ? `/ads?project_id=${detail.project_id}` : "/ads";
  return <div className="resultShell"><ProjectSidebar project={project} projectId={detail?.project_id || null} active="ads"/>
    <main className="resultMain vkDetailMain"><div className="resultBreadcrumb"><Link href={back}>Реклама</Link><span>/</span> VK Реклама <span>/</span> Кампания</div>
      <header className="vkDetailHeader"><div><span className="vkEyebrow">VK ADS · НАСТРОЙКИ КАМПАНИИ</span>
        <h1>{campaign?.name || (loading ? "Загружаем кампанию…" : "Кампания VK")}</h1>
        <p>{detail?.connection_name || "Рекламный кабинет"} · ID {campaignId}</p></div>
        <div className="vkDetailHeaderActions"><Link href={back}>← К кабинету</Link><button onClick={() => setRevision(value => value + 1)} disabled={loading}>↻ Обновить из VK</button></div></header>
      {error && <div className="resultError" role="alert">{error}<div><Link href={back}>Вернуться к рекламному кабинету</Link></div></div>}
      {loading && <div className="resultLoading">Загружаем реальные настройки из VK Ads…</div>}
      {campaign && !loading && <>
        <div className="vkDetailIntro"><span className={`vkState ${campaign.status || ""}`}>{present(campaign.status)}</span>
          <span>Только просмотр · данные из VK API</span><span>Последнее изменение: {present(campaign.updated)}</span></div>
        <section className="vkHeroGrid"><article className="resultPanel vkHeroCard"><span>Дневной бюджет</span><strong>{budget(campaign.budget_limit_day)}</strong><small>Лимит, заданный в VK Рекламе</small></article>
          <article className="resultPanel vkHeroCard"><span>Общий бюджет</span><strong>{budget(campaign.budget_limit)}</strong><small>За весь период кампании</small></article>
          <article className="resultPanel vkHeroCard"><span>Цель</span><strong>{present(campaign.objective)}</strong><small>Код цели, полученный из VK</small></article>
          <article className="resultPanel vkHeroCard"><span>Группы / объявления</span><strong>{detail?.groups.length ?? 0} / {detail?.groups.reduce((sum, group) => sum + group.banners.length, 0) ?? 0}</strong><small>Структура кампании</small></article></section>
        <section className="resultPanel vkDetailSection"><div className="vkSectionHead"><div><span className="vkSectionIndex">01</span><h2>Параметры кампании</h2></div><p>Синхронизировано напрямую из рекламного кабинета</p></div>
          <div className="vkFieldGrid"><Field label="Статус" value={campaign.status}/><Field label="Статус трансляции" value={campaign.vkads_status}/>
            <Field label="Стратегия" value={campaign.autobidding_mode}/><Field label="Максимальная цена" value={campaign.max_price}/>
            <Field label="Дата начала" value={campaign.date_start}/><Field label="Дата окончания" value={campaign.date_end}/>
            <Field label="Создана" value={campaign.created}/><Field label="Обновлена" value={campaign.updated}/>
            <Field label="Оплачиваемая цель" value={campaign.priced_goal}/></div></section>
        <section className="vkDetailSection vkGroupsSection"><div className="vkSectionHead"><div><span className="vkSectionIndex">02</span><h2>Группы объявлений</h2></div><p>Аудитории, расписание, лимиты и креативы</p></div>
          {!detail?.groups.length && <div className="resultPanel vkEmpty">VK не вернул групп для этой кампании.</div>}
          {detail?.groups.map((group, index) => <article className="resultPanel vkGroupCard" key={group.id}><div className="vkGroupHead"><div><span>ГРУППА {String(index + 1).padStart(2, "0")} · ID {group.id}</span><h3>{group.name || `Группа ${group.id}`}</h3></div><span className={`vkState ${group.status || ""}`}>{present(group.status)}</span></div>
            <div className="vkFieldGrid"><Field label="Статус показов" value={group.delivery}/><Field label="Цель группы" value={group.objective}/>
              <Field label="Дневной бюджет" value={budget(group.budget_limit_day)}/>
              <Field label="Общий бюджет" value={budget(group.budget_limit)}/>
              <Field label="Ставка" value={budget(group.price)}/>
              <Field label="Начало / окончание" value={`${present(group.date_start)} — ${present(group.date_end)}`}/>
              <Field label="Возрастное ограничение" value={group.age_restrictions}/>
              <Field label="Автоматические UTM" value={group.enable_utm}/><Field label="UTM-метки" value={group.utm}/></div>
            <Issues values={group.issues}/>
            <div className="vkSubsection"><h4>Аудитория и таргетинг</h4>{group.targetings && Object.keys(group.targetings).length ?
              <div className="vkTargetGrid">{Object.entries(group.targetings).map(([key, value]) => <Field key={key} label={targetingLabels[key] || key} value={value}/>)}</div> : <p>Настройки таргетинга не возвращены VK API.</p>}
              <small>Если VK возвращает числовые ID аудиторий или регионов, показываем их без выдуманных названий.</small></div>
            <div className="vkSubsection"><h4>Объявления <span>{group.banners.length}</span></h4>{group.banners.length ? <div className="vkBannerGrid">{group.banners.map(banner => <article className="vkBannerCard" key={banner.id}><div><strong>{banner.name || `Объявление #${banner.id}`}</strong><span className={`vkState ${banner.status || ""}`}>{present(banner.status)}</span></div><small>ID {banner.id} · {present(banner.delivery)} · модерация: {present(banner.moderation_status)}</small>
                  <Field label="Текст объявления" value={banner.textblocks}/><Field label="Ссылки" value={banner.urls}/><Issues values={banner.issues}/></article>)}</div> : <p>Объявлений в группе нет или VK не вернул их.</p>}</div>
          </article>)}</section>
      </>}
    </main></div>;
}
