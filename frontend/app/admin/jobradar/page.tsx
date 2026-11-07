"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { date, PageHeader } from "../leadgen/shared";
import styles from "../leadgen/leadgen.module.css";
import local from "./jobradar.module.css";
import { SourcesPanel } from "./sources-panel";
import { base, Channel, send, Source } from "./source-settings";
import {
  actionLabels, Breakdown, Client, Overview, scannerError, Scanner, ScannerShort, Stats, Template,
} from "./scanner-types";

export default function JobRadarPage() {
  const [scanners, setScanners] = useState<ScannerShort[] | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [sources, setSources] = useState<Source[] | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(n => n + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    setError("");
    const fail = (e: unknown) => { if (!controller.signal.aborted) setError(scannerError(e)); };
    api<{ items: ScannerShort[] }>(base + "/scanners", options)
      .then(d => { if (!controller.signal.aborted) setScanners(d.items); }).catch(fail);
    api<Overview>(base + "/overview", options)
      .then(d => { if (!controller.signal.aborted) setOverview(d); }).catch(fail);
    api<{ items: Source[] }>(base + "/sources", options)
      .then(d => { if (!controller.signal.aborted) setSources(d.items); }).catch(fail);
    api<{ items: Channel[] }>(base + "/channels", options)
      .then(d => { if (!controller.signal.aborted) setChannels(d.items); }).catch(fail);
    return () => controller.abort();
  }, [revision]);
  async function toggle(scanner: ScannerShort) {
    setBusy(true);
    setError("");
    try {
      await send("/scanners/" + scanner.id, { enabled: !scanner.enabled }, "PATCH");
      refresh();
    } catch (e) { setError(scannerError(e)); }
    finally { setBusy(false); }
  }
  return <div className={`page ${styles.page}`}>
    <PageHeader title="Радар вакансий" subtitle="Площадки и сканеры вакансий и проектов.">
      <button type="button" className={styles.primary} onClick={() => setCreating(true)}>＋ Новый сканер</button>
    </PageHeader>
    {error && <p className={styles.error} role="alert">{error}
      <button type="button" className={styles.secondary} onClick={refresh}>Повторить</button></p>}
    {overview && !overview.bot_configured && <p className={styles.error}>
      Бот портала не подключён — отправка в Telegram недоступна. Подключите бота в настройках интеграций.
    </p>}
    {sources ? <SourcesPanel sources={sources} channels={channels} scanners={scanners || []} refresh={refresh}/>
      : <p role="status">Загружаем площадки…</p>}
    <section aria-labelledby="scanners-heading">
      <h2 id="scanners-heading">Сканеры</h2>
      {!scanners ? <p role="status">Загружаем сканеры…</p>
        : !scanners.length ? <p className={styles.card}>Сканеров пока нет. Создайте сканер из шаблона.</p>
        : <div className={local.scannerList}>{scanners.map(scanner => <article className={styles.card}
          key={scanner.id}>
          <div className={local.head}>
            <div><h3><Link href={`/admin/jobradar/${scanner.id}`}>{scanner.name}</Link></h3>
              <p className={styles.muted}>{scanner.owner ? "Клиент: " + scanner.owner.name : "Агентство"}
                {" · запросов: " + scanner.queries}</p></div>
            <label className={styles.check}><input type="checkbox" checked={scanner.enabled} disabled={busy}
              onChange={() => toggle(scanner)} aria-label={"Включить сканер «" + scanner.name + "»"}/>Вкл.</label>
          </div>
          <div className={styles.row}>{scanner.actions.map(code => <span className={styles.badge} key={code}>
            {actionLabels[code] || code}</span>)}</div>
          <Stats scanner={scanner}/>
          <small>Последняя проверка: {date(scanner.last_run_at)}</small>
          <Breakdown result={scanner.last_result}/>
        </article>)}</div>}
    </section>
    {overview && <RunInterval overview={overview} onSaved={refresh}/>}
    {creating && <ScannerWizard close={() => setCreating(false)}/>}
  </div>;
}

