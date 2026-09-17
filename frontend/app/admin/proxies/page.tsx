"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Proxy } from "@/lib/api";

export default function ProxiesPage() {
  const [items, setItems] = useState<Proxy[]>([]);
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api<Proxy[]>("/proxies").then(setItems).catch((error) => setNotice(error.message)), []);
  useEffect(() => { load(); }, [load]);

  async function addBulk(event: FormEvent) {
    event.preventDefault(); setBusy(true); setNotice("");
    try {
      const result = await api<{ imported: number; skipped: number; errors: string[] }>("/proxies/bulk", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entries }) });
      setNotice(`Добавлено: ${result.imported}. Пропущено: ${result.skipped}.${result.errors.length ? ` ${result.errors[0]}` : ""}`);
      setEntries(""); setOpen(false); await load();
    } catch (error) { setNotice((error as Error).message); } finally { setBusy(false); }
  }

  async function remove(item: Proxy) {
    if (!window.confirm(`Удалить прокси ${item.host}:${item.port}? Он будет отвязан от аккаунтов.`)) return;
    try { await api(`/proxies/${item.id}`, { method: "DELETE" }); await load(); }
    catch (error) { setNotice((error as Error).message); }
  }

  return <div className="page"><header className="pageHeader"><div><p className="eyebrow">Инфраструктура</p><h1>Прокси</h1><p className="subtitle">Единый пул подключений для Telegram-аккаунтов.</p></div><button className="button primary" onClick={() => setOpen(true)}>+ Массовое добавление</button></header>
    {notice && <div className="notice">{notice}<button onClick={() => setNotice("")}>×</button></div>}
    <section className="proxySummary"><article><span>Всего</span><strong>{items.length}</strong></article><article><span>Назначено</span><strong>{items.filter((item) => item.assigned_accounts).length}</strong></article><article><span>Свободно</span><strong>{items.filter((item) => !item.assigned_accounts).length}</strong></article></section>
    <section className="tablePanel"><div className="tableTitle"><div><p className="eyebrow">Пул</p><h2>Все прокси</h2></div><span>{items.length} подключений</span></div><div className="tableWrap"><table><thead><tr><th>Адрес</th><th>Протокол</th><th>Авторизация</th><th>Аккаунты</th><th>Статус</th><th /></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td><strong className="mono">{item.host}:{item.port}</strong></td><td><span className="protocolTag">{item.scheme.toUpperCase()}</span></td><td>{item.username ? <span className="mutedText">{item.username}:••••••</span> : <span className="mutedText">Без пароля</span>}</td><td>{item.assigned_accounts}</td><td><span className={`liveStatus ${item.status}`}>{item.status === "untested" ? "Не проверен" : item.status}</span></td><td><button className="rowDelete" onClick={() => remove(item)} aria-label="Удалить прокси">×</button></td></tr>)}</tbody></table>{!items.length && <div className="empty compact"><span>05</span><h3>Пул прокси пуст</h3><p>Добавьте список одним блоком — по одному прокси в строке.</p></div>}</div></section>
    {open && <div className="modalBackdrop" onMouseDown={() => setOpen(false)}><form className="modal proxyModal" onSubmit={addBulk} onMouseDown={(event) => event.stopPropagation()}><div className="modalHead"><div><p className="eyebrow">Пакетный импорт</p><h2>Добавить прокси</h2></div><button type="button" className="close" onClick={() => setOpen(false)}>×</button></div><p className="formHint">Один прокси в строке. Поддерживаются SOCKS5, SOCKS4 и HTTP.</p><label>Список прокси<textarea required className="codeArea" value={entries} onChange={(event) => setEntries(event.target.value)} placeholder={"socks5://user:password@127.0.0.1:1080\n127.0.0.1:1080:user:password\n127.0.0.1:1080"} /></label><div className="formatHint"><span>URL</span><span>host:port:user:password</span><span>host:port</span></div><button className="button primary full" disabled={busy}>{busy ? "Добавляем…" : "Добавить в пул"}</button></form></div>}
  </div>;
}
