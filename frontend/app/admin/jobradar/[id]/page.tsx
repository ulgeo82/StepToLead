"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { date, PageHeader } from "../../leadgen/shared";
import styles from "../../leadgen/leadgen.module.css";
import local from "../jobradar.module.css";
import { base, Channel, send, Source } from "../source-settings";
import { Client, Overview, Scanner, scannerError } from "../scanner-types";
import { Queries } from "../scanner-queries";
import { Platforms, ScannerActions, ScannerAI, ScannerStatistics } from "../scanner-panels";
import { ScannerFeed } from "../scanner-feed";
import { Estimate, EstimateHint, ProgressSnapshot, TaskProgress } from "@/components/task-progress";

const tabs = ["Запросы", "Площадки", "Действия", "ИИ", "Лента", "Статистика"];

export default function ScannerPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [scanner, setScanner] = useState<Scanner | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [clients, setClients] = useState<Client[]>([]);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [tab, setTab] = useState("Запросы");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState<{ since: number } | null>(null);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(n => n + 1), []);
  useEffect(() => {
    setScanner(null);
    setRunning(null);
    setTab("Запросы");
  }, [id]);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setLoading(true);
    const options = { signal: controller.signal };
    Promise.all([
      api<Scanner>(`${base}/scanners/${id}`, options),
      api<{ items: Source[] }>(`${base}/sources`, options),
      api<{ items: Channel[] }>(`${base}/channels`, options),
      api<{ items: Client[] }>("/admin/leadgen/clients", options),
      api<Overview>(`${base}/overview`, options),
    ]).then(([s, platforms, c, owners, o]) => {
      if (controller.signal.aborted) return;
      api<ProgressSnapshot>(`${base}/scanners/${id}/progress`, options).then(p => {   // фоновая проверка уже идёт
        if (!controller.signal.aborted && (p.state === "queued" || p.state === "running"))
          setRunning(current => current || { since: p.started_at ?? Date.now() / 1000 });
      }).catch(() => undefined);
      setScanner(s);
      setName(s.name);
      setSources(platforms.items);
      setChannels(c.items);
      setClients(owners.items);
      setOverview(o);
    }).catch(e => { if (!controller.signal.aborted) setError(scannerError(e)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [id, revision]);

  // Оценка времени пересчитывается при любой смене площадок, каналов, действий и запросов.
  const shape = scanner ? JSON.stringify([scanner.sources, scanner.source_settings, scanner.actions,
    scanner.query_items.map(q => [q.enabled, q.keywords, q.regions]), scanner.ai_min_score]) : "";
  useEffect(() => {
    if (!shape) return;
    let alive = true;
    send<Estimate>(`/scanners/${id}/estimate`, { kind: "run" })
      .then(value => { if (alive) setEstimate(value); }).catch(() => { if (alive) setEstimate(null); });
    return () => { alive = false; };
  }, [id, shape, revision]);   // после проверки радар знает реальную скорость площадок — пересчитываем
  function finished(snapshot: ProgressSnapshot) {
    setRunning(null);
    if (snapshot.state === "error") setError(snapshot.message || "Проверка прервалась");
    else setNotice((snapshot.message || "Проверка завершена") + ". Подробности — в ленте и статистике.");
    refresh();
  }
  async function save(body: Partial<Scanner>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await send<Scanner>(`/scanners/${id}`, body, "PATCH");
      setScanner(value);
      setName(value.name);
      setNotice("Изменения сохранены");
      return true;
    } catch (e) { setError(scannerError(e)); return false; }
    finally { setBusy(false); }
  }
  async function action(kind: "run" | "test" | "delete") {
    if (!scanner) return;
    if (kind === "delete" && !window.confirm(`Удалить сканер «${scanner.name}» вместе с историей?`)) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (kind === "delete") {
        await api(`${base}/scanners/${id}`, { method: "DELETE" });
        router.push("/admin/jobradar");
      } else if (kind === "run") {
        const since = Date.now() / 1000;
        await send(`/scanners/${id}/run`, {});
        setRunning({ since });
      } else {
        await send(`/scanners/${id}/test`, {});
        setNotice("Тестовый пост отправлен — проверьте чат.");
      }
    } catch (e) { setError(scannerError(e)); }
    finally { setBusy(false); }
  }
  const disabled = busy || !!running || loading;
  return <div className={`page ${styles.page}`}>
    <Link href="/admin/jobradar">← Все сканеры</Link>
    <PageHeader title={scanner?.name || "Сканер"} subtitle={scanner?.owner
      ? `Клиент: ${scanner.owner.name}` : "Для агентства"}/>
    {error && <p role="alert" className={styles.error}>{error}
      <button type="button" className={styles.secondary} disabled={disabled} onClick={refresh}>Повторить</button></p>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}
    {loading && <p role="status">Загружаем сканер…</p>}
    {scanner && <>
      <section className={styles.card}>
        <form className={`${styles.inlineForm} ${local.wrapForm}`} onSubmit={e => { e.preventDefault(); void save({ name: name.trim() }); }}>
          <label className={styles.field}>Название<input required minLength={2} maxLength={160} value={name}
            disabled={disabled} onChange={e => setName(e.target.value)}/></label>
          <button type="submit" className={styles.secondary} disabled={disabled || name.trim() === scanner.name}>
            Сохранить название</button>
          <label className={styles.check}><input type="checkbox" checked={scanner.enabled} disabled={disabled}
            onChange={e => void save({ enabled: e.target.checked })}/>Вкл.</label>
        </form>
        <div className={styles.actions}>
          <button type="button" className={styles.primary} disabled={disabled} onClick={() => action("run")}>
            {running ? "Проверяем…" : "Проверить сейчас"}</button>
          <button type="button" className={`${styles.secondary} ${styles.danger}`} disabled={disabled}
            onClick={() => action("delete")}>Удалить сканер</button>
        </div>
        {!running && <EstimateHint estimate={estimate}/>}
        {running && <TaskProgress url={`${base}/scanners/${id}/progress?kind=run`} active since={running.since}
          onFinish={finished}/>}
        <small>Последняя проверка: {date(scanner.last_run_at)}</small>
      </section>
      <div className={`${styles.tabs} ${local.scannerTabs}`} role="tablist" aria-label="Сканер">
        {tabs.map(label => <button type="button" key={label} role="tab" id={`tab-${label}`}
          aria-controls="scanner-panel" aria-selected={tab === label} className={styles.tab}
          onClick={() => setTab(label)}>{label}</button>)}
      </div>
      <div role="tabpanel" id="scanner-panel" aria-labelledby={`tab-${tab}`}>
        <fieldset disabled={disabled} className={local.panelFieldset}>
          {tab === "Запросы" && <Queries scanner={scanner} changed={refresh}/>}
          {tab === "Площадки" && <Platforms scanner={scanner} sources={sources} channels={channels}
            busy={disabled} save={save} refresh={refresh}/>}
          {tab === "Действия" && <ScannerActions scanner={scanner} clients={clients} overview={overview}
            busy={disabled} save={save} test={() => action("test")}/>}
          {tab === "ИИ" && <ScannerAI scanner={scanner} overview={overview} busy={disabled} save={save}/>}
          {tab === "Лента" && <ScannerFeed scanner={scanner}/>}
          {tab === "Статистика" && <ScannerStatistics scanner={scanner}/>}
        </fieldset>
      </div>
    </>}
  </div>;
}
