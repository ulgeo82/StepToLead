"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

const json = (method: string, body?: unknown) => ({ method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
const when = (value?: string | null) => value ? new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";

type Conversions = { can_manage: boolean; kinds: Record<string, string>;
  stats: Record<string, { pending: number; sent: number; no_id: number; error: number }>;
  metrika: { connected: boolean; counter_id: number | null; counter_name: string | null; last_error: string | null;
    last_upload_at: string | null; last_upload_rows: number | null } };

/** Settings → «Подключения»: sales from the CRM back to Yandex Metrica (Direct strategies) and a CSV for VK Ads. */
export function ConversionsCard({ projectId }: { projectId: number }) {
  const [data, setData] = useState<Conversions | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api<Conversions>(`/crm/projects/${projectId}/conversions`).then(setData).catch(e => setError((e as Error).message)), [projectId]);
  useEffect(() => { load(); }, [load]);
  async function run(action: () => Promise<unknown>, message: string) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); setNotice(message); await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget);
    await run(() => api(`/crm/projects/${projectId}/metrika`, json("PUT", { counter_id: Number(f.get("counter_id")), token: String(f.get("token") || "").trim() })),
      "Метрика подключена: цели «StepToLead: целевой лид» и «StepToLead: продажа» созданы в счётчике");
  }
  if (!data) return error ? <section className="resultPanel settingsCard"><p className="crmFormError">{error}</p></section> : null;
  const m = data.metrika;
  return <section className="resultPanel settingsCard"><h2>Продажи — обратно в рекламу</h2>
    <p>Когда сделка становится целевой или закрывается продажей, портал отправляет это в Яндекс Метрику. Стратегии Директа начинают искать не просто заявки, а покупателей. Для VK Рекламы — выгрузка в CSV для раздела «Офлайн-конверсии».</p>
    {error && <p className="crmFormError">{error}</p>}{notice && <p className="crmOk">{notice}</p>}
    <div className="convStats">{Object.entries(data.kinds).map(([kind, label]) => { const s = data.stats[kind] || { pending: 0, sent: 0, no_id: 0, error: 0 };
      return <div key={kind}><span>{label}</span><b>{s.sent + s.pending + s.no_id}</b><small>отправлено в Метрику {s.sent} · в очереди {s.pending} · без метки визита {s.no_id}</small></div>; })}</div>
    {m.connected ? <div className="convMetrika"><span className="integrationDot connected"/><div><b>Метрика: {m.counter_name || m.counter_id}</b>
        <small>{m.last_upload_at ? `последняя отправка ${when(m.last_upload_at)}, строк: ${m.last_upload_rows}` : "отправка раз в час"}</small>
        {m.last_error && <small className="chatError">{m.last_error}</small>}</div>
      {data.can_manage && <><button className="crmGhost" disabled={busy} onClick={() => run(() => api(`/crm/projects/${projectId}/conversions/upload`, json("POST")), "Отправлено")}>Отправить сейчас</button>
        <button className="crmGhost" disabled={busy} onClick={() => run(() => api(`/crm/projects/${projectId}/metrika`, json("DELETE")), "Метрика отключена")}>Отключить</button></>}</div>
      : data.can_manage && <form className="convForm" onSubmit={connect}>
        <label>Номер счётчика Метрики<input name="counter_id" inputMode="numeric" pattern="\d+" required placeholder="12345678"/></label>
        <label>OAuth-токен Яндекса (права на изменение счётчика)<input name="token" required autoComplete="off" type="password"/></label>
        <button className="crmPrimary" disabled={busy}>{busy ? "Проверяем…" : "Подключить Метрику"}</button></form>}
    <p className="convHint">Метрика сопоставляет продажу с визитом по метке клика (yclid) или ClientId — их собирает код отслеживания StepToLead на сайте. Заявки без визита на сайт (звонки, Авито) уходят только в CSV.</p>
    {data.can_manage && <a className="crmGhost convCsv" href={`/api/crm/projects/${projectId}/conversions.csv`}>⬇ Скачать CSV для VK Рекламы</a>}
  </section>;
}

type AiSettings = { knowledge: string; tone: string; goal: string; configured: boolean; provider: string | null; can_manage: boolean;
  calls: boolean; call_checklist: string; calls_available: boolean; default_checklist: string; chat_allowed?: boolean };

