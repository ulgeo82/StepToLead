"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Batch, InfraProxy, Pool, ProxyBulkResult, ProxyDeleteResult, ProxyScheme } from "../telegram-types";
import { HealthBadge, InfraProgress, isFreeProxy, json, pools, proxyImportError, proxyStatuses, Stats, toggleId,
  useInfraTask } from "../telegram-infra";
import css from "../telegram-infra.module.css";
import { MoveToClient } from "../move-to-client";
import { hasPossiblyPublicProxy, publicProxyWarning } from "./public-proxy-warning";

export default function ProxiesPage() {
  const [items, setItems] = useState<InfraProxy[]>([]);
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState("");
  const [pool, setPool] = useState<Pool>("agency");
  const [filter, setFilter] = useState<Pool | "">("");
  const [scheme, setScheme] = useState<ProxyScheme>("auto");
  const [checked, setChecked] = useState<number[]>([]);
  const [replacing, setReplacing] = useState<InfraProxy | null>(null);
  const [bulk, setBulk] = useState<ProxyBulkResult | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const job = useInfraTask();
  const load = useCallback(() => api<InfraProxy[]>("/proxies").then(setItems)
    .catch(() => setError("Не удалось загрузить прокси.")), []);
  useEffect(() => { void load(); }, [load]);
  const rows = items.filter(item => !filter || item.pool === filter);
  const warnAboutSavedProxies = hasPossiblyPublicProxy(rows);
  const warnAboutImport = hasPossiblyPublicProxy(entries);
  const ids = rows.filter(item => checked.includes(item.id)).map(item => item.id);
  const locked = busy || !!job.task?.active;
  function progress(batch: number) {
    job.start(`/proxies/checks/${batch}/progress`);
  }
  async function addBulk(event: FormEvent) {
    event.preventDefault();
    if (!entries.trim() || locked) return;
    setBusy(true);
    setError("");
    try {
      const result = await json<ProxyBulkResult>("/proxies/bulk", { entries, pool, scheme });
      setBulk({ ...result, errors: result.errors.map(proxyImportError) });
      if (result.batch_id != null) progress(result.batch_id);
      setEntries("");
      setOpen(false);
      await load();
    } catch { setError("Не удалось добавить прокси. Проверьте формат списка."); }
    finally { setBusy(false); }
  }
  async function change(item: InfraProxy, values: Partial<InfraProxy>) {
    setBusy(true);
    try {
      await api(`/proxies/${item.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pool: item.pool, max_accounts: item.max_accounts,
          dedicated_workspace_id: item.dedicated_workspace_id, ...values }),
      });
      await load();
    } catch { setError("Не удалось изменить параметры прокси."); }
    finally { setBusy(false); }
  }
  async function check(selection?: number[]) {
    setBusy(true); setError("");
    try {
      const result = await json<Batch>("/proxies/check", selection ? { ids: selection } : {});
      progress(result.batch_id);
    } catch { setError("Не удалось запустить проверку прокси."); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!ids.length || !window.confirm(`Удалить выбранные прокси (${ids.length})?`)) return;
    setBusy(true); setError("");
    try {
      const result = await json<ProxyDeleteResult>("/proxies/bulk-delete", { ids });
      const skipped = result.skipped.map(item => `№${item.id}`).join(", ");
      setNotice(`Удалено: ${result.deleted}. Пропущено: ${result.skipped.length}${skipped ? ` (${skipped})` : ""}.`);
      setChecked(result.skipped.map(item => item.id));
      await load();
    } catch { setError("Не удалось удалить выбранные прокси."); }
    finally { setBusy(false); }
  }
  async function replace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!replacing || !window.confirm("Сменить IP? Telegram может запросить повторный вход.")) return;
    const form = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      if (replacing.pool === "client") {
        await json(`/proxies/${replacing.id}/replace`, { line: form.get("line"), confirm_ip_change: true }, "PUT");
      } else {
        const proxy = form.get("proxy");
        await json(`/telegram-accounts/${form.get("account")}/replace-proxy`, {
          proxy_id: proxy ? Number(proxy) : null, confirm_ip_change: true,
        });
      }
      setNotice("Прокси заменён.");
      setReplacing(null);
      await load();
    } catch { setError("Не удалось заменить прокси. Проверьте, что есть свободный рабочий прокси."); }
    finally { setBusy(false); }
  }
  async function remap(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!window.confirm("Смена IP может вызвать ограничения Telegram. Продолжить?")) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      await api(`/proxies/profiles/${form.get("channel")}/remap`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proxy_id: Number(form.get("proxy")), confirm_ip_change: true }),
      });
      setNotice("IP переназначен. Можно включить канал.");
      await load();
    } catch { setError("Не удалось переназначить IP. Проверьте, что канал выключен."); }
    finally { setBusy(false); }
  }
  return <div className={`page ${css.screen}`}>
    <header className="pageHeader"><div><p className="eyebrow">Инфраструктура</p><h1>Прокси</h1></div>
      <button className="button primary" disabled={locked} onClick={() => setOpen(true)}>+ Массовое добавление</button></header>
    {notice && <div className="notice" role="status">{notice}</div>}
    {error && <p role="alert">{error}</p>}
    {bulk && <div role="status" className="notice">
      Добавлено: {bulk.imported} · Дубли: {bulk.duplicates} · Ошибки: {bulk.errors.length}
      {!!bulk.errors.length && <ul>{bulk.errors.map((message, i) => <li key={i}>{message}</li>)}</ul>}
    </div>}
    <InfraProgress job={job} onFinish={() => void load()}/>
    <Stats values={[["Всего", rows.length], ["Работают", rows.filter(p => p.status === "working").length],
      ["Предупреждения", rows.filter(p => p.health?.level === "warn").length],
      ["Мертвы", rows.filter(p => p.health?.level === "dead").length],
      ["Свободны", rows.filter(p => isFreeProxy(p) || (p.pool === "client" && p.status === "working"
        && !p.dedicated_workspace_id && p.client_accounts < p.max_accounts)).length]]}/>
    <div className={css.toolbar}>
      <label>Пул <select value={filter} onChange={e => { setFilter(e.target.value as Pool | ""); setChecked([]); }}>
        <option value="">Все пулы</option><option value="agency">{pools.agency}</option>
        <option value="client">{pools.client}</option>
      </select></label>
      <button className="button secondary" disabled={locked || !ids.length} onClick={() => void check(ids)}>
        Проверить выбранные ({ids.length})
      </button>
      <button className="button secondary" disabled={locked || !items.length} onClick={() => void check()}>
        Проверить все пулы
      </button>
      <button className="button secondary" disabled={locked || !ids.length} onClick={() => void remove()}>Удалить</button>
      <MoveToClient path="/proxies/move" disabled={locked}
        ids={rows.filter(p => checked.includes(p.id) && p.pool === "agency").map(p => p.id)}
        onDone={message => { setNotice(message); setChecked([]); void load(); }}/>
    </div>
    {warnAboutSavedProxies && <p className="notice" role="status">{publicProxyWarning}</p>}
    <section className={`tablePanel ${css.panel}`}><div className={css.scroll} tabIndex={0}
      aria-label="Прокси"><table><thead><tr>
      <th><input type="checkbox" aria-label="Отметить все видимые прокси" disabled={!rows.length || locked}
        checked={!!rows.length && ids.length === rows.length}
        onChange={e => setChecked(e.target.checked ? rows.map(p => p.id) : [])}/></th>
      <th>Адрес</th><th>Тип</th><th>Внешний IP</th><th>Задержка</th><th>Здоровье</th>
      <th>Пул</th><th>Аккаунты</th><th>Выделен для</th><th>Статус</th><th>Действия</th>
    </tr></thead><tbody>{rows.map(item => <tr key={item.id}>
      <td><input type="checkbox" aria-label={`Отметить прокси №${item.id}`} disabled={locked}
        checked={checked.includes(item.id)} onChange={() => setChecked(prev => toggleId(prev, item.id))}/></td>
      <td className="mono">{item.host}:{item.port}</td><td>{item.scheme.toUpperCase()}</td>
      <td>{item.exit_ip ?? "—"}{item.rotating && <small>IP меняется</small>}
        {item.duplicate_of != null && <small>Тот же IP, что у №{item.duplicate_of}</small>}</td>
      <td>{item.latency_ms == null ? "—" : `${item.latency_ms} мс`}</td>
      <td><HealthBadge health={item.health}/>{item.health?.uptime_24h != null &&
        <small>Доступность за 24 ч: {item.health.uptime_24h}%</small>}</td>
      <td><select aria-label="Пул прокси" disabled={locked} value={item.pool}
        onChange={e => void change(item, { pool: e.target.value as Pool })}>
        <option value="agency">{pools.agency}</option><option value="client">{pools.client}</option></select></td>
      <td>{item.pool === "client" ? item.client_accounts : item.assigned_accounts}/{item.max_accounts}
        {item.accounts?.map(account => <small key={account.id}>{account.name || account.phone}</small>)}
        {item.pool === "client" && <button disabled={locked} onClick={() => {
          const value = window.prompt("Лимит профилей", String(item.max_accounts));
          if (value) void change(item, { max_accounts: Number(value) });
        }}>Лимит</button>}</td>
      <td>{item.dedicated_workspace_name || "—"}</td>
      <td>{proxyStatuses[item.status] ?? "Неизвестно"}</td>
      <td>{item.health?.level === "dead" && <button disabled={locked}
        onClick={() => setReplacing(item)}>Заменить</button>}</td>
    </tr>)}</tbody></table>{!rows.length && <p>Прокси в выбранном пуле нет.</p>}</div></section>
    <form className="tablePanel" onSubmit={remap}><h2>Переназначить IP профиля вручную</h2>
      <p>Сначала выключите канал. Автоматическая смена IP запрещена.</p>
      <label>ID канала<input name="channel" required type="number" min={1}/></label>
      <label>Новый прокси<select name="proxy" required>{items.filter(p => p.pool === "client").map(p =>
        <option key={p.id} value={p.id}>{p.host}:{p.port} · {p.client_accounts}/{p.max_accounts}</option>)}</select></label>
      <button disabled={locked || !items.some(p => p.pool === "client")}>Переназначить с подтверждением</button>
    </form>
    {open && <div className="modalBackdrop"><form role="dialog" aria-label="Добавить прокси"
      className={`modal proxyModal ${css.modal}`} onSubmit={addBulk}>
      <div className="modalHead"><h2>Добавить прокси</h2>
        <button type="button" disabled={busy} aria-label="Закрыть"
          onClick={() => { setEntries(""); setOpen(false); }}>×</button></div>
      <label>Пул<select value={pool} onChange={e => setPool(e.target.value as Pool)}>
        <option value="agency">{pools.agency}</option><option value="client">{pools.client}</option></select></label>
      <label>Тип<select value={scheme} onChange={e => setScheme(e.target.value as ProxyScheme)}>
        <option value="auto">Авто</option><option value="socks5">SOCKS5</option><option value="http">HTTP</option>
      </select></label>
      <p>Один прокси в строке. Клиентские Telegram-профили используют SOCKS5.</p>
      <p>Форматы: host:port, host:port:login:pass, login:pass@host:port, host:port@login:pass
        или scheme://login:pass@host:port. Пустые строки и комментарии с # пропускаются.</p>
      <label>Список прокси<textarea value="" placeholder="Вставьте список — содержимое скрыто"
        onChange={() => undefined} onPaste={event => {
          event.preventDefault();
          if (!busy) setEntries(event.clipboardData.getData("text"));
        }}/></label>
      <p role="status">Строк к добавлению: {entries.split(/\r?\n/).filter(line => line.trim() &&
        !line.trimStart().startsWith("#")).length}</p>
      {warnAboutImport && <p className="notice" role="status">{publicProxyWarning}</p>}
      {error && <p role="alert">{error}</p>}
      <button className="button primary" disabled={locked || !entries.trim()}>{busy ? "Добавляем…" : "Добавить"}</button>
    </form></div>}
    {replacing && <div className="modalBackdrop"><form className={`modal ${css.modal}`} role="dialog"
      aria-label="Замена прокси" onSubmit={replace}>
      <h2>Замена прокси №{replacing.id}</h2>
      {replacing.pool === "client" ? <label>Новый адрес и реквизиты
        <input autoComplete="off" type="password" name="line" required/>
      </label> : <>
        <label>Аккаунт<select name="account" required>
          <option value="">Выберите аккаунт</option>
          {replacing.accounts?.map(account => <option key={account.id} value={account.id}>
            {account.name || account.phone}
          </option>)}
        </select></label>
        {!replacing.accounts?.length && <p>Привязанных аккаунтов нет. Добавьте новый прокси в пул.</p>}
        <label>Новый прокси<select name="proxy">
          <option value="">Назначить автоматически</option>
          {items.filter(p => p.id !== replacing.id && isFreeProxy(p)).map(p =>
            <option value={p.id} key={p.id}>{p.host}:{p.port}</option>)}
        </select></label>
      </>}
      {error && <p role="alert">{error}</p>}
      <div className="modalActions"><button type="button" disabled={busy}
        onClick={() => setReplacing(null)}>Отмена</button>
        <button className="button primary" disabled={locked ||
          (replacing.pool === "agency" && !replacing.accounts?.length)}>Заменить</button></div>
    </form></div>}
  </div>;
}
