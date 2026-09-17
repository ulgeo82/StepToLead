"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const mainNavigation = [
  { label: "Обзор", href: "/admin", icon: "01" },
  { label: "Реклама", href: "/admin/advertising", icon: "02" },
  { label: "Клиенты и доступы", href: "/admin/clients", icon: "03" },
  { label: "Кампании", href: "/admin/campaigns", icon: "04" },
  { label: "Очередь", href: "/admin/leads", icon: "05" },
  { label: "Telegram", href: "/admin/accounts", icon: "06" },
  { label: "Сбор контактов", href: "/admin/parser", icon: "07" },
  { label: "Прокси", href: "/admin/proxies", icon: "08" },
  { label: "Заявки роста", href: "/admin/growth-leads", icon: "09" },
];

const futureNavigation = [
  { label: "Входящие", icon: "06" },
  { label: "AI-настройки", icon: "07" },
];

export default function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="sidebar">
      <Link href="/admin" className="brand" aria-label="StepToLead — управление">
        <span className="brandMark"><i /><i /><i /></span><span className="brandText">StepToLead</span>
      </Link>
      <div className="workspaceCard">
        <span className="workspaceAvatar">ЕД</span><span><small>Администратор</small><strong>StepToLead Agency</strong></span><b>⌄</b>
      </div>
      <nav className="navGroup" aria-label="Основная навигация">
        <p className="navLabel">Работа</p>
        {mainNavigation.map((item) => {
          const active = item.href === "/admin" ? pathname === "/admin" : pathname.startsWith(item.href);
          return <Link key={item.href} href={item.href} className={`navItem ${active ? "active" : ""}`}><span className="navIcon">{item.icon}</span><span>{item.label}</span>{active && <i className="activeMark" />}</Link>;
        })}
      </nav>
      <nav className="navGroup future" aria-label="Будущие разделы">
        <p className="navLabel">Автоматизация</p>
        {futureNavigation.map((item) => <span key={item.label} className="navItem disabled"><span className="navIcon">{item.icon}</span><span>{item.label}</span><small>soon</small></span>)}
      </nav>
      <button className="button soft" onClick={async () => { await fetch("/api/auth/logout", { method: "POST" }); window.location.assign("/login"); }}>Выйти</button>
      <div className="sidebarFoot"><span className="statusDot" /><span><strong>Система работает</strong><small>Локальная среда</small></span></div>
    </aside>
  );
}
