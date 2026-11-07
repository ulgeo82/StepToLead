"use client";

import { FormEvent, useState } from "react";
import { api } from "@/lib/api";
import { date } from "../leadgen/shared";
import styles from "../leadgen/leadgen.module.css";
import local from "./jobradar.module.css";
import {
  base, Channel, CheckResult, list, normalizeValues, Previews, SchemaFields, send, Settings, shortMessage, Source,
} from "./source-settings";

type ScannerRef = { id: number; name: string };

export function ScannerSourceOptions({ sources, selected, settings, channels, toggle, onChange }: {
  sources: Source[]; selected: string[]; settings: Record<string, Settings>; channels: Channel[];
  toggle: (code: string) => void; onChange: (value: Record<string, Settings>) => void;
}) {
  return <fieldset className={`${styles.formFieldset} ${styles.wide}`}>
    <legend className={styles.legend}>Площадки</legend>
    {sources.map(source => {
      const active = selected.includes(source.code);
      const disabled = !source.enabled || source.state === "needs_key" || source.state === "off";
      const values = settings[source.code] || {};
      const update = (value: Settings) => onChange({ ...settings, [source.code]: value });
      return <div key={source.code} className={local.sourceOption}>
        <button type="button" className={`${styles.source} ${disabled ? local.disabled : ""}`}
          aria-pressed={active} disabled={disabled && !active}
          title={disabled ? source.state === "needs_key" ? "Сначала задайте ключ на плашке площадки"
            : "Сначала включите площадку" : source.purpose} onClick={() => toggle(source.code)}>
          <span aria-hidden="true">{source.icon}</span>{source.title}
        </button>
        {active && <fieldset disabled={disabled} className={styles.formFieldset}>
          <SchemaFields schema={source.filter_schema} values={values} prefix={`scanner-${source.code}`}
            onChange={update}/>
          {source.code === "telegram" && <>
            <div className={styles.listChips}>{list(values.channels).map(channel =>
              <span className={styles.listChip} key={channel}>@{channel.replace(/^@/, "")}</span>)}</div>
            {channels.map(channel => <label className={styles.check} key={channel.channel}>
              <input type="checkbox" checked={list(values.channels).map(v => v.replace(/^@/, "").toLowerCase())
                .includes(channel.channel.toLowerCase())} onChange={e => update({ ...values,
                channels: e.target.checked ? [...list(values.channels), channel.channel] : list(values.channels)
                  .filter(v => v.replace(/^@/, "").toLowerCase() !== channel.channel.toLowerCase()),
              })}/>@{channel.channel}</label>)}
            <small className={styles.hint}>До 30 публичных каналов. Ссылки-приглашения читать нельзя.</small>
          </>}
        </fieldset>}
      </div>;
    })}
  </fieldset>;
}

function sourceStatus(source: Source) {
  if (!source.enabled || source.state === "off") return "⚪ выключена";
  if (source.state === "needs_key") return "🟡 нужен ключ";
  if (source.state === "error") return `🔴 ${shortMessage(new Error(source.message || "Ошибка площадки"))}`;
  if (source.state === "ok") return `🟢 работает (последний ответ ${source.last_ok_at
    ? new Date(source.last_ok_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "—"})`;
  return "⚪ ещё не проверена";
}

export function SourcesPanel({ sources, channels, scanners, refresh }: {
  sources: Source[]; channels: Channel[]; scanners: ScannerRef[]; refresh: () => void;
}) {
  return <section aria-label="Площадки радара" className={local.platforms}>
    {sources.map(source => <SourceCard key={source.code} source={source} channels={channels}
      scanners={scanners} refresh={refresh}/>)}
  </section>;
}

function SourceCard({ source, channels, scanners, refresh }: {
  source: Source; channels: Channel[]; scanners: ScannerRef[]; refresh: () => void;
}) {
  const [draft, setDraft] = useState<Settings>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<CheckResult | null>(null);
  const [notice, setNotice] = useState("");
  async function request(action: "save" | "toggle" | "check") {
    setBusy(true); setError(""); setNotice(""); setResult(null);
    try {
      if (action === "check") {
        setResult(await send<CheckResult>(`/sources/${source.code}/check`, {}));
      } else {
        await send(`/sources/${source.code}`, action === "toggle" ? { enabled: !source.enabled }
          : { settings: normalizeValues(source.settings_schema, draft) }, "PATCH");
        if (action === "save") setDraft({});
        setNotice(action === "save" ? "Настройки сохранены" : "Состояние сохранено");
      }
      refresh();
    } catch (reason) { setError(shortMessage(reason)); }
    finally { setBusy(false); }
  }
  return <article className={`${styles.card} ${local.platform}`}>
    <div className={local.head}>
      <div><h2><span aria-hidden="true">{source.icon}</span> {source.title}</h2>
        <span className={styles.muted}>{source.purpose}</span></div>
      <label className={styles.check}><input type="checkbox" checked={source.enabled} disabled={busy}
        aria-label={`Включить ${source.title}`} onChange={() => request("toggle")}/>Вкл.</label>
    </div>
    <p className={styles.muted}>{sourceStatus(source)}</p>
    <div className={local.stats} aria-label="За последние 24 часа">
      <span><b>{source.stats_24h.found}</b>найдено</span>
      <span><b>{source.stats_24h.passed}</b>прошло</span>
      <span><b>{source.stats_24h.sent}</b>отправлено</span>
    </div>
    <button type="button" className={styles.secondary} disabled={busy} onClick={() => request("check")}>
      {busy ? "Подождите…" : "Проверить"}
    </button>
    <details className={local.settings}><summary>Настройки</summary>
      {source.settings_schema.length ? <form onSubmit={event => { event.preventDefault(); request("save"); }}>
        <fieldset disabled={busy} className={styles.formFieldset}>
          <SchemaFields schema={source.settings_schema} values={draft} saved={source.settings}
            prefix={source.code} onChange={setDraft} proxyOptions={source.proxy_options}/>
          <button type="submit" className={styles.secondary} disabled={!Object.keys(draft).length}>
            Сохранить настройки
          </button>
        </fieldset>
      </form> : <p className={styles.muted}>Дополнительных настроек нет.</p>}
    </details>
    {error && <p className={styles.error} role="alert">{error}</p>}
    {notice && <p className={styles.muted} role="status">{notice}</p>}
    {result && <Previews result={result}/>}
    {source.code === "telegram" && <Channels channels={channels} scanners={scanners} refresh={refresh}/>}
  </article>;
}

