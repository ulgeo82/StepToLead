"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { count, money } from "@/components/reporting";

type Balance = Record<string, number | null> | null;
type LeadState = { chats: boolean; calls: boolean; calls_mode: string | null; calls_note: string | null;
  last_check_at: string | null; last_error: string | null; imported_chats: number; imported_calls: number;
  period?: { chats: number; calls: number; accepted: number } };
type Campaign = { id: string; name: string; status?: string | null; payment_model?: string | null;
  campaign_type?: string | null; budget?: number | null; start_date?: string | null; end_date?: string | null;
  spend: number; impressions: number; clicks: number; ctr: number | null; cpc: number | null; cpm: number | null };
type Overview = { connection_id: number; platform: string; name: string; status: string; external_account_id: string;
  profile_name: string | null; profile_url: string | null; balance: Balance; balance_at: string | null;
  last_synced_at: string | null; leads?: LeadState; campaigns?: Campaign[] };
type Listing = { id: string; title: string; url: string | null; status: string | null; price: number | null;
  category: string | null; impressions: number; views: number; contacts: number; contacts_phone: number;
  contacts_chat: number; favorites: number; spend: number; bonus: number; crm_leads: number;
  ctr: number | null; conversion: number | null; cost_per_contact: number | null };
type Listings = { listings: Listing[]; fetched_at: string; stale?: boolean; warning?: string };

export const avitoStatus: Record<string, string> = { draft: "Черновик", in_moderation: "На модерации",
  moderation_failed: "Модерация не пройдена", partial_moderation: "Частичная модерация", active: "Активна",
  pausing: "Приостанавливается", paused: "Приостановлена", unpausing: "Возобновляется", stopped: "Остановлена",
  finished: "Завершена", archived: "В архиве", will_launch_soon: "Запустится скоро", will_stop_soon: "Остановится скоро",
  ready_for_moderation: "Готов к модерации", erir_registration: "Регистрация в ЕРИР", old: "Завершено",
  removed: "Удалено", blocked: "Заблокировано", rejected: "Отклонено" };
const listingStatus: Record<string, string> = { active: "Активно", old: "Завершено", removed: "Удалено",
  blocked: "Заблокировано", rejected: "Отклонено" };
export const share = (value: number | null | undefined) => value == null ? "—" :
  `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value)}%`;
