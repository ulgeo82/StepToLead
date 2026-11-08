"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { FormEvent, Suspense, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { base, date, LeadList, PageHeader, Segment, signalLabels } from "../shared";
import styles from "../leadgen.module.css";
import { SourceFields } from "./source-fields";
import { SourceRunner } from "./source-run";
import { CompanySource, defaults, normalize, shortError, Values } from "./source-types";

export default function SourcesPage() {
  return <Suspense fallback={<p role="status">Загружаем источники…</p>}><Sources/></Suspense>;
}

function Sources() {
  const params = useSearchParams();
  const [sources, setSources] = useState<CompanySource[] | null>(null);
  const [lists, setLists] = useState<LeadList[]>([]);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [error, setError] = useState("");
  const [targetError, setTargetError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setTargetError("");
    api<{ items: CompanySource[] }>(`${base}/sources`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setSources(value.items);
    }).catch(e => { if (!controller.signal.aborted) setError(shortError(e)); });
    Promise.all([
      api<{ items: LeadList[] }>(`${base}/lists`, { signal: controller.signal }),
      api<{ items: Segment[] }>(`${base}/segments`, { signal: controller.signal }),
    ]).then(([l, s]) => {
      if (!controller.signal.aborted) { setLists(l.items); setSegments(s.items); }
    }).catch(e => { if (!controller.signal.aborted) setTargetError(shortError(e)); });
    return () => controller.abort();
  }, [revision]);
  return <div className={`page ${styles.page} ${styles.companySources}`}>
    <PageHeader title="Источники компаний" subtitle="Поиск новых компаний и дообогащение существующих списков.">
      <Link href="/admin/leadgen/companies" className={styles.secondary}>База компаний</Link>
    </PageHeader>
    {error && <p className={styles.error} role="alert">{error}</p>}
    {(error || targetError) && <button type="button" className={styles.secondary}
      onClick={() => setRevision(n => n + 1)}>Повторить загрузку</button>}
    {!sources && !error && <p role="status">Загружаем источники…</p>}
    {sources?.length === 0 && <p className={styles.card}>Источников пока нет.</p>}
    <div className={styles.companySourceGrid}>{sources?.map(source => <SourceCard key={source.code}
      source={source} lists={lists} segments={segments} targetError={targetError}
      initialList={params.get("list_id") || ""} updated={value => setSources(current =>
        current?.map(item => item.code === value.code ? value : item) || [])}/>)}</div>
  </div>;
}

function SourceCard({ source, lists, segments, targetError, initialList, updated }: {
  source: CompanySource; lists: LeadList[]; segments: Segment[]; targetError: string; initialList: string;
  updated: (value: CompanySource) => void;
}) {
  const [draft, setDraft] = useState<Values>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [check, setCheck] = useState<{ ok: boolean; message: string } | null>(null);
  const [notice, setNotice] = useState("");
  async function act(kind: "toggle" | "save" | "check") {
    setBusy(true);
    setError("");
    setNotice("");
    setCheck(null);
    try {
      const path = `${base}/sources/${encodeURIComponent(source.code)}`;
      if (kind === "check") {
        setCheck(await api<{ ok: boolean; message: string }>(`${path}/check`, {
          method: "POST", timeoutMs: 75_000,
        }));
      } else {
        const value = await api<CompanySource>(path, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(kind === "toggle" ? { enabled: !source.enabled }
            : { settings: normalize(source.settings_schema, draft) }),
        });
        if (kind === "save") setDraft({});
        updated(value);
        setNotice("Настройки сохранены");
      }
    } catch (e) { setError(shortError(e)); }
    finally { setBusy(false); }
  }
  function submit(event: FormEvent) { event.preventDefault(); void act("save"); }
  const status = !source.enabled || source.state === "off" ? "⚪ Выключен"
    : !source.ready ? "🟡 Требуется настройка" : source.state === "ok" ? "🟢 Работает"
      : source.state === "error" ? "🔴 Ошибка" : "⚪ Ещё не проверялся";
  return <article className={`${styles.card} ${styles.companySourceCard}`}>
    <div className={styles.sourceCardHead}>
      <div><h2>{source.icon} {source.title}</h2><p className={styles.muted}>{source.purpose}</p></div>
      <label className={styles.check}><input type="checkbox" checked={source.enabled} disabled={busy}
        onChange={() => act("toggle")} aria-label={`Включить источник «${source.title}»`}/>Вкл.</label>
    </div>
    <p>{status} · {source.kind === "finder" ? "Поиск компаний" : "Дообогащение"}</p>
    {!source.ready && source.ready_reason && <p className={styles.muted}>
      {shortError(new Error(source.ready_reason))}</p>}
    {source.last_error && <p className={styles.errorText}>{shortError(new Error(source.last_error))}</p>}
    <small className={styles.muted}>Последняя успешная проверка: {date(source.last_ok_at)}</small>
    <p><button type="button" className={styles.secondary} disabled={busy} onClick={() => act("check")}>
      {busy ? "Подождите…" : "Проверить"}</button></p>
    {source.settings_schema.length > 0 && <details className={styles.sourceSettings}>
      <summary>Настройки</summary>
      <form onSubmit={submit}>
        <fieldset disabled={busy} className={styles.formFieldset}>
          <SourceFields schema={source.settings_schema} values={draft}
            saved={{ ...defaults(source.settings_schema), ...source.settings }}
            prefix={`settings-${source.code}`} changed={setDraft}/>
          <button type="submit" className={styles.secondary} disabled={!Object.keys(draft).length}>
            Сохранить настройки</button>
        </fieldset>
      </form>
    </details>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}
    {check && <p role={check.ok ? "status" : "alert"} className={check.ok ? styles.notice : styles.error}>
      {check.ok ? "✓ " : "✕ "}{shortError(new Error(check.message))}</p>}
    {source.signals.length > 0 && <details className={styles.sourceSettings}>
      <summary>Сигналы источника</summary><ul>{source.signals.map(signal => <li key={signal.kind}>
        {signalLabels[signal.kind] || signal.label || signal.kind}</li>)}</ul>
    </details>}
    <SourceRunner source={source} lists={lists} segments={segments} targetError={targetError} initialList={initialList}/>
  </article>;
}
