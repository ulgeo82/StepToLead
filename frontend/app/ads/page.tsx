"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { count, dateInput, money, percent, PeriodControls, ProjectSidebar } from "@/components/reporting";
import { AvitoPanel } from "@/components/avito-panel";
import { MapsPanel } from "@/components/maps-panel";
import "../result/result.css";
import "./ads.css";

type Project = { id: number; name: string; organization_name: string };
type Metric = "spend" | "clicks" | "leads" | "cpc" | "cpl";
type Point = { label: string; spend: number | null; clicks: number | null; leads: number | null;
  cpc: number | null; cpl: number | null };
type Metrics = { spend: number | null; clicks: number | null; cpc: number | null; leads: number | null;
  cpl: number | null; sales: number | null; revenue: number | null; romi: number | null; trend: Point[] };
type Account = { id: number; name: string; external_account_id: string; status: string; last_error: string | null;
  last_synced_at: string | null; current: Metrics; previous: Metrics };
type Platform = { id: string; name: string; account_count: number; connected_count: number; status: string;
  last_synced_at: string | null; current: Metrics; previous: Metrics; accounts: Account[] };
type Ads = { current: Metrics; previous: Metrics; platforms: Platform[];
  viewer: { role: string; can_manage_integrations: boolean };
  supported_platforms: { id: string; name: string }[] };
type RemoteCampaign = { id: string; name: string; status: string | null; objective: string | null; budget_limit_day: string | number | null };
const asDate = (value: string | null) => value ? new Date(value).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const valueOf = (metric: Metric | "romi", value: number | null | undefined) => metric === "spend" || metric === "cpc" || metric === "cpl" ? money(value) : metric === "romi" ? percent(value) : count(value);
const credentialPlatforms = ["vk_ads", "avito_items", "avito_ads"];
const usesClientKeys = (platform: string | null | undefined) => credentialPlatforms.includes(platform || "");
const mapPlatforms = ["yandex_maps", "2gis"];
const isMap = (id: string | null | undefined) => mapPlatforms.includes(id || "");
const logoText = (id: string, name: string) => id === "yandex_maps" ? "ЯК" : id === "2gis" ? "2Г" : id === "yandex" ? "Я" : id === "meta" ? "M" : id === "vk_ads" ? "VK" : id.startsWith("avito") ? "А" : name.slice(0, 1);
const statusText: Record<string, string> = { connected: "Подключено", syncing: "Синхронизация", error: "Ошибка", disconnected: "Отключено", pending: "Не проверено" };

function Sparkline({ points }: { points: Point[] }) {
  const values = points.map(point => point.spend);
  const available = values.filter((value): value is number => value != null);
  if (!available.length) return <span className="adsSparkEmpty">—</span>;
  const max = Math.max(...available, 1);
  const polyline = values.map((value, index) => `${index * 150 / Math.max(1, values.length - 1)},${36 - (value || 0) / max * 32}`).join(" ");
  return <svg className="adsSpark" viewBox="0 0 150 40" role="img" aria-label="Динамика расходов"><polyline points={polyline} fill="none" stroke="#006bfd" strokeWidth="2" strokeLinejoin="round"/></svg>;
}

