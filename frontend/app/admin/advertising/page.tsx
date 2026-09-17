"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, AdConnection, ClientWorkspace, MarketingSummary } from "@/lib/api";

const emptySummary: MarketingSummary = { spend: 0, impressions: 0, clicks: 0, applications: 0, cpc: null, conversion_rate: null, cost_per_application: null, connections: 0, workspaces: 0 };
const money = (value: number) => new Intl.NumberFormat("ru-RU", { style: "currency", currency: "RUB", maximumFractionDigits: 0 }).format(value);
const number = (value: number) => new Intl.NumberFormat("ru-RU").format(value);

export default function AdvertisingPage() {
  const [workspaces, setWorkspaces] = useState<ClientWorkspace[]>([]);
  const [connections, setConnections] = useState<AdConnection[]>([]);
  const [summary, setSummary] = useState(emptySummary);
  const [modal, setModal] = useState<"client" | "connection" | "token" | null>(null);
  const [selectedConnection, setSelectedConnection] = useState<AdConnection | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<number | string | null>(null);

  const load = useCallback(async () => {
    try {
      const [workspaceRows, connectionRows, totals] = await Promise.all([
        api<ClientWorkspace[]>("/marketing/workspaces"), api<AdConnection[]>("/marketing/connections"), api<MarketingSummary>("/marketing/summary"),
      ]);
      setWorkspaces(workspaceRows); setConnections(connectionRows); setSummary(totals); setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось загрузить рекламные кабинеты"); }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function addClient(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy("client"); setError("");
    const data = new FormData(event.currentTarget);
    try { await api("/marketing/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: data.get("name") }) }); setModal(null); setNotice("Клиентский кабинет создан."); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось создать клиента"); } finally { setBusy(null); }
  }
  async function addConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy("connection"); setError("");
    const data = new FormData(event.currentTarget);
    try {
      const row = await api<AdConnection>("/marketing/connections", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: Number(data.get("workspace_id")), platform: data.get("platform"), name: data.get("name"), external_account_id: data.get("external_account_id"), access_token: data.get("access_token") }) });
      setModal(null); setNotice("Кабинет сохранён. Теперь проверьте соединение."); await load(); await test(row.id);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось добавить кабинет"); } finally { setBusy(null); }
  }
  async function test(id: number) {
    setBusy(id); setError(""); setNotice("");
    try { await api(`/marketing/connections/${id}/test`, { method: "POST", timeoutMs: 35_000 }); setNotice("Соединение подтверждено рекламной платформой."); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Проверка не прошла"); await load(); } finally { setBusy(null); }
  }
  async function replaceToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedConnection) return;
    const data = new FormData(event.currentTarget); setBusy("token"); setError("");
    try {
      await api(`/marketing/connections/${selectedConnection.id}/token`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ access_token: data.get("access_token") }) });
      setModal(null); setNotice("Токен заменён. Запускаю повторную проверку…"); await load(); await test(selectedConnection.id);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось заменить токен"); } finally { setBusy(null); }
  }
  async function sync(id: number) {
    setBusy(id); setError(""); setNotice("");
    try {
      const result = await api<{ days: number; first_date: string | null; last_date: string | null }>(`/marketing/connections/${id}/sync`, { method: "POST", timeoutMs: 240_000 });
      const period = result.first_date && result.last_date ? ` (${new Date(result.first_date + "T00:00:00").toLocaleDateString("ru-RU")} — ${new Date(result.last_date + "T00:00:00").toLocaleDateString("ru-RU")})` : "";
      setNotice(`Загружена вся доступная история: ${result.days} дней${period}.`); await load();
    }
    catch (e) { setError(e instanceof Error ? e.message : "Синхронизация не прошла"); await load(); } finally { setBusy(null); }
  }
  async function removeConnection(item: AdConnection) {
    if (!window.confirm(`Удалить подключение «${item.name}»? Загруженная статистика этого подключения также будет удалена.`)) return;
    setBusy(item.id); setError(""); setNotice("");
    try { await api(`/marketing/connections/${item.id}`, { method: "DELETE" }); setNotice("Рекламное подключение удалено."); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось удалить подключение"); }
    finally { setBusy(null); }
  }

  return <div className="page advertisingPage">
    <header className="topbar"><div className="crumbs"><span>StepToLead</span><b>/</b><strong>Рекламные кабинеты</strong></div><div className="topbarActions"><span className="userAvatar">ЕД</span></div></header>
    <section className="pageHeader"><div><p className="eyebrow">Маркетинг клиентов</p><h1>Рекламные кабинеты</h1><p className="subtitle">Подключайте официальные API, проверяйте доступ и собирайте статистику в одном месте.</p></div><div className="pageHeaderActions"><button className="button secondary" onClick={() => setModal("client")}>＋ Клиент</button><button className="button primary" disabled={!workspaces.length} onClick={() => setModal("connection")}>＋ Подключить рекламу</button></div></section>
    {error && <p className="notice error" role="alert"><strong>Ошибка:</strong> {error}</p>}
    {notice && <p className="notice success" role="status">{notice}</p>}
    <section className="metricsGrid marketingMetrics">
      <article className="metricCard featured"><div className="metricTop"><span>Вложено · всё время</span><i>API</i></div><strong>{money(summary.spend)}</strong><p>{summary.first_date ? `Данные с ${new Date(summary.first_date + "T00:00:00").toLocaleDateString("ru-RU")}` : "По рекламным кабинетам"}</p></article>
      <article className="metricCard"><div className="metricTop"><span>Кликов</span><i>Трафик</i></div><strong>{number(summary.clicks)}</strong><p>{number(summary.impressions)} показов</p></article>
      <article className="metricCard"><div className="metricTop"><span>Цена клика</span><i>CPC</i></div><strong>{summary.cpc == null ? "—" : money(summary.cpc)}</strong><p>Вложено ÷ клики</p></article>
      <article className="metricCard"><div className="metricTop"><span>Конверсия</span><i>CR</i></div><strong>{summary.conversion_rate == null ? "—" : `${summary.conversion_rate.toFixed(2)}%`}</strong><p>Заявки из CRM ÷ клики</p></article>
      <article className="metricCard"><div className="metricTop"><span>Цена заявки</span><i>CPL</i></div><strong>{summary.cost_per_application == null ? "—" : money(summary.cost_per_application)}</strong><p>Вложено ÷ реальные заявки</p></article>
      <article className="metricCard"><div className="metricTop"><span>Заявки</span><i>CRM</i></div><strong>{number(summary.applications)}</strong><p>Приняты через webhook без дублей</p></article>
    </section>
    <section className="panel adPanel"><div className="panelHead"><div><p className="eyebrow">Источники данных</p><h2>Рекламные кабинеты · {summary.connections}</h2></div><span className="secureHint">Токены хранятся зашифрованно</span></div>
      <div className="adConnectionGrid">{connections.map(item => <article className="adConnection" key={item.id}>
        <div className="adConnectionTop"><span className={`platformLogo ${item.platform}`}>{item.platform === "meta" ? "M" : "Я"}</span><div><strong>{item.name}</strong><small>{item.workspace_name} · {item.platform_name}</small></div><span className={`adStatus ${item.status}`}>{item.status === "connected" ? "Подключён" : item.status === "error" ? "Ошибка" : "Не проверен"}</span></div>
        <dl><div><dt>ID кабинета</dt><dd>{item.external_account_id}</dd></div><div><dt>Последняя синхронизация</dt><dd>{item.last_synced_at ? new Date(item.last_synced_at).toLocaleString("ru-RU") : "Ещё не запускалась"}</dd></div></dl>
        {item.last_error && <p className="adError">{item.last_error}</p>}
        <div className="adActions"><Link href={`/admin/advertising/${item.workspace_id}`}>Открыть аналитику</Link><button onClick={() => { setSelectedConnection(item); setModal("token"); }}>Обновить токен</button><button onClick={() => test(item.id)} disabled={busy === item.id}>{busy === item.id ? "Проверяем…" : "Проверить"}</button><button className="sync" onClick={() => sync(item.id)} disabled={busy === item.id}>{busy === item.id ? "Загружаем историю…" : "Загрузить всю историю"}</button><button className="danger" onClick={() => removeConnection(item)} disabled={busy === item.id}>Удалить</button></div>
      </article>)}{!connections.length && <div className="empty adEmpty"><span>↗</span><h3>Реклама ещё не подключена</h3><p>Сначала создайте клиента, затем добавьте кабинет Meta Ads или Яндекс Директ.</p></div>}</div>
    </section>
    <section className="adGuide"><strong>Как это работает по-настоящему</strong><span>1. Создаёте клиента</span><span>2. Получаете официальный OAuth-токен платформы</span><span>3. Добавляете ID кабинета и токен</span><span>4. StepToLead проверяет API и загружает всю доступную историю</span></section>

    {modal === "client" && <div className="modalBackdrop"><form className="modal" onSubmit={addClient}><div className="modalHead"><div><p className="eyebrow">Новый workspace</p><h2>Добавить клиента</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div><label>Название компании<input name="name" required minLength={2} maxLength={180} placeholder="Например, VBI Group" autoFocus /></label><button className="button primary full" disabled={busy === "client"}>{busy ? "Создаём…" : "Создать кабинет"}</button></form></div>}
    {modal === "connection" && <div className="modalBackdrop"><form className="modal adModal" onSubmit={addConnection}><div className="modalHead"><div><p className="eyebrow">Официальное API</p><h2>Подключить рекламу</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div>
      <label>Клиент<select name="workspace_id" required defaultValue=""><option value="" disabled>Выберите клиента</option>{workspaces.map(x => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
      <label>Платформа<select name="platform" required defaultValue="yandex"><option value="yandex">Яндекс Директ</option><option value="meta">Meta Ads</option></select></label>
      <label>Название подключения<input name="name" required placeholder="Основной рекламный кабинет" /></label>
      <label>ID / логин кабинета<input name="external_account_id" required placeholder="Для Яндекса — Client-Login; для Meta — act_123…" /></label>
      <label>OAuth access token<input name="access_token" type="password" required minLength={10} autoComplete="off" placeholder="Токен не будет показан повторно" /></label>
      <p className="formHint">Пароль от рекламного кабинета не нужен. Используется только выданный платформой токен с правом чтения статистики.</p><button className="button primary full" disabled={busy === "connection"}>{busy ? "Подключаем…" : "Сохранить и проверить"}</button>
    </form></div>}
    {modal === "token" && selectedConnection && <div className="modalBackdrop"><form className="modal" onSubmit={replaceToken}><div className="modalHead"><div><p className="eyebrow">{selectedConnection.platform_name}</p><h2>Обновить OAuth-токен</h2></div><button type="button" className="close" onClick={() => setModal(null)}>×</button></div><p className="formHint">Вставьте только значение <b>access_token</b>. ID и секрет OAuth-приложения сюда не подходят.</p><label>Новый access token<input name="access_token" type="password" required minLength={10} autoComplete="off" autoFocus placeholder="Будет сохранён в зашифрованном виде" /></label><button className="button primary full" disabled={busy === "token"}>{busy ? "Сохраняем…" : "Заменить и проверить"}</button></form></div>}
  </div>;
}