/** Settings → «Подключения»: the knowledge base the AI assistant answers from. */
export function AiSettingsCard({ projectId }: { projectId: number }) {
  const [data, setData] = useState<AiSettings | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { api<AiSettings>(`/crm/projects/${projectId}/ai-settings`).then(setData).catch(e => setError((e as Error).message)); }, [projectId]);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget); setError(""); setNotice("");
    try { setData(await api<AiSettings>(`/crm/projects/${projectId}/ai-settings`, json("PUT", { knowledge: f.get("knowledge"), tone: f.get("tone"), goal: f.get("goal"),
      calls: f.get("calls") === "on", call_checklist: f.get("call_checklist") || "" }))); setNotice("Сохранено"); }
    catch (e) { setError((e as Error).message); }
  }
  if (!data) return null;
  return <section className="resultPanel settingsCard"><h2>ИИ-помощник: чаты и звонки</h2>
    {data.chat_allowed === false && <p className="crmFormError">ИИ-подсказки и резюме в чатах входят в тариф «Рост», разбор звонков — в «Систему». Базу знаний можно заполнить заранее.</p>}
    <p>{data.configured ? `Подключён: ${data.provider}. ` : "Сервис ИИ ещё не подключён на сервере — базу знаний можно заполнить заранее. "}
      В чате появятся кнопки «Подсказать ответ» и «Резюме переписки». ИИ только пишет черновик — отправляет менеджер. Факты ИИ берёт только отсюда.</p>
    {error && <p className="crmFormError">{error}</p>}{notice && <p className="crmOk">{notice}</p>}
    <form className="aiForm" onSubmit={save}>
      <label>База знаний: цены, сроки, гарантия, что входит, адрес, частые вопросы<textarea name="knowledge" rows={10} maxLength={8000} defaultValue={data.knowledge} disabled={!data.can_manage}
        placeholder={"Кухни на заказ от 60 000 ₽ за погонный метр, фасады МДФ и пластик.\nЗамер и дизайн-проект бесплатно, в Самаре и пригороде.\nСрок изготовления 25–35 рабочих дней. Гарантия 2 года.\nРассрочка 0% на 6 месяцев через банк-партнёр."}/></label>
      <label>Цель диалога<input name="goal" maxLength={300} defaultValue={data.goal} disabled={!data.can_manage} placeholder="Записать на бесплатный замер"/></label>
      <label>Тон<input name="tone" maxLength={300} defaultValue={data.tone} disabled={!data.can_manage} placeholder="Дружелюбно, на «вы», без смайликов"/></label>
      <h3>Разбор звонков</h3>
      <p className="crmMuted">{data.calls_available ? "Каждый состоявшийся звонок дольше 25 секунд расшифровывается: резюме, следующий шаг и оценка менеджера по чек-листу появятся в карточке звонка и сделки."
        : "Нужен ключ ИИ и распознавания речи на сервере (SpeechKit). Чек-лист можно заполнить заранее."}</p>
      <label className="aiCheck"><input type="checkbox" name="calls" defaultChecked={data.calls} disabled={!data.can_manage}/> Разбирать звонки автоматически</label>
      <label>Чек-лист скрипта продаж — по пункту в строке<textarea name="call_checklist" rows={6} maxLength={2000} defaultValue={data.call_checklist} disabled={!data.can_manage}
        placeholder={data.default_checklist}/></label>
      {data.can_manage && <button className="crmPrimary">Сохранить</button>}</form></section>;
}

type Care = { reminders: boolean; reminder_hours: number; reminder_text: string; review: boolean; review_days: number; review_url: string;
  review_text: string; repeat: boolean; repeat_days: number; repeat_text: string };
type CareView = { care: Care; defaults: Care; can_manage: boolean; stats: Record<string, { sent: number; task: number; failed: number; skipped: number }> };

