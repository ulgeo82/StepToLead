"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { api, Campaign, TelegramAccount, Proxy } from "@/lib/api";
import "./settings.css";

type Settings = Record<string, string | number | boolean>;
type Runtime = { campaign_id: number; status: string; settings: Settings; account_ids: number[]; api_key_set: boolean; last_error: string | null; next_action_at: string | null; counts: Record<string, number> };
type Dialog = { id: number; username: string; account_id: number; state: string; peer_id: number; followup_sent: boolean };
type Message = { id: number; dialog_id: number; username: string; account_id: number; kind: string; body: string; status: string; error: string | null; created_at: string };
type EventLog = { id: number; level: "info" | "success" | "warning" | "error"; event: string; message: string; account_id: number | null; username: string | null; details: Record<string, unknown> | null; created_at: string };
type Block = { peer_id: number; username: string | null };
const labels: Record<string, string> = { paused: "На паузе", running: "Запущена", pending: "Ожидает", sending: "Отправляется", sent: "Отправлено", received: "Получено", failed: "Ошибка", unknown: "Нужна проверка", cancelled: "Отменено", first: "Первое сообщение", incoming: "Входящее", reply: "ИИ-ответ", followup: "Follow-up", handoff: "Передан менеджеру", active: "Активен", stopped: "Остановлен", review: "Нужна проверка", manager_requested: "Ожидает передачи", partner_requested: "Ожидает передачи партнёру" };