const when = (value: string | null | undefined) => value ? new Date(value).toLocaleString("ru-RU",
  { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";

function BalanceCard({ data }: { data: Overview }) {
  const b = data.balance || {};
  const real = b.real ?? b.balance ?? null;
  const bonus = b.bonus ?? b.bonusBalance ?? null;
  return <div className="avitoBalance"><div><span>Баланс</span><strong>{money(real as number | null)}</strong></div>
    <div><span>Бонусы</span><strong>{money(bonus as number | null)}</strong></div>
    <div><span>Профиль</span><strong>{data.profile_url ? <a href={data.profile_url} target="_blank" rel="noopener noreferrer">{data.profile_name || data.external_account_id}</a> : data.profile_name || data.external_account_id}</strong></div>
    <small>Обновлено {when(data.balance_at)}. Баланс и профиль обновляются при проверке и синхронизации.</small></div>;
}

function LeadsCard({ data, canManage, projectId, onChanged }: { data: Overview; canManage: boolean;
  projectId: number | null; onChanged: () => void }) {
  const state = data.leads!;
  const [chats, setChats] = useState(state.chats);
  const [calls, setCalls] = useState(state.calls);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => { setChats(state.chats); setCalls(state.calls); }, [state.chats, state.calls]);
  const dirty = chats !== state.chats || calls !== state.calls;
  async function run(name: string, action: () => Promise<string>) {
    setBusy(name); setMessage("");
    try { setMessage(await action()); onChanged(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(""); }
  }
  const save = () => run("save", async () => {
    await api(`/ads/connections/${data.connection_id}/avito/leads`, { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chats, calls }) });
    return chats || calls ? "Сохранено. Новые обращения будут попадать во «Входящие» CRM каждые несколько минут." : "Импорт заявок выключен.";
  });
  const check = () => run("check", async () => {
    const result = await api<{ imported: { chats: number; calls: number } }>(`/ads/connections/${data.connection_id}/avito/leads/check`,
      { method: "POST", timeoutMs: 180_000 });
    return `Проверено: новых чатов ${result.imported.chats}, звонков ${result.imported.calls}.`;
  });
  return <section className="resultPanel avitoCard"><div className="avitoCardHead"><div><h3>Заявки с Авито → CRM</h3>
    <p>Новые чаты по вашим объявлениям и звонки становятся заявками во «Входящих» CRM с объявлением, текстом и контактом. Лид засчитывается этому кабинету — так считаются CPL и ROMI Авито.</p></div>
    {projectId && <Link className="avitoLink" href={`/crm?project_id=${projectId}`}>Открыть CRM →</Link>}</div>
    <div className="avitoToggles"><label><input type="checkbox" checked={chats} disabled={!canManage} onChange={e => setChats(e.target.checked)}/><span><b>Чаты Авито Мессенджера</b><small>Каждый новый чат по объявлению — одна заявка. Импорт начинается с момента включения.</small></span></label>
      <label><input type="checkbox" checked={calls} disabled={!canManage} onChange={e => setCalls(e.target.checked)}/><span><b>Звонки</b><small>Через Коллтрекинг Авито или звонки по тарифу CPA — с номером покупателя.</small></span></label></div>
    {state.calls && state.calls_note && <p className="avitoWarn">{state.calls_note}</p>}
    {state.last_error && <p className="adsAccountError avitoInlineError">{state.last_error}</p>}
    <div className="avitoLeadStats"><span>За период: <b>{count(state.period?.chats ?? 0)}</b> чатов · <b>{count(state.period?.calls ?? 0)}</b> звонков · принято в работу <b>{count(state.period?.accepted ?? 0)}</b></span>
      <span>Всего импортировано: {count(state.imported_chats)} чатов, {count(state.imported_calls)} звонков{state.calls_mode && state.calls_mode !== "unavailable" ? ` · звонки через ${state.calls_mode === "cpa" ? "CPA" : "Коллтрекинг"}` : ""} · проверка {when(state.last_check_at)}</span></div>
    {canManage && <div className="avitoActions"><button className="adsBlueButton" disabled={!dirty || !!busy} onClick={save}>{busy === "save" ? "Сохраняем…" : "Сохранить"}</button>
      <button className="avitoGhost" disabled={!!busy || !(state.chats || state.calls) || data.status !== "connected"} onClick={check}>{busy === "check" ? "Проверяем…" : "Проверить сейчас"}</button></div>}
    {message && <p className="avitoMessage" role="status">{message}</p>}</section>;
}

function ListingsCard({ data, start, end }: { data: Overview; start: string; end: string }) {
  const [result, setResult] = useState<Listings | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  useEffect(() => { setResult(null); setError(""); }, [data.connection_id, start, end]);
  async function load() {
    setLoading(true); setError("");
    try { setResult(await api<Listings>(`/ads/connections/${data.connection_id}/avito/listings?${new URLSearchParams({ start, end })}`, { timeoutMs: 180_000 })); }
    catch (e) { setError((e as Error).message); } finally { setLoading(false); }
  }
  const rows = (result?.listings || []).filter(item => !filter || item.title.toLowerCase().includes(filter.toLowerCase()) || item.id.includes(filter));
  const total = rows.reduce((acc, item) => ({ spend: acc.spend + item.spend, views: acc.views + item.views,
    contacts: acc.contacts + item.contacts, leads: acc.leads + item.crm_leads }), { spend: 0, views: 0, contacts: 0, leads: 0 });
  return <section className="resultPanel avitoCard"><div className="avitoCardHead"><div><h3>Объявления</h3>
    <p>Показы, просмотры, контакты и расходы по каждому объявлению за выбранный период. Авито отдаёт эту статистику не чаще раза в минуту, поэтому она загружается по кнопке.</p></div>
    <button className="avitoGhost" disabled={loading || data.status !== "connected"} onClick={load}>{loading ? "Загружаем…" : result ? "↻ Обновить" : "Загрузить статистику"}</button></div>
    {error && <p className="adsAccountError avitoInlineError">{error}</p>}
    {result?.warning && <p className="avitoWarn">Показаны данные из кэша: {result.warning}</p>}
    {result && <><div className="avitoTableTools"><input placeholder="Поиск по названию или ID" value={filter} onChange={e => setFilter(e.target.value)}/><span>{rows.length} объявлений · {money(total.spend)} · {count(total.views)} просмотров · {count(total.contacts)} контактов · {count(total.leads)} заявок в CRM</span></div>
      <div className="avitoTableScroll"><table><thead><tr><th>Объявление</th><th>Показы</th><th>Просмотры</th><th>CTR</th><th>Контакты</th><th>Конверсия</th><th>Расход</th><th>Цена контакта</th><th>Заявки CRM</th></tr></thead>
        <tbody>{rows.map(item => <tr key={item.id}><td><div className="avitoListing">{item.url ? <a href={item.url} target="_blank" rel="noopener noreferrer">{item.title}</a> : <strong>{item.title}</strong>}<small>ID {item.id}{item.status ? ` · ${listingStatus[item.status] || item.status}` : ""}{item.price ? ` · ${money(item.price)}` : ""}</small></div></td>
          <td>{count(item.impressions)}</td><td>{count(item.views)}</td><td>{share(item.ctr)}</td>
          <td title={`Телефон: ${item.contacts_phone}, чат: ${item.contacts_chat}`}>{count(item.contacts)}<small className="avitoSub">☎ {item.contacts_phone} · ✉ {item.contacts_chat}</small></td>
          <td>{share(item.conversion)}</td><td>{money(item.spend)}{item.bonus > 0 && <small className="avitoSub">+ {money(item.bonus)} бонусами</small>}</td>
          <td>{money(item.cost_per_contact)}</td><td>{count(item.crm_leads)}</td></tr>)}</tbody></table>
        {!rows.length && <p className="resultTableEmpty">Нет объявлений с данными за период.</p>}</div></>}
  </section>;
}