/** Settings → «Воронка»: meeting reminders, review requests after a sale, repeat-sale tasks. */
export function CareCard({ projectId }: { projectId: number }) {
  const [data, setData] = useState<CareView | null>(null);
  const [c, setC] = useState<Care | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { api<CareView>(`/crm/projects/${projectId}/care`).then(d => { setData(d); setC(d.care); }).catch(e => setError((e as Error).message)); }, [projectId]);
  const set = (patch: Partial<Care>) => { setC(v => v && { ...v, ...patch }); setNotice(""); };
  async function save(event: FormEvent) {
    event.preventDefault(); setError(""); setNotice("");
    try { const d = await api<CareView>(`/crm/projects/${projectId}/care`, json("PUT", c)); setData(d); setC(d.care); setNotice("Сохранено"); }
    catch (e) { setError((e as Error).message); }
  }
  if (!data || !c) return error ? <section className="resultPanel settingsCard"><p className="crmFormError">{error}</p></section> : null;
  const off = !data.can_manage;
  const s = (k: string) => data.stats[k] || { sent: 0, task: 0, failed: 0, skipped: 0 };
  return <section className="resultPanel settingsCard"><h2>Забота о клиенте</h2>
    <p>Портал сам напомнит клиенту о встрече, попросит отзыв после покупки и напомнит менеджеру о повторной продаже. Сообщения уходят в чат сделки (WhatsApp, Telegram, Авито) с 9:00 до 21:00; если написать некуда — менеджер получает задачу с готовым текстом.</p>
    {error && <p className="crmFormError">{error}</p>}{notice && <p className="crmOk">{notice}</p>}
    <form className="aiForm careForm" onSubmit={save}>
      <h3>Напоминание о встрече</h3>
      <label className="aiCheck"><input type="checkbox" checked={c.reminders} disabled={off} onChange={e => set({ reminders: e.target.checked })}/> Напоминать клиенту о встречах и замерах (задачи типа «Встреча»)</label>
      {c.reminders && <><label>За сколько часов<input type="number" min={2} max={72} value={c.reminder_hours} disabled={off} onChange={e => set({ reminder_hours: Number(e.target.value) || 24 })}/></label>
        <label>Текст <small>— {"{name}"}, {"{date}"}, {"{time}"}, {"{what}"}, {"{company}"}</small><textarea rows={3} maxLength={600} value={c.reminder_text} disabled={off} onChange={e => set({ reminder_text: e.target.value })}/></label></>}
      <p className="crmMuted">За 30 дней: отправлено {s("reminder").sent}{s("reminder").skipped ? `, некуда написать ${s("reminder").skipped}` : ""}{s("reminder").failed ? `, ошибок ${s("reminder").failed}` : ""}</p>

      <h3>Отзыв после покупки</h3>
      <label className="aiCheck"><input type="checkbox" checked={c.review} disabled={off} onChange={e => set({ review: e.target.checked })}/> Просить отзыв после подтверждённой продажи</label>
      {c.review && <><div className="careRow"><label>Через, дней<input type="number" min={1} max={60} value={c.review_days} disabled={off} onChange={e => set({ review_days: Number(e.target.value) || 3 })}/></label>
        <label className="grow">Где оставить отзыв<input type="url" value={c.review_url} disabled={off} placeholder="https://yandex.ru/maps/org/..." onChange={e => set({ review_url: e.target.value })}/></label></div>
        <label>Текст <small>— {"{name}"}, {"{company}"}, {"{review_url}"}</small><textarea rows={3} maxLength={600} value={c.review_text} disabled={off} onChange={e => set({ review_text: e.target.value })}/></label></>}
      <p className="crmMuted">За 30 дней: отправлено {s("review").sent}, задач менеджерам {s("review").task}</p>

      <h3>Повторная продажа</h3>
      <label className="aiCheck"><input type="checkbox" checked={c.repeat} disabled={off} onChange={e => set({ repeat: e.target.checked })}/> Ставить менеджеру задачу связаться с клиентом снова</label>
      {c.repeat && <><label>Через, дней после покупки<input type="number" min={7} max={1095} value={c.repeat_days} disabled={off} onChange={e => set({ repeat_days: Number(e.target.value) || 180 })}/></label>
        <label>Описание задачи <small>— {"{name}"}, {"{days}"}</small><textarea rows={2} maxLength={600} value={c.repeat_text} disabled={off} onChange={e => set({ repeat_text: e.target.value })}/></label></>}
      <p className="crmMuted">За 30 дней: задач {s("repeat").task}</p>
      {data.can_manage && <button className="crmPrimary">Сохранить</button>}</form></section>;
}
