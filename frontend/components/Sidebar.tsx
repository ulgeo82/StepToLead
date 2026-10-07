"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import Logo from "./Logo";
import { activeParents, adminNavigation, filterNavigation, isActiveRoute, navigationNumbers, type NavigationNode } from "./admin-navigation";
import styles from "./Sidebar.module.css";

const STORAGE_KEY = "steptolead.admin-navigation.v1";
const numbers = navigationNumbers(adminNavigation);
const defaults: Record<string, boolean> = { clients: true, growth: true, system: true, outreach: true, telegram: true, leadgen: false };
function Icon({ folder = false }: { folder?: boolean }) {
  return <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{folder ? <path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z" /> : <path d="m9 5 7 7-7 7" />}</svg>;
}
export default function Sidebar() {
  const pathname = usePathname();
  const [expanded, setExpanded] = useState(defaults);
  const [loaded, setLoaded] = useState(false);
  const [query, setQuery] = useState("");
  const [mobile, setMobile] = useState(false);
  const [open, setOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const burgerRef = useRef<HTMLButtonElement>(null);
  const filtered = useMemo(() => filterNavigation(adminNavigation, query), [query]);
  const searching = Boolean(query.trim());
  useEffect(() => {
    let saved: Record<string, boolean> = {};
    try {
      const value: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      if (value && typeof value === "object" && !Array.isArray(value)) saved = Object.fromEntries(Object.entries(value).filter(([key, val]) => key in defaults && typeof val === "boolean"));
    } catch { /* Storage is optional, including in private browsing. */ }
    setExpanded(previous => ({ ...previous, ...saved })); setLoaded(true);
  }, []);
  useEffect(() => {
    if (!loaded) return;
    setExpanded(previous => ({ ...previous, ...Object.fromEntries(activeParents(adminNavigation, pathname).map(id => [id, true])) }));
    setOpen(false);
  }, [pathname, loaded]);
  useEffect(() => {
    if (loaded) { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(expanded)); } catch { /* Storage is optional. */ } }
  }, [expanded, loaded]);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 860px)");
    const update = () => { setMobile(media.matches); if (!media.matches) setOpen(false); };
    update(); media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); if (mobile) setOpen(true); searchRef.current?.focus(); }
    };
    document.addEventListener("keydown", shortcut); return () => document.removeEventListener("keydown", shortcut);
  }, [mobile]);
  useEffect(() => {
    if (!mobile || !open) return;
    const overflow = document.body.style.overflow;
    const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : burgerRef.current;
    document.body.style.overflow = "hidden"; searchRef.current?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setOpen(false); return; }
      if (event.key !== "Tab") return;
      const elements = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input') || []).filter(el => el.getClientRects().length);
      const first = elements[0], last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", trap);
    return () => { document.body.style.overflow = overflow; document.removeEventListener("keydown", trap); returnFocus?.focus(); };
  }, [mobile, open]);
  function renderNode(node: NavigationNode): React.ReactNode {
    if (node.kind === "item") {
      const content = <><span className={styles.number}>{numbers[node.id]}</span><span className={styles.text}>{node.label}</span>{node.disabled && <span className={styles.soon}>скоро</span>}</>;
      if (node.disabled) return <li key={node.id}><span className={`${styles.item} ${styles.disabled}`} aria-disabled="true">{content}</span></li>;
      const active = isActiveRoute(pathname, node.href);
      return <li key={node.id}><Link href={node.href} aria-current={active ? "page" : undefined} className={`${styles.item} ${active ? styles.active : ""}`} onClick={() => setOpen(false)}>{content}</Link></li>;
    }
    const isExpanded = searching || expanded[node.id] || (!loaded && activeParents([node], pathname).length > 0);
    const group = node.kind === "group";
    return <li key={node.id} className={group ? styles.group : styles.folder}>
      <button type="button" className={group ? styles.groupHeading : styles.folderHeading} aria-expanded={Boolean(isExpanded)} aria-controls={`nav-${node.id}`} disabled={searching} onClick={() => setExpanded(previous => ({ ...previous, [node.id]: !isExpanded }))}>
        {!group && <span className={styles.folderIcon}><Icon folder /></span>}<span className={styles.text}>{node.label}</span>{node.badge && <span className={styles.newBadge}>{node.badge}</span>}<span className={`${styles.chevron} ${isExpanded ? styles.rotated : ""}`}><Icon /></span>
      </button>
      <ul id={`nav-${node.id}`} hidden={!isExpanded} className={group ? styles.groupItems : styles.children}>{node.children.map(renderNode)}</ul>
    </li>;
  }
  return <>
    <button ref={burgerRef} type="button" className={styles.burger} aria-label="Открыть меню" aria-expanded={open} aria-controls="admin-navigation-panel" onClick={() => setOpen(true)}><span aria-hidden="true">☰</span><span>Меню</span></button>
    {mobile && open && <button type="button" className={styles.backdrop} tabIndex={-1} aria-label="Закрыть меню" onClick={() => setOpen(false)} />}
    <aside ref={panelRef} id="admin-navigation-panel" className={`${styles.sidebar} ${open ? styles.open : ""}`} role={mobile && open ? "dialog" : undefined} aria-modal={mobile && open ? true : undefined} aria-label="Навигация администратора" inert={mobile && !open}>
      <div className={styles.logo}><Logo /><button type="button" className={styles.close} aria-label="Закрыть меню" onClick={() => setOpen(false)}>×</button></div>
      <div className={styles.workspace}><span className={styles.avatar}>ЕД</span><span><small>Администратор</small><strong>StepToLead Agency</strong></span><span aria-hidden="true">⌄</span></div>
      <div className={styles.search}><label className={styles.srOnly} htmlFor="admin-section-search">Поиск по разделам</label><input ref={searchRef} id="admin-section-search" type="search" placeholder="Найти раздел…" value={query} onChange={event => setQuery(event.target.value)} /><kbd>⌘K / Ctrl K</kbd></div>
      <nav className={styles.navigation} aria-label="Разделы админки"><ul className={styles.root}>{filtered.map(renderNode)}</ul>{!filtered.length && <p className={styles.empty} role="status">Разделы не найдены</p>}</nav>
      <div className={styles.footer}><button type="button" className={styles.logout} onClick={async () => { await fetch("/api/auth/logout", { method: "POST" }); window.location.assign("/login"); }}>Выйти</button><div className={styles.status}><span aria-hidden="true" /><div><strong>Система работает</strong><small>Локальная среда</small></div></div></div>
    </aside>
  </>;
}