export default function CampaignDetail() {
  const { id } = useParams<{ id: string }>();
  const base = `/campaigns/${id}`;
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [settings, setSettings] = useState<Settings>({});
  const [accounts, setAccounts] = useState<TelegramAccount[]>([]);
  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [key, setKey] = useState("");
  const [proxy, setProxy] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [tab, setTab] = useState("settings");
  const [dialogs, setDialogs] = useState<Dialog[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [logs, setLogs] = useState<EventLog[]>([]);
  const [logLevel, setLogLevel] = useState("all");
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [blockId, setBlockId] = useState("");
  const [blockUsername, setBlockUsername] = useState("");

  const refresh = useCallback(async () => {
    const [c, r, a, p, d, m, l, b] = await Promise.all([
      api<Campaign>(base), api<Runtime>(`${base}/automation`), api<TelegramAccount[]>("/telegram-accounts"),
      api<Proxy[]>("/proxies"), api<Dialog[]>(`${base}/dialogs`), api<Message[]>(`${base}/messages`), api<EventLog[]>(`${base}/logs`), api<Block[]>("/telegram-blocklist"),
    ]);
    setCampaign(c); setRuntime(r); setAccounts(a); setProxies(p); setDialogs(d); setMessages(m); setLogs(l); setBlocks(b);
    return r;
  }, [base]);
  useEffect(() => { refresh().then(r => { setSettings(r.settings); setSelected(r.account_ids); }).catch(e => setNotice(e.message)); }, [refresh]);
  useEffect(() => { const timer = setInterval(() => { refresh().catch(e => setNotice(e.message)); }, 10000); return () => clearInterval(timer); }, [refresh]);
  const running = runtime?.status === "running";
  function change(name: string, value: string | number | boolean) { setSettings(s => ({ ...s, [name]: value })); setDirty(true); }
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setNotice("");
    try { await action(); } catch (e) { setNotice((e as Error).message); } finally { setBusy(false); }
  }
  async function save() {
    const result = await api<Runtime>(`${base}/automation`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings, account_ids: selected, ...(key ? { api_key: key } : {}) }) });
    setRuntime(result); setSettings(result.settings); setKey(""); setDirty(false); setNotice("Настройки сохранены");
  }
  async function start() {
    if (dirty) await save();
    const r = await api<Runtime>(`${base}/start`, { method: "POST" }); setRuntime(r); setNotice("Кампания запущена. Очередь работает по расписанию.");
  }
  async function testAi() {
    if (dirty) await save();
    const result = await api<{ status: string; answer: string }>(`${base}/test-ai`, { method: "POST", timeoutMs: 40000 });
    setNotice(`ИИ подключён: ${result.answer}`);
  }
  async function uploadBase(file?: File) {
    if (!file) return;
    const data = new FormData(); data.append("file", file);
    const r = await api<{ imported: number; skipped: number; errors: string[] }>(`${base}/import`, { method: "POST", body: data });
    setNotice(`Добавлено: ${r.imported}. Пропущено: ${r.skipped}. ${r.errors.join("; ")}`); await refresh();
  }
  async function uploadTdata(files: FileList | null) {
    if (!files?.length) return;
    const data = new FormData(); Array.from(files).slice(0, 50).forEach(file => data.append("files", file)); if (proxy) data.append("proxy_id", proxy);
    const result = await api<{ items: { account_id: number | null; filename: string; error: string | null }[] }>("/telegram-accounts/import-tdata", {
      method: "POST", body: data, timeoutMs: files.length * 65000 + 30000,

    });
    setSelected(current => [...new Set([...current, ...result.items.flatMap(item => item.account_id ? [item.account_id] : [])])]);
    setDirty(true); setNotice(result.items.map(item => `${item.filename}: ${item.account_id ? "добавлен к выбору" : item.error}`).join("; ")); await refresh();
  }
  const field = (name: string, label: string, type = "text", hint?: string) => <label key={name}>{label}<input type={type} value={String(settings[name] ?? "")} onChange={e => change(name, type === "number" ? Number(e.target.value) : e.target.value)} />{hint && <small>{hint}</small>}</label>;
  const area = (name: string, label: string, rows = 3) => <label key={name}>{label}<textarea rows={rows} value={String(settings[name] ?? "")} onChange={e => change(name, e.target.value)} /></label>;
  const toggle = (name: string, label: string) => <label className="automationToggle" key={name}><input type="checkbox" checked={Boolean(settings[name])} onChange={e => change(name, e.target.checked)} />{label}</label>;
  const range = (name: string, label: string) => <label key={name}>{label}<div className="automationRange"><input aria-label={`${label}: минимум`} type="number" value={Number(settings[`${name}_min`] ?? 0)} onChange={e => change(`${name}_min`, Number(e.target.value))} /><span>—</span><input aria-label={`${label}: максимум`} type="number" value={Number(settings[`${name}_max`] ?? 0)} onChange={e => change(`${name}_max`, Number(e.target.value))} /></div></label>;
  const visibleLogs = logLevel === "all" ? logs : logs.filter(item => item.level === logLevel);
  const logCounts = logs.reduce<Record<string, number>>((result, item) => ({ ...result, [item.level]: (result[item.level] || 0) + 1 }), {});
  const detailText = (details: Record<string, unknown> | null) => details ? Object.entries(details).filter(([, value]) => value !== null && value !== undefined).map(([name, value]) => `${name}=${String(value)}`).join(" · ") : "";
  if (!campaign || !runtime) return <div className="page"><p>{notice || "Загружаем кампанию…"}</p><Link href="/admin/campaigns">К кампаниям</Link></div>;
  return <div className="page automationPage">
    <Link href="/admin/campaigns" className="automationBack">← Кампании</Link>
    <header className="pageHeader"><div><p className="eyebrow">Управление кампанией</p><h1>{campaign.name}</h1><p className="subtitle">{labels[runtime.status]} · {campaign.leads_count} получателей · {runtime.account_ids.length} аккаунтов</p></div><div className="pageHeaderActions">
      {running ? <button className="button secondary" disabled={busy} onClick={() => perform(async () => { setRuntime(await api<Runtime>(`${base}/pause`, { method: "POST" })); setNotice("Кампания на паузе"); })}>Пауза</button> : <><button className="button secondary" disabled={busy || !dirty} onClick={() => perform(save)}>Сохранить</button><button className="button primary" disabled={busy} onClick={() => perform(start)}>▶ Запустить</button></>}
    </div></header>
    {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice("")}>×</button></div>}
    {runtime.last_error && <div className="notice" role="alert">{runtime.last_error}</div>}
    <div className="automationStats"><span><b>{runtime.counts.sent || 0}</b> исходящих</span><span><b>{runtime.counts.received || 0}</b> входящих</span><span><b>{(runtime.counts.failed || 0) + (runtime.counts.unknown || 0)}</b> требуют внимания</span><span>{dirty ? "Есть несохранённые изменения" : running ? "Следующая проверка: " + (runtime.next_action_at ? new Date(runtime.next_action_at).toLocaleString("ru") : "ожидается") : "Запуск только по кнопке"}</span></div>
    <nav className="automationTabs" aria-label="Разделы кампании">{[["settings", "Настройки"], ["accounts", "Аккаунты и база"], ["dialogs", "Диалоги"], ["messages", "Журнал"], ["blocks", "Чёрный список"]].map(([value, label]) => <button key={value} className={tab === value ? "selected" : ""} onClick={() => setTab(value)}>{label}</button>)}</nav>
    {tab === "settings" && <fieldset disabled={busy || running} className="automationFields">
      <section className="automationSection"><h2>ИИ-ответы</h2>{toggle("auto_reply", "Автоматически отвечать по системному промпту")}{area("system_prompt", "Системный промпт", 8)}<div className="automationGrid">{field("ai_url", "Адрес API", "url", "Для OpenAI: https://api.openai.com/v1/chat/completions")}{field("ai_model", "Модель")}<label>API-ключ<input type="password" autoComplete="new-password" value={key} placeholder={runtime.api_key_set ? "Ключ сохранён; оставьте пустым, чтобы сохранить его" : "Введите ключ выбранного сервиса"} onChange={e => { setKey(e.target.value); setDirty(true); }} /><small>Сохраняется в зашифрованном виде и не показывается повторно</small></label>{field("context_messages", "Сообщений в контексте ИИ", "number")}</div><button className="button secondary" type="button" onClick={() => perform(testAi)}>Проверить ИИ</button>{toggle("fallback_enabled", "Отправлять резервный ответ при ошибке ИИ")}{settings.fallback_enabled && area("fallback_text", "Резервный ответ")}</section>
      <section className="automationSection"><h2>Передача диалогов</h2><div className="automationGrid">{area("positive_trigger", "Положительный триггер в ответе ИИ")}{area("negative_trigger", "Отрицательный триггер в ответе ИИ")}{field("positive_chat", "Чат менеджера (+)")}{field("negative_chat", "Чат для отказов (−)")}{field("partner_chat", "Чат партнёров")}{field("forwarded_messages", "Сообщений при передаче", "number")}</div><p>При срабатывании триггера история отправляется в указанный чат, автоматические ответы в диалоге прекращаются. Передача партнёру доступна в разделе «Диалоги».</p></section>
      <section className="automationSection"><h2>Отправка и расписание</h2><div className="automationGrid">{field("daily_limit", "Первых сообщений на аккаунт в сутки", "number", "0 — новые первые сообщения отключены")}{field("first_message_max", "Максимум символов первого сообщения", "number")}{field("timezone_offset", "Часовой пояс UTC±", "number")}{field("restriction_hours", "Пауза после ограничения Telegram, часов", "number")}{range("read", "Пауза перед прочтением, сек")}{range("action", "Пауза между действиями, сек")}{range("account", "Пауза между аккаунтами, сек")}{range("round", "Пауза между кругами, сек")}</div>{field("sleep_periods", "Периоды сна", "text", "Например: 00:00-08:00,13:00-14:00. Пусто — круглосуточно.")}{toggle("ignore_bots", "Игнорировать ботов")}{toggle("ignore_without_username", "Игнорировать получателей без username")}<p>Ответы обрабатываются только в диалогах этой кампании после первого исходящего сообщения. Слово «стоп» останавливает диалог и добавляет получателя в глобальный чёрный список.</p></section>
      <section className="automationSection"><h2>Follow-up</h2>{toggle("followup_enabled", "Отправлять одно напоминание, если ответа нет")}{settings.followup_enabled && <>{field("followup_hours", "Через сколько часов", "number")}{area("followup_text", "Текст напоминания")}</>}</section>
      <button className="button primary" type="button" onClick={() => perform(save)}>Сохранить настройки</button>
    </fieldset>}
    {tab === "accounts" && <section className="automationSection"><h2>Аккаунты кампании</h2><p>Выберите уже импортированные аккаунты или загрузите TData здесь. После изменения выбора нажмите «Сохранить».</p><fieldset disabled={busy || running} className="automationFields"><div className="automationAccountList">{accounts.map(a => <label key={a.id}><input type="checkbox" checked={selected.includes(a.id)} onChange={e => { setSelected(ids => e.target.checked ? [...ids, a.id] : ids.filter(i => i !== a.id)); setDirty(true); }} /><div><strong>{[a.first_name, a.last_name].filter(Boolean).join(" ") || a.phone}</strong><small>{a.username ? `@${a.username}` : a.phone} · {a.proxy_id ? "Прокси назначен" : "Без прокси"}</small></div></label>)}</div><div className="automationGrid"><label>Прокси для новых аккаунтов<select value={proxy} onChange={e => setProxy(e.target.value)}><option value="">Без прокси</option>{proxies.map(p => <option value={p.id} key={p.id}>{p.host}:{p.port} · {p.scheme}</option>)}</select></label><label>Добавить TData<input type="file" accept=".zip" multiple onChange={e => { const files = e.target.files; perform(() => uploadTdata(files)); }} /></label></div><h2>База получателей</h2><p>{campaign.leads_count} получателей. Формат CSV/XLSX: колонки username и first_message.</p><input aria-label="Импорт базы получателей" type="file" accept=".csv,.xlsx" onChange={e => { const file = e.target.files?.[0]; perform(() => uploadBase(file)); }} /></fieldset></section>}
    {tab === "dialogs" && <section className="automationSection"><h2>Диалоги ({dialogs.length})</h2>{!dialogs.length && <p>Диалоги появятся после отправки первых сообщений.</p>}{dialogs.map(d => <div className="automationDialog" key={d.id}><div><strong>@{d.username}</strong><small>Аккаунт #{d.account_id} · {labels[d.state] || d.state}</small></div><div className="automationDialogActions"><button disabled={busy} onClick={() => perform(async () => { await api(`${base}/dialogs/${d.id}/stop`, { method: "POST" }); await refresh(); })}>Остановить</button>{["manager", "partner"].map(kind => <button key={kind} disabled={busy} onClick={() => { if (window.confirm(`Передать историю @${d.username} ${kind === "manager" ? "менеджеру" : "партнёру"}? Отправка произойдёт при работающей кампании.`)) perform(async () => { await api(`${base}/dialogs/${d.id}/handoff/${kind}`, { method: "POST" }); await refresh(); }); }}>{kind === "manager" ? "Менеджеру" : "Партнёру"}</button>)}</div></div>)}</section>}
    {tab === "messages" && <>
      <section className="automationSection automationConsoleSection"><div className="automationLogHeader"><div><h2>Технический журнал</h2><p>Показывает каждый этап проверки диалогов, ИИ и Telegram. Обновляется каждые 10 секунд.</p></div><button className="button secondary" disabled={busy} onClick={() => perform(refresh)}>↻ Обновить</button></div><div className="automationLogFilters" aria-label="Фильтр журнала">{[["all", "Все", logs.length], ["info", "INFO", logCounts.info || 0], ["success", "Успешно", logCounts.success || 0], ["warning", "WARNING", logCounts.warning || 0], ["error", "ERROR", logCounts.error || 0]].map(([value, label, count]) => <button key={String(value)} className={logLevel === value ? "selected" : ""} onClick={() => setLogLevel(String(value))}>{label} <b>{count}</b></button>)}</div><div className="automationConsole" role="log" aria-live="polite">{!visibleLogs.length && <p className="automationConsoleEmpty">Для выбранного фильтра записей пока нет.</p>}{visibleLogs.map(item => <article className={`automationConsoleLine automationConsoleLine--${item.level}`} key={item.id}><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString("ru")}</time><b>{item.level.toUpperCase()}</b><div><span>{item.account_id ? `Аккаунт #${item.account_id}: ` : ""}{item.username ? `@${item.username}: ` : ""}{item.message}</span><small>{item.event}{detailText(item.details) ? ` · ${detailText(item.details)}` : ""}</small></div></article>)}</div></section>
      <section className="automationSection"><h2>История сообщений</h2><p>Последние 200 входящих и исходящих сообщений с результатом доставки.</p>{!messages.length && <p>Сообщений ещё не было.</p>}{messages.map(m => <article className="automationMessage" key={m.id}><div><strong>@{m.username} · {labels[m.kind] || m.kind}</strong><span>{labels[m.status] || m.status}</span></div><p>{m.body}</p><small>{new Date(m.created_at).toLocaleString("ru")} · Аккаунт #{m.account_id}</small>{m.error && <p role="alert">{m.error}</p>}</article>)}</section>
    </>}
    {tab === "blocks" && <section className="automationSection"><h2>Исключения кампании</h2><fieldset disabled={busy || running} className="automationFields">{area("blacklist", "Username через запятую")}<button className="button secondary" onClick={() => perform(save)}>Сохранить</button></fieldset><h2>Глобальный чёрный список</h2><p>Действует для всех кампаний и аккаунтов.</p><div className="automationGrid"><label>Telegram user ID<input inputMode="numeric" value={blockId} onChange={e => setBlockId(e.target.value)} /></label><label>Username (необязательно)<input value={blockUsername} onChange={e => setBlockUsername(e.target.value)} /></label></div><button className="button secondary" disabled={busy || !blockId} onClick={() => perform(async () => { await api("/telegram-blocklist", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ peer_id: Number(blockId), username: blockUsername || null }) }); setBlockId(""); setBlockUsername(""); await refresh(); })}>Заблокировать</button>{blocks.map(b => <div className="automationDialog" key={b.peer_id}><span>{b.peer_id} {b.username && `· @${b.username}`}</span><button disabled={busy} onClick={() => perform(async () => { await api(`/telegram-blocklist/${b.peer_id}`, { method: "DELETE" }); await refresh(); })}>Разблокировать</button></div>)}</section>}
  </div>;
}
