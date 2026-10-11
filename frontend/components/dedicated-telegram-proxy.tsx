"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Profile = { id: number; name: string; status: string; last_error: string | null };
type ClientProxy = { id: number; host: string; port: number; username: string | null; status: string;
  last_error: string | null; last_checked_at: string | null; max_accounts: number; profiles: Profile[] };
type Payload = { proxies: ClientProxy[]; shared_pool_profiles: number; waiting: { id: number; name: string }[] };

const statusLabel: Record<string, string> = { working: "работает", error: "нет связи", untested: "не проверен" };
const json = (method: string, body: unknown) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// Прокси для Telegram-профилей клиента: агентство вставляет их в карточке клиента и при необходимости заменяет.
export function DedicatedTelegramProxy({ workspaceId }: { workspaceId: number }) {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [replacing, setReplacing] = useState<number | null>(null);
  const load = useCallback(() => api<Payload>(`/proxies/workspace/${workspaceId}`).then(setData).catch(e => setError((e as Error).message)), [workspaceId]);
  useEffect(() => { void load(); }, [load]);

  async function run(action: () => Promise<Payload>, done: string, form?: HTMLFormElement) {
    setBusy(true); setError(""); setNotice("");
    try { setData(await action()); setNotice(done); form?.reset(); setReplacing(null); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void run(() => api<Payload>(`/proxies/workspace/${workspaceId}`, json("POST", {
      line: String(form.get("line") || ""), max_accounts: Number(form.get("max_accounts") || 3) })),
      "Прокси проверен и добавлен", event.currentTarget);
  }
  function replace(event: FormEvent<HTMLFormElement>, proxy: ClientProxy) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    if (proxy.profiles.length && !window.confirm("Профили переподключатся через новый IP. Telegram может попросить подтвердить вход заново. Заменить?")) return;
    void run(() => api<Payload>(`/proxies/${proxy.id}/replace`, json("PUT", {
      line: String(form.get("line") || ""), confirm_ip_change: true })), "Прокси заменён — профили переподключаются", event.currentTarget);
  }
  async function remove(proxy: ClientProxy) {
    if (!window.confirm(`Удалить прокси ${proxy.host}:${proxy.port}?`)) return;
    setBusy(true); setError("");
    try { await api(`/proxies/${proxy.id}`, { method: "DELETE" }); await load(); setNotice("Прокси удалён"); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  return <section className="tablePanel clientProxyPanel">
    <div className="clientProxyHead"><div><h2>Прокси для Telegram-профилей</h2>
      <p>Через эти адреса работают рабочие Telegram-аккаунты компании. До 3 профилей на один прокси, IP за профилем закрепляется. Если прокси перестал работать — замените его, профили переподключатся сами.</p></div></div>
    {error && <p role="alert" className="clientProxyError">{error}</p>}
    {notice && <p role="status" className="clientProxyNotice">{notice}</p>}
    {data?.waiting.length ? <p className="clientProxyError">Ждут свободный прокси: {data.waiting.map(w => w.name).join(", ")}. Добавьте прокси — затем подключите профиль заново.</p> : null}
    {data && !data.proxies.length && <p className="clientProxyEmpty">{data.shared_pool_profiles
      ? `Своих прокси нет — ${data.shared_pool_profiles} профил(ей) работают через общий пул агентства.`
      : "Прокси ещё не добавлены. Без прокси клиент не сможет подключить Telegram-профиль (если нет свободного места в общем пуле)."}</p>}
    {data?.proxies.map(proxy => <div className="clientProxyRow" key={proxy.id}>
      <div className="clientProxyMain"><strong>{proxy.host}:{proxy.port}{proxy.username ? ` · ${proxy.username}` : ""}</strong>
        <span className={`clientProxyStatus ${proxy.status}`}>{statusLabel[proxy.status] || proxy.status}</span>
        <small>Профили: {proxy.profiles.length}/{proxy.max_accounts}{proxy.profiles.length ? ` — ${proxy.profiles.map(p => p.name).join(", ")}` : ""}</small>
        {proxy.last_error && <small className="clientProxyErrorText">{proxy.last_error}</small>}</div>
      <div className="clientProxyActions">
        <button className="button secondary" disabled={busy} onClick={() => setReplacing(replacing === proxy.id ? null : proxy.id)}>{replacing === proxy.id ? "Отмена" : "Заменить"}</button>
        {!proxy.profiles.length && <button className="button secondary" disabled={busy} onClick={() => void remove(proxy)}>Удалить</button>}</div>
      {replacing === proxy.id && <form className="clientProxyForm" onSubmit={e => replace(e, proxy)}>
        <input name="line" required placeholder="host:port:логин:пароль" autoComplete="off"/>
        <button className="button primary" disabled={busy}>{busy ? "Проверяем…" : "Проверить и заменить"}</button></form>}
    </div>)}
    <form className="clientProxyForm" onSubmit={add}>
      <input name="line" required placeholder="host:port:логин:пароль, socks5://логин:пароль@host:port или http://логин:пароль@host:port" autoComplete="off"/>
      <label>Профилей на прокси<input name="max_accounts" type="number" min={1} max={100} defaultValue={3}/></label>
      <button className="button primary" disabled={busy}>{busy ? "Проверяем…" : "Проверить и добавить"}</button></form>
    <p className="formHint">Подходит SOCKS5 или HTTP-прокси (строка без схемы считается SOCKS5). Перед сохранением портал проверяет, что через него открывается Telegram.</p>
  </section>;
}
