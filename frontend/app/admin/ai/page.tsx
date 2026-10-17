"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { AdminTokensConfig } from "@/components/ai-tokens";

type FunctionState = {feature: string; provider: string; model: string; key_set: boolean; configured: boolean};
type Overview = {functions: FunctionState[]; stt: FunctionState; limits: {global: number; workspace: number}; workspaces: {id: number; name: string}[]};
type Usage = {feature: string; model: string; workspace_id: number | null; requests: number; errors: number; tokens_in: number; tokens_out: number; audio_seconds: number; cost_rub: number; price_known: boolean};
const names: Record<string, string> = {chat: "Подсказка ответа", summary: "Резюме переписки", calls: "Анализ звонка", campaigns: "Кампании", stt: "Распознавание речи"};

type Field = {source: "portal" | "env" | null; value?: string; effective?: string | number; set?: boolean; hint?: string};
type Fields = Record<string, Field>;
const PROVIDERS = [["", "Не подключать"], ["anthropic", "Claude (Anthropic)"], ["openai", "OpenAI (ChatGPT)"], ["yandex", "YandexGPT"], ["openai_compatible", "Другой (OpenAI-совместимый API)"]];
const SECRETS = new Set(["llm_api_key", "stt_api_key", "XMLSTOCK_KEY", "DADATA_TOKEN", "YOOKASSA_SECRET_KEY"]);

