"use client";

import Link from "next/link";
import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { IntegrationsCard } from "@/components/integrations-card";
import { AiSettingsCard, CareCard, ConversionsCard } from "@/components/growth-cards";
import { PushCard } from "@/components/pwa";
import { RequisitesCard } from "@/components/crm/documents";
import { ClientPlanCard } from "@/components/plan-cards";
import LinkBrief from "@/components/brief-settings-link";
import { api } from "@/lib/api";
import { money, ProjectSidebar } from "@/components/reporting";
import "../result/result.css";
import "./settings.css";

type ProjectOption = { id: number; name: string; organization_name: string };
type Company = { id: number; name: string; legal_name: string | null; contact_email: string | null;
  contact_phone: string | null; website: string | null; timezone: string; currency: string; logo_data: string | null };
type Project = { id: number; name: string; website: string | null; description: string | null;
  status: string; timezone: string | null; meeting_enabled: boolean };
type Reason = { id: number; label: string; is_system: boolean; status: string; position: number };
type Source = { id: string; name: string; category: string | null; kind: string; method: string;
  data_mode: string; status: string; connection_id: number | null };
type Rule = { key: string; label: string; active: boolean; enabled: boolean; threshold: number | null;
  unit?: string; in_app: boolean; email: boolean; telegram: boolean; recipients?: boolean;
  recipient_user_ids: number[] | null; notify_assignee: boolean };
type Member = { id: number; display_name: string; role: string; telegram_linked: boolean };
type RuleValue = { enabled: boolean; threshold: number | null; in_app: boolean; telegram: boolean;
  recipient_user_ids: number[] | null; notify_assignee: boolean };
type TelegramStatus = { configured: boolean; linked: boolean; telegram_username: string | null; bot_username: string | null };
const roleNames: Record<string, string> = { client_owner: "Собственник", sales_head: "Руководитель продаж",
  sales_manager: "Менеджер продаж", client_marketer: "Маркетолог", viewer: "Наблюдатель" };
const defaultRoles = ["client_owner", "sales_head"];
type Settings = { company: Company; project: Project; economics: { name: string; updated_at: string;
  average_check: number | null; margin: number | null; allowable_cac: number | null } | null;
  reasons: Reason[]; sources: Source[]; notifications: Rule[]; permissions: string[];
  members: Member[]; telegram_bot_configured: boolean };
type Tab = "company" | "project" | "funnel" | "sources" | "integrations" | "notifications";
const tabs: { key: Tab; label: string; icon: string }[] = [
  { key: "company", label: "Компания", icon: "▣" }, { key: "project", label: "Проект", icon: "◇" },
  { key: "funnel", label: "Воронка", icon: "▽" }, { key: "sources", label: "Источники", icon: "♧" },
  { key: "integrations", label: "Подключения", icon: "⇄" }, { key: "notifications", label: "Уведомления", icon: "♧" },
];
const sourceKind: Record<string, string> = { INTEGRATION: "Интеграция", INTERNAL: "Внутренний", CUSTOM: "Кастомный", MANUAL: "Ручной" };
const dateTime = (value: string) => new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
const empty = (value: string | null) => value || "";

