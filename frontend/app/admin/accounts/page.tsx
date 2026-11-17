"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Batch, HealthLevel, HealthSummary, InfraAccount, InfraProxy } from "../telegram-types";
import { accountStatuses, HealthBadge, healthLabels, InfraProgress, isFreeProxy, json,
  proxyStatuses, Stats, toggleId, useInfraTask } from "../telegram-infra";
import { accountName, AvatarDialog, ImportDialog } from "./batch-dialogs";
import css from "../telegram-infra.module.css";
import { MoveToClient } from "../move-to-client";

/* Синхронизация и живые статусы (WebSocket) приходят без посчитанного здоровья и без данных прокси —
   не затираем ими то, что уже показано, иначе экран падал на health.reasons. */
function mergeAccount(current: InfraAccount | undefined, update: InfraAccount): InfraAccount {
  const merged = { ...current, ...update } as InfraAccount;
  const health = update.health as InfraAccount["health"] | null | undefined;
  if (!health || !Array.isArray(health.reasons)) merged.health = current?.health as InfraAccount["health"];
  if (update.proxy == null && current?.proxy && update.proxy_id === current.proxy_id) merged.proxy = current.proxy;
  return merged;
}

export default function TelegramAccountsPage() {
  const [accounts, setAccounts] = useState<InfraAccount[]>([]);
  const [proxies, setProxies] = useState<InfraProxy[]>([]);
  const [configured, setConfigured] = useState(true);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [avatarsOpen, setAvatarsOpen] = useState(false);
  const [summary, setSummary] = useState<HealthSummary | null>(null);
  const [health, setHealth] = useState<HealthLevel | "">("");
  const [status, setStatus] = useState("");
  const [withoutProxy, setWithoutProxy] = useState(false);
  const [checked, setChecked] = useState<number[]>([]);
  const job = useInfraTask();
  const [profile, setProfile] = useState<InfraAccount | null>(null);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [bio, setBio] = useState("");
  const [username, setUsername] = useState("");
  const [avatar, setAvatar] = useState<File | null>(null);
  const [usernameState, setUsernameState] = useState("");

  const load = useCallback(async () => {
    try {
      const [accountData, proxyData, config, healthData] = await Promise.all([
        api<InfraAccount[]>("/telegram-accounts"),
        api<InfraProxy[]>("/proxies"),
        api<{ configured: boolean }>("/telegram-accounts/config"),
        api<HealthSummary>("/telegram-accounts/health-summary").catch(() => null),
      ]);
      setAccounts(accountData);
      setProxies(proxyData);
      setConfigured(config.configured);
      setSummary(healthData);
    } catch (error) {
      setNotice("Не удалось выполнить операцию с аккаунтами.");
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    function connect() {
      socket = new WebSocket(`${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/api/telegram-accounts/ws/status`);
      socket.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.type !== "snapshot" && payload.type !== "account_status") return;
        const updates: InfraAccount[] = payload.type === "snapshot" ? payload.accounts : [payload.account];
        setAccounts(current => {
          const byId = new Map(current.map(account => [account.id, account]));
          // Сокет общий на весь портал: обновляем только аккаунты, которые уже на экране (этого клиента).
          const known = new Map(updates.filter(account => byId.has(account.id)).map(account => [account.id, account]));
          return current.map(item => known.has(item.id) ? mergeAccount(item, known.get(item.id)!) : item);
        });
      };
      socket.onclose = () => { if (!stopped) retry = setTimeout(connect, 3000); };
    }
    connect();
    return () => { stopped = true; if (retry) clearTimeout(retry); socket?.close(); };
  }, []);

  async function replaceProxy(accountId: number, proxyId: string) {
    if (busy || !window.confirm("Сменить IP? Telegram может запросить повторный вход.")) return;
    setBusy(true);
    setNotice("");
    try {
      await json<InfraAccount>(`/telegram-accounts/${accountId}/replace-proxy`, {
        proxy_id: proxyId ? Number(proxyId) : null, confirm_ip_change: true,
      });
      await load();
    } catch { setNotice("Не удалось заменить прокси. Проверьте наличие свободного рабочего прокси."); }
    finally { setBusy(false); }
  }

  async function mass(action: "spamcheck" | "assign-proxies", ids: number[]) {
    if (busy || job.task?.active || !ids.length) return;
    setBusy(true);
    setNotice("");
    try {
      if (action === "spamcheck") {
        const batch = await json<Batch>("/telegram-accounts/spamcheck", { ids });
        job.start(`/telegram-accounts/spamcheck/${batch.batch_id}/progress`);
      } else {
        const result = await json<{ assigned: number; still_waiting: number }>("/telegram-accounts/assign-proxies", { ids });
        setNotice(`Назначено: ${result.assigned}. Продолжают ждать: ${result.still_waiting}.`);
        await load();
      }
    } catch { setNotice("Не удалось выполнить массовое действие."); }
    finally { setBusy(false); }
  }

  async function sync(accountId: number) {
    setNotice("Синхронизируем профиль…");
    try {
      const updated = await api<InfraAccount>(`/telegram-accounts/${accountId}/sync`, { method: "POST" });
      setAccounts((current) => current.map((item) => item.id === updated.id ? mergeAccount(item, updated) : item));
      setNotice("Данные обновлены из Telegram");
    } catch (error) { setNotice("Не удалось выполнить операцию с аккаунтами."); }
  }

  function openProfile(account: InfraAccount) {
    setProfile(account); setFirstName(account.first_name || ""); setLastName(account.last_name || "");
    setBio(account.bio || ""); setUsername(account.username || ""); setAvatar(null); setUsernameState("");
  }

  async function saveProfile(event: FormEvent) {
    event.preventDefault(); if (!profile) return; setBusy(true); setNotice("");
    const data = new FormData(); data.append("first_name", firstName); data.append("last_name", lastName);
    data.append("bio", bio); data.append("username", username.replace("@", "")); if (avatar) data.append("avatar", avatar);
    try {
      const updated = await api<InfraAccount>(`/telegram-accounts/${profile.id}/profile`, { method: "PATCH", body: data });
      setAccounts((current) => current.map((item) => item.id === updated.id ? mergeAccount(item, updated) : item));
      setProfile(null); setNotice("Профиль синхронизирован с Telegram");
    } catch (error) { setNotice("Не удалось выполнить операцию с аккаунтами."); }
    finally { setBusy(false); }
  }

  async function checkName() {
    if (!profile || !username) return; setUsernameState("Проверяем…");
    try {
      const result = await api<{ username: string; available: boolean }>(`/telegram-accounts/${profile.id}/username/check`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: username.replace("@", "") }),
      });
      setUsernameState(result.available ? "Свободен" : "Занят");
    } catch (error) { setUsernameState("Не удалось проверить имя пользователя."); }
  }

  async function suggestName() {
    if (!profile) return; setUsernameState("Ищем свободный…");
    try {
      const result = await api<{ username: string; available: boolean }>(`/telegram-accounts/${profile.id}/username/suggest`, { method: "POST" });
      setUsername(result.username); setUsernameState("Свободен");
    } catch (error) { setUsernameState("Не удалось проверить имя пользователя."); }
  }

  async function removeAccount(account: InfraAccount) {
    if (!window.confirm(`Удалить аккаунт ${account.phone}? Его подключение к Telegram будет удалено.`)) return;
    try { await api(`/telegram-accounts/${account.id}`, { method: "DELETE" }); await load(); }
    catch (error) { setNotice("Не удалось выполнить операцию с аккаунтами."); }
  }

  const rows = accounts.filter(account =>
    (!health || (account.health?.level ?? "unknown") === health)
    && (!status || account.status === status)
    && (!withoutProxy || account.proxy_id == null));
  const selected = rows.filter(account => checked.includes(account.id));
  const ids = selected.map(account => account.id);
  const waitingIds = selected.filter(account => account.status === "waiting_proxy").map(account => account.id);
  const available = proxies.filter(isFreeProxy);
  const counts = summary?.accounts;
  const locked = busy || !!job.task?.active;
  const changeFilter = () => setChecked([]);

  return <div className={`page ${css.screen}`}>
    <header className="pageHeader">
      <div><p className="eyebrow">Telegram-аутрич</p><h1>Telegram-аккаунты</h1>
        <p className="subtitle">Прокси, здоровье и активность аккаунтов.</p></div>
      <div className="pageHeaderActions">
        <button className="button primary" onClick={() => setImportOpen(true)} disabled={!configured || locked}>
          Импорт tdata
        </button>
        <button className="button secondary" onClick={() => setAvatarsOpen(true)} disabled={!accounts.length || locked}>
          Аватарки
        </button>
      </div>
    </header>
    {!configured && <div className="configBanner">Хранилище сессий ещё не настроено. Импорт недоступен.</div>}
    {notice && <div className="notice" role="status">{notice}
      <button onClick={() => setNotice("")} aria-label="Закрыть уведомление">×</button></div>}
    <Stats values={[
      ["Всего", counts?.total ?? accounts.length], ["В порядке", counts?.ok],
      ["Предупреждения", counts?.warn], ["Не работают", counts?.dead],
      ["Ждут прокси", counts?.waiting_proxy], ["Спам-блок", counts?.spamblock],
      ["Свободные прокси аутрича", summary?.proxies.free],
    ]}/>
    <InfraProgress job={job} onFinish={() => void load()}/>
    <div className={css.toolbar}>
      <label>Здоровье <select value={health} onChange={e => {
        setHealth(e.target.value as HealthLevel | ""); changeFilter();
      }}>
        <option value="">Любое</option>
        {Object.entries(healthLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label>Статус <select value={status} onChange={e => { setStatus(e.target.value); changeFilter(); }}>
        <option value="">Все статусы</option>
        {Object.entries(accountStatuses).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label><input type="checkbox" checked={withoutProxy}
        onChange={e => { setWithoutProxy(e.target.checked); changeFilter(); }}/> Без прокси</label>
    </div>
    <div className={css.toolbar} aria-label="Действия с отмеченными аккаунтами">
      <span>Отмечено: {ids.length}</span>
      <button className="button secondary" disabled={locked || !ids.length}
        onClick={() => void mass("spamcheck", ids)}>Проверить спам-блок</button>
      <button className="button secondary" disabled={locked || !waitingIds.length}
        onClick={() => void mass("assign-proxies", waitingIds)}>Раздать прокси ждущим</button>
      <button className="button secondary" disabled={locked || !ids.length} onClick={() => setAvatarsOpen(true)}>
        Поставить аватарки
      </button>
      <MoveToClient path="/telegram-accounts/move" ids={ids} disabled={locked}
        onDone={message => { setNotice(message); setChecked([]); void load(); }}/>
    </div>
    <section className={`tablePanel ${css.panel}`}>
      <div className={css.scroll} tabIndex={0} aria-label="Telegram-аккаунты">
        <table><thead><tr>
          <th><input type="checkbox" aria-label="Отметить все видимые аккаунты" disabled={locked || !rows.length}
            checked={!!rows.length && ids.length === rows.length}
            onChange={e => setChecked(e.target.checked ? rows.map(account => account.id) : [])}/></th>
          <th>Аккаунт</th><th>Статус</th><th>Здоровье</th><th>Спам-блок</th><th>Отправлено сегодня</th>
          <th>Прокси</th><th>Действия</th>
        </tr></thead><tbody>{rows.map(account => {
          const spam = account.health?.spamblock;
          return <tr key={account.id}>
            <td><input type="checkbox" aria-label={`Отметить аккаунт ${account.phone}`}
              checked={checked.includes(account.id)} disabled={locked}
              onChange={() => setChecked(prev => toggleId(prev, account.id))}/></td>
            <td className={css.who}>{account.avatar_thumb
              ? <img className={css.avatar} src={account.avatar_thumb} alt=""/>
              : <span className={css.avatar} aria-hidden="true">{(accountName(account) || "?").slice(0, 1).toUpperCase()}</span>}
              <span>{accountName(account)}<small>{account.username ? `@${account.username} · ` : ""}{account.phone}</small></span></td>
            <td>{accountStatuses[account.status] ?? "Неизвестно"}</td>
            <td><HealthBadge health={account.health}/></td>
            <td>{spam?.state === "limited" ? "Ограничен" : spam?.state === "none" ? "Нет ограничений" : "Не проверен"}
              {spam?.until && <small>До {new Date(spam.until).toLocaleString("ru-RU")}</small>}</td>
            <td>{account.health?.sent_today ?? "—"}</td>
            <td>{account.proxy ? <>
              {account.proxy.host}:{account.proxy.port} · {proxyStatuses[account.proxy.status] ?? "Неизвестно"}
              <small><HealthBadge level={account.proxy.health_level}/></small>
            </> : account.proxy_id ? `Прокси №${account.proxy_id}` : "Нет прокси"}
              <small><select aria-label={`Заменить прокси аккаунта ${account.phone}`} value="" disabled={locked}
                onChange={e => void replaceProxy(account.id, e.target.value === "auto" ? "" : e.target.value)}>
                <option value="" disabled>Заменить прокси…</option>
                <option value="auto">Назначить автоматически</option>
                {available.filter(item => item.id !== account.proxy_id).map(item =>
                  <option key={item.id} value={item.id}>{item.host}:{item.port} · {item.scheme.toUpperCase()}</option>)}
              </select></small>
            </td>
            <td><div className="accountActions">
              <button disabled={locked || !account.proxy_id} onClick={() => openProfile(account)}>Изменить профиль</button>
              <button disabled={locked || !account.proxy_id} onClick={() => void sync(account.id)}>Синхронизировать</button>
              <button disabled={locked} onClick={() => void removeAccount(account)}>Удалить</button>
            </div></td>
          </tr>;
        })}</tbody></table>
        {!rows.length && <p>{accounts.length ? "Нет аккаунтов по выбранным фильтрам." : "Аккаунтов пока нет."}</p>}
      </div>
    </section>
    {importOpen && <ImportDialog proxies={proxies} free={summary?.proxies.free}
      onChanged={() => void load()} onClose={() => setImportOpen(false)}/>}
    {avatarsOpen && <AvatarDialog accounts={accounts} initialIds={ids}
      onChanged={() => void load()} onClose={() => setAvatarsOpen(false)}/>}

    {profile && <div className="modalBackdrop" onMouseDown={() => setProfile(null)}><form className={`modal profileModal ${css.modal}`} onSubmit={saveProfile} onMouseDown={(event) => event.stopPropagation()}><div className="modalHead"><div><p className="eyebrow">Синхронизация с Telegram</p><h2>Профиль аккаунта</h2></div><button type="button" className="close" onClick={() => setProfile(null)}>×</button></div><div className="profileFields"><label>Имя<input required value={firstName} onChange={(event) => setFirstName(event.target.value)} /></label><label>Фамилия<input value={lastName} onChange={(event) => setLastName(event.target.value)} /></label></div><label>Username<div className="usernameInput"><span>@</span><input value={username} onChange={(event) => { setUsername(event.target.value.replace(/[^A-Za-z0-9_]/g, "")); setUsernameState(""); }} /></div></label><div className="usernameTools"><button type="button" onClick={suggestName}>Сгенерировать свободный</button><button type="button" onClick={checkName}>Проверить</button>{usernameState && <span className={usernameState === "Свободен" ? "available" : ""}>{usernameState}</span>}</div><label>Bio<textarea maxLength={255} value={bio} onChange={(event) => setBio(event.target.value)} /><small className="counter">{bio.length}/255</small></label><label className="avatarUpload">Аватар<input type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => setAvatar(event.target.files?.[0] || null)} /><span>{avatar ? avatar.name : "Выбрать JPG, PNG или WEBP"}</span></label><button className="button primary full" disabled={busy}>{busy ? "Синхронизируем…" : "Сохранить в Telegram"}</button></form></div>}
  </div>;
}
