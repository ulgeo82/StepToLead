"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { api, ClientWorkspace, LeadInboundSource, PortalRole, PortalUser } from "@/lib/api";

const roles: { value: PortalRole; label: string; permissions: string[] }[] = [
  { value: "client_owner", label: "Собственник", permissions: ["Вся аналитика компании", "Реклама и продажи", "Команда и бриф"] },
  { value: "sales_head", label: "Руководитель продаж", permissions: ["Все лиды отдела", "Распределение менеджеров", "Воронка и отчёты"] },
  { value: "sales_manager", label: "Менеджер продаж", permissions: ["Назначенные лиды", "Статусы и комментарии", "Уведомления о лидах"] },
  { value: "client_marketer", label: "Маркетолог клиента", permissions: ["Рекламная аналитика", "Кампании и источники", "Без управления командой"] },
  { value: "viewer", label: "Наблюдатель", permissions: ["Только просмотр сводки", "Без редактирования", "Без управления доступами"] },
];

export default function ClientDetailsPage() {
  const params = useParams<{ id: string }>();
  const workspaceId = Number(params.id);
  const [workspace, setWorkspace] = useState<ClientWorkspace | null>(null);
  const [users, setUsers] = useState<PortalUser[]>([]);
  const [sources, setSources] = useState<LeadInboundSource[]>([]);
  const [modal, setModal] = useState<"user" | "credentials" | "source" | "webhook" | null>(null);
  const [credentials, setCredentials] = useState<{ username: string; password: string } | null>(null);
  const [webhook, setWebhook] = useState<{ url: string; token: string } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<number | string | null>(null);

  const load = useCallback(async () => {
    try {
      const [workspaces, members, inboundSources] = await Promise.all([
        api<ClientWorkspace[]>("/marketing/workspaces"),
        api<PortalUser[]>(`/portal/admin/users?workspace_id=${workspaceId}`),
        api<LeadInboundSource[]>(`/portal/admin/lead-sources?workspace_id=${workspaceId}`),
      ]);
      setWorkspace(workspaces.find(item => item.id === workspaceId) || null);
      setUsers(members);
      setSources(inboundSources);
      setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось загрузить компанию"); }
  }, [workspaceId]);
  useEffect(() => { load(); }, [load]);

  const roleCounts = useMemo(() => roles.map(role => ({ ...role, count: users.filter(user => user.role === role.value).length })), [users]);

  async function updateUser(user: PortalUser, changes: { role?: PortalRole; active?: boolean }) {
    setBusy(user.id); setError(""); setNotice("");
    try {
      await api(`/portal/admin/users/${user.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) });
      setNotice(changes.active === false ? `Доступ для ${user.display_name} отключён.` : changes.active === true ? `Доступ для ${user.display_name} включён.` : "Роль пользователя обновлена.");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось обновить доступ"); }
    finally { setBusy(null); }
  }

  async function addUser(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy("user"); setError("");
    const data = new FormData(event.currentTarget);
    try {
      const created = await api<PortalUser & { temporary_password: string }>("/portal/admin/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspaceId, username: data.get("username"), display_name: data.get("display_name"), role: data.get("role") }) });
      setCredentials({ username: created.username, password: created.temporary_password });
      setModal("credentials"); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось создать доступ"); }
    finally { setBusy(null); }
  }

  async function resetPassword(user: PortalUser) {
    if (!window.confirm(`Выдать новый временный пароль для ${user.display_name}? Старый пароль и открытые сеансы перестанут работать.`)) return;
    setBusy(user.id); setError(""); setNotice("");
    try {
      const updated = await api<PortalUser & { temporary_password: string }>(`/portal/admin/users/${user.id}/reset-password`, { method: "POST" });
      setCredentials({ username: updated.username, password: updated.temporary_password });
      setModal("credentials");
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось выдать новый пароль"); }
    finally { setBusy(null); }
  }

  async function createSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy("source"); setError("");
    const data = new FormData(event.currentTarget);
    try {
      const created = await api<LeadInboundSource & { webhook_token: string; webhook_path: string }>("/portal/admin/lead-sources", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspaceId, name: data.get("name"), auto_assign: data.get("auto_assign") === "on" }) });
      setWebhook({ url: `${window.location.origin}${created.webhook_path}`, token: created.webhook_token });
      setModal("webhook"); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось создать источник"); }
    finally { setBusy(null); }
  }

  async function revokeSource(source: LeadInboundSource) {
    if (!window.confirm(`Отключить источник «${source.name}»? Его текущая ссылка сразу перестанет принимать лиды.`)) return;
    setBusy(`source-${source.id}`); setError("");
    try { await api(`/portal/admin/lead-sources/${source.id}`, { method: "DELETE" }); setNotice("Приём лидов из источника отключён."); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось отключить источник"); }
    finally { setBusy(null); }
  }

  if (!workspace && !error) return <div className="page"><p className="mutedText">Загружаем компанию…</p></div>;
  if (!workspace) return <div className="page"><Link href="/admin/clients" className="backLink">← Клиенты</Link><p className="notice error">{error || "Компания не найдена"}</p></div>;

  return <div className="page clientDetailPage">
    <header className="topbar"><div className="crumbs"><Link href="/admin/clients">Клиенты</Link><b>/</b><strong>{workspace.name}</strong></div><span className="userAvatar">ЕД</span></header>
    <section className="pageHeader"><div><Link href="/admin/clients" className="backLink">← Все компании</Link><p className="eyebrow">Карточка компании</p><h1>{workspace.name}</h1><p className="subtitle">Команда, роли и выданные доступы собраны в одном месте.</p></div><button className="button primary" onClick={() => setModal("user")}>＋ Добавить сотрудника</button></section>
    {error && <p className="notice error">{error}</p>}{notice && <p className="notice success">{notice}</p>}
    <section className="roleSummary">{roleCounts.map(role => <article key={role.value}><span>{role.label}</span><strong>{role.count}</strong></article>)}</section>
    <section className="panel accessPanel"><div className="panelHead"><div><p className="eyebrow">Пользователи</p><h2>Роли и доступы</h2></div><span className="secureHint">Сотрудников можно добавлять без ограничения</span></div>
      <div className="accessGrid">{users.map(user => { const role = roles.find(item => item.value === user.role) || roles[4]; return <article className={`accessCard ${user.active ? "" : "disabled"}`} key={user.id}>
        <div className="accessIdentity"><span>{user.display_name.slice(0,1).toUpperCase()}</span><div><h3>{user.display_name}</h3><p>Логин: <b>{user.username}</b></p></div><i>{user.active ? "Доступ открыт" : "Отключён"}</i></div>
        <div className="accountCredentials"><div><span>Адрес входа</span><strong>localhost:3000/portal/login</strong></div><div><span>Пароль</span><strong>{user.must_change_password ? "Выдан временный пароль" : "Установлен пользователем"}</strong></div></div>
        <label>Роль<select value={user.role} disabled={busy === user.id} onChange={event => updateUser(user, { role: event.target.value as PortalRole })}>{roles.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <div className="permissionList"><strong>Что доступно</strong>{role.permissions.map(permission => <span key={permission}>✓ {permission}</span>)}</div>
        <button className="credentialButton" disabled={busy === user.id} onClick={() => resetPassword(user)}>{busy === user.id ? "Создаём данные…" : "Получить логин и новый пароль"}</button>
        <button className={user.active ? "accessToggle danger" : "accessToggle"} disabled={busy === user.id} onClick={() => updateUser(user, { active: !user.active })}>{busy === user.id ? "Сохраняем…" : user.active ? "Отключить доступ" : "Включить доступ"}</button>
      </article>})}{!users.length && <div className="empty accessEmpty"><span>00</span><h3>В компании пока никого нет</h3><p>Добавьте собственника, руководителя, менеджеров и маркетологов — каждому можно назначить свою роль.</p><button className="button primary" onClick={() => setModal("user")}>Добавить первого сотрудника</button></div>}</div>
    </section>
    <section className="panel leadIntakePanel"><div className="panelHead"><div><p className="eyebrow">Интеграции CRM</p><h2>Автоматический приём лидов</h2></div><button className="button primary" onClick={() => setModal("source")}>＋ Создать webhook</button></div><p className="portalMuted">Подключите форму сайта, квиз или рекламный сервис. Новые обращения попадут в CRM, дубли будут остановлены, а менеджеры назначены по очереди.</p><div className="sourceList">{sources.map(source => <article key={source.id}><span className={`sourceState ${source.active ? "active" : ""}`}></span><div><strong>{source.name}</strong><small>Ключ: {source.token_prefix}•••• · {source.auto_assign ? "автораспределение включено" : "без назначения"}</small></div><b>{source.active ? "Принимает лиды" : "Отключён"}</b>{source.active && <button disabled={busy === `source-${source.id}`} onClick={() => revokeSource(source)}>Отключить</button>}</article>)}{!sources.length && <div className="sourceEmpty">Webhook ещё не создан. Создайте его, чтобы подключить первый источник лидов.</div>}</div></section>
    {modal === "user" && <div className="modalBackdrop"><form className="modal" onSubmit={addUser}><div className="modalHead"><div><p className="eyebrow">{workspace.name}</p><h2>Новый сотрудник</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div><label>Имя и фамилия<input name="display_name" required minLength={2} autoFocus /></label><label>Логин<input name="username" required minLength={3} autoComplete="off" /></label><label>Роль<select name="role" defaultValue="sales_manager">{roles.map(role => <option value={role.value} key={role.value}>{role.label}</option>)}</select></label><p className="formHint">После создания вы увидите временный пароль. Позже роль и доступ можно менять прямо в карточке сотрудника.</p><button className="button primary full" disabled={busy === "user"}>{busy ? "Создаём…" : "Создать доступ"}</button></form></div>}
    {modal === "credentials" && credentials && <div className="modalBackdrop"><div className="modal"><div className="modalHead"><div><p className="eyebrow">Сохраните данные</p><h2>Данные для входа</h2></div><button className="close" onClick={() => setModal(null)}>×</button></div><div className="credentialBox"><span>Адрес</span><strong>http://localhost:3000/portal/login</strong><span>Логин</span><strong>{credentials.username}</strong><span>Временный пароль</span><strong>{credentials.password}</strong></div><p className="formHint">Пароль показывается только сейчас. Позже можно выдать новый временный пароль из карточки сотрудника.</p><div className="credentialActions"><button className="button secondary" onClick={() => setModal("user")}>Добавить ещё сотрудника</button><button className="button primary" onClick={() => setModal(null)}>Готово</button></div></div></div>}
    {modal === "source" && <div className="modalBackdrop"><form className="modal" onSubmit={createSource}><div className="modalHead"><div><p className="eyebrow">{workspace.name}</p><h2>Новый источник лидов</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div><label>Название источника<input name="name" required minLength={2} placeholder="Например, форма на основном сайте" autoFocus /></label><label className="checkLine"><input name="auto_assign" type="checkbox" defaultChecked /><span>Автоматически распределять лиды между менеджерами</span></label><p className="formHint">После создания портал один раз покажет секретную ссылку. Её нельзя публиковать или передавать посторонним.</p><button className="button primary full" disabled={busy === "source"}>{busy ? "Создаём…" : "Создать защищённый webhook"}</button></form></div>}
    {modal === "webhook" && webhook && <div className="modalBackdrop"><div className="modal webhookModal"><div className="modalHead"><div><p className="eyebrow">Показывается один раз</p><h2>Webhook готов</h2></div><button className="close" onClick={() => setModal(null)}>×</button></div><div className="credentialBox"><span>Адрес для POST-запроса</span><strong>{webhook.url}</strong><span>Секретный ключ</span><strong>{webhook.token}</strong></div><button className="button secondary full copyWebhook" onClick={() => navigator.clipboard.writeText(webhook.url)}>Скопировать адрес</button><div className="webhookExample"><span>Пример JSON со сквозной аналитикой</span><pre>{`{\n  "external_id": "form-123",\n  "full_name": "Иван Петров",\n  "phone": "+7 900 000-00-00",\n  "email": "client@example.ru",\n  "source": "Форма сайта",\n  "external_campaign_id": "123456789",\n  "external_ad_id": "987654321",\n  "utm_source": "yandex",\n  "utm_medium": "cpc",\n  "utm_campaign": "search_brand",\n  "utm_content": "banner_1",\n  "utm_term": "автоматизация продаж",\n  "landing_url": "https://site.ru/landing"\n}`}</pre></div><p className="formHint">Повторный external_id, телефон или email не создаст второй лид. Передавайте ID рекламной кампании и UTM-метки — портал автоматически свяжет заявку с гипотезой.</p><button className="button primary full" onClick={() => setModal(null)}>Готово</button></div></div>}
  </div>;
}