export default function AdsPage() {
  const today = useMemo(() => new Date(), []);
  const [start, setStart] = useState(dateInput(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 29)));
  const [end, setEnd] = useState(dateInput(today));
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [data, setData] = useState<Ads | null>(null);
  const [granularity, setGranularity] = useState("day");
  const [selectedPlatform, setSelectedPlatform] = useState<string | null>(null);
  const [selectedAccount, setSelectedAccount] = useState<number | null>(null);
  const [modal, setModal] = useState<"connect" | "token" | null>(null);
  const [connectPlatform, setConnectPlatform] = useState("yandex");
  const [tokenAccount, setTokenAccount] = useState<Account | null>(null);
  const [busy, setBusy] = useState<string | number | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [remoteCampaigns, setRemoteCampaigns] = useState<RemoteCampaign[]>([]);
  const [remoteError, setRemoteError] = useState("");
  const [remoteLoading, setRemoteLoading] = useState(false);
  useEffect(() => { api<Project[]>("/result/projects").then(rows => {
    setProjects(rows);
    const desired = Number(new URLSearchParams(window.location.search).get("project_id"));
    setProjectId(rows.find(row => row.id === desired)?.id || rows[0]?.id || null);
    if (!rows.length) setLoading(false);
  }).catch(e => { setError(e.message); setLoading(false); }); }, []);
  useEffect(() => { setSelectedPlatform(null); setSelectedAccount(null); }, [projectId]);
  useEffect(() => { if (!projectId) return;
    let active = true; setLoading(true); setError("");
    const query = new URLSearchParams({ project_id: String(projectId), start, end, granularity });
    api<Ads>(`/ads?${query}`).then(value => { if (active) setData(value); })
      .catch(e => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, start, end, granularity, revision]);
  // Live: new leads and fresh ad spend show up without reloading (quiet refresh while the tab is visible).
  useEffect(() => { if (!projectId) return;
    const query = new URLSearchParams({ project_id: String(projectId), start, end, granularity });
    const timer = setInterval(() => { if (document.visibilityState === "visible") api<Ads>(`/ads?${query}`).then(setData).catch(() => {}); }, 60000);
    return () => clearInterval(timer); }, [projectId, start, end, granularity]);
  useEffect(() => {
    setRemoteCampaigns([]); setRemoteError("");
    if (selectedPlatform !== "vk_ads" || !selectedAccount) return;
    let active = true; setRemoteLoading(true);
    api<{ campaigns: RemoteCampaign[] }>(`/ads/connections/${selectedAccount}/vk-campaigns`)
      .then(result => { if (active) setRemoteCampaigns(result.campaigns); })
      .catch(cause => { if (active) setRemoteError((cause as Error).message); })
      .finally(() => { if (active) setRemoteLoading(false); });
    return () => { active = false; };
  }, [selectedPlatform, selectedAccount, revision]);
  const project = projects.find(row => row.id === projectId);
  const platform = data?.platforms.find(row => row.id === selectedPlatform);
  const account = platform?.accounts.find(row => row.id === selectedAccount);
  const canManage = data?.viewer.can_manage_integrations || false;
  const activeAccounts = data?.platforms.flatMap(item => item.accounts).filter(item => item.status === "connected") || [];

  async function callAction(path: string, method = "POST", body?: unknown, id: string | number = path) {
    setBusy(id); setError(""); setNotice("");
    try { await api(path, { method, headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined, timeoutMs: 240_000 });
      setRevision(value => value + 1); return true;
    } catch (e) { setError((e as Error).message); setRevision(value => value + 1); return false; }
    finally { setBusy(null); }
  }
  async function submitConnect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!projectId) return;
    const form = new FormData(event.currentTarget);
    if (isMap(connectPlatform)) {
      const made = await callAction("/ads/maps", "POST", { project_id: projectId, platform: connectPlatform, name: form.get("name"),
        card_url: String(form.get("card_url") || "").trim() }, "connect");
      if (made) { setModal(null); setNotice("Канал добавлен. Откройте его: укажите расход за месяц, загрузите статистику и привяжите номер для звонков."); }
      return;
    }
    const created = await callAction("/ads/connections", "POST", { project_id: projectId,
      platform: connectPlatform, name: form.get("name"),
      ...(connectPlatform === "vk_ads" || connectPlatform === "avito_items" ? { client_id: form.get("client_id"), client_secret: form.get("client_secret") }
        : connectPlatform === "avito_ads" ? { external_account_id: form.get("account_id"), client_id: form.get("client_id"), client_secret: form.get("client_secret") }
        : { external_account_id: form.get("account_id"), access_token: form.get("token") }) }, "connect");
    if (created) { setModal(null); setNotice("Кабинет сохранён. Проверьте подключение и запустите синхронизацию."); }
  }
  async function submitToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!tokenAccount) return;
    const form = new FormData(event.currentTarget);
    const updated = await callAction(`/ads/connections/${tokenAccount.id}/token`, "PUT",
      usesClientKeys(selectedPlatform) ? { client_id: form.get("client_id"), client_secret: form.get("client_secret") }
        : { access_token: form.get("token") }, tokenAccount.id);
    if (updated) { setModal(null); setTokenAccount(null); setNotice("Данные доступа обновлены. Проверьте подключение."); }
  }
  async function syncAll() {
    setBusy("all"); setError(""); setNotice("");
    let completed = 0; const failed: string[] = [];
    for (const item of activeAccounts) {
      try { await api(`/ads/connections/${item.id}/sync`, { method: "POST", timeoutMs: 240_000 }); completed++; }
      catch (e) { failed.push(`${item.name}: ${(e as Error).message}`); }
    }
    setBusy(null); setRevision(value => value + 1);
    if (failed.length) setError(`Синхронизировано ${completed} из ${activeAccounts.length}. ${failed.join("; ")}`);
    else setNotice(`Синхронизировано кабинетов: ${completed}.`);
  }
  async function disconnect(item: Account) {
    if (!window.confirm(`Отключить «${item.name}»? Исторические показатели сохранятся.`)) return;
    if (await callAction(`/ads/connections/${item.id}`, "DELETE", undefined, item.id)) setNotice("Кабинет отключён. История сохранена.");
  }

  const summary = (item: Metrics, platformId?: string) => <div className="adsMetricStrip">{(["spend","clicks","cpc","leads","cpl","romi"] as const).map(key => <div key={key}><span>{key === "spend" ? "Расходы" : key === "clicks" ? (platformId === "avito_items" ? "Просмотры" : isMap(platformId) ? "Переходы" : "Клики") : key === "cpc" && platformId === "avito_items" ? "Цена просмотра" : key === "leads" ? "Лиды" : key.toUpperCase()}</span><strong>{valueOf(key, item[key])}</strong></div>)}</div>;
  const actionButtons = (item: Account) => canManage && (isMap(selectedPlatform) ? <div className="adsAccountActions"><button disabled={busy !== null || item.status === "disconnected"} onClick={() => disconnect(item)}>Отключить</button></div> : <div className="adsAccountActions"><button disabled={busy !== null} onClick={() => callAction(`/ads/connections/${item.id}/test`, "POST", undefined, item.id)}>Проверить</button><button disabled={busy !== null || item.status === "disconnected"} onClick={() => callAction(`/ads/connections/${item.id}/sync`, "POST", undefined, item.id)}>Синхронизировать</button><button onClick={() => { setTokenAccount(item); setModal("token"); }}>{usesClientKeys(selectedPlatform) ? "Обновить API-ключи" : "Обновить токен"}</button><button disabled={busy !== null || item.status === "disconnected"} onClick={() => disconnect(item)}>Отключить</button></div>);

  return <div className="resultShell"><ProjectSidebar project={project} projectId={projectId} active="ads" role={data?.viewer.role}/>
    <main className="resultMain adsMain"><div className="resultBreadcrumb">StepToLead <span>/</span> Реклама</div>
      <header className="resultHeader adsHeader"><div><h1>Реклама</h1><p>Подключения и синхронизация кабинетов проекта. Анализ расходов и эффективности — в «Аналитике».</p></div>
        <div className="adsHeaderActions"><PeriodControls projects={projects} projectId={projectId} setProjectId={setProjectId} start={start} setStart={setStart} end={end} setEnd={setEnd}/>{canManage && <button className="adsBlueButton" onClick={() => setModal("connect")}>＋ Подключить рекламу</button>}</div></header>
      {error && <div className="resultError" role="alert">{error}{!projects.length && <div><Link href="/portal/login?next=/ads">Войти в кабинет</Link> · <Link href="/login?next=/ads">Войти как администратор</Link></div>}</div>}
      {notice && <div className="adsNotice" role="status">{notice}</div>}
      {data && <><div className="adsAnalyticsLink"><Link href={`/analytics?tab=marketing${projectId ? `&project_id=${projectId}` : ""}`}>Смотреть маркетинговую аналитику →</Link></div>
        <section className="adsSources"><div className="adsSourcesHead"><div><h2>Рекламные платформы и кабинеты</h2><p>Только реальные подключения проекта. Общие маркетинговые источники сюда не включаются.</p></div>{canManage && activeAccounts.length > 0 && <button disabled={busy !== null} onClick={syncAll}>{busy === "all" ? "Синхронизируем…" : "↻ Синхронизировать все"}</button>}</div>
          {selectedPlatform && <nav className="adsCrumbs"><button onClick={() => { setSelectedPlatform(null); setSelectedAccount(null); }}>Все платформы</button><span>›</span><button onClick={() => setSelectedAccount(null)}>{platform?.name || selectedPlatform}</button>{account && <><span>›</span><strong>{account.name}</strong></>}</nav>}
          {!selectedPlatform && (data.platforms.length ? <div className="adsPlatformList">{data.platforms.map(item => <button className="adsPlatformRow" key={item.id} onClick={() => { setSelectedPlatform(item.id); setSelectedAccount(null); }}><div className="adsPlatformIdentity"><span className={`adsLogo ${item.id}`}>{logoText(item.id, item.name)}</span><div><strong>{item.name}</strong><small>{item.account_count} кабинетов · синхронизация {asDate(item.last_synced_at)}</small><i className={`adsStatus ${item.status}`}>{statusText[item.status] || item.status}</i></div></div>{summary(item.current, item.id)}<Sparkline points={item.current.trend}/><span className="adsArrow">→</span></button>)}</div> : <div className="resultPanel adsEmpty"><h3>Реклама ещё не подключена</h3><p>Подключите поддерживаемый рекламный кабинет для выбранного проекта. Демонстрационных платформ и цифр здесь нет.</p></div>)}
          {platform && !account && <div className="adsPlatformList">{platform.accounts.map(item => <div className="adsAccountRow" key={item.id}><button className="adsAccountOpen" onClick={() => setSelectedAccount(item.id)}><div className="adsPlatformIdentity"><span className={`adsLogo ${platform.id}`}>{logoText(platform.id, platform.name)}</span><div><strong>{item.name}</strong><small>ID {item.external_account_id} · синхронизация {asDate(item.last_synced_at)}</small><i className={`adsStatus ${item.status}`}>{statusText[item.status] || item.status}</i></div></div>{summary(item.current, platform.id)}<Sparkline points={item.current.trend}/><span className="adsArrow">→</span></button>{item.last_error && <p className="adsAccountError">{item.last_error}</p>}{actionButtons(item)}</div>)}</div>}
          {platform && account && <><article className="resultPanel adsAccountDetail"><div className="adsAccountDetailHead"><span className={`adsLogo ${platform.id}`}>{logoText(platform.id, platform.name)}</span><div><h3>{account.name}</h3><p>{platform.name} · ID {account.external_account_id} · последняя синхронизация {asDate(account.last_synced_at)}</p><i className={`adsStatus ${account.status}`}>{statusText[account.status] || account.status}</i></div></div>{summary(account.current, platform.id)}{account.last_error && <p className="adsAccountError">{account.last_error}</p>}<div className="adsDetailActions">{!isMap(platform.id) && <Link href={`/campaigns?project_id=${projectId}&platform=${encodeURIComponent(platform.id)}&account=${account.id}`}>Открыть аналитику кампаний →</Link>}{actionButtons(account)}</div></article>
            {isMap(platform.id) && <MapsPanel connectionId={account.id} start={start} end={end} canManage={canManage} revision={revision} onChanged={() => setRevision(value => value + 1)}/>}
            {platform.id.startsWith("avito") && <AvitoPanel connectionId={account.id} start={start} end={end} canManage={canManage} projectId={projectId} revision={revision}/>}
            {platform.id === "vk_ads" && <section className="resultPanel adsRemoteCampaigns"><div className="adsRemoteHead"><div><h3>Кампании в VK Рекламе</h3><p>Отображаются даже без расходов и показов. Нажмите на кампанию, чтобы увидеть её настройки.</p></div><span>{remoteLoading ? "Загружаем…" : `${remoteCampaigns.length} кампаний`}</span></div>
              {remoteError && <p className="adsAccountError">{remoteError}</p>}
              {!remoteLoading && !remoteError && !remoteCampaigns.length && <p className="adsRemoteEmpty">VK не вернул кампаний для этого кабинета.</p>}
              <div className="adsRemoteList">{remoteCampaigns.map(campaign => <Link key={campaign.id} href={`/ads/vk/${account.id}/${campaign.id}`}><span className="adsRemoteIcon">VK</span><span className="adsRemoteIdentity"><strong>{campaign.name}</strong><small>ID {campaign.id} · {campaign.status || "Статус не указан"}</small></span><span className="adsRemoteBudget">{campaign.budget_limit_day == null ? "—" : money(Number(campaign.budget_limit_day))}<small>в день</small></span><span className="adsArrow">→</span></Link>)}</div>
            </section>}</>}
        </section></>}
      {loading && !data && <div className="resultLoading">Загружаем рекламу…</div>}
      {modal && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) setModal(null); }}><form className="resultModal" onSubmit={modal === "connect" ? submitConnect : submitToken}><header><h2>{modal === "connect" ? "Подключить рекламу" : usesClientKeys(selectedPlatform) ? "Обновить API-ключи" : "Обновить токен"}</h2><button type="button" onClick={() => setModal(null)}>×</button></header>{modal === "connect" ? <><p>Подключение будет принадлежать выбранному проекту, а не вашему пользователю.</p><label>Платформа<select name="platform" required value={connectPlatform} onChange={event => setConnectPlatform(event.target.value)}>{data?.supported_platforms.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>{isMap(connectPlatform) ? "Название" : "Название кабинета"}<input name="name" required minLength={2} maxLength={180} placeholder={isMap(connectPlatform) ? (connectPlatform === "2gis" ? "2ГИС · Ромакс" : "Яндекс Карты · Ромакс") : undefined}/></label>{isMap(connectPlatform) ? <><p className="avitoHint">{connectPlatform === "2gis" ? "У 2ГИС нет API статистики" : "У Яндекс Бизнеса нет API статистики"}: расход за месяц и цифры карточки вносятся вручную или загрузкой выгрузки, а заявки считаются автоматически — по номеру коллтрекинга в карточке и UTM-метке на сайт.</p><label>Ссылка на карточку (необязательно)<input name="card_url" type="url" placeholder={connectPlatform === "2gis" ? "https://2gis.ru/samara/firm/..." : "https://yandex.ru/maps/org/..."}/></label></>
  : connectPlatform === "vk_ads" ? <><p>VK Реклама → Настройки → Доступ к API. Используйте перевыпущенный секрет; не вставляйте ключ из чата.</p><label>Client ID<input name="client_id" required autoComplete="off"/></label><label>Client Secret<input name="client_secret" type="password" required minLength={10} autoComplete="off"/></label></>
  : connectPlatform === "avito_items" ? <><p className="avitoHint">Объявления, расходы на размещение и продвижение, контакты и заявки из чатов и звонков. Ключи: <a href="https://www.avito.ru/professionals/api" target="_blank" rel="noopener noreferrer">avito.ru/professionals/api</a> → «Получить ключи» (нужен профиль компании/Авито Pro). ID профиля определится сам.</p><label>Client ID<input name="client_id" required autoComplete="off"/></label><label>Client Secret<input name="client_secret" type="password" required minLength={10} autoComplete="off"/></label></>
  : connectPlatform === "avito_ads" ? <><p className="avitoHint">Баннерная и видеореклама (кабинет ads.avito.ru). Ключи создаёт администратор во вкладке «API» кабинета Авито Рекламы; ключи привязаны к одному аккаунту. ID аккаунта — число в настройках кабинета.</p><label>ID аккаунта Авито Рекламы<input name="account_id" required inputMode="numeric" pattern="[0-9]+" maxLength={20}/></label><label>Client ID<input name="client_id" required autoComplete="off"/></label><label>Client Secret<input name="client_secret" type="password" required minLength={10} autoComplete="off"/></label></>
  : <><label>ID / логин кабинета<input name="account_id" required maxLength={180}/></label><label>OAuth access token<input name="token" type="password" required minLength={10} autoComplete="off"/></label></>}</> : <><p>{tokenAccount?.name}. После замены проверьте подключение.</p>{usesClientKeys(selectedPlatform) ? <><label>Новый Client ID<input name="client_id" required autoComplete="off"/></label><label>Новый Client Secret<input name="client_secret" type="password" required minLength={10} autoComplete="off"/></label></> : <label>Новый OAuth access token<input name="token" type="password" required minLength={10} autoComplete="off"/></label>}</>}<button className="resultPrimary" disabled={busy !== null}>{busy ? "Сохраняем…" : "Сохранить"}</button></form></div>}
    </main></div>;
}