export function Channels({ channels, scanners, refresh }: {
  channels: Channel[]; scanners: ScannerRef[]; refresh: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [scannerId, setScannerId] = useState(scanners.length === 1 ? String(scanners[0].id) : "");
  const [checkedName, setCheckedName] = useState("");
  const [result, setResult] = useState<CheckResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function check(channel: string, forAdd = false) {
    setBusy(true); setError(""); setResult(null); setCheckedName("");
    try {
      const value = await send<CheckResult>("/channels/check", { channel });
      setResult(value);
      if (forAdd && value.ok) setCheckedName(channel.trim());
      refresh();
    } catch (reason) { setError(shortMessage(reason)); }
    finally { setBusy(false); }
  }
  async function add(event: FormEvent) {
    event.preventDefault();
    if (!result?.ok || checkedName !== name.trim() || !scannerId) return;
    setBusy(true); setError("");
    try {
      await send("/channels", { channel: result.channel || name.trim(), scanner_id: Number(scannerId) });
      setAdding(false); setName(""); setCheckedName(""); setResult(null); refresh();
    } catch (reason) { setError(shortMessage(reason)); }
    finally { setBusy(false); }
  }
  async function remove(channel: Channel, scanner: ScannerRef) {
    if (!window.confirm(`Убрать @${channel.channel} из сканера «${scanner.name}»?`)) return;
    setBusy(true); setError("");
    try {
      await api(`${base}/channels/${encodeURIComponent(channel.channel)}?scanner_id=${scanner.id}`, { method: "DELETE" });
      refresh();
    } catch (reason) { setError(shortMessage(reason)); }
    finally { setBusy(false); }
  }
  return <div className={local.channels}>
    {channels.map(channel => <div key={channel.channel} className={local.channel}>
      <b>{channel.status === "ok" ? "🟢" : channel.status === "unknown" ? "⚪" : "🔴"} @{channel.channel}</b>
      <small>Прошло за 24 ч: {channel.passed_24h} · последний пост: {date(channel.last_post_at)}</small>
      {channel.error && <small>{shortMessage(new Error(channel.error))}</small>}
      <button type="button" className={styles.secondary} disabled={busy}
        onClick={() => check(channel.channel)}>Проверить канал</button>
      {(channel.scanners || []).map(scanner => <div key={scanner.id} className={styles.row}>
        <small>{scanner.name}</small><button type="button" disabled={busy} className={styles.secondary}
          aria-label={`Удалить @${channel.channel} из ${scanner.name}`} onClick={() => remove(channel, scanner)}>Удалить</button>
      </div>)}
    </div>)}
    <button type="button" disabled={busy} className={styles.secondary}
      onClick={() => { setAdding(!adding); setResult(null); setCheckedName(""); }}>＋ Добавить канал</button>
    {adding && <form className={local.channelForm} onSubmit={add}>
      <label className={styles.field}>Канал<input required value={name} disabled={busy} placeholder="@имя"
        onChange={e => { setName(e.target.value); setResult(null); setCheckedName(""); }}/></label>
      <label className={styles.field}>Сканер<select required disabled={busy} value={scannerId}
        onChange={e => setScannerId(e.target.value)}><option value="">Выберите сканер</option>
        {scanners.map(scanner => <option key={scanner.id} value={scanner.id}>{scanner.name}</option>)}
      </select></label>
      {!scanners.length && <small className={styles.muted}>Сначала создайте сканер.</small>}
      <div className={styles.row}>
        <button type="button" className={styles.secondary} disabled={busy || !name.trim()}
          onClick={() => check(name, true)}>Проверить перед добавлением</button>
        <button type="submit" className={styles.primary}
          disabled={busy || !scannerId || !result?.ok || checkedName !== name.trim()}>Добавить</button>
      </div>
    </form>}
    {busy && <small role="status">Подождите…</small>}
    {error && <p className={styles.error} role="alert">{error}</p>}
    {result && <Previews result={result}/>}
  </div>;
}
