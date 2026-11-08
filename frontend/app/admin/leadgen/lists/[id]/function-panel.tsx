"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Estimate, EstimateHint, TaskProgress } from "@/components/task-progress";
import { base, post } from "../../shared";
import { ChainResult } from "../../sources/chain-result";
import { SourceCost } from "../../sources/source-cost";
import { CompanySource, shortError, SourceRun } from "../../sources/source-types";
import styles from "../../leadgen.module.css";
import css from "./workspace.module.css";
import { orderedSteps, safeRead, safeWrite, stepNames, stepOrder } from "./workspace-utils";

type RunEstimate = Estimate & { cost?: { requests: number; rub: number | null }; ai_tokens?: number };

export function FunctionPanel({ listId, sources, ids, total, allSelected, locked, changed, running }: {
  listId: number; sources: CompanySource[]; ids: number[]; total: number; allSelected: boolean;
  locked: boolean; changed: () => void; running: (value: boolean) => void;
}) {
  const storageKey = `stl:leadgen:list:${listId}:steps`;
  const [selected, setSelected] = useState<string[]>([]);
  const [scope, setScope] = useState("selected");
  const [hydrated, setHydrated] = useState(false);
  const [estimate, setEstimate] = useState<{ key: string; data: RunEstimate } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<{ id: number; since: number } | null>(null);
  const [active, setActive] = useState(false);
  const [finished, setFinished] = useState(false);
  const [result, setResult] = useState<SourceRun | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!sources.length) return;
    const saved = safeRead(storageKey);
    setSelected(Array.isArray(saved) ? saved.filter((item): item is string => typeof item === "string")
      : sources.filter(item => item.chain_default).map(item => item.code));
    setHydrated(true);
  }, [storageKey, sources]);
  const steps = orderedSteps(selected).filter(code => {
    const source = sources.find(item => item.code === code);
    return source?.ready && source.enabled;
  });
  const whole = scope === "all" || allSelected;
  const count = whole ? total : ids.length;
  const body = { steps, ...(!whole ? { company_ids: ids } : {}) };
  const key = JSON.stringify(body);
  const canEstimate = hydrated && steps.length > 0 && count > 0 && !locked && !active && !busy;
  useEffect(() => {
    setEstimate(null);
    setError("");
    if (!canEstimate) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      post<RunEstimate>(`/lists/${listId}/estimate`, JSON.parse(key), controller.signal)
        .then(data => !controller.signal.aborted && setEstimate({ key, data }))
        .catch(e => !controller.signal.aborted && setError(shortError(e)));
    }, 350);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [key, listId, canEstimate, retry]);
  useEffect(() => {
    if (!run || !finished) return;
    const controller = new AbortController();
    api<SourceRun>(`${base}/runs/${run.id}`, { signal: controller.signal })
      .then(data => {
        if (controller.signal.aborted) return;
        setResult(data);
        changed();
      }).catch(e => !controller.signal.aborted && setError(shortError(e)));
    return () => controller.abort();
  }, [run, finished, changed, retry]);

  function toggle(code: string) {
    const next = selected.includes(code) ? selected.filter(item => item !== code) : [...selected, code];
    setSelected(next);
    safeWrite(storageKey, next);
  }
  async function start() {
    if (!canEstimate || estimate?.key !== key) return;
    setBusy(true);
    setError("");
    setResult(null);
    setFinished(false);
    const since = Date.now() / 1000;
    try {
      const data = await post<{ run_id: number }>(`/lists/${listId}/run`, body);
      setRun({ id: data.run_id, since });
      setActive(true);
      running(true);
    } catch (e) { setError(shortError(e)); }
    finally { setBusy(false); }
  }
  return <section className={`${styles.card} ${css.panel}`} aria-label="Функции базы">
    <h2>Функции</h2>
    <div className={css.functions}>{stepOrder.map(code => {
      const source = sources.find(item => item.code === code);
      const unavailable = !source?.ready || !source.enabled;
      const reason = unavailable ? source?.ready_reason || "Функция пока недоступна" : source.purpose;
      return <span key={code} title={shortError(new Error(reason || "Функция доступна"))}
        tabIndex={unavailable ? 0 : undefined}
        aria-label={unavailable ? `${source?.title || stepNames[code]}: ${reason}` : undefined}>
        <button type="button" className={`${css.function} ${unavailable ? css.unavailable : ""}`}
          disabled={unavailable || locked || busy || active} aria-pressed={!unavailable && selected.includes(code)}
          onClick={() => toggle(code)}>{source?.icon} {source?.title || stepNames[code]}</button>
      </span>;
    })}</div>
    <div className={css.controls}>
      <label>Область <select value={scope} disabled={active || busy || locked}
        onChange={e => setScope(e.target.value)}>
        <option value="selected">Отмеченные ({allSelected ? total : ids.length})</option>
        <option value="all">Вся база ({total})</option>
      </select></label>
      <button type="button" className={styles.primary} disabled={!canEstimate || estimate?.key !== key}
        onClick={start}>{busy ? "Запускаем…" : active ? "Выполняется…" : `▶ Запустить выбранное (${steps.length})`}</button>
      {canEstimate && !estimate && !error && <small role="status">Оцениваем…</small>}
      {estimate?.key === key && <span><EstimateHint estimate={estimate.data}/>
        {estimate.data.cost && <> · <SourceCost cost={estimate.data.cost}/></>}
        {estimate.data.ai_tokens != null && <> · ИИ: ≈ {estimate.data.ai_tokens.toLocaleString("ru-RU")} токенов</>}
      </span>}
    </div>
    {error && <p role="alert" className={styles.error}>{error}{" "}
      <button type="button" onClick={() => setRetry(value => value + 1)}>Повторить проверку</button></p>}
    {run && <TaskProgress key={run.id} url={`${base}/runs/${run.id}/progress`} active={active}
      since={run.since} onFinish={() => { setActive(false); running(false); setFinished(true); }}/>}
    {result?.result?.chain && <ChainResult chain={result.result.chain} title="Итог по функциям"
      names={Object.fromEntries(sources.map(source => [source.code, source.title]))}/>}
    {result?.result?.errors && Object.entries(result.result.errors).map(([code, reason]) =>
      <p key={code} role="alert" className={styles.error}>{shortError(new Error(reason))}</p>)}
  </section>;
}
