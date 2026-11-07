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
  const [running, setRunning] = useState<{ baseline: string | null; started: number } | null>(null);
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

  useEffect(() => {
    if (!running) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const deadline = setTimeout(() => {
      controller.abort();
      clearTimeout(timer);
      setRunning(null);
      setNotice("Проверка ещё не завершилась за 3 минуты. Обновите страницу позже, не запускайте повторно.");
    }, Math.max(0, 180_000 - (Date.now() - running.started)));
    async function poll() {
      try {
        const value = await api<Scanner>(`${base}/scanners/${id}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if ((value.last_result?.at || value.last_run_at) !== running!.baseline) {
          setScanner(value);
          setRunning(null);
          setNotice(Object.keys(value.last_result?.errors || {}).length
            ? "Проверка завершена с ошибками площадок — подробности во вкладке «Статистика»."
            : "Проверка завершена. Результаты доступны в ленте и статистике.");
        } else timer = setTimeout(poll, 5000);
      } catch (e) {
        if (!controller.signal.aborted) {
          setError(scannerError(e));
          setRunning(null);
        }
      }
    }
    timer = setTimeout(poll, 5000);
    return () => { controller.abort(); clearTimeout(timer); clearTimeout(deadline); };
  }, [id, running]);

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
        const started = Date.now();
        await send(`/scanners/${id}/run`, {});
        setRunning({ baseline: scanner.last_result?.at || scanner.last_run_at, started });
        setNotice("Проверяем площадки. Результат появится в течение нескольких минут.");
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
