"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, ClientWorkspace, PortalRole, PortalUser } from "@/lib/api";

const roles: { value: PortalRole; label: string }[] = [
  { value: "client_owner", label: "Собственник" }, { value: "sales_head", label: "Руководитель продаж" },
  { value: "sales_manager", label: "Менеджер продаж" }, { value: "client_marketer", label: "Маркетолог клиента" },
  { value: "viewer", label: "Наблюдатель" },
];

type Health = { workspace_id: number; score: number; status: "good" | "warning" | "risk"; reasons: string[]; leads_7d: number;
  median_response_min: number | null; unanswered_7d: number; open_without_task: number; sales_30d: number };

export default function ClientsPage() {
  const [workspaces, setWorkspaces] = useState<ClientWorkspace[]>([]);
  const [health, setHealth] = useState<Health[]>([]);
  const [users, setUsers] = useState<PortalUser[]>([]);
  const [modal, setModal] = useState<"client" | "user" | "credentials" | null>(null);
  const [credentials, setCredentials] = useState<{ username: string; password: string } | null>(null);
  const [portalLoginUrl, setPortalLoginUrl] = useState("/portal/login");
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const [deleted, setDeleted] = useState<ClientWorkspace[]>([]);
  const [toDelete, setToDelete] = useState<ClientWorkspace | null>(null);
  const [confirmName, setConfirmName] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { setPortalLoginUrl(`${window.location.origin}/portal/login`); }, []);
  const load = useCallback(async () => { try { const [w, u] = await Promise.all([api<ClientWorkspace[]>("/marketing/workspaces"), api<PortalUser[]>("/portal/admin/users")]); setWorkspaces(w); setUsers(u);
    api<Health[]>("/portal/admin/health").then(setHealth).catch(() => setHealth([]));
    api<ClientWorkspace[]>("/marketing/workspaces?deleted=true").then(setDeleted).catch(() => setDeleted([])); setError(""); } catch (e) { setError(e instanceof Error ? e.message : "Не удалось загрузить клиентов"); } }, []);
  useEffect(() => { load(); }, [load]);
  async function addClient(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setBusy(true); const data = new FormData(event.currentTarget); try { await api("/marketing/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: data.get("name") }) }); setModal(null); await load(); } catch (e) { setError(e instanceof Error ? e.message : "Ошибка"); } finally { setBusy(false); } }
  async function createDemo() { setBusy(true); setError("");
    try { const d = await api<{ name: string; username: string; password: string; deals: number }>("/marketing/workspaces/demo", { method: "POST", timeoutMs: 180_000 });
      setCredentials({ username: d.username, password: d.password }); setNotice(`Создан «${d.name}»: ${d.deals} сделок за 4 месяца, реклама, звонки и переписка. Данные вымышленные.`); setModal("credentials"); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось создать демо"); } finally { setBusy(false); } }
  async function removeClient(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!toDelete) return; setBusy(true);
    try { await api(`/marketing/workspaces/${toDelete.id}/delete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm_name: confirmName }) });
      setNotice(`Компания «${toDelete.name}» удалена. Её можно восстановить внизу страницы.`); setToDelete(null); setConfirmName(""); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Ошибка"); } finally { setBusy(false); } }
  async function restoreClient(workspace: ClientWorkspace) { setBusy(true);
    try { await api(`/marketing/workspaces/${workspace.id}/restore`, { method: "POST" }); setNotice("Компания восстановлена: доступы сотрудников и подключения включены снова, сотрудникам нужно войти заново."); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Ошибка"); } finally { setBusy(false); } }
  async function addUser(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setBusy(true); const data = new FormData(event.currentTarget); try { const created = await api<PortalUser & { temporary_password: string }>("/portal/admin/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: Number(data.get("workspace_id")), username: data.get("username"), display_name: data.get("display_name"), role: data.get("role") }) }); setCredentials({ username: created.username, password: created.temporary_password }); setModal("credentials"); await load(); } catch (e) { setError(e instanceof Error ? e.message : "Ошибка"); } finally { setBusy(false); } }
  return <div className="page clientsPage"><header className="topbar"><div className="crumbs"><span>StepToLead</span><b>/</b><strong>Клиенты и доступы</strong></div><span className="userAvatar">ЕД</span></header>
    <section className="pageHeader"><div><p className="eyebrow">Мультитенантный портал</p><h1>Клиенты и команда</h1><p className="subtitle">Создавайте изолированные кабинеты и выдавайте доступы по ролям.</p></div><div className="pageHeaderActions"><button className="button secondary" disabled={busy} onClick={createDemo} title="Компания с вымышленными данными для показа на встречах">{busy ? "Создаём…" : "＋ Демо-портал"}</button><button className="button secondary" onClick={() => setModal("client")}>＋ Компания</button><button className="button primary" disabled={!workspaces.length} onClick={() => setModal("user")}>＋ Пользователь</button></div></section>
    {error && <p className="notice error">{error}</p>}{notice && <p className="notice success">{notice}</p>}
    <div className="clientWorkspaceGrid">{workspaces.map(workspace => { const members = users.filter(u => u.workspace_id === workspace.id); return <Link href={`/admin/clients/${workspace.id}`} className="panel clientWorkspace clickable" key={workspace.id}><div className="clientWorkspaceHead"><span>{workspace.name.slice(0,2).toUpperCase()}</span><div><h2>{workspace.name}</h2><p>{members.length} пользователей · {workspace.name.startsWith("Демо · ") ? "демо-данные для показа" : "кабинет активен"}</p></div><i>Открыть →</i><button type="button" className="clientDelete" title="Удалить компанию" onClick={e => { e.preventDefault(); e.stopPropagation(); setToDelete(workspace); setConfirmName(""); }}>Удалить</button></div>{(() => { const h = health.find(x => x.workspace_id === workspace.id); return h ? <div className={`clientHealth ${h.status}`} title={h.reasons.join("\n") || "Всё в порядке"}>
      <b>{h.score}</b><div><strong>{h.status === "good" ? "Клиент в порядке" : h.status === "warning" ? "Нужно внимание" : "Риск ухода"}</strong>
      <small>{h.reasons[0] || `Заявок за неделю: ${h.leads_7d} · ответ ${h.median_response_min == null ? "—" : `${h.median_response_min} мин`} · продаж за месяц: ${h.sales_30d}`}</small></div></div> : null; })()}<div className="memberList">{members.slice(0,3).map(user => <div className="memberRow" key={user.id}><span>{user.display_name.slice(0,1)}</span><div><strong>{user.display_name}</strong><small>{user.username} · {user.role_name}</small></div><b>{user.active ? "Доступ открыт" : "Отключён"}</b></div>)}{members.length > 3 && <p className="moreMembers">Ещё сотрудников: {members.length - 3}</p>}{!members.length && <p className="mutedText">Пока нет пользователей. Откройте компанию и добавьте команду.</p>}</div></Link>; })}{!workspaces.length && <div className="empty"><span>01</span><h3>Создайте первого клиента</h3><p>У компании появится отдельный кабинет и собственная команда.</p></div>}</div>
    {deleted.length > 0 && <section className="panel deletedClients"><div className="panelHead"><div><p className="eyebrow">Удалённые компании</p><h2>Можно восстановить</h2></div></div>
      {deleted.map(w => <div className="deletedClientRow" key={w.id}><strong>{w.name.split(" · удалена #")[0]}</strong><span>доступы и подключения выключены, история сохранена</span>
        <button className="button secondary" disabled={busy} onClick={() => restoreClient(w)}>Восстановить</button></div>)}</section>}
    {toDelete && <div className="modalBackdrop"><form className="modal" onSubmit={removeClient}><div className="modalHead"><div><p className="eyebrow">Удаление компании</p><h2>Удалить «{toDelete.name}»?</h2></div><button type="button" className="close" onClick={() => setToDelete(null)}>×</button></div>
      <p className="formHint">Компания исчезнет из портала. Сотрудники больше не смогут войти, рекламные кабинеты, чаты, телефония, вебхуки и виджеты отключатся, уведомления и автоматизации остановятся. История сделок, звонков и продаж сохранится — компанию можно восстановить.</p>
      <label>Чтобы подтвердить, введите название компании<input value={confirmName} onChange={e => setConfirmName(e.target.value)} placeholder={toDelete.name} autoFocus autoComplete="off"/></label>
      <button className="button danger full" disabled={busy || confirmName.trim().toLowerCase() !== toDelete.name.trim().toLowerCase()}>{busy ? "Удаляем…" : "Удалить компанию"}</button></form></div>}
    {modal === "client" && <div className="modalBackdrop"><form className="modal" onSubmit={addClient}><div className="modalHead"><div><p className="eyebrow">Новый клиент</p><h2>Создать кабинет</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div><label>Название компании<input name="name" required minLength={2} autoFocus /></label><button className="button primary full" disabled={busy}>Создать</button></form></div>}
    {modal === "user" && <div className="modalBackdrop"><form className="modal" onSubmit={addUser}><div className="modalHead"><div><p className="eyebrow">Доступ в портал</p><h2>Добавить пользователя</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div><label>Компания<select name="workspace_id" required defaultValue=""><option value="" disabled>Выберите компанию</option>{workspaces.map(w => <option value={w.id} key={w.id}>{w.name}</option>)}</select></label><label>Имя и фамилия<input name="display_name" required minLength={2} /></label><label>Логин<input name="username" required minLength={3} autoComplete="off" /></label><label>Роль<select name="role" defaultValue="client_owner">{roles.map(r => <option value={r.value} key={r.value}>{r.label}</option>)}</select></label><button className="button primary full" disabled={busy}>Создать доступ</button></form></div>}
    {modal === "credentials" && credentials && <div className="modalBackdrop"><div className="modal"><div className="modalHead"><div><p className="eyebrow">Показывается один раз</p><h2>Доступ создан</h2></div><button className="close" onClick={() => setModal(null)}>×</button></div><div className="credentialBox"><span>Адрес</span><strong><a href={portalLoginUrl} target="_blank" rel="noopener noreferrer">{portalLoginUrl}</a></strong><span>Логин</span><strong>{credentials.username}</strong><span>Временный пароль</span><strong>{credentials.password}</strong></div><p className="formHint">Передайте данные пользователю безопасным способом. После первого входа пароль следует заменить.</p><button className="button primary full" onClick={() => setModal(null)}>Готово</button></div></div>}
  </div>;
}
