"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export type ReportingProject = { id: number; name: string; organization_name: string };
export type ReportingPoint = { label: string };

export const money = (n: number | null | undefined) => n == null ? "—" : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(n)} ₽`;
export const count = (n: number | null | undefined) => n == null ? "—" : new Intl.NumberFormat("ru-RU").format(n);
export const percent = (n: number | null | undefined) => n == null ? "—" : `${n > 0 ? "+" : ""}${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(n)}%`;
export const dateInput = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const shortDate = (s: string) => new Date(`${s}T12:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });

export function ProjectSidebar({ project, projectId, active, role }: {
  project?: ReportingProject; projectId: number | null; active: "result" | "analytics" | "leads" | "crm" | "sales" | "ads" | "team" | "settings"; role?: string;
}) {
  const [permissions, setPermissions] = useState<string[] | null>(null);
  useEffect(() => { api<{ permissions: string[] }>("/portal/auth/me").then(user => setPermissions(user.permissions)).catch(() => setPermissions([])); }, []);
  const allowed = (capability: string) => role === "admin" || permissions?.includes(capability);
  const suffix = projectId ? `?project_id=${projectId}` : "";
  return <aside className="resultSidebar"><Link className="resultBrand" href={`/result${suffix}`}><span>↗</span> StepToLead</Link>
    <div className="resultProject"><small>ПРОЕКТ</small><strong>{project?.organization_name || "StepToLead"}</strong><span>{project?.name || "Выберите проект"}</span></div>
    <nav aria-label="Навигация"><small>АНАЛИТИКА</small>
      {allowed("view_result") && <Link className={active === "result" ? "active" : ""} href={`/result${suffix}`}><b>01</b> Результат</Link>}
      {allowed("view_analytics") && <Link className={active === "analytics" ? "active" : ""} href={`/analytics${suffix}`}><b>02</b> Аналитика</Link>}
      <small>ПРОДАЖИ</small>{allowed("view_crm") && <Link className={active === "crm" || active === "leads" ? "active" : ""} href={`/crm${suffix}`}><b>03</b> CRM</Link>}
      <small>МАРКЕТИНГ</small>
      {allowed("view_ads") && <Link className={active === "ads" ? "active" : ""} href={`/ads${suffix}`}><b>04</b> Реклама</Link>}
      <small>УПРАВЛЕНИЕ</small>{permissions?.includes("manage_team") && <Link className={active === "team" ? "active" : ""} href="/team" title="Команда клиентской организации"><b>05</b> Команда</Link>}{(permissions?.includes("manage_settings") || permissions?.includes("manage_sources")) && <Link className={active === "settings" ? "active" : ""} href={`/settings${suffix}`} title="Настройки клиентской организации"><b>06</b> Настройки</Link>}
    </nav><div className="resultSidebarFoot"><span>●</span> Данные вашего проекта · {role === "admin" ? <Link href="/admin">В админку</Link>
      : <button type="button" onClick={async () => { await api("/portal/auth/logout", { method: "POST" }).catch(() => undefined); location.assign("/portal/login"); }}>Выйти</button>}</div>
  </aside>;
}

export function PeriodControls({ projects, projectId, setProjectId, start, setStart, end, setEnd }: {
  projects: ReportingProject[]; projectId: number | null; setProjectId: (value: number) => void;
  start: string; setStart: (value: string) => void; end: string; setEnd: (value: string) => void;
}) {
  return <div className="resultControls">
    <label className="resultSelect"><span>Проект</span><select value={projectId || ""} onChange={event => setProjectId(Number(event.target.value))}>{projects.map(project => <option key={project.id} value={project.id}>{project.organization_name} · {project.name}</option>)}</select></label>
    <label className="resultDate"><span>С</span><input type="date" value={start} max={end} onChange={event => setStart(event.target.value)} /></label>
    <label className="resultDate"><span>По</span><input type="date" value={end} min={start} onChange={event => setEnd(event.target.value)} /></label>
    <div className="resultCompare">Сравнение: прошлый период</div>
  </div>;
}

export function MetricChart({ current, previous, metric, label }: {
  current: ReportingPoint[]; previous: ReportingPoint[]; metric: string; label: string;
}) {
  const a = current.map(point => (point as unknown as Record<string, number | null>)[metric]);
  const b = previous.map(point => (point as unknown as Record<string, number | null>)[metric]);
  const all = [...a, ...b].filter((value): value is number => value != null);
  if (!all.length) return <div className="resultNoChart">За выбранный период нет данных для графика</div>;
  const min = Math.min(0, ...all), max = Math.max(...all, 1);
  const coordinates = (values: (number | null)[]) => values.map((value, index) => value == null ? null :
    `${28 + index * 648 / Math.max(values.length - 1, 1)},${181 - (value - min) / (max - min || 1) * 150}`);
  const segments = (values: (number | null)[]) => {
    const result: string[] = []; let currentSegment: string[] = [];
    for (const point of coordinates(values)) { if (point) currentSegment.push(point); else if (currentSegment.length) { result.push(currentSegment.join(" ")); currentSegment = []; } }
    if (currentSegment.length) result.push(currentSegment.join(" "));
    return result;
  };
  return <div className="resultChartWrap"><svg className="resultChart" viewBox="0 0 704 204" role="img" aria-label={`Динамика: ${label}`}>
    {[31, 81, 131, 181].map(y => <line key={y} x1="28" x2="676" y1={y} y2={y} stroke="#e8eef9" />)}
    {[28, 136, 244, 352, 460, 568, 676].map(x => <line key={x} x1={x} x2={x} y1="31" y2="181" stroke="#edf2fa" />)}
    {segments(b).map((points, index) => <polyline key={`prior-${index}`} points={points} fill="none" stroke="#99a8bf" strokeWidth="2" strokeDasharray="6 5" strokeLinecap="round" strokeLinejoin="round" />)}
    {segments(a).map((points, index) => <polyline key={`current-${index}`} points={points} fill="none" stroke="#006BFD" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />)}
  </svg><div className="resultAxis"><span>{current[0]?.label && shortDate(current[0].label)}</span><span>{current.at(-1)?.label && shortDate(current.at(-1)!.label)}</span></div></div>;
}
