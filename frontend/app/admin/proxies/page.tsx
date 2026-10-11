"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Proxy } from "@/lib/api";

export default function ProxiesPage() {
  const [items, setItems] = useState<Proxy[]>([]);
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState("");
  const [pool, setPool] = useState("agency");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api<Proxy[]>("/proxies").then(setItems)
    .catch(error => setNotice(error.message)), []);
  useEffect(() => { void load(); }, [load]);
  async function addBulk(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api<{ imported: number; skipped: number; errors: string[] }>("/proxies/bulk", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entries, pool }),
      });
      setNotice(`Добавлено: ${result.imported}. Пропущено: ${result.skipped}. ${result.errors.join(" ")}`);
      setEntries("");
      setOpen(false);
      await load();
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  }
  async function change(item: Proxy, values: Partial<Proxy>) {
    setBusy(true);
    try {
      await api(`/proxies/${item.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pool: item.pool, max_accounts: item.max_accounts,
          dedicated_workspace_id: item.dedicated_workspace_id, ...values }),
      });
      await load();
    } catch (e) { setNotice((e as Error).message); }
    finally { setBusy(false); }
  }
  async function remove(item: Proxy) {
    if (!window.confirm(`Удалить прокси ${item.host}:${item.port}?`)) return;
    try { await api(`/proxies/${item.id}`, { method: "DELETE" }); await load(); }
    catch (e) { setNotice((e as Error).message); }
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
    } catch (e) { setNotice((e as Error).message); }
    finally { setBusy(false); }
  }
  return <div className="page">
    <header className="pageHeader"><div><p className="eyebrow">Инфраструктура</p><h1>Прокси</h1></div>
      <button className="button primary" onClick={() => setOpen(true)}>+ Массовое добавление</button></header>
    {notice && <div className="notice" role="status">{notice}</div>}
    <section className="tablePanel"><div className="tableWrap"><table><thead><tr>
      <th>Адрес</th><th>Протокол</th><th>Пул</th><th>Загрузка</th><th>Выделен для</th><th>Статус</th><th/>
    </tr></thead><tbody>{items.map(item => <tr key={item.id}>
      <td className="mono">{item.host}:{item.port}</td><td>{item.scheme.toUpperCase()}</td>
      <td><select aria-label="Пул" disabled={busy} value={item.pool}
        onChange={e => void change(item, { pool: e.target.value as Proxy["pool"] })}>
        <option value="agency">Агентство</option><option value="client">Клиенты</option></select></td>
      <td>{item.pool === "client" ? `${item.client_accounts}/${item.max_accounts}` : item.assigned_accounts}
        {item.pool === "client" && <button disabled={busy} onClick={() => {
          const value = window.prompt("Лимит профилей", String(item.max_accounts));
          if (value) void change(item, { max_accounts: Number(value) });
        }}>Лимит</button>}</td>
      <td>{item.dedicated_workspace_name || "—"}</td>
      <td>{item.status === "untested" ? "Не проверен" : item.status}<small>{item.last_error}</small></td>
      <td><button disabled={busy} onClick={() => void remove(item)} aria-label="Удалить прокси">×</button></td>
    </tr>)}</tbody></table>{!items.length && <p>Пул прокси пуст.</p>}</div></section>
    <form className="tablePanel" onSubmit={remap}><h2>Переназначить IP профиля вручную</h2>
      <p>Сначала выключите канал. Автоматическая смена IP запрещена.</p>
      <label>ID канала<input name="channel" required type="number" min={1}/></label>
      <label>Новый прокси<select name="proxy" required>{items.filter(p => p.pool === "client").map(p =>
        <option key={p.id} value={p.id}>{p.host}:{p.port} · {p.client_accounts}/{p.max_accounts}</option>)}</select></label>
      <button disabled={busy}>Переназначить с подтверждением</button>
    </form>
    {open && <div className="modalBackdrop"><form className="modal proxyModal" onSubmit={addBulk}>
      <div className="modalHead"><h2>Добавить прокси</h2>
        <button type="button" onClick={() => setOpen(false)}>×</button></div>
      <label>Пул<select value={pool} onChange={e => setPool(e.target.value)}>
        <option value="agency">Агентство</option><option value="client">Клиенты</option></select></label>
      <p>Один прокси в строке. Клиентские Telegram-профили используют SOCKS5.</p>
      <label>Список прокси<textarea required value={entries} onChange={e => setEntries(e.target.value)}
        placeholder="socks5://user:password@127.0.0.1:1080"/></label>
      <button className="button primary" disabled={busy}>{busy ? "Добавляем…" : "Добавить"}</button>
    </form></div>}
  </div>;
}