function ScannerWizard({ close }: { close: () => void }) {
  const router = useRouter();
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [clients, setClients] = useState<Client[]>([]);
  const [step, setStep] = useState(1);
  const [template, setTemplate] = useState<Template | null>(null);
  const [name, setName] = useState("");
  const [project, setProject] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      api<{ items: Template[] }>(base + "/templates", { signal: controller.signal }),
      api<{ items: Client[] }>("/admin/leadgen/clients", { signal: controller.signal }),
    ]).then(([t, c]) => {
      if (!controller.signal.aborted) { setTemplates(t.items); setClients(c.items); }
    }).catch(e => { if (!controller.signal.aborted) setError(scannerError(e)); });
    return () => controller.abort();
  }, []);
  async function create(event: FormEvent) {
    event.preventDefault();
    if (!template) return;
    setBusy(true);
    setError("");
    try {
      const scanner = await send<Scanner>("/scanners", {
        name: name.trim(), template: template.code, ...(project ? { project_id: Number(project) } : {}),
      });
      router.push("/admin/jobradar/" + scanner.id);
    } catch (e) { setError(scannerError(e)); setBusy(false); }
  }
  return <>
    <button type="button" className={styles.overlay} aria-label="Закрыть мастер" onClick={() => !busy && close()}/>
    <form className={`${styles.card} ${styles.modal} ${styles.modalWide}`} onSubmit={create}
      role="dialog" aria-modal="true" aria-labelledby="new-scanner">
      <h2 id="new-scanner">Новый сканер · шаг {step} из 2</h2>
      {error && <p role="alert" className={styles.error}>{error}</p>}
      {step === 1 ? <>
        {!templates ? <p role="status">Загружаем шаблоны…</p> : <div className={local.templateGrid}>
          {templates.map(item => <button type="button" key={item.code} className={local.templateCard}
            onClick={() => { setTemplate(item); setName(item.name); setStep(2); }}>
            <span aria-hidden="true">{item.icon}</span><b>{item.name}</b><span>{item.description}</span>
            <small>Запросов: {item.preview.queries} · площадки: {item.preview.sources.join(", ") || "—"}</small>
            <small>{item.preview.actions.map(code => actionLabels[code] || code).join(" · ") || "Без действий"}</small>
          </button>)}
        </div>}
      </> : <fieldset disabled={busy} className={styles.formFieldset}>
        <p>Шаблон: {template?.name}</p>
        <label className={styles.field}>Название<input required minLength={2} maxLength={160}
          value={name} onChange={e => setName(e.target.value)}/></label>
        <label className={styles.field}>Для кого<select value={project} onChange={e => setProject(e.target.value)}>
          <option value="">Агентство</option>
          {clients.map(client => <option key={client.project_id} value={client.project_id}>
            {client.name} · {client.project}</option>)}
        </select></label>
        <div className={styles.actions}>
          <button type="button" className={styles.secondary} onClick={() => setStep(1)}>Назад</button>
          <button type="submit" className={styles.primary}>{busy ? "Создаём…" : "Создать сканер"}</button>
        </div>
      </fieldset>}
      <button type="button" className={styles.secondary} disabled={busy} onClick={close}>Отмена</button>
    </form>
  </>;
}

function RunInterval({ overview, onSaved }: { overview: Overview; onSaved: () => void }) {
  const [every, setEvery] = useState(overview.settings.interval_minutes);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try { await send("/settings", { interval_minutes: every }, "PATCH"); onSaved(); }
    catch (e) { setError(scannerError(e)); }
    finally { setBusy(false); }
  }
  return <section className={styles.card}>
    <h2>Проверка по расписанию</h2>
    <form className={styles.inlineForm} onSubmit={save}>
      <label className={styles.field}>Проверять каждые, мин<input type="number" min={5} max={1440}
        required disabled={busy} value={every} onChange={e => setEvery(Number(e.target.value))}/></label>
      <button type="submit" disabled={busy} className={styles.secondary}>Сохранить</button>
    </form>
    {error && <p className={styles.error} role="alert">{error}</p>}
  </section>;
}
