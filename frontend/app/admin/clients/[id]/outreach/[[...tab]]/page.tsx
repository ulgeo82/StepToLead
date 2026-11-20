"use client";

/* Аутрич внутри компании: всё для запуска клиента — аккаунты, прокси, прогрев, кампании, сбор, входящие — на
   одной странице с вкладками. Клиент задан адресом (/admin/clients/12/outreach/…), поэтому экраны вкладок
   сразу работают в его пространстве (lib/space.ts → companyFromPath). */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import AccountsPage from "@/app/admin/accounts/page";
import ProxiesPage from "@/app/admin/proxies/page";
import TgWarmupPage from "@/app/admin/tg-warmup/page";
import ParserPage from "@/app/admin/parser/page";
import MailWarmupPage from "@/app/admin/outreach/warmup/page";
import css from "./company.module.css";
import Sending from "./sending";

type Tab = { id: string; label: string; href?: string };
const TABS: Tab[] = [
  { id: "", label: "Запуск" },
  { id: "accounts", label: "Аккаунты" },
  { id: "proxies", label: "Прокси" },
  { id: "warmup", label: "Прогрев" },
  { id: "sending", label: "Рассылка и гипотезы" },
  { id: "parser", label: "Сбор контактов" },
  { id: "inbox", label: "Входящие", href: "/admin/outreach/inbox" },
  { id: "email", label: "Почта", href: "/admin/outreach/campaigns" },
  { id: "mail-warmup", label: "Прогрев почты" },
];
const SCREENS: Record<string, () => React.ReactNode> = {
  accounts: () => <AccountsPage/>, proxies: () => <ProxiesPage/>, warmup: () => <TgWarmupPage/>,
  parser: () => <ParserPage/>, "mail-warmup": () => <MailWarmupPage/>,
};

type Workspace = { id: number; name: string; status: string };

export default function CompanyOutreach() {
  const params = useParams<{ id: string; tab?: string[] }>();
  const id = Number(params.id);
  const tab = params.tab?.[0] ?? "";
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  useEffect(() => {
    api<Workspace[]>("/marketing/workspaces").then(list => setWorkspace(list.find(w => w.id === id) ?? null)).catch(() => {});
  }, [id]);
  const base = `/admin/clients/${id}/outreach`;
  const screen = SCREENS[tab];

  return <div className={css.company}>
    <header className={css.head}>
      <Link href={`/admin/clients/${id}`} className={css.back}>← Карточка компании</Link>
      <h1>{workspace?.name ?? "Компания"}</h1>
      <p>Аутрич компании: всё, что нужно для запуска, — здесь. Аккаунты, прокси, лимиты и гипотезы этой компании
        не смешиваются с другими.</p>
      <nav className={css.tabs} aria-label="Разделы аутрича компании">
        {TABS.map(t => t.href
          ? <a key={t.id} href={`${t.href}?ws=${id}`} className={css.tab}>{t.label} ↗</a>
          : <Link key={t.id} href={t.id ? `${base}/${t.id}` : base} className={css.tab}
              aria-current={tab === t.id ? "page" : undefined}>{t.label}</Link>)}
      </nav>
    </header>
    <div className={css.body}>{tab === "sending" ? <Sending base={base}/> : screen ? screen() : <Launch id={id} base={base}/>}</div>
  </div>;
}

type Summary = { accounts: Record<string, number>; proxies: Record<string, number> };
type WarmRow = { enabled: boolean; stage: string | null; day: number | null };
type Campaign = { id: number; name: string; leads_count: number };

function Launch({ id, base }: { id: number; base: string }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [warm, setWarm] = useState<WarmRow[]>([]);
  const [limit, setLimit] = useState<number | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  useEffect(() => {
    api<Summary>("/telegram-accounts/health-summary").then(setSummary).catch(() => {});
    api<WarmRow[]>("/telegram-warmup/accounts").then(setWarm).catch(() => {});
    api<{ first_per_day: number }>("/telegram-accounts/limits").then(r => setLimit(r.first_per_day)).catch(() => {});
    api<Campaign[]>("/campaigns").then(setCampaigns).catch(() => {});
  }, [id]);
  const a = summary?.accounts, p = summary?.proxies;
  const warming = warm.filter(w => w.enabled);
  const maxDay = Math.max(0, ...warming.map(w => w.day ?? 0));
  const leads = campaigns.reduce((n, c) => n + c.leads_count, 0);
  const steps: { done: boolean; title: string; text: string; href: string; action: string }[] = [
    { done: !!a?.total, title: "Аккаунты подключены", text: a ? `${a.total} аккаунтов, в порядке ${a.ok}, спам-блок ${a.spamblock}` : "—",
      href: `${base}/accounts`, action: "Добавить аккаунты" },
    { done: !!a && a.total > 0 && a.waiting_proxy === 0, title: "У каждого аккаунта свой прокси",
      text: a && p ? `ждут прокси ${a.waiting_proxy}, свободных прокси ${p.free}` : "—", href: `${base}/proxies`, action: "Прокси" },
    { done: warming.length > 0 && warming.length >= (a?.total ?? 1), title: "Прогрев запущен",
      text: warming.length ? `в прогреве ${warming.length}, старшему аккаунту ${maxDay} дн.` : "прогрев не запущен", href: `${base}/warmup`, action: "Прогрев" },
    { done: limit != null, title: "Лимит первых касаний", text: limit != null ? `${limit} на аккаунт в день · с 4–5-го дня прогрева начинайте с 2` : "—",
      href: `${base}/accounts`, action: "Изменить" },
    { done: leads > 0, title: "Гипотеза с базой получателей", text: campaigns.length ? `${campaigns.length} гипотез, ${leads} получателей` : "гипотез нет",
      href: `${base}/sending`, action: "Гипотезы" },
  ];
  const ready = steps.every(s => s.done) && maxDay >= 4;
  return <section className={css.launch}>
    <div className={css.verdict} data-ready={ready}>
      <strong>{ready ? "Можно запускать рассылку" : "Пока не запускаем"}</strong>
      <span>{ready ? "Начинайте с 2 первых сообщений в день на аккаунт и поднимайте по рекомендации прогрева."
        : maxDay < 4 && warming.length ? `Аккаунтам нужно ещё ${4 - maxDay} дн. прогрева.` : "Пройдите шаги ниже."}</span>
    </div>
    <ol className={css.steps}>{steps.map(s => <li key={s.title} data-done={s.done}>
      <span className={css.mark} aria-hidden="true">{s.done ? "✓" : ""}</span>
      <div><strong>{s.title}</strong><small>{s.text}</small></div>
      <Link className="button secondary" href={s.href}>{s.action}</Link>
    </li>)}</ol>
  </section>;
}
