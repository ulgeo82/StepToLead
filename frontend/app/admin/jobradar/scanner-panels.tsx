"use client";

import { FormEvent, useEffect, useState } from "react";
import { date } from "../leadgen/shared";
import styles from "../leadgen/leadgen.module.css";
import local from "./jobradar.module.css";
import { Channel, list, normalizeValues, shortMessage, Source } from "./source-settings";
import { Channels, ScannerSourceOptions } from "./sources-panel";
import { Actions, Breakdown, Client, Overview, Scanner, Stats } from "./scanner-types";

const roleNames: Record<string, string> = { client_owner: "Собственник", sales_head: "Руководитель продаж",
  sales_manager: "Менеджер продаж", client_marketer: "Маркетолог" };

export type SaveScanner = (body: Partial<Scanner>) => Promise<boolean>;

export function Platforms({ scanner, sources, channels, busy, save, refresh }: {
  scanner: Scanner; sources: Source[]; channels: Channel[]; busy: boolean; save: SaveScanner; refresh: () => void;
}) {
  const [selected, setSelected] = useState(scanner.sources);
  const [settings, setSettings] = useState(scanner.source_settings);
  useEffect(() => { setSelected(scanner.sources); setSettings(scanner.source_settings); }, [scanner]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    await save({ sources: selected, source_settings: Object.fromEntries(Object.entries(settings).map(([code, values]) =>
      [code, normalizeValues(sources.find(s => s.code === code)?.filter_schema || [], values)])) });
  }
  const names = list(scanner.source_settings.telegram?.channels).map(v => v.replace(/^@/, "").toLowerCase());
  const ownChannels = channels.filter(c => c.scanners?.some(s => s.id === scanner.id) || names.includes(c.channel));
  return <section className={styles.card}>
    <h2>Площадки сканера</h2>
    <form onSubmit={submit}>
      <fieldset disabled={busy} className={styles.formFieldset}>
        <ScannerSourceOptions sources={sources} selected={selected} settings={settings} channels={[]}
          toggle={code => setSelected(selected.includes(code) ? selected.filter(v => v !== code) : [...selected, code])}
          onChange={setSettings}/>
        <button type="submit" className={styles.primary}>Сохранить площадки</button>
      </fieldset>
    </form>
    {selected.includes("telegram") && <div className={local.queryCard}>
      <h3>Каналы этого сканера</h3>
      <small>Добавление и удаление каналов сохраняется сразу.</small>
      <Channels channels={ownChannels.map(c => ({ ...c, scanners: [{ id: scanner.id, name: scanner.name }] }))}
        scanners={[{ id: scanner.id, name: scanner.name }]} refresh={refresh}/>
    </div>}
  </section>;
}

