"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import "./ai-tokens.css";

type Pack = { tokens: number; price: number };
type Purchase = { id: number; created_at: string | null; tokens: number; price_rub: number; status: "pending" | "paid" | "canceled"; provider: string; comment: string | null; paid_at: string | null };
type TokenState = {
  plan: string | null; ai_included: boolean; unlimited: boolean; monthly: number | null; carryover: number; package: number | null;
  used: number; bought_left: number; remaining: number | null; capacity: number | null; share_left: number | null;
  level: "ok" | "low" | "empty" | "unlimited"; days_left: number | null; days_to_renew: number; renews_at: string; warn_share: number;
};
type ClientTokens = TokenState & { packs: Pack[]; can_buy: boolean; online_payment: boolean; purchases: Purchase[] };
type AdminWorkspace = TokenState & { reserve_limit: number; reserve_used: number; reserve_left: number; monthly_override: number | null; reserve_override: number | null; purchases: Purchase[] };
type Config = { monthly: Record<string, number>; reserve: Record<string, number>; packs: Pack[]; warn_share: number };
type AdminOverview = { config: Config; online_payment: boolean; pending: (Purchase & { workspace_id: number; workspace: string })[] };

const json = (method: string, body?: unknown) => ({ method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
export function tokens(n: number | null | undefined) {
  if (n == null) return "∞";
  if (n >= 1_000_000) return `${(n / 1_000_000).toLocaleString("ru-RU", { maximumFractionDigits: n % 1_000_000 ? 2 : 0 })} млн`;
  if (n >= 1000) return `${Math.round(n / 1000).toLocaleString("ru-RU")} тыс.`;
  return n.toLocaleString("ru-RU");
}
const rub = (n: number) => `${n.toLocaleString("ru-RU")} ₽`;
const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
const days = (n: number) => `${n} ${n % 10 === 1 && n % 100 !== 11 ? "день" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "дня" : "дней"}`;
const STATUS: Record<Purchase["status"], string> = { pending: "ждёт оплаты", paid: "зачислено", canceled: "отменено" };

/** Сколько дней хватит: с начала — 30+, при большом расходе — N дней. */
function forecast(s: TokenState) {
  if (s.level === "empty") return { chip: "0 дней", text: `Токены закончились — ИИ-функции на паузе. Пакет обновится ${day(s.renews_at)}.`, warn: true };
  const n = s.days_left;
  if (n === null || n >= 30) return { chip: "30+ дней", text: `При текущем расходе токенов хватит больше чем на месяц. Пакет обновится ${day(s.renews_at)}.`, warn: false };
  const short = n < s.days_to_renew;
  return { chip: `≈ ${days(n)}`, text: short ? `При текущем расходе токенов хватит примерно на ${days(n)} — раньше, чем обновится пакет (${day(s.renews_at)}).` : `При текущем расходе токенов хватит примерно на ${days(n)}. Пакет обновится ${day(s.renews_at)}.`, warn: short };
}

function Meter({ s }: { s: TokenState }) {
  const capacity = s.capacity || 0;
  const left = capacity ? Math.max(0, Math.min(100, Math.round(((s.remaining || 0) / capacity) * 100))) : 0;
  return <div className={`tokMeter ${s.level}`} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={left} aria-label="Остаток токенов ИИ">
    <i style={{ width: `${left}%` }} /></div>;
}

/** Настройки → «ИИ-токены»: остаток, прогноз на N дней и покупка пакета. Модель и резерв агентства не показываются. */
export function TokensCard() {
  const [data, setData] = useState<ClientTokens | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  useEffect(() => {
    const paid = new URLSearchParams(window.location.search).get("paid");
    const load = paid ? api<ClientTokens>(`/ai-tokens/purchase/${Number(paid)}/check`, json("POST")) : api<ClientTokens>("/ai-tokens");
    load.then(d => { setData(d); if (paid) setMessage(d.purchases.find(p => p.id === Number(paid))?.status === "paid" ? "Оплата прошла — токены зачислены." : "Оплата обрабатывается — токены зачислятся автоматически в течение пары минут."); })
      .catch(e => setError((e as Error).message));
  }, []);
  async function buy(index: number) {
    setBusy(index); setError(""); setMessage("");
    try {
      const out = await api<{ payment_url: string | null; message: string | null }>("/ai-tokens/purchase", json("POST", { pack: index }));
      if (out.payment_url) { window.location.href = out.payment_url; return; }
      setMessage(out.message || "Заявка отправлена"); setData(await api<ClientTokens>("/ai-tokens"));
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }
  if (!data) return <section className="resultPanel settingsCard"><h2>ИИ-токены</h2>{error ? <p className="tokError">{error}</p> : <p>Загружаем…</p>}</section>;
  if (data.unlimited) return <section className="resultPanel settingsCard tokCard"><h2>ИИ-токены</h2>
    <p>По вашему договору ИИ работает без лимита токенов. В этом месяце использовано {tokens(data.used)}.</p></section>;
  if (!data.ai_included && !data.bought_left) return <section className="resultPanel settingsCard tokCard"><h2>ИИ-токены</h2>
    <p>ИИ-подсказки, резюме и разбор звонков входят в тарифы «Рост» и «Система». Чтобы подключить, напишите вашему маркетологу StepToLead.</p></section>;
  const f = forecast(data);
  return <section className="resultPanel settingsCard tokCard">
    <div className="settingsCardHead"><div><h2>ИИ-токены</h2><p>Токены тратят ИИ-подсказки, резюме переписки, автоответы и разбор звонков. Неиспользованный пакет переносится на следующий месяц.</p></div>
      <span className={`tokChip ${f.warn ? "warn" : ""} ${data.level}`} title="Прогноз по расходу за последние 7 дней">{f.chip}</span></div>
    <div className="tokBig"><strong>{tokens(data.remaining)}</strong><span>осталось из {tokens(data.capacity)}</span></div>
    <Meter s={data}/>
    <p className={`tokForecast ${f.warn ? "warn" : ""}`}>{f.text}</p>
    {data.level === "low" && <p className="tokAlert">Осталось меньше {Math.round(data.warn_share * 100)}% токенов — пополните заранее, чтобы ИИ не остановился.</p>}
    {data.level === "empty" && <p className="tokAlert empty">Токены закончились: подсказки, резюме и разбор звонков остановлены. Пополните, чтобы продолжить.</p>}
    <dl className="tokFacts">
      <div><dt>Пакет месяца</dt><dd>{tokens(data.monthly)}</dd></div>
      {data.carryover > 0 && <div><dt>Перенос с прошлого месяца</dt><dd>+{tokens(data.carryover)}</dd></div>}
      <div><dt>Купленные, остаток</dt><dd>{tokens(data.bought_left)}</dd></div>
      <div><dt>Израсходовано в этом месяце</dt><dd>{tokens(data.used)}</dd></div>
    </dl>
    {data.can_buy && <><h3 className="tokSub">Пополнить</h3>
      <div className="tokPacks">{data.packs.map((p, i) => <button key={i} disabled={busy !== null} onClick={() => buy(i)}>
        <b>{tokens(p.tokens)} токенов</b><span>{rub(p.price)}</span><small>{busy === i ? "Открываем оплату…" : data.online_payment ? "Картой или СБП" : "По счёту"}</small></button>)}</div>
      <small className="tokNote">{data.online_payment ? "Оплата через ЮKassa — токены зачисляются сразу после оплаты и не сгорают." : "Мы пришлём счёт — токены зачислятся после оплаты и не сгорают."}</small></>}
    {!data.can_buy && data.ai_included && <small className="tokNote">Пополнить токены может владелец аккаунта.</small>}
    {message && <p className="tokOk">{message}</p>}{error && <p className="tokError">{error}</p>}
    {!!data.purchases.length && <ul className="tokHistory">{data.purchases.map(p => <li key={p.id}><span>{p.created_at ? new Date(p.created_at).toLocaleDateString("ru-RU") : ""}</span>
      <b>+{tokens(p.tokens)}</b><span>{p.price_rub ? rub(p.price_rub) : "бонус"}</span><em className={p.status}>{STATUS[p.status]}</em></li>)}</ul>}
  </section>;
}

const PLAN_NAMES: [string, string][] = [["start", "Старт"], ["growth", "Рост"], ["system", "Система"], ["trial", "Демо"], ["individual", "Индивидуальный"]];
const millions = (n: number | undefined) => n == null ? "" : String(n / 1_000_000);
const fromMillions = (v: string) => Math.round(Number(v.replace(",", ".") || 0) * 1_000_000);

/** Админка → ИИ: сколько токенов по тарифам, скрытый резерв, пакеты и заявки на оплату. */
export function AdminTokensConfig() {
  const [data, setData] = useState<AdminOverview | null>(null);
  const [form, setForm] = useState<{ monthly: Record<string, string>; reserve: Record<string, string>; packs: { tokens: string; price: string }[]; warn: string } | null>(null);
  const [message, setMessage] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const fill = (d: AdminOverview) => { setData(d); setForm({ monthly: Object.fromEntries(Object.entries(d.config.monthly).map(([k, v]) => [k, millions(v)])),
    reserve: Object.fromEntries(Object.entries(d.config.reserve).map(([k, v]) => [k, millions(v)])),
    packs: d.config.packs.map(p => ({ tokens: millions(p.tokens), price: String(p.price) })), warn: String(Math.round(d.config.warn_share * 100)) }); };
  const reload = () => api<AdminOverview>("/portal/admin/ai-tokens").then(fill).catch(e => setError((e as Error).message));
  useEffect(() => { reload(); }, []);
  if (!data || !form) return <section className="panel"><h2>Токены клиентов</h2>{error ? <p className="crmFormError">{error}</p> : <p>Загружаем…</p>}</section>;
  async function save() {
    if (!form) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const num = (o: Record<string, string>) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, fromMillions(v)]));
      await api("/portal/admin/ai-tokens/config", json("PUT", { monthly: num(form.monthly), reserve: num(form.reserve), warn_share: Number(form.warn) / 100,
        packs: form.packs.filter(p => p.tokens && p.price).map(p => ({ tokens: fromMillions(p.tokens), price: Math.round(Number(p.price)) })) }));
      await reload(); setMessage("Сохранено — лимиты клиентов пересчитаны сразу.");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function act(id: number, action: "paid" | "cancel") {
    if (action === "cancel" && !window.confirm("Отменить заявку?")) return;
    try { fill(await api<AdminOverview>(`/portal/admin/ai-tokens/purchases/${id}/${action}`, json("POST"))); } catch (e) { setError((e as Error).message); }
  }
  const cell = (group: "monthly" | "reserve", key: string) => <input inputMode="decimal" value={form[group][key] ?? ""} onChange={e => setForm({ ...form, [group]: { ...form[group], [key]: e.target.value } })} aria-label={`${group} ${key}`}/>;
  return <section className="panel tokAdmin"><h2>Токены клиентов</h2>
    <p className="subtitle">В миллионах токенов в месяц. Клиент видит только свой пакет и остаток — модель и резерв скрыты. Резерв тратят обязательные отчёты (бриф, ИИ-выводы в отчётах), пока он не закончится — потом пакет клиента.</p>
    <div className="tokTable"><table><thead><tr><th>Тариф</th><th>Пакет клиенту, млн</th><th>Скрытый резерв, млн</th></tr></thead>
      <tbody>{PLAN_NAMES.map(([k, name]) => <tr key={k}><td>{name}</td><td>{k === "individual" ? <small>без лимита, если не задан у клиента</small> : cell("monthly", k)}</td><td>{cell("reserve", k)}</td></tr>)}</tbody></table></div>
    <h3>Пакеты для покупки</h3>
    <div className="tokPackEdit">{form.packs.map((p, i) => <div key={i}><label>Токенов, млн<input inputMode="decimal" value={p.tokens} onChange={e => setForm({ ...form, packs: form.packs.map((x, j) => j === i ? { ...x, tokens: e.target.value } : x) })}/></label>
      <label>Цена, ₽<input inputMode="numeric" value={p.price} onChange={e => setForm({ ...form, packs: form.packs.map((x, j) => j === i ? { ...x, price: e.target.value } : x) })}/></label>
      <button type="button" className="linkButton" onClick={() => setForm({ ...form, packs: form.packs.filter((_, j) => j !== i) })}>убрать</button></div>)}
      {form.packs.length < 6 && <button type="button" className="linkButton" onClick={() => setForm({ ...form, packs: [...form.packs, { tokens: "", price: "" }] })}>＋ пакет</button>}</div>
    <label className="tokWarn">Предупреждать клиента, когда осталось, %<input inputMode="numeric" value={form.warn} onChange={e => setForm({ ...form, warn: e.target.value })}/></label>
    <p className="subtitle">Оплата онлайн: {data.online_payment ? "ЮKassa подключена — клиент платит картой или СБП, токены зачисляются сами." : "ЮKassa не подключена (ключи — в «Подключение ИИ и сервисов» ниже). Пока клиент отправляет заявку, а вы подтверждаете оплату здесь."}</p>
    {error && <p className="crmFormError">{error}</p>}{message && <p className="aiSaved">{message}</p>}
    <button className="button primary" disabled={busy} onClick={save}>{busy ? "Сохраняем…" : "Сохранить"}</button>
    <h3>Заявки на оплату</h3>
    {data.pending.length ? <ul className="tokPending">{data.pending.map(p => <li key={p.id}><b>{p.workspace}</b><span>{tokens(p.tokens)} · {rub(p.price_rub)}</span>
      <small>{p.provider === "yookassa" ? "ЮKassa, ждём оплату" : "по счёту"}</small>
      <button className="button primary" onClick={() => act(p.id, "paid")}>Оплачено — зачислить</button><button className="linkButton" onClick={() => act(p.id, "cancel")}>отменить</button></li>)}</ul>
      : <p className="subtitle">Новых заявок нет.</p>}
  </section>;
}

/** Админка → клиент: остаток с резервом, свой лимит, ручное начисление. */
export function AdminTokensClient({ workspaceId }: { workspaceId: number }) {
  const [data, setData] = useState<AdminWorkspace | null>(null);
  const [monthly, setMonthly] = useState(""); const [reserve, setReserve] = useState(""); const [credit, setCredit] = useState("");
  const [message, setMessage] = useState(""); const [busy, setBusy] = useState(false);
  const fill = (d: AdminWorkspace) => { setData(d); setMonthly(d.monthly_override == null ? "" : millions(d.monthly_override)); setReserve(d.reserve_override == null ? "" : millions(d.reserve_override)); };
  useEffect(() => { api<AdminWorkspace>(`/portal/admin/ai-tokens/workspaces/${workspaceId}`).then(fill).catch(e => setMessage((e as Error).message)); }, [workspaceId]);
  async function run(path: string, method: string, body: unknown, done: string) {
    setBusy(true); setMessage("");
    try { fill(await api<AdminWorkspace>(`/portal/admin/ai-tokens/workspaces/${workspaceId}${path}`, json(method, body))); setMessage(done); }
    catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  if (!data) return message ? <p className="notice error">{message}</p> : null;
  const f = forecast(data);
  return <section className="panel tokAdmin"><div className="panelHead"><div><p className="eyebrow">ИИ-токены</p><h2>{data.unlimited ? "Без лимита" : `Осталось ${tokens(data.remaining)} из ${tokens(data.capacity)}`}</h2></div>{!data.unlimited && <span className={`tokChip ${f.warn ? "warn" : ""} ${data.level}`}>{f.chip}</span>}</div>
    {!data.unlimited && <><Meter s={data}/><p className={`tokForecast ${f.warn ? "warn" : ""}`}>{f.text}</p></>}
    <dl className="tokFacts">
      <div><dt>Пакет месяца</dt><dd>{tokens(data.monthly)}{data.monthly_override != null ? " (свой)" : ""}</dd></div>
      <div><dt>Перенос</dt><dd>{tokens(data.carryover)}</dd></div>
      <div><dt>Купленные, остаток</dt><dd>{tokens(data.bought_left)}</dd></div>
      <div><dt>Израсходовано клиентом</dt><dd>{tokens(data.used)}</dd></div>
      <div><dt>Резерв на отчёты (скрыт)</dt><dd>{tokens(data.reserve_used)} из {tokens(data.reserve_limit)}</dd></div>
    </dl>
    <div className="tokPackEdit"><div><label>Свой пакет, млн/мес<input inputMode="decimal" placeholder="по тарифу" value={monthly} onChange={e => setMonthly(e.target.value)}/></label>
      <label>Свой резерв, млн<input inputMode="decimal" placeholder="по тарифу" value={reserve} onChange={e => setReserve(e.target.value)}/></label>
      <button className="button" disabled={busy} onClick={() => run("", "PUT", { monthly_override: monthly.trim() ? fromMillions(monthly) : null, reserve_override: reserve.trim() ? fromMillions(reserve) : null }, "Лимиты сохранены")}>Сохранить</button></div>
      <div><label>Начислить токены, млн<input inputMode="decimal" value={credit} onChange={e => setCredit(e.target.value)}/></label>
        <button className="button" disabled={busy || !(fromMillions(credit) > 0)} onClick={() => run("/credit", "POST", { tokens: fromMillions(credit), comment: "Начислено агентством" }, "Токены начислены").then(() => setCredit(""))}>Начислить</button></div></div>
    {message && <p className="planMessage">{message}</p>}
    {!!data.purchases.length && <ul className="tokHistory">{data.purchases.map(p => <li key={p.id}><span>{p.created_at ? new Date(p.created_at).toLocaleDateString("ru-RU") : ""}</span>
      <b>+{tokens(p.tokens)}</b><span>{p.price_rub ? rub(p.price_rub) : p.comment || "бонус"}</span><em className={p.status}>{STATUS[p.status]}</em></li>)}</ul>}
  </section>;
}

/* ------------------------------------------------------------------ состояние для всего портала */

type Mini = Pick<TokenState, "ai_included" | "unlimited" | "level" | "remaining" | "capacity" | "days_left" | "days_to_renew" | "renews_at" | "bought_left" | "warn_share"> & { can_buy?: boolean };
let cache: { at: number; value: Mini | null } | null = null;
let pending: Promise<Mini | null> | null = null;
const listeners = new Set<(v: Mini | null) => void>();

/** Остаток токенов одной загрузкой на страницу (кэш 60 с). refreshAiTokens() — после ИИ-действия. */
export function useAiTokens(): Mini | null {
  const [value, setValue] = useState<Mini | null>(cache?.value ?? null);
  useEffect(() => {
    listeners.add(setValue);
    if (!cache || Date.now() - cache.at > 60_000) void refreshAiTokens();
    return () => { listeners.delete(setValue); };
  }, []);
  return value;
}

export function refreshAiTokens(): Promise<Mini | null> {
  if (!pending) {
    pending = api<Mini>("/ai-tokens").catch(() => null).then(v => {
      cache = { at: Date.now(), value: v }; pending = null;
      listeners.forEach(fn => fn(v));
      return v;
    });
  }
  return pending;
}

const shown = (s: Mini | null): s is Mini => !!s && !s.unlimited && (s.ai_included || s.bought_left > 0);

/** Сайдбар клиента: компактная полоса остатка и «хватит на N дней». */
export function AiTokensMini({ href }: { href?: string }) {
  const s = useAiTokens();
  if (!shown(s)) return null;
  const f = forecast(s as TokenState);
  const body = <>
    <span className="tokMiniHead"><span>✦ ИИ-токены</span><b>{tokens(s.remaining)}</b></span>
    <Meter s={s as TokenState}/>
    <span className={`tokMiniNote ${s.level}`}>{s.level === "empty" ? "Закончились — ИИ на паузе" : s.level === "low" ? `Мало: ${f.chip}` : `Хватит на ${f.chip}`}</span>
  </>;
  return href ? <a className={`tokMini ${s.level}`} href={href} title="Подробнее и пополнение">{body}</a>
    : <div className={`tokMini ${s.level}`}>{body}</div>;
}

/** Рядом с ИИ-кнопками: предупреждение, когда токенов мало или они закончились. */
export function AiTokensHint({ settingsHref }: { settingsHref?: string }) {
  const s = useAiTokens();
  if (!shown(s) || s.level === "ok") return null;
  return <p className={`tokHint ${s.level}`} role="status">
    {s.level === "empty" ? "Токены ИИ закончились — подсказки и резюме на паузе." : `Токены ИИ заканчиваются: ${forecast(s as TokenState).chip}.`}
    {settingsHref && s.can_buy ? <> <a href={settingsHref}>{s.level === "empty" ? "Пополнить" : "Подробнее"}</a></>
      : <> Пополнить может владелец аккаунта.</>}
  </p>;
}

/** true — кнопки ИИ отключаем заранее, а не после ошибки. */
export function aiTokensEmpty(s: Mini | null) {
  return shown(s) && s.level === "empty";
}