export default function SettingsPage() {
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [data, setData] = useState<Settings | null>(null);
  const [company, setCompany] = useState<Company | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [tab, setTab] = useState<Tab>("company");
  const [sourceModal, setSourceModal] = useState(false);
  const [reasonModal, setReasonModal] = useState(false);
  const [saving, setSaving] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [logoError, setLogoError] = useState("");
  useEffect(() => { api<ProjectOption[]>("/result/projects").then(rows => {
    setProjects(rows); const desired = Number(new URLSearchParams(window.location.search).get("project_id"));
    setProjectId(rows.find(item => item.id === desired)?.id || rows[0]?.id || null);
    const anchor = window.location.hash.slice(1);
    const fromHash: Record<string, Tab> = { telegram: "notifications", notifications: "notifications", economics: "project",
      sources: "sources", integrations: "integrations", funnel: "funnel", requisites: "company" };
    if (fromHash[anchor]) setTab(fromHash[anchor]);
  }).catch(e => setError(e.message)); }, []);
  useEffect(() => {
    const fromHash: Record<string, Tab> = { telegram: "notifications", notifications: "notifications", economics: "project",
      sources: "sources", integrations: "integrations", funnel: "funnel", requisites: "company" };
    const onHash = () => { const tab = fromHash[window.location.hash.slice(1)]; if (tab) setTab(tab); };
    window.addEventListener("hashchange", onHash); return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => { if (!projectId) return;
    let active = true; setError("");
    api<Settings>(`/settings?project_id=${projectId}`).then(value => { if (active) { setData(value); setCompany(value.company); setProject(value.project); } })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [projectId, revision]);
  const projectOption = projects.find(item => item.id === projectId);
  const dirtyCompany = !!data && !!company && JSON.stringify(company) !== JSON.stringify(data.company);
  const dirtyProject = !!data && !!project && JSON.stringify(project) !== JSON.stringify(data.project);
  const dirty = dirtyCompany || dirtyProject;
  const canSettings = data?.permissions.includes("manage_settings") || false;
  const canSources = data?.permissions.includes("manage_sources") || false;
  useEffect(() => { const warn = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn); }, [dirty]);
  function switchTab(next: Tab) { if (dirty && !window.confirm("Есть несохранённые изменения. Перейти без сохранения?")) return;
    if (dirty && data) { setCompany(data.company); setProject(data.project); } setTab(next); setNotice(""); }
  function switchProject(next: number) { if (dirty && !window.confirm("Есть несохранённые изменения. Сменить проект без сохранения?")) return;
    setData(null); setProjectId(next); }
  async function save(path: string, body: unknown, message: string, reload = true) {
    if (!projectId) return false; setSaving(path); setError(""); setNotice("");
    const payload = path === "/company" || path === "/project"
      ? Object.fromEntries(Object.entries(body as Record<string, unknown>).filter(([key]) => key !== "id" && key !== "meeting_enabled"))
      : body;
    try { await api(`/settings${path}?project_id=${projectId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      setNotice(message); if (reload) setRevision(value => value + 1); return true;
    } catch (e) { setError((e as Error).message); return false; } finally { setSaving(""); }
  }
  async function create(path: string, body: unknown, message: string) {
    if (!projectId) return false; setSaving(path); setError(""); setNotice("");
    try { await api(`/settings${path}?project_id=${projectId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      setNotice(message); setRevision(value => value + 1); return true;
    } catch (e) { setError((e as Error).message); return false; } finally { setSaving(""); }
  }
  async function logoChange(event: ChangeEvent<HTMLInputElement>) { const file = event.target.files?.[0];
    if (!file) return; setLogoError("");
    if (!["image/png", "image/jpeg"].includes(file.type) || file.size > 1024 * 1024) { setLogoError("PNG/JPEG до 1 МБ"); return; }
    const reader = new FileReader(); reader.onload = () => setCompany(value => value ? { ...value, logo_data: String(reader.result) } : value);
    reader.readAsDataURL(file);
  }
  async function reasonAction(reason: Reason, action: "archive" | "restore" | "rename") {
    if (action === "rename") { const label = window.prompt("Новое название причины", reason.label)?.trim();
      if (!label || label === reason.label) return; await save(`/reasons/${reason.id}`, { label }, "Причина переименована."); return; }
    await save(`/reasons/${reason.id}`, { status: action === "archive" ? "archived" : "active" },
      action === "archive" ? "Причина архивирована. История лидов сохранена." : "Причина восстановлена.");
  }
  async function moveReason(reason: Reason, offset: number) { if (!data || !projectId) return;
    const items = [...data.reasons]; const index = items.findIndex(item => item.id === reason.id), target = index + offset;
    if (target < 0 || target >= items.length) return;
    [items[index], items[target]] = [items[target], items[index]];
    setSaving("reason-order"); setError(""); try { await api(`/settings/reasons/order?project_id=${projectId}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: items.map(item => item.id) }) });
      setNotice("Порядок причин обновлён."); setRevision(value => value + 1);
    } catch (e) { setError((e as Error).message); } finally { setSaving(""); }
  }
  async function submitReason(event: FormEvent<HTMLFormElement>) { event.preventDefault();
    const form = new FormData(event.currentTarget); if (await create("/reasons", { label: form.get("label") }, "Причина добавлена.")) setReasonModal(false); }
  async function submitSource(event: FormEvent<HTMLFormElement>) { event.preventDefault();
    const form = new FormData(event.currentTarget); if (await create("/sources", { name: form.get("name"), category: form.get("category"),
      kind: form.get("kind"), method: form.get("method") }, "Источник добавлен.")) setSourceModal(false); }
  const sourceList = useMemo(() => data?.sources || [], [data]);

  return <div className="resultShell"><ProjectSidebar project={projectOption} projectId={projectId} active="settings" role={undefined}/>
    <main className="resultMain settingsMain"><div className="resultBreadcrumb">StepToLead <span>/</span> Настройки</div>
      <header className="resultHeader settingsHeader"><div><h1>Настройки</h1><p>Как настроены ваша компания, проект, источники и уведомления.</p></div>
        {projects.length > 1 && <label className="settingsProjectPicker">Проект<select value={projectId || ""} onChange={event => switchProject(Number(event.target.value))}>{projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}</header>
      {error && <div className="resultError" role="alert">{error}{!data && <div><Link href="/portal/login?next=/settings">Войти в клиентский кабинет</Link></div>}</div>}
      {notice && <div className="settingsNotice" role="status">{notice}</div>}
      <nav className="settingsTabs" aria-label="Разделы настроек">{tabs.map(item => <button key={item.key} className={tab === item.key ? "active" : ""} onClick={() => switchTab(item.key)}><span>{item.icon}</span>{item.label}</button>)}</nav>
      {data && company && project && <div className="settingsContent">
        {tab === "company" && <><form className="resultPanel settingsCard" onSubmit={event => { event.preventDefault(); save("/company", company, "Данные компании сохранены."); }}><div className="settingsCardHead"><div><h2>Основная информация</h2><p>Данные только вашей организации.</p></div>{canSettings && <button className="settingsPrimary" disabled={!dirtyCompany || !!saving}>{saving === "/company" ? "Сохраняем…" : "Сохранить изменения"}</button>}</div><div className="settingsCompanyGrid"><div className="settingsLogoBlock"><div className="settingsLogo">{company.logo_data ? <img src={company.logo_data} alt="Логотип компании"/> : company.name.slice(0,2).toUpperCase()}</div><div><label className="settingsOutline">Изменить логотип<input type="file" accept="image/png,image/jpeg" onChange={logoChange} disabled={!canSettings}/></label><small>PNG/JPEG до 1 МБ</small>{logoError && <small className="settingsInlineError">{logoError}</small>}</div></div><div className="settingsFields"><label>Название компании *<input required value={company.name} disabled={!canSettings} onChange={e => setCompany({ ...company, name: e.target.value })}/></label><label>Юридическое название<input value={empty(company.legal_name)} disabled={!canSettings} onChange={e => setCompany({ ...company, legal_name: e.target.value })}/></label><label>Контактный email<input type="email" value={empty(company.contact_email)} disabled={!canSettings} onChange={e => setCompany({ ...company, contact_email: e.target.value })}/></label><label>Контактный телефон<input value={empty(company.contact_phone)} disabled={!canSettings} onChange={e => setCompany({ ...company, contact_phone: e.target.value })}/></label><label>Часовой пояс<input value={company.timezone} disabled={!canSettings} onChange={e => setCompany({ ...company, timezone: e.target.value })} placeholder="Europe/Moscow"/></label><label>Валюта<input value={company.currency} maxLength={3} disabled={!canSettings} onChange={e => setCompany({ ...company, currency: e.target.value.toUpperCase() })}/></label><label className="wide">Сайт<input type="url" value={empty(company.website)} disabled={!canSettings} onChange={e => setCompany({ ...company, website: e.target.value })}/></label></div></div></form><div className="settingsTwoColumns"><ProjectCard project={project} setProject={setProject} dirty={dirtyProject} saving={saving} canSettings={canSettings} onSave={() => save("/project", project, "Проект сохранён.")}/><EconomicsCard data={data.economics} projectId={projectId}/></div><ClientPlanCard/>{projectId && <RequisitesCard projectId={projectId}/>}</>}
        {tab === "project" && <div className="settingsTwoColumns"><ProjectCard project={project} setProject={setProject} dirty={dirtyProject} saving={saving} canSettings={canSettings} onSave={() => save("/project", project, "Проект сохранён.")}/><EconomicsCard data={data.economics} projectId={projectId}/></div>}
        {tab === "funnel" && <><div className="settingsTwoColumns"><section className="resultPanel settingsCard"><h2>Воронка продаж</h2><p>Основные этапы влияют на аналитику и не удаляются.</p><div className="settingsStage"><strong>01</strong><span>Лид</span><small>Системный</small></div><div className="settingsStage"><strong>02</strong><span>Квалифицирован</span><small>Системный</small></div><label className="settingsStage"><strong>03</strong><span>Встреча</span><input type="checkbox" checked={project.meeting_enabled} disabled={!canSettings || !!saving} onChange={event => save("/funnel", { meeting_enabled: event.target.checked }, "Настройка встречи сохранена.")}/><small>Опционально</small></label><div className="settingsStage"><strong>04</strong><span>Продажа</span><small>Системный</small></div><p className="settingsHint">Встреча учитывается только когда менеджер отмечает её в карточке лида. Без событий в аналитике будет «—».</p></section><section className="resultPanel settingsCard"><div className="settingsCardHead"><div><h2>Причины потери</h2><p>Архивирование не удаляет историю лидов.</p></div>{canSettings && <button className="settingsOutline" onClick={() => setReasonModal(true)}>＋ Причина</button>}</div><div className="settingsReasonList">{data.reasons.map((reason,index) => <div key={reason.id} className={reason.status === "archived" ? "archived" : ""}><span>{reason.label}</span><small>{reason.status === "archived" ? "Архив" : reason.is_system ? "Системная" : "Пользовательская"}</small>{canSettings && <span className="settingsRowActions"><button onClick={() => moveReason(reason,-1)} disabled={index === 0 || !!saving} title="Выше">↑</button><button onClick={() => moveReason(reason,1)} disabled={index === data.reasons.length-1 || !!saving} title="Ниже">↓</button>{!reason.is_system && <button onClick={() => reasonAction(reason,"rename")} title="Переименовать">✎</button>}<button onClick={() => reasonAction(reason,reason.status === "archived" ? "restore" : "archive")} title={reason.status === "archived" ? "Восстановить" : "Архивировать"}>{reason.status === "archived" ? "↺" : "⊘"}</button></span>}</div>)}</div></section></div>{projectId && <CareCard projectId={projectId}/>}</>}
        {tab === "sources" && <section className="resultPanel settingsCard"><div className="settingsCardHead"><div><h2>Источники</h2><p>Универсальные источники проекта. Рекламные кабинеты подключаются в разделе «Реклама».</p></div>{canSources && <button className="settingsPrimary" onClick={() => setSourceModal(true)}>＋ Источник</button>}</div><div className="settingsTableScroll"><table><thead><tr><th>Название</th><th>Категория</th><th>Тип</th><th>Способ данных</th><th>Статус</th><th>Действия</th></tr></thead><tbody>{sourceList.map(source => <tr key={source.id}><td><strong>{source.name}</strong></td><td>{source.category || "—"}</td><td>{sourceKind[source.kind] || source.kind}</td><td>{source.method}</td><td><span className={`settingsBadge ${source.status}`}>{source.status === "active" ? "Активен" : source.status === "archived" ? "Архив" : "Ошибка"}</span></td><td>{source.connection_id ? <Link href={`/ads?project_id=${projectId}`}>Управлять в рекламе →</Link> : canSources ? <button className="settingsTextButton" onClick={() => save(`/sources/${source.id.split(":")[1]}`, { status: source.status === "archived" ? "active" : "archived" }, source.status === "archived" ? "Источник восстановлен." : "Источник архивирован; история сохранена.")}>{source.status === "archived" ? "Восстановить" : "Архивировать"}</button> : "—"}</td></tr>)}</tbody></table>{!sourceList.length && <p className="resultTableEmpty">Источники проекта ещё не добавлены.</p>}</div></section>}
        {tab === "integrations" && projectId && <><IntegrationsCard projectId={projectId}/><ConversionsCard projectId={projectId}/><AiSettingsCard projectId={projectId}/><LinkBrief projectId={projectId}/></>}
        {tab === "notifications" && <><PushCard/><TelegramCard configured={data.telegram_bot_configured} onChanged={() => setRevision(value => value + 1)}/><section className="resultPanel settingsCard"><h2>Уведомления</h2><p>Событие → кому → канал доставки. Для новых лидов и продаж можно выбрать участников проекта; доставка — в кабинет StepToLead и в Telegram.</p><div className="settingsRules">{data.notifications.map(rule => <RuleRow key={rule.key} rule={rule} members={data.members} botConfigured={data.telegram_bot_configured} canSettings={canSettings} saving={saving} onSave={value => save(`/notifications/${rule.key}`, value, "Правило сохранено.")}/>)}</div><div className="settingsChannels"><span><i className="on"/> Внутри StepToLead</span><span><i className={data.telegram_bot_configured ? "on" : ""}/> Telegram — {data.telegram_bot_configured ? "бот подключён" : "бот не настроен на сервере"}</span><span><i/> Email — не подключён</span></div></section></>}
      </div>}
      {reasonModal && <div className="resultModalBackdrop" onMouseDown={event => { if (event.target === event.currentTarget) setReasonModal(false); }}><form className="resultModal" onSubmit={submitReason}><header><h2>Добавить причину потери</h2><button type="button" onClick={() => setReasonModal(false)}>×</button></header><label>Название<input name="label" minLength={2} maxLength={160} required/></label><button className="resultPrimary" disabled={!!saving}>Сохранить</button></form></div>}
      {sourceModal && <div className="resultModalBackdrop" onMouseDown={event => { if (event.target === event.currentTarget) setSourceModal(false); }}><form className="resultModal" onSubmit={submitSource}><header><h2>Добавить источник</h2><button type="button" onClick={() => setSourceModal(false)}>×</button></header><p>Только описание источника. Для API/BOT понадобится отдельный коннектор; настройки не создают работающую интеграцию.</p><label>Название<input name="name" minLength={2} maxLength={180} required/></label><label>Категория<input name="category" maxLength={80} placeholder="Например, органика или партнёры"/></label><label>Тип<select name="kind" defaultValue="MANUAL"><option value="MANUAL">Ручной</option><option value="CUSTOM">Кастомный</option><option value="INTERNAL">Внутренний</option></select></label><label>Способ данных<select name="method" defaultValue="MANUAL"><option value="MANUAL">Ручной ввод</option><option value="FILE">Файл</option><option value="WEBHOOK">Webhook (потребуется коннектор)</option><option value="API">API (потребуется коннектор)</option><option value="BOT">Bot (потребуется коннектор)</option><option value="INTERNAL">Внутренний</option></select></label><button className="resultPrimary" disabled={!!saving}>Добавить</button></form></div>}
    </main></div>;
}

function ProjectCard({ project, setProject, dirty, saving, canSettings, onSave }: { project: Project; setProject: (value: Project) => void;
  dirty: boolean; saving: string; canSettings: boolean; onSave: () => void }) {
  return <form className="resultPanel settingsCard" onSubmit={event => { event.preventDefault(); onSave(); }}><div className="settingsCardHead"><div><h2>Информация о проекте</h2><p>Настройка текущего проекта.</p></div>{canSettings && <button className="settingsPrimary" disabled={!dirty || !!saving}>{saving === "/project" ? "Сохраняем…" : "Сохранить"}</button>}</div><div className="settingsFields"><label>Название проекта *<input required value={project.name} disabled={!canSettings} onChange={event => setProject({ ...project, name: event.target.value })}/></label><label>Сайт проекта<input type="url" value={empty(project.website)} disabled={!canSettings} onChange={event => setProject({ ...project, website: event.target.value })}/></label><label>Статус<select value={project.status} disabled={!canSettings} onChange={event => setProject({ ...project, status: event.target.value })}><option value="active">Активный</option><option value="archived">Архив</option></select></label><label>Часовой пояс<input value={empty(project.timezone)} disabled={!canSettings} placeholder="Как у компании" onChange={event => setProject({ ...project, timezone: event.target.value })}/></label><label className="wide">Описание<textarea value={empty(project.description)} disabled={!canSettings} maxLength={3000} onChange={event => setProject({ ...project, description: event.target.value })}/></label></div></form>;
}

function EconomicsCard({ data, projectId }: { data: Settings["economics"]; projectId: number | null }) {
  return <section className="resultPanel settingsCard"><h2>Экономика проекта</h2><p>Одна сохранённая модель для выбранного проекта.</p>{data ? <div className="settingsEconomics"><strong>{data.name}</strong><small>Обновлена {dateTime(data.updated_at)}</small><div><span>Средний чек<b>{money(data.average_check)}</b></span><span>Маржинальность<b>{data.margin == null ? "—" : `${data.margin}%`}</b></span><span>Допустимый CAC<b>{money(data.allowable_cac)}</b></span></div></div> : <p className="settingsEmpty">Активной экономической модели нет.</p>}<Link className="settingsOutline" href={projectId ? `/result?project_id=${projectId}` : "/result"}>Заполнить или изменить в «Результате» →</Link></section>;
}

function RuleRow({ rule, members, botConfigured, canSettings, saving, onSave }: { rule: Rule; members: Member[];
  botConfigured: boolean; canSettings: boolean; saving: string; onSave: (value: RuleValue) => void }) {
  const initial = (): RuleValue => ({ enabled: rule.enabled, threshold: rule.threshold, in_app: rule.in_app, telegram: rule.telegram,
    recipient_user_ids: rule.recipient_user_ids, notify_assignee: rule.notify_assignee });
  const [value, setValue] = useState<RuleValue>(initial);
  const [threshold, setThreshold] = useState(rule.threshold == null ? "" : String(rule.threshold));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setValue(initial()); setThreshold(rule.threshold == null ? "" : String(rule.threshold)); }, [rule]);
  const current: RuleValue = { ...value, threshold: threshold === "" ? null : Number(threshold) };
  const dirty = JSON.stringify(current) !== JSON.stringify(initial());
  const custom = value.recipient_user_ids !== null;
  const selected = new Set(value.recipient_user_ids ?? members.filter(m => defaultRoles.includes(m.role)).map(m => m.id));
  const set = (patch: Partial<RuleValue>) => setValue(old => ({ ...old, ...patch }));
  function toggleMember(id: number) { const next = new Set(selected); if (next.has(id)) next.delete(id); else next.add(id);
    set({ recipient_user_ids: Array.from(next).sort((a, b) => a - b) }); }
  const withoutTelegram = value.telegram ? members.filter(m => selected.has(m.id) && !m.telegram_linked) : [];
  return <div className="settingsRuleBlock"><div className="settingsRule"><div><strong>{rule.label}</strong><small>{rule.active ? "Событие обрабатывается" : "Обработчик события ещё не реализован; правило можно подготовить"}</small></div>
    <label><input type="checkbox" checked={value.enabled} disabled={!canSettings} onChange={e => set({ enabled: e.target.checked })}/> Включено</label>
    {rule.unit && <label>Порог <input type="number" min="0" step="0.1" value={threshold} disabled={!canSettings} onChange={e => setThreshold(e.target.value)}/>{rule.unit}</label>}
    <label><input type="checkbox" checked={value.in_app} disabled={!canSettings} onChange={e => set({ in_app: e.target.checked })}/> В кабинете</label>
    {rule.recipients && <label title={botConfigured ? "" : "Задайте TELEGRAM_BOT_TOKEN на сервере"}><input type="checkbox" checked={value.telegram} disabled={!canSettings || (!botConfigured && !value.telegram)} onChange={e => set({ telegram: e.target.checked })}/> Telegram</label>}
    {canSettings && <button disabled={!dirty || !!saving} onClick={() => onSave(current)}>Сохранить</button>}</div>
    {rule.recipients && value.enabled && <div className="settingsRecipients">
      <div className="settingsRecipientMode"><span>Кому:</span>
        <label><input type="radio" checked={!custom} disabled={!canSettings} onChange={() => set({ recipient_user_ids: null })}/> По умолчанию — ответственному, иначе собственнику и руководителям продаж</label>
        <label><input type="radio" checked={custom} disabled={!canSettings} onChange={() => set({ recipient_user_ids: Array.from(selected).sort((a, b) => a - b) })}/> Выбранным участникам проекта</label></div>
      {custom && <><div className="settingsMemberList">{members.map(member => <label key={member.id} className={selected.has(member.id) ? "on" : ""}><input type="checkbox" checked={selected.has(member.id)} disabled={!canSettings} onChange={() => toggleMember(member.id)}/><span>{member.display_name}<small>{roleNames[member.role] || member.role}{member.telegram_linked ? " · Telegram ✓" : ""}</small></span></label>)}{!members.length && <small>В проекте пока нет участников.</small>}</div>
        <label className="settingsAssignee"><input type="checkbox" checked={value.notify_assignee} disabled={!canSettings} onChange={e => set({ notify_assignee: e.target.checked })}/> Дополнительно уведомлять ответственного за лид</label></>}
      {custom && !selected.size && !value.notify_assignee && <p className="settingsWarn">Никто не получит уведомление — выберите хотя бы одного участника.</p>}
      {withoutTelegram.length > 0 && <p className="settingsWarn">Не подключили Telegram: {withoutTelegram.map(m => m.display_name).join(", ")}. Они получат уведомление только в кабинете — пусть нажмут «Подключить Telegram» у себя в настройках.</p>}
    </div>}</div>;
}

function TelegramCard({ configured, onChanged }: { configured: boolean; onChanged: () => void }) {
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [busy, setBusy] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [message, setMessage] = useState("");
  const call = async (path: string, method: string) => api<TelegramStatus>(`/portal/telegram${path}`, { method });
  useEffect(() => {
    if (!waiting) return;
    let active = true; let pending = false;
    const started = Date.now();
    const timer = setInterval(async () => {
      if (Date.now() - started >= 120000) { clearInterval(timer); if (active) { setWaiting(false); setMessage("Проверка завершена. Если Telegram не подключён, откройте ссылку ещё раз."); } return; }
      if (pending) return;
      pending = true;
      try {
        const next = await api<TelegramStatus>("/portal/telegram");
        if (active) { setStatus(next); if (next.linked) { setWaiting(false); setMessage("Telegram подключён."); onChanged(); } }
      } catch { /* keep checking until the timeout */ } finally { pending = false; }
    }, 3000);
    return () => { active = false; clearInterval(timer); };
  }, [waiting]);
  useEffect(() => { api<TelegramStatus>("/portal/telegram").then(setStatus).catch(() => setStatus(null)); }, [configured]);
  async function run(name: string, action: () => Promise<void>) { setBusy(name); setMessage("");
    try { await action(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(""); } }
  const link = () => run("link", async () => { const result = await api<{ url: string }>("/portal/telegram/link", { method: "POST" });
    window.open(result.url, "_blank", "noopener,noreferrer"); setWaiting(true);
    setMessage("В Telegram нажмите «Запустить» (Start). Подключение подтвердится здесь автоматически."); });
  const check = () => run("check", async () => { const next = await call("/check", "POST"); setStatus(next);
    if (next.linked) { setWaiting(false); setMessage("Telegram подключён."); onChanged(); }
    else setMessage("Бот пока не получил команду Start. Откройте ссылку ещё раз и нажмите «Запустить»."); });
  const test = () => run("test", async () => { await api("/portal/telegram/test", { method: "POST" }); setMessage("Тестовое сообщение отправлено."); });
  const unlink = () => run("unlink", async () => { setStatus(await call("", "DELETE")); setMessage("Telegram отключён."); onChanged(); });
  return <section className="resultPanel settingsCard settingsTelegram"><div className="settingsCardHead"><div><h2>Мой Telegram</h2>
    <p>{!configured ? "Бот для уведомлений ещё не настроен на сервере (переменная TELEGRAM_BOT_TOKEN)." : status?.linked ? `Подключён${status.telegram_username ? ` @${status.telegram_username}` : ""}. Сюда приходят уведомления, если они включены для вас в правилах ниже, и код для восстановления пароля.` : `Подключите Telegram, чтобы получать уведомления через бота${status?.bot_username ? ` @${status.bot_username}` : ""} — и сами восстанавливать пароль, если забудете.`}</p></div>
    {configured && <div className="settingsTelegramActions">{status?.linked ? <><button className="settingsOutline" disabled={!!busy} onClick={test}>{busy === "test" ? "Отправляем…" : "Отправить тест"}</button><button className="settingsOutline" disabled={!!busy} onClick={unlink}>Отключить</button></> : <><button className="settingsPrimary" disabled={!!busy} onClick={link}>{busy === "link" ? "Готовим ссылку…" : "Подключить Telegram"}</button>{waiting && <button className="settingsOutline" disabled={!!busy} onClick={check}>{busy === "check" ? "Проверяем…" : "Проверить"}</button>}</>}</div>}</div>
    {message && <p className="settingsHint" role="status">{message}</p>}</section>;
}
