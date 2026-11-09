"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { EstimateHint, ProgressSnapshot, TaskProgress } from "@/components/task-progress";
import { notifyTasks, useResumeTask } from "@/components/tasks";
import { base, LeadList, Segment, signalLabels } from "../shared";
import styles from "../leadgen.module.css";
import { SourceFields } from "./source-fields";
import { SourceCost } from "./source-cost";
import { ChainResult, CheckCounters } from "./chain-result";
import { AfterSearch, defaultChain } from "./after-search";
import {
  CompanySource, defaults, normalize, RunBody, shortError, SourceEstimate, SourceRun, Target, valid, Values,
} from "./source-types";

export function SourceRunner({ source, lists, segments, targetError, initialList, chainSources = [] }: {
  source: CompanySource; lists: LeadList[]; segments: Segment[]; targetError: string; initialList: string;
  chainSources?: CompanySource[];
}) {
  const [after, setAfter] = useState<string[]>(() => defaultChain(chainSources));
  const [recheck, setRecheck] = useState(false);
  const [query, setQuery] = useState<Values>(() => defaults(source.query_schema));
  const [listName, setListName] = useState("");
  const [targetKind, setTargetKind] = useState<"list" | "segment">("list");
  const [targetId, setTargetId] = useState(initialList);
  const [estimate, setEstimate] = useState<{ key: string; value: SourceEstimate } | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [estimateError, setEstimateError] = useState("");
  const [busy, setBusy] = useState(false);
  const [active, setActive] = useState(false);
  const [run, setRun] = useState<{ id: number; since: number; target?: Target } | null>(null);
  const [finished, setFinished] = useState<ProgressSnapshot | null>(null);
  const [result, setResult] = useState<SourceRun | null>(null);
  const [loadingResult, setLoadingResult] = useState(false);
  const [resultRevision, setResultRevision] = useState(0);
  const [error, setError] = useState("");
  const finder = source.kind === "finder";
  const resumed = useResumeTask(task => task.page === "source" && task.source === source.code);
  useEffect(() => {
    if (!resumed?.run_id || run) return;
    setRun({ id: resumed.run_id, since: resumed.started_at ?? 0 });
    setActive(true);
  }, [resumed?.key]); // eslint-disable-line react-hooks/exhaustive-deps
  const target: Target | undefined = targetId ? targetKind === "list"
    ? { list_id: Number(targetId) } : { segment_id: Number(targetId) } : undefined;
  const targetSize = targetKind === "list" ? lists.find(l => String(l.id) === targetId)?.total
    : segments.find(s => String(s.id) === targetId)?.count;
  const targetValid = !!target && Number.isSafeInteger(Number(targetId)) && Number(targetId) > 0 &&
    targetSize != null && targetSize > 0 && targetSize <= 500 && !targetError;
  const canEstimate = source.ready && source.enabled && !busy && !active &&
    (finder ? valid(source.query_schema, query) : targetValid);
  const ordered = chainSources.filter(item => after.includes(item.code)).map(item => item.code);
  const estimateBody = finder ? { query: normalize(source.query_schema, query), then: ordered } : { target };
  const key = JSON.stringify(estimateBody);

  useEffect(() => {
    const controller = new AbortController();
    setEstimate(null);
    setEstimateError("");
    setEstimating(false);
    if (!canEstimate) return () => controller.abort();
    setEstimating(true);
    const timer = setTimeout(() => {
      api<SourceEstimate>(`${base}/sources/${encodeURIComponent(source.code)}/estimate`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: key, signal: controller.signal,
      }).then(value => {
        if (!controller.signal.aborted) setEstimate({ key, value });
      }).catch(e => { if (!controller.signal.aborted) setEstimateError(shortError(e)); })
        .finally(() => { if (!controller.signal.aborted) setEstimating(false); });
    }, 400);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [key, source, canEstimate, resultRevision]);

  useEffect(() => {
    if (!finished || !run) return;
    const controller = new AbortController();
    setLoadingResult(true);
    api<SourceRun>(`${base}/runs/${run.id}`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) { setResult(value); setError(""); }
    }).catch(e => { if (!controller.signal.aborted) setError(shortError(e)); })
      .finally(() => { if (!controller.signal.aborted) setLoadingResult(false); });
    return () => controller.abort();
  }, [finished, run, resultRevision]);

  async function start(event: FormEvent) {
    event.preventDefault();
    if (!canEstimate || !estimate || estimate.key !== key || busy) return;
    setBusy(true);
    setError("");
    setResult(null);
    setFinished(null);
    const since = Date.now() / 1000;
    const body: RunBody = { ...estimateBody, recheck,
      ...(finder && listName.trim() ? { list_name: listName.trim() } : {}) };
    try {
      const value = await api<{ run_id: number }>(`${base}/sources/${encodeURIComponent(source.code)}/run`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      setRun({ id: value.run_id, since, ...(finder ? {} : { target }) });
      setActive(true);
      notifyTasks();
    } catch (e) { setError(shortError(e)); }
    finally { setBusy(false); }
  }
  const labels = { ...Object.fromEntries(source.signals.map(s => [s.kind, s.label])), ...signalLabels };
  const targetLink = run?.target && "list_id" in run.target
    ? `/admin/leadgen/companies?list_id=${run.target.list_id}` : run?.target && "segment_id" in run.target
      ? `/admin/leadgen/companies?segment_id=${run.target.segment_id}` : null;
  return <section className={styles.sourceLaunch}>
    <h3>{finder ? "Найти компании" : "Дообогатить компании"}</h3>
    <form onSubmit={start}>
      <fieldset className={styles.formFieldset} disabled={busy || active}>
        {finder ? <>
          <SourceFields schema={source.query_schema} values={query} prefix={`query-${source.code}`} changed={setQuery}/>
          <label className={styles.field}>Название нового списка (необязательно)<input value={listName}
            onChange={e => setListName(e.target.value)}/></label>
          {!!chainSources.length && <AfterSearch sources={chainSources} selected={after} changed={setAfter}/>}
        </> : <>
          <label className={styles.field}>Кого дополнить<select value={targetKind} onChange={e => {
            setTargetKind(e.target.value as "list" | "segment"); setTargetId("");
          }}><option value="list">Список</option><option value="segment">Сегмент</option></select></label>
          <label className={styles.field}>{targetKind === "list" ? "Список компаний" : "Сегмент"}
            <select required value={targetId} onChange={e => setTargetId(e.target.value)}>
              <option value="">Выберите {targetKind === "list" ? "список" : "сегмент"}</option>
              {(targetKind === "list" ? lists.map(l => ({ id: l.id, name: l.name, count: l.total }))
                : segments).map(item => <option key={item.id} value={item.id}>{item.name} · {item.count}</option>)}
            </select></label>
          {targetError && <p role="alert" className={styles.error}>{targetError}</p>}
          {targetSize === 0 && <p className={styles.muted}>В выбранном наборе нет компаний.</p>}
          {targetSize != null && targetSize > 500 && <p role="alert" className={styles.error}>
            Не больше 500 компаний за запуск. Выберите меньший список или сегмент.</p>}
          <small className={styles.hint}>Компании без необходимых для этого шага данных будут пропущены.</small>
          {source.code === "email_by_inn" && <label className={styles.check}>
            <input type="checkbox" checked={recheck} onChange={e => setRecheck(e.target.checked)}/>
            Перепроверить недавно проверенные
          </label>}
        </>}
        {estimating && <p role="status">Оцениваем время…</p>}
        {estimate?.key === key && <p><EstimateHint estimate={estimate.value}/>
          {estimate.value.cost && <> · <SourceCost cost={estimate.value.cost}/></>}
          {estimate.value.companies != null && <small> · компаний: {estimate.value.companies}</small>}</p>}
        {estimateError && <p role="alert" className={styles.error}>{estimateError}
          <button type="button" className={styles.secondary} onClick={() => setResultRevision(n => n + 1)}>
            Повторить оценку</button></p>}
        <button type="submit" className={styles.primary} disabled={!canEstimate || estimating || estimate?.key !== key}>
          {busy ? "Запускаем…" : active ? "Выполняется…" : finder ? "Запустить поиск" : "Запустить дообогащение"}
        </button>
      </fieldset>
    </form>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {run && <TaskProgress key={run.id} url={`${base}/runs/${run.id}/progress`} active={active} since={run.since}
      onFinish={snapshot => { setFinished(snapshot); setActive(false); }}/>} 
    {finished && <div role="status" className={styles.sourceResult}>
      <p>{shortError(new Error(finished.message || (finished.state === "error" ? "Ошибка выполнения" : "Готово")))}</p>
      {loadingResult && <p>Загружаем итог…</p>}
      {result?.result && <>
        {finder && <h4>Поиск</h4>}
        <p>Найдено: {result.result.found} · новых: {result.result.new} · обновлено: {result.result.updated}
          {" · "}пропущено: {result.result.skipped}</p>
        <CheckCounters result={result.result}/>
        {Object.entries(result.result.reasons || {}).map(([reason, count]) => <p key={reason}>
          {shortError(new Error(reason))}: {count}</p>)}
        <ul>{Object.entries(result.result.signals).map(([kind, count]) => <li key={kind}>
          {labels[kind] || kind}: {count}</li>)}</ul>
        {Object.entries(result.result.errors).map(([code, reason]) => <p key={code} className={styles.errorText}>
          {shortError(new Error(reason))}</p>)}
      </>}
      {result?.result.chain && <ChainResult chain={result.result.chain}
        names={Object.fromEntries(chainSources.map(item => [item.code, item.title]))}/>}
      {finder && result?.list_id != null && <Link href={`/admin/leadgen/companies?list_id=${result.list_id}`}>
        Открыть список компаний →</Link>}
      {!finder && targetLink && <Link href={targetLink}>Открыть компании выбранного набора →</Link>}
      {error && <button type="button" className={styles.secondary} onClick={() => setResultRevision(n => n + 1)}>
        Повторить загрузку итога</button>}
    </div>}
  </section>;
}