function ConnectionSettings({onSaved}: {onSaved: () => void}) {
  const [fields, setFields] = useState<Fields | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [clear, setClear] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const load = (data: {fields: Fields}) => {
    setFields(data.fields); setClear([]);
    setForm(Object.fromEntries(Object.entries(data.fields).map(([k, f]) => [k, SECRETS.has(k) ? "" : String(f.value ?? "")])));
  };
  useEffect(() => { api<{fields: Fields}>("/admin/ai/settings").then(load).catch(e => setError(e.message)); }, []);
  if (!fields) return <section className="panel"><h2>Подключение ИИ и сервисов</h2>{error ? <p className="crmFormError">{error}</p> : <p>Загружаем…</p>}</section>;
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm(prev => ({...prev, [k]: e.target.value}));
  const hint = (k: string) => {
    const f = fields[k];
    if (SECRETS.has(k)) return clear.includes(k) ? "будет удалён" : f.set ? `сохранён ${f.hint}${f.source === "env" ? " (из .env на сервере)" : ""} — оставьте поле пустым, чтобы не менять` : "не задан";
    return f.source === "env" && !form[k] ? `сейчас из .env: ${f.effective}` : "";
  };
  const input = (k: string, label: string, placeholder = "") => <label key={k}>{label}
    <input type={SECRETS.has(k) ? "password" : "text"} autoComplete="off" value={form[k] || ""} onChange={set(k)} placeholder={placeholder} />
    {hint(k) && <small>{hint(k)}{SECRETS.has(k) && fields[k].source === "portal" && !clear.includes(k) && <> · <button type="button" className="linkButton" onClick={() => setClear(c => [...c, k])}>удалить ключ</button></>}</small>}</label>;
  const provider = form.llm_provider || (fields.llm_provider.source === "env" ? String(fields.llm_provider.effective || "") : "");
  const save = async () => {
    setBusy(true); setError(""); setMessage("");
    try {
      const values = Object.fromEntries(Object.entries(form).filter(([k, v]) => !SECRETS.has(k) || v));
      load(await api<{fields: Fields}>("/admin/ai/settings", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({values, clear})}));
      setMessage("Сохранено. Работает сразу, перезапуск сервера не нужен."); onSaved();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось сохранить"); }
    finally { setBusy(false); }
  };
  return <section className="panel aiSettings"><h2>Подключение ИИ и сервисов</h2>
    <p className="subtitle">Ключи хранятся в зашифрованном виде и после сохранения не показываются. Значение из портала главнее, чем в .env на сервере; пустое поле — берётся из .env.</p>
    <h3>ИИ для чатов, звонков и кампаний</h3>
    <label>Провайдер<select value={form.llm_provider || ""} onChange={set("llm_provider")}>{PROVIDERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>{hint("llm_provider") && <small>{hint("llm_provider")}</small>}</label>
    {input("llm_api_key", "API-ключ")}
    {input("llm_model", "Модель", provider === "anthropic" ? "claude-sonnet-5-5" : provider === "openai" ? "например gpt-5" : provider === "yandex" ? "gpt://<каталог>/yandexgpt/latest" : "")}
    {provider === "yandex" && input("yandex_folder_id", "ID каталога Yandex Cloud")}
    {provider === "openai_compatible" && input("llm_base_url", "Адрес API", "https://…/v1")}
    <details><summary>Отдельные модели для функций (необязательно)</summary>
      {input("llm_model_chat", "Подсказка ответа")}{input("llm_model_summary", "Резюме переписки")}{input("llm_model_calls", "Разбор звонков")}{input("llm_model_campaigns", "Кампании")}
      {input("ai_daily_token_limit", "Дневной лимит токенов, всего (0 — без лимита)")}{input("ai_workspace_daily_token_limit", "Дневной лимит токенов на компанию")}
    </details>
    <h3>Распознавание речи (звонки)</h3>
    <label>Провайдер<select value={form.stt_provider || ""} onChange={set("stt_provider")}><option value="">Как у ИИ</option><option value="openai">OpenAI (Whisper)</option><option value="yandex">Yandex SpeechKit</option></select>{hint("stt_provider") && <small>{hint("stt_provider")}</small>}</label>
    {input("stt_api_key", "API-ключ распознавания (если отличается от ключа ИИ)")}{input("stt_model", "Модель", "whisper-1")}
    <h3>Лидогенерация</h3>
    {input("XMLSTOCK_USER", "XMLStock: пользователь")}{input("XMLSTOCK_KEY", "XMLStock: ключ")}{input("XMLSTOCK_LIVE_URL", "XMLStock: адрес Live", "https://xmlstock.com/yandexlive/xml/")}
    {input("DADATA_TOKEN", "DaData: токен")}
    <h3>Оплата токенов клиентами (ЮKassa)</h3>
    {input("YOOKASSA_SHOP_ID", "ЮKassa: shopId")}{input("YOOKASSA_SECRET_KEY", "ЮKassa: секретный ключ")}
    <small>Адрес для уведомлений в кабинете ЮKassa: https://portal.steptolead.ru/api/public/yookassa (событие payment.succeeded).</small>
    {error && <p role="alert" className="crmFormError">{error}</p>}{message && <p className="aiSaved">{message}</p>}
    <button className="button primary" disabled={busy} onClick={save}>{busy ? "Сохраняем…" : "Сохранить"}</button>
  </section>;
}

export default function AIPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [rows, setRows] = useState<Usage[]>([]);
  const [days, setDays] = useState(1);
  const [workspace, setWorkspace] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [answers, setAnswers] = useState<Record<string, unknown>>({});
  const [file, setFile] = useState<File | null>(null);
  const refresh = async () => setRows((await api<{groups: Usage[]}>(`/admin/ai/usage?days=${days}`)).groups);
  const reload = () => api<Overview>("/admin/ai").then(setData).catch(e => setError(e.message));
  useEffect(() => { reload(); }, []);
  useEffect(() => { refresh().catch(e => setError(e.message)); }, [days]);
  const test = async (feature: string) => {
    setBusy(feature); setError("");
    try {
      let answer;
      if (feature === "stt") {
        if (!file) throw new Error("Выберите WAV с русской речью длительностью до 60 секунд");
        const body = new FormData(); body.append("file", file);
        const response = await fetch("/api/admin/ai/stt", {method: "POST", body, credentials: "same-origin"});
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Не удалось проверить распознавание");
        answer = result;
      } else {
        answer = await api(`/admin/ai/test/${feature}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({workspace_id: workspace ? Number(workspace) : null}), timeoutMs: 300000});
      }
      setAnswers(prev => ({...prev, [feature]: answer}));
    } catch (e) { setError(e instanceof Error ? e.message : "Ошибка проверки ИИ"); }
    finally { setBusy(""); await refresh().catch(() => {}); }
  };
  return <div className="page aiPage"><header className="topbar"><div className="crumbs"><span>StepToLead</span><b>/</b><strong>ИИ: подключение и расход</strong></div></header>
    <section className="pageHeader"><div><p className="eyebrow">Сервис</p><h1>ИИ: подключение и расход</h1><p className="subtitle">Ключи и модели задаются здесь, без правки сервера. Проверки платные. Используйте вымышленные данные. Периоды расхода — по UTC.</p></div></section>
    {error && <p role="alert" className="crmFormError">{error}</p>}
    <AdminTokensConfig />
    <ConnectionSettings onSaved={reload} />
    <section className="panel"><h2>Проверка</h2>
      <label>Компания для текстовых проверок <select value={workspace} onChange={e => setWorkspace(e.target.value)}><option value="">Без компании</option>{data?.workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}</select></label>
      <p>Дневной лимит: общий {data?.limits.global || "без ограничения"}; на компанию {data?.limits.workspace || "без ограничения"} токенов.</p>
      {[...(data?.functions || []), ...(data ? [data.stt] : [])].map(f => <div key={f.feature} className="aiFunction">
        <h3>{names[f.feature]}</h3><p>Провайдер: {f.provider || "не задан"} · Модель: {f.model || "не задана"} · Ключ: {f.key_set ? "задан" : "не задан"} · {f.configured ? "Настроено" : "Не настроено"}</p>
        {f.feature === "stt" && <label>WAV с русской фразой, до 60 секунд <input type="file" accept=".wav,audio/wav" onChange={e => setFile(e.target.files?.[0] || null)} /></label>}
        <button className="button primary" disabled={!!busy || !f.configured} onClick={() => test(f.feature)}>{busy === f.feature ? "Проверяем…" : "Проверить"}</button>
        {answers[f.feature] !== undefined && <pre style={{whiteSpace: "pre-wrap", overflowWrap: "anywhere"}}>{JSON.stringify(answers[f.feature], null, 2)}</pre>}
      </div>)}
      <p>Проверка кампаний использует серверную конфигурацию. Собственный ключ кампании проверяйте в её настройках. Тарифные права клиентов проверяются отдельно.</p>
    </section>
    <section className="panel"><h2>Расход</h2><select value={days} onChange={e => setDays(Number(e.target.value))}><option value={1}>Сегодня</option><option value={7}>7 дней</option><option value={30}>30 дней</option></select>
      <div style={{overflowX: "auto"}}><table><thead><tr>{["Функция", "Модель", "Компания", "Запросы", "Ошибки", "Токены вход / выход", "Аудио, сек.", "Оценка, ₽"].map(x => <th key={x}>{x}</th>)}</tr></thead><tbody>{rows.map((r, i) => <tr key={i}><td>{names[r.feature]}</td><td>{r.model}</td><td>{data?.workspaces.find(w => w.id === r.workspace_id)?.name || "Без компании"}</td><td>{r.requests}</td><td>{r.errors}</td><td>{r.tokens_in} / {r.tokens_out}</td><td>{r.audio_seconds.toFixed(1)}</td><td>{r.price_known ? r.cost_rub.toFixed(4) : "Цена не задана"}</td></tr>)}</tbody></table></div>
      {!rows.length && <p>Вызовов за выбранный период нет.</p>}<p>Стоимость приблизительная, по AI_PRICES. Стоимость секунд STT не включена.</p>
    </section>
  </div>;
}
