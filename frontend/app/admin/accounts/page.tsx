"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Proxy, TelegramAccount } from "@/lib/api";

type ImportItem = {
  filename: string;
  status: "imported" | "skipped" | "error";
  account_id?: number | null;
  phone?: string | null;
  error?: string | null;
};

type ImportResult = {
  imported: number;
  skipped: number;
  failed: number;
  items: ImportItem[];
};

const statusLabels: Record<string, string> = {
  online: "Онлайн",
  offline: "Не в сети",
  checking: "Проверяем",
  disconnected: "Отключён",
  connecting: "Подключаем",
  code_sent: "Ожидает код",
  password_required: "Нужен 2FA",
  flood_wait: "Ожидание",
  error: "Ошибка",
};

export default function TelegramAccountsPage() {
  const [accounts, setAccounts] = useState<TelegramAccount[]>([]);
  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [configured, setConfigured] = useState(true);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importFiles, setImportFiles] = useState<File[]>([]);
  const [importProxy, setImportProxy] = useState("");
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [authOpen, setAuthOpen] = useState(false);
  const [authStep, setAuthStep] = useState<"phone" | "code" | "password">("phone");
  const [authAccountId, setAuthAccountId] = useState<number | null>(null);
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [authProxy, setAuthProxy] = useState("");
  const [profile, setProfile] = useState<TelegramAccount | null>(null);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [bio, setBio] = useState("");
  const [username, setUsername] = useState("");
  const [avatar, setAvatar] = useState<File | null>(null);
  const [usernameState, setUsernameState] = useState("");

  const load = useCallback(async () => {
    try {
      const [accountData, proxyData, config] = await Promise.all([
        api<TelegramAccount[]>("/telegram-accounts"),
        api<Proxy[]>("/proxies"),
        api<{ configured: boolean }>("/telegram-accounts/config"),
      ]);
      setAccounts(accountData);
      setProxies(proxyData);
      setConfigured(config.configured);
    } catch (error) {
      setNotice((error as Error).message);
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
        if (payload.type === "snapshot") setAccounts(payload.accounts);
        if (payload.type === "account_status") {
          setAccounts((current) => current.map((item) => item.id === payload.account.id ? payload.account : item));
        }
      };
      socket.onclose = () => { if (!stopped) retry = setTimeout(connect, 3000); };
    }
    connect();
    return () => { stopped = true; if (retry) clearTimeout(retry); socket?.close(); };
  }, []);

  function closeImport() {
    if (busy) return;
    setImportOpen(false);
    setImportFiles([]);
    setImportProxy("");
    setImportResult(null);
  }

  async function importTData(event: FormEvent) {
    event.preventDefault();
    if (!importFiles.length) return;
    setBusy(true);
    setNotice("");
    setImportResult(null);
    const data = new FormData();
    importFiles.forEach((file) => data.append("files", file));
    if (importProxy) data.append("proxy_id", importProxy);
    try {
      const result = await api<ImportResult>("/telegram-accounts/import-tdata", {
        method: "POST",
        body: data,

        // На каждый архив backend получает отдельное окно для подключения к Telegram.
        timeoutMs: Math.max(90_000, importFiles.length * 65_000),
      });
      setImportResult(result);
      await load();
    } catch (error) {
      setNotice((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function resetAuth() {
    setAuthStep("phone"); setAuthAccountId(null); setPhone(""); setCode(""); setPassword(""); setAuthProxy("");
  }
  function closeAuth() { setAuthOpen(false); resetAuth(); }

  async function submitAuth(event: FormEvent) {
    event.preventDefault(); setBusy(true); setNotice("");
    try {
      if (authStep === "phone") {
        const account = await api<TelegramAccount>("/telegram-accounts/auth/request-code", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phone, proxy_id: authProxy ? Number(authProxy) : null }),
        });
        setAuthAccountId(account.id); setAuthStep("code"); setNotice("Код отправлен в Telegram");
      } else {
        const result = await api<{ account: TelegramAccount; requires_2fa: boolean }>("/telegram-accounts/auth/confirm", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ account_id: authAccountId, code: authStep === "code" ? code : null, password: authStep === "password" ? password : null }),
        });
        if (result.requires_2fa) { setAuthStep("password"); setNotice("На аккаунте включена двухэтапная аутентификация"); }
        else { closeAuth(); setNotice("Telegram-аккаунт подключён"); await load(); }
      }
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  }

  async function assignProxy(accountId: number, proxyId: string) {
    try {
      const updated = await api<TelegramAccount>(`/telegram-accounts/${accountId}/proxy`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proxy_id: proxyId ? Number(proxyId) : null }),
      });
      setAccounts((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (error) { setNotice((error as Error).message); }
  }

  async function sync(accountId: number) {
    setNotice("Синхронизируем профиль…");
    try {
      const updated = await api<TelegramAccount>(`/telegram-accounts/${accountId}/sync`, { method: "POST" });
      setAccounts((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice("Данные обновлены из Telegram");
    } catch (error) { setNotice((error as Error).message); }
  }

  function openProfile(account: TelegramAccount) {
    setProfile(account); setFirstName(account.first_name || ""); setLastName(account.last_name || "");
    setBio(account.bio || ""); setUsername(account.username || ""); setAvatar(null); setUsernameState("");
  }

  async function saveProfile(event: FormEvent) {
    event.preventDefault(); if (!profile) return; setBusy(true); setNotice("");
    const data = new FormData(); data.append("first_name", firstName); data.append("last_name", lastName);
    data.append("bio", bio); data.append("username", username.replace("@", "")); if (avatar) data.append("avatar", avatar);
    try {
      const updated = await api<TelegramAccount>(`/telegram-accounts/${profile.id}/profile`, { method: "PATCH", body: data });
      setAccounts((current) => current.map((item) => item.id === updated.id ? updated : item));
      setProfile(null); setNotice("Профиль синхронизирован с Telegram");
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  }

  async function checkName() {
    if (!profile || !username) return; setUsernameState("Проверяем…");
    try {
      const result = await api<{ username: string; available: boolean }>(`/telegram-accounts/${profile.id}/username/check`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: username.replace("@", "") }),
      });
      setUsernameState(result.available ? "Свободен" : "Занят");
    } catch (error) { setUsernameState((error as Error).message); }
  }

  async function suggestName() {
    if (!profile) return; setUsernameState("Ищем свободный…");
    try {
      const result = await api<{ username: string; available: boolean }>(`/telegram-accounts/${profile.id}/username/suggest`, { method: "POST" });
      setUsername(result.username); setUsernameState("Свободен");
    } catch (error) { setUsernameState((error as Error).message); }
  }

  async function removeAccount(account: TelegramAccount) {
    if (!window.confirm(`Удалить ${account.phone} из Reachboard? Локальная Telegram-сессия также будет удалена.`)) return;
    try { await api(`/telegram-accounts/${account.id}`, { method: "DELETE" }); await load(); }
    catch (error) { setNotice((error as Error).message); }
  }

  const onlineCount = accounts.filter((item) => item.status === "online").length;

  return <div className="page">
    <header className="pageHeader"><div><p className="eyebrow">Подключения</p><h1>Telegram-аккаунты</h1><p className="subtitle">Импорт tdata, прокси и состояние сессий в реальном времени.</p></div><div className="pageHeaderActions"><button className="button primary" onClick={() => setImportOpen(true)} disabled={!configured}>Импорт tdata</button></div></header>
    {!configured && <div className="configBanner"><span>!</span><div><strong>Хранилище сессий ещё не настроено</strong><p>Задайте уникальный ENCRYPTION_SECRET в .env и перезапустите backend.</p></div></div>}
    {notice && <div className="notice">{notice}<button onClick={() => setNotice("")}>×</button></div>}
    <section className="accountSummary"><article><span className="summaryIcon lime">●</span><div><small>Онлайн</small><strong>{onlineCount}</strong></div></article><article><span className="summaryIcon violet">A</span><div><small>Всего аккаунтов</small><strong>{accounts.length}</strong></div></article><article><span className="summaryIcon blue">P</span><div><small>С прокси</small><strong>{accounts.filter((item) => item.proxy_id).length}</strong></div></article><div className="realtimeLabel"><i />Live monitoring</div></section>
    <section className="accountsGrid">{accounts.map((account) => <article className="accountCard" key={account.id}><div className="accountCardTop"><div className="accountIdentity"><span className="accountAvatar">{(account.first_name || account.phone).slice(0, 1).toUpperCase()}<i className={`presence ${account.status}`} /></span><div><strong>{[account.first_name, account.last_name].filter(Boolean).join(" ") || "Новый аккаунт"}</strong><small>{account.username ? `@${account.username}` : account.phone}</small></div></div><button className="moreButton" onClick={() => removeAccount(account)} aria-label="Удалить аккаунт">•••</button></div><div className="accountState"><span className={`liveStatus ${account.status}`}><i />{statusLabels[account.status] || account.status}</span><small>{account.last_seen_at ? `Проверен ${new Date(account.last_seen_at).toLocaleTimeString("ru", { hour: "2-digit", minute: "2-digit" })}` : "Ожидает проверки"}</small></div>{account.last_error && <p className="accountError">{account.last_error}</p>}<div className="accountProfile"><div><span>Имя</span><strong>{account.first_name || "—"}</strong></div><div><span>Username</span><strong>{account.username ? `@${account.username}` : "Не задан"}</strong></div><div className="bioRow"><span>Bio</span><strong>{account.bio || "Не заполнено"}</strong></div></div><label className="proxySelectLabel">Прокси<select value={account.proxy_id || ""} onChange={(event) => assignProxy(account.id, event.target.value)}><option value="">Без прокси</option>{proxies.map((item) => <option key={item.id} value={item.id}>{item.host}:{item.port} · {item.scheme}</option>)}</select></label><div className="accountActions"><button onClick={() => openProfile(account)}>Изменить профиль</button><button onClick={() => sync(account.id)}>↻ Синхронизировать</button></div></article>)}
      {!accounts.length && <div className="empty accountEmpty"><span>04</span><h3>Подключённых аккаунтов нет</h3><p>Импортируйте ZIP-архив с папкой tdata.</p><button className="button primary" onClick={() => setImportOpen(true)} disabled={!configured}>Импортировать tdata</button></div>}
    </section>

    {importOpen && <div className="modalBackdrop" onMouseDown={closeImport}><form className="modal tdataModal" onSubmit={importTData} onMouseDown={(event) => event.stopPropagation()}><div className="modalHead"><div><p className="eyebrow">Массовая загрузка</p><h2>Импорт tdata</h2></div><button type="button" className="close" onClick={closeImport}>×</button></div><p className="formHint">Выберите до 50 ZIP-архивов. Каждый архив должен содержать папку tdata одного аккаунта и быть не больше 100 МБ.</p>{!importResult && <><label className="proxySelectLabel">Подключение к Telegram<select value={importProxy} onChange={(event) => setImportProxy(event.target.value)}><option value="">Без прокси</option>{proxies.map((item) => <option key={item.id} value={item.id}>{item.label ? `${item.label} · ` : ""}{item.host}:{item.port} · {item.scheme}</option>)}</select></label>{!proxies.length && <p className="formHint">Прокси пока не добавлены. Добавьте SOCKS5/HTTP-прокси во вкладке «Прокси», если Telegram недоступен напрямую.</p>}<label className="tdataDrop"><input type="file" accept=".zip,application/zip" multiple onChange={(event) => setImportFiles(Array.from(event.target.files || []).slice(0, 50))} /><span className="tdataDropIcon">ZIP</span><strong>{importFiles.length ? "Выбрать другие архивы" : "Выбрать ZIP-архивы"}</strong><small>До 50 аккаунтов за одну загрузку</small></label>{!!importFiles.length && <div className="tdataFileList">{importFiles.map((file) => <span key={`${file.name}-${file.lastModified}`}><b>{file.name}</b><small>{(file.size / 1024 / 1024).toFixed(1)} МБ</small></span>)}</div>}</>}{importResult && <div className="tdataResults"><div className="tdataTotals"><span>Добавлено <b>{importResult.imported}</b></span><span>Пропущено <b>{importResult.skipped}</b></span><span>Ошибок <b>{importResult.failed}</b></span></div>{importResult.items.map((item) => <div className={`tdataResultRow ${item.status}`} key={item.filename}><i>{item.status === "imported" ? "✓" : item.status === "skipped" ? "–" : "!"}</i><div><strong>{item.filename}</strong><small>{item.phone || item.error || "Аккаунт уже добавлен"}</small></div></div>)}</div>}<div className="modalActions">{importResult ? <button type="button" className="button primary" onClick={closeImport}>Готово</button> : <><button type="button" className="button secondary" onClick={closeImport}>Отмена</button><button className="button primary" disabled={busy || !importFiles.length}>{busy ? "Импортируем…" : `Импортировать${importFiles.length ? ` · ${importFiles.length}` : ""}`}</button></>}</div></form></div>}

    {profile && <div className="modalBackdrop" onMouseDown={() => setProfile(null)}><form className="modal profileModal" onSubmit={saveProfile} onMouseDown={(event) => event.stopPropagation()}><div className="modalHead"><div><p className="eyebrow">Синхронизация с Telegram</p><h2>Профиль аккаунта</h2></div><button type="button" className="close" onClick={() => setProfile(null)}>×</button></div><div className="profileFields"><label>Имя<input required value={firstName} onChange={(event) => setFirstName(event.target.value)} /></label><label>Фамилия<input value={lastName} onChange={(event) => setLastName(event.target.value)} /></label></div><label>Username<div className="usernameInput"><span>@</span><input value={username} onChange={(event) => { setUsername(event.target.value.replace(/[^A-Za-z0-9_]/g, "")); setUsernameState(""); }} /></div></label><div className="usernameTools"><button type="button" onClick={suggestName}>Сгенерировать свободный</button><button type="button" onClick={checkName}>Проверить</button>{usernameState && <span className={usernameState === "Свободен" ? "available" : ""}>{usernameState}</span>}</div><label>Bio<textarea maxLength={255} value={bio} onChange={(event) => setBio(event.target.value)} /><small className="counter">{bio.length}/255</small></label><label className="avatarUpload">Аватар<input type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => setAvatar(event.target.files?.[0] || null)} /><span>{avatar ? avatar.name : "Выбрать JPG, PNG или WEBP"}</span></label><button className="button primary full" disabled={busy}>{busy ? "Синхронизируем…" : "Сохранить в Telegram"}</button></form></div>}
  </div>;
}