export function ScannerActions({ scanner, clients, overview, busy, save, test }: {
  scanner: Scanner; clients: Client[]; overview: Overview | null; busy: boolean;
  save: SaveScanner; test: () => void;
}) {
  const [actions, setActions] = useState<Actions>(scanner.actions);
  useEffect(() => setActions(scanner.actions), [scanner]);
  const client = clients.find(c => c.project_id === scanner.owner?.project_id);
  const team = client?.team || [];
  const agency = !scanner.owner;
  const update = <K extends keyof Actions>(key: K, value: Partial<Actions[K]>) =>
    setActions(previous => ({ ...previous, [key]: { ...previous[key], ...value } }));
  async function submit(event: FormEvent) {
    event.preventDefault();
    await save({ actions });
  }
  return <section className={styles.card}>
    <h2>Действия с находками</h2>
    <form onSubmit={submit}>
      <fieldset disabled={busy} className={styles.formFieldset}>
        <div className={local.actionGrid}>
          <article className={local.queryCard}>
            <label className={styles.check}><input type="checkbox" checked={actions.telegram.enabled}
              onChange={e => update("telegram", { enabled: e.target.checked })}/>✈️ Telegram</label>
            <label className={styles.field}>Известный чат<select value={overview?.chats.some(c =>
              c.id === actions.telegram.chat_id) ? actions.telegram.chat_id || "" : ""}
              onChange={e => update("telegram", { chat_id: e.target.value || null })}>
              <option value="">Выберите чат или введите вручную ниже</option>
              {overview?.chats.map(c => <option key={c.id} value={c.id}>{c.title}</option>)}
            </select></label>
            <label className={styles.field}>ID или имя чата<input value={actions.telegram.chat_id || ""}
              placeholder="-100… или @имя" onChange={e => update("telegram", { chat_id: e.target.value || null })}/>
            </label>
            <small>Для публикаций в канал добавьте бота портала администратором с правом отправки сообщений.</small>
            {overview && !overview.bot_configured && <p className={styles.errorText}>Бот портала не подключён.</p>}
            <button type="button" className={styles.secondary} disabled={!scanner.actions.telegram.chat_id ||
              JSON.stringify(actions.telegram) !== JSON.stringify(scanner.actions.telegram)} onClick={test}>
              Тестовый пост</button>
            <small>Тест использует сохранённые настройки. Сначала сохраните изменения.</small>
          </article>
          <article className={`${local.queryCard} ${!agency ? local.disabled : ""}`}>
            <fieldset disabled={!agency} className={styles.formFieldset}>
              <label className={styles.check}><input type="checkbox" checked={actions.companies.enabled}
                onChange={e => update("companies", { enabled: e.target.checked })}/>📇 Компания в базу компаний</label>
              <label className={styles.field}>Название списка<input value={actions.companies.list_name}
                onChange={e => update("companies", { list_name: e.target.value })}/></label>
              <label className={styles.field}>Сигнал<select value={actions.companies.signal}
                onChange={e => update("companies", { signal: e.target.value as Actions["companies"]["signal"] })}>
                <option value="hh_marketing">Ищут маркетолога</option><option value="hiring">Нанимает</option>
              </select></label>
            </fieldset>
            {!agency && <small>Доступно только для сканеров агентства.</small>}
          </article>
          <article className={`${local.queryCard} ${agency ? local.disabled : ""}`}>
            <fieldset disabled={agency} className={styles.formFieldset}>
              <label className={styles.check}><input type="checkbox" checked={actions.client_crm.enabled}
                onChange={e => update("client_crm", { enabled: e.target.checked })}/>🧲 Лид в CRM клиента</label>
              <label className={styles.field}>Ответственный<select value={actions.client_crm.responsible_user_id ?? ""}
                onChange={e => update("client_crm", { responsible_user_id: e.target.value ? Number(e.target.value) : null })}>
                <option value="">Не назначен</option>
                {team.map(user => <option key={user.id} value={user.id}>{user.name} · {roleNames[user.role] || user.role}</option>)}
                {actions.client_crm.responsible_user_id && !team.some(u => u.id === actions.client_crm.responsible_user_id) &&
                  <option value={actions.client_crm.responsible_user_id}>Пользователь #{actions.client_crm.responsible_user_id}</option>}
              </select></label>
            </fieldset>
            {agency && <small>Выберите клиентский сканер: у агентства нет клиентской CRM.</small>}
          </article>
        </div>
        <button type="submit" className={styles.primary}>Сохранить действия</button>
      </fieldset>
    </form>
  </section>;
}

export function ScannerAI({ scanner, overview, busy, save }: {
  scanner: Scanner; overview: Overview | null; busy: boolean; save: SaveScanner;
}) {
  const [profile, setProfile] = useState(scanner.profile || "");
  const [score, setScore] = useState(scanner.ai_min_score);
  useEffect(() => { setProfile(scanner.profile || ""); setScore(scanner.ai_min_score); }, [scanner]);
  return <section className={styles.card}><h2>ИИ-отбор</h2>
    {overview && !overview.ai_available && <p className={styles.notice}>ИИ в портале не подключён.</p>}
    <form onSubmit={e => { e.preventDefault(); void save({ profile: profile.trim() || null, ai_min_score: score }); }}>
      <fieldset disabled={busy} className={styles.formFieldset}>
        <label className={styles.field}>Кого ищем<textarea rows={5} maxLength={2000} value={profile}
          onChange={e => setProfile(e.target.value)}/></label>
        <label className={styles.field}>Порог ИИ, 0–100<input type="number" min={0} max={100} required value={score}
          onChange={e => setScore(Number(e.target.value))}/><small>0 — без ИИ-отбора.</small></label>
        <p>👍 {scanner.stats.liked_share == null ? "—" : `${scanner.stats.liked_share}%`}
          {scanner.stats.liked_share == null ? " · 👎 —" : ` · 👎 ${100 - scanner.stats.liked_share}%`}
          {` · оценок: ${scanner.stats.rated}`}</p>
        <button type="submit" className={styles.primary}>Сохранить ИИ</button>
      </fieldset>
    </form>
  </section>;
}

export function ScannerStatistics({ scanner }: { scanner: Scanner }) {
  const result = scanner.last_result;
  return <section className={styles.card}>
    <h2>Статистика за 7 дней</h2><Stats scanner={scanner}/>
    <h3>Последняя проверка: {date(scanner.last_run_at)}</h3>
    {!result ? <p className={styles.muted}>Проверок ещё не было.</p> : <>
      <div className={local.stats}>{[
        ["Найдено", result.found], ["Прошло", result.matched], ["Отправлено", result.sent],
        ["В базе", result.leads], ["В CRM", result.deals], ["Отсеяно", result.skipped],
        ["Отсеял ИИ", result.ai_low], ["Ошибок", result.failed],
      ].map(([label, value]) => <span key={label}><b>{value ?? "—"}</b>{label}</span>)}</div>
      <h3>По площадкам</h3><Breakdown result={result}/>
      <div className={styles.scroll}><table className={styles.table}>
        <thead><tr><th>Площадка</th><th>Найдено</th><th>Прошло</th><th>Отсеяно</th><th>Причина</th></tr></thead>
        <tbody>{Object.entries(result.by_source || {}).map(([code, row]) => <tr key={code}>
          <td>{code}</td><td>{row.found ?? "—"}</td><td>{row.passed ?? "—"}</td><td>{row.dropped ?? "—"}</td>
          <td>{row.reason || row.error || row.note ? shortMessage(new Error(row.reason || row.error || row.note)) : "—"}</td></tr>)}</tbody>
      </table></div>
      <h3>По запросам</h3><div className={styles.scroll}><table className={styles.table}>
        <thead><tr><th>Запрос</th><th>Найдено</th><th>Прошло</th></tr></thead>
        <tbody>{Object.entries(result.by_query || {}).map(([id, row]) => <tr key={id}>
          <td>{row.name}</td><td>{row.found}</td><td>{row.passed}</td></tr>)}</tbody>
      </table></div>
    </>}
  </section>;
}