function CampaignsCard({ data, start, end }: { data: Overview; start: string; end: string }) {
  const campaigns = data.campaigns || [];
  const query = new URLSearchParams({ start, end }).toString();
  return <section className="resultPanel avitoCard"><div className="avitoCardHead"><div><h3>Кампании Авито Рекламы</h3>
    <p>Медийные кампании кабинета: показы, клики и расход (деньгами, без бонусов) за период из последней синхронизации. Нажмите на кампанию, чтобы увидеть группы и креативы.</p></div>
    <span className="avitoMuted">{campaigns.length} кампаний</span></div>
    {!campaigns.length ? <p className="resultTableEmpty">Кампаний пока нет — запустите синхронизацию кабинета.</p> :
      <div className="avitoTableScroll"><table><thead><tr><th>Кампания</th><th>Статус</th><th>Модель</th><th>Показы</th><th>Клики</th><th>CTR</th><th>Расход</th><th>CPC</th><th>CPM</th></tr></thead>
        <tbody>{campaigns.map(item => <tr key={item.id}><td><Link className="avitoListing" href={`/ads/avito/${data.connection_id}/${item.id}?${query}`}><strong>{item.name}</strong><small>ID {item.id}{item.start_date ? ` · ${item.start_date}${item.end_date ? ` — ${item.end_date}` : ""}` : ""}</small></Link></td>
          <td><span className={`avitoPill ${item.status || ""}`}>{avitoStatus[item.status || ""] || item.status || "—"}</span></td>
          <td>{item.payment_model || "—"}{item.campaign_type && <small className="avitoSub">{item.campaign_type === "textImage" ? "Текст + изображение" : item.campaign_type === "video" ? "Видео" : item.campaign_type}</small>}</td>
          <td>{count(item.impressions)}</td><td>{count(item.clicks)}</td><td>{share(item.ctr)}</td><td>{money(item.spend)}</td><td>{money(item.cpc)}</td><td>{money(item.cpm)}</td></tr>)}</tbody></table></div>}
  </section>;
}

export function AvitoPanel({ connectionId, start, end, canManage, projectId, revision }: { connectionId: number;
  start: string; end: string; canManage: boolean; projectId: number | null; revision: number }) {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [local, setLocal] = useState(0);
  useEffect(() => { let active = true; setError("");
    api<Overview>(`/ads/connections/${connectionId}/avito?${new URLSearchParams({ start, end })}`)
      .then(value => { if (active) setData(value); }).catch(e => { if (active) setError((e as Error).message); });
    return () => { active = false; };
  }, [connectionId, start, end, revision, local]);
  if (error) return <p className="adsAccountError avitoInlineError">{error}</p>;
  if (!data) return <div className="resultLoading">Загружаем данные Авито…</div>;
  return <div className="avitoPanel"><BalanceCard data={data}/>
    {data.platform === "avito_items" ? <><LeadsCard data={data} canManage={canManage} projectId={projectId} onChanged={() => setLocal(v => v + 1)}/><ListingsCard data={data} start={start} end={end}/></>
      : <CampaignsCard data={data} start={start} end={end}/>}</div>;
}
