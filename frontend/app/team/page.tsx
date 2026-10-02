"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { ProjectSidebar } from "@/components/reporting";
import "../result/result.css";
import "./team.css";

type Member = { id: number; display_name: string; email: string; phone: string | null; role: string;
  role_name: string; permissions: string[]; status: "invited" | "active" | "blocked";
  created_at: string; last_activity_at: string | null };
type Team = { members: Member[]; viewer: Member; roles: Record<string, string>;
  sections: Record<string, [string, string | null]>; defaults: Record<string, string[]> };
type Project = { id: number; name: string; organization_name: string };
const labels: Record<string, string> = { result: "Результат", analytics: "Аналитика", leads: "Лиды", sales: "Продажи", ads: "Реклама", campaigns: "Кампании", team: "Команда", settings: "Настройки" };
const extras: Record<string, string> = { manage_sources: "Управление источниками", edit_manual_metrics: "Ручные показатели" };
const dateTime = (value: string | null) => value ? new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const initials = (name: string) => name.trim().split(/\s+/).slice(0, 2).map(piece => piece[0]?.toUpperCase()).join("");
const statusLabel = { active: "Активен", invited: "Приглашён", blocked: "Заблокирован" };

export default function TeamPage() {
  const [data, setData] = useState<Team | null>(null);
  const [project, setProject] = useState<Project | undefined>();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [editing, setEditing] = useState(false);
  const [invite, setInvite] = useState(false);
  const [role, setRole] = useState("sales_manager");
  const [draftPermissions, setDraftPermissions] = useState<string[]>([]);
  const [temporaryPassword, setTemporaryPassword] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => { api<Team>("/team").then(setData).catch(e => setError(e.message)); }, [revision]);
  useEffect(() => { api<Project[]>("/result/projects").then(rows => setProject(rows[0])).catch(() => {}); }, []);
  const selected = data?.members.find(member => member.id === selectedId);
  const filtered = useMemo(() => (data?.members || []).filter(member =>
    (!roleFilter || member.role === roleFilter) && `${member.display_name} ${member.email}`.toLocaleLowerCase("ru").includes(search.toLocaleLowerCase("ru"))), [data, roleFilter, search]);
  const counts = useMemo(() => ({ total: data?.members.length || 0,
    owner: data?.members.filter(item => item.role === "client_owner").length || 0,
    marketer: data?.members.filter(item => item.role === "client_marketer").length || 0,
    sales: data?.members.filter(item => ["sales_head", "sales_manager"].includes(item.role)).length || 0,
    viewer: data?.members.filter(item => item.role === "viewer").length || 0 }), [data]);

  function selectMember(member: Member) { setSelectedId(member.id); setEditing(false); setRole(member.role); setDraftPermissions(member.permissions); setError(""); }
  function openInvite() { setInvite(true); setRole("sales_manager"); setDraftPermissions(data?.defaults.sales_manager || []); setError(""); }
  function setLevel(section: string, level: string) {
    const pair = data?.sections[section]; if (!pair) return;
    const next = new Set(draftPermissions); for (const permission of pair) if (permission) next.delete(permission);
    if (level !== "none") next.add(pair[0]);
    if (level === "manage" && pair[1]) next.add(pair[1]);
    setDraftPermissions([...next]);
  }
  function levelFor(section: string) {
    const pair = data?.sections[section]; if (!pair) return "none";
    if (pair[1] && draftPermissions.includes(pair[1])) return "manage";
    return draftPermissions.includes(pair[0]) ? "view" : "none";
  }
  async function saveMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!selected) return;
    const form = new FormData(event.currentTarget); setBusy(true); setError("");
    try { await api(`/team/${selected.id}`, { method: "PATCH", body: JSON.stringify({ display_name: form.get("name"),
      email: form.get("email"), phone: form.get("phone"), role, permissions: draftPermissions }),
      headers: { "Content-Type": "application/json" } });
      setNotice("Данные сотрудника сохранены."); setEditing(false); setRevision(value => value + 1);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function inviteMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = new FormData(event.currentTarget); setBusy(true); setError("");
    try { const created = await api<Member & { temporary_password: string }>("/team", { method: "POST",
      body: JSON.stringify({ display_name: form.get("name"), email: form.get("email"), phone: form.get("phone"), role,
        permissions: draftPermissions }), headers: { "Content-Type": "application/json" } });
      setInvite(false); setSelectedId(created.id); setTemporaryPassword(created.temporary_password);
      setNotice("Сотрудник создан. Email не отправлен: передайте временный пароль безопасным каналом.");
      setRevision(value => value + 1);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function changeStatus(member: Member) {
    const status = member.status === "blocked" ? "active" : "blocked";
    if (!window.confirm(`${status === "blocked" ? "Заблокировать" : "Активировать"} «${member.display_name}»?`)) return;
    setBusy(true); setError(""); try { await api(`/team/${member.id}`, { method: "PATCH", body: JSON.stringify({ status }),
      headers: { "Content-Type": "application/json" } }); setRevision(value => value + 1);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const permissionsEditor = <div className="teamPermissions"><h3>Доступ к разделам</h3><p>Изменения применяются после сохранения.</p>
    {Object.keys(labels).map(key => { const pair = data?.sections[key]; if (!pair) return null;
      const canView = data?.viewer.permissions.includes(pair[0]);
      const canManage = pair[1] && data?.viewer.permissions.includes(pair[1]);
      return <label key={key}><span>{labels[key]}</span><select value={levelFor(key)} disabled={(!editing && !invite) || !canView || role === "client_owner"}
        onChange={event => setLevel(key, event.target.value)}><option value="none">Нет доступа</option>
        {pair[1] !== pair[0] && <option value="view">Просмотр</option>}
        {pair[1] && <option value="manage" disabled={!canManage}>Управление</option>}</select></label>; })}
    <div className="teamExtras">{Object.entries(extras).map(([permission, label]) => <label key={permission}><input type="checkbox"
      checked={draftPermissions.includes(permission)} disabled={(!editing && !invite) || !data?.viewer.permissions.includes(permission) || role === "client_owner"}
      onChange={event => setDraftPermissions(current => event.target.checked ? [...current, permission] : current.filter(item => item !== permission))}/>{label}</label>)}</div></div>;

  return <div className="resultShell"><ProjectSidebar project={project} projectId={project?.id || null} active="team" role={data?.viewer.role}/>
    <main className="resultMain teamMain"><div className="resultBreadcrumb">StepToLead <span>/</span> Команда</div>
      <header className="resultHeader teamHeader"><div><h1>Команда</h1><p>Сотрудники вашей организации и доступ к разделам клиентского кабинета.</p></div>
        {data?.viewer.permissions.includes("manage_team") && <button className="teamPrimary" onClick={openInvite}>＋ Пригласить сотрудника</button>}</header>
      {error && <div className="resultError" role="alert">{error}{!data && <div><Link href="/portal/login?next=/team">Войти в клиентский кабинет</Link></div>}</div>}{notice && <div className="teamNotice" role="status">{notice}</div>}
      {temporaryPassword && <div className="teamPassword" role="alert"><strong>Временный пароль</strong><code>{temporaryPassword}</code><span>Показан один раз. Email-приглашение не отправлялось.</span><button onClick={() => setTemporaryPassword(null)}>Скрыть</button></div>}
      {data && <><section className="teamStats">{([
        ["Всего сотрудников", counts.total, "♙"], ["Владельцы", counts.owner, "♛"],
        ["Маркетологи", counts.marketer, "▥"], ["Отдел продаж", counts.sales, "◉"],
        ["Только просмотр", counts.viewer, "◉"]] as const).map(([label, value, icon]) => <div className="teamStat" key={label}><span>{icon}</span><small>{label}</small><strong>{value}</strong></div>)}</section>
        <div className="teamGrid"><section className="resultPanel teamList"><h2>Сотрудники</h2><div className="teamFilters"><input aria-label="Поиск сотрудника" placeholder="Поиск по имени или email…" value={search} onChange={event => setSearch(event.target.value)}/><select aria-label="Фильтр по роли" value={roleFilter} onChange={event => setRoleFilter(event.target.value)}><option value="">Все роли</option>{Object.entries(data.roles).map(([key,label]) => <option value={key} key={key}>{label}</option>)}</select></div>
          <div className="teamTableScroll"><table><thead><tr><th>Сотрудник</th><th>Роль</th><th>Статус</th><th>Последняя активность</th><th>Действия</th></tr></thead><tbody>{filtered.map(member => <tr key={member.id} className={selectedId === member.id ? "selected" : ""} onClick={() => selectMember(member)}><td><span className="teamAvatar">{initials(member.display_name)}</span><span><strong>{member.display_name}</strong><small>{member.email}</small></span></td><td><span className={`teamRole ${member.role}`}>{member.role_name}</span></td><td><span className={`teamStatus ${member.status}`}>{statusLabel[member.status]}</span></td><td>{dateTime(member.last_activity_at)}</td><td><button aria-label={`Открыть ${member.display_name}`} onClick={event => { event.stopPropagation(); selectMember(member); }}>⋮</button></td></tr>)}</tbody></table>{!filtered.length && <p className="resultTableEmpty">Сотрудники не найдены.</p>}</div></section>
          <aside className="resultPanel teamDrawer">{selected ? <><div className="teamDrawerHead"><span className="teamAvatar large">{initials(selected.display_name)}</span><div><h2>{selected.display_name}</h2><p>{selected.email}</p><span className={`teamStatus ${selected.status}`}>{statusLabel[selected.status]}</span></div><button onClick={() => setEditing(value => !value)}>{editing ? "Отмена" : "Редактировать"}</button></div>
            {editing ? <form onSubmit={saveMember} className="teamEditForm"><label>Имя<input name="name" defaultValue={selected.display_name} required minLength={2}/></label><label>Email<input name="email" type="email" defaultValue={selected.email} required/></label><label>Телефон<input name="phone" defaultValue={selected.phone || ""}/></label><label>Роль<select value={role} onChange={event => { const next = event.target.value; setRole(next); setDraftPermissions(data.defaults[next] || []); }}>{Object.entries(data.roles).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select></label>{permissionsEditor}<button className="teamPrimary" disabled={busy}>Сохранить изменения</button></form> : <><h3>Основная информация</h3><dl><div><dt>Роль</dt><dd>{selected.role_name}</dd></div><div><dt>Email</dt><dd>{selected.email}</dd></div><div><dt>Телефон</dt><dd>{selected.phone || "—"}</dd></div><div><dt>Дата добавления</dt><dd>{dateTime(selected.created_at)}</dd></div><div><dt>Последняя активность</dt><dd>{dateTime(selected.last_activity_at)}</dd></div></dl>{permissionsEditor}<button className="teamSecondary" disabled={busy} onClick={() => changeStatus(selected)}>{selected.status === "blocked" ? "Активировать" : "Заблокировать"}</button></>}</> : <div className="teamEmpty">Выберите сотрудника, чтобы посмотреть профиль и права доступа.</div>}</aside></div></>}
      {invite && <div className="resultModalBackdrop" onMouseDown={event => { if (event.target === event.currentTarget) setInvite(false); }}><form className="resultModal teamInvite" onSubmit={inviteMember}><header><h2>Пригласить сотрудника</h2><button type="button" onClick={() => setInvite(false)}>×</button></header><p>Создадим доступ в этой организации. Почта не отправляется — временный пароль появится после сохранения.</p><label>Имя<input name="name" required minLength={2}/></label><label>Email<input name="email" type="email" required/></label><label>Телефон (необязательно)<input name="phone"/></label><label>Роль<select value={role} onChange={event => { const next = event.target.value; setRole(next); setDraftPermissions(data?.defaults[next] || []); }}>{Object.entries(data?.roles || {}).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select></label>{permissionsEditor}<button className="resultPrimary" disabled={busy}>Создать доступ</button></form></div>}
    </main></div>;
}
