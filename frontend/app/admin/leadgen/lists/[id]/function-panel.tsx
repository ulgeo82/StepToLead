"use client";

import { ReactNode, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Estimate, EstimateHint, TaskProgress } from "@/components/task-progress";
import { notifyTasks, useResumeTask } from "@/components/tasks";
import { base, post } from "../../shared";
import { ChainResult } from "../../sources/chain-result";
import { SourceCost } from "../../sources/source-cost";
import { CompanySource, shortError, SourceRun } from "../../sources/source-types";
import css from "./workspace.module.css";
import { Popover } from "./popover";
import { orderedSteps, safeRead, safeWrite, scopeForSelection, stepNames, stepOrder } from "./workspace-utils";

type RunEstimate = Estimate & { cost?: { requests: number; rub: number | null }; ai_tokens?: number };

export function FunctionPanel({ listId, sources, ids, total, allSelected, locked, changed, running, actions }: {
  listId: number; sources: CompanySource[]; ids: number[]; total: number; allSelected: boolean;
  locked: boolean; changed: () => void; running: (value: boolean) => void; actions?: ReactNode;
}) {
  const storageKey = `stl:leadgen:list:${listId}:steps`;
  const [selected, setSelected] = useState<string[]>([]);
  const [scope, setScope] = useState<"all" | "selected">("all");
  const [recheck, setRecheck] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  const [estimate, setEstimate] = useState<{ key: string; data: RunEstimate } | null>(null);
  const [estimateError, setEstimateError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState<{ id: number; since: number } | null>(null);
  const [active, setActive] = useState(false);
  const [finished, setFinished] = useState(false);
  const [failed, setFailed] = useState(false);
  const [result, setResult] = useState<SourceRun | null>(null);
  const [retry, setRetry] = useState(0);
  const selectionCount = allSelected ? total : ids.length;
  useEffect(() => setRecheck(false), [listId]);
  // Вернулись на страницу, а запуск по этой базе ещё идёт — снова показываем его прогресс.
  const resumed = useResumeTask(task => task.page === "list" && task.link === `/admin/leadgen/lists/${listId}`);
  useEffect(() => {
    if (!resumed?.run_id || run) return;
    setRun({ id: resumed.run_id, since: resumed.started_at ?? 0 });
    setActive(true);
    running(true);
  }, [resumed?.key]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => setScope(scopeForSelection(selectionCount)), [selectionCount]);
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
    setEstimateError("");
    if (!canEstimate) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      post<RunEstimate>(`/lists/${listId}/estimate`, JSON.parse(key), controller.signal)
        .then(data => !controller.signal.aborted && setEstimate({ key, data }))
        .catch(e => !controller.signal.aborted && setEstimateError(shortError(e)));
    }, 350);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [key, listId, canEstimate, retry]);
  useEffect(() => {
    if (!run || !finished) return;
    const controller = new AbortController();
    api<SourceRun>(`${base}/runs/${run.id}`, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) { setResult(data); setError(""); } })
      .catch(e => !controller.signal.aborted && setError(shortError(e)));
    return () => controller.abort();
  }, [run, finished, retry]);

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
    setFailed(false);
    const since = Date.now() / 1000;
    try {
      const data = await post<{ run_id: number }>(`/lists/${listId}/run`, { ...body, recheck });
      setRun({ id: data.run_id, since });
      setActive(true);
      running(true);
      notifyTasks();
    } catch (e) { setError(shortError(e)); }
    finally { setBusy(false); }
  }
  const disabledReason = active ? "Идёт запуск — дождитесь завершения"
    : busy ? "Запускаем…" : locked ? "Выполняется другое действие"
      : !sources.length || !hydrated ? "Функции не загружены"
        : !steps.length ? "Выберите хотя бы одну доступную функцию"
          : !count ? "Нет компаний для запуска" : estimateError
            || (estimate?.key !== key ? "Оцениваем время и стоимость…" : "");
  const summary = result?.result?.chain;
  const errors = summary ? Object.values(summary).reduce((sum, item) => sum + item.failed, 0) : 0;
  const completedText = failed ? "Запуск завершился с ошибкой" : result
    ? `Готово: функций ${Object.keys(summary || {}).length}${errors ? ` · ошибок ${errors}` : ""}`
    : error ? "Не удалось получить итог" : "Готово: загружаем итог…";
  const buttons = stepOrder.map(code => {
    const source = sources.find(item => item.code === code);
    const unavailable = !source?.ready || !source.enabled;
    const reason = unavailable ? source?.ready_reason || "Функция пока недоступна" : source.purpose;
    const label = source?.title || stepNames[code];
    return <span key={code} title={`${label}: ${shortError(new Error(reason || "Функция доступна"))}`}
      tabIndex={unavailable ? 0 : undefined} aria-label={unavailable ? `${label}: ${reason}` : undefined}>
      <button type="button" className={`${css.function} ${unavailable ? css.unavailable : ""}`}
        disabled={unavailable || locked || busy || active} aria-pressed={!unavailable && selected.includes(code)}
        onClick={() => toggle(code)}>{source?.icon} {label}</button>
    </span>;
  });
  return <section className={css.tools} aria-label="Функции базы">
    <div className={css.toolRow}>
      <div className={css.functions}>{buttons}</div>
      <Popover label="Функции" trigger={`Функции (${steps.length}) ▾`} className={css.functionMenu}>
        <div className={css.functionItems}>{buttons}</div>
      </Popover>
      <div className={css.launch}>
        {steps.includes("email_by_inn") && <label>
          <input type="checkbox" checked={recheck} disabled={active || busy || locked}
            onChange={event => setRecheck(event.target.checked)}/> Перепроверить недавно проверенные
        </label>}
        <select aria-label="Область запуска" value={scope} disabled={active || busy || locked}
          onChange={e => setScope(e.target.value as "all" | "selected")}>
          <option value="all">Вся база ({total})</option>
          <option value="selected" disabled={!selectionCount}>Отмеченные ({selectionCount})</option>
        </select>
        <button type="button" className={`${css.smallButton} ${css.runButton}`}
          disabled={!canEstimate || estimate?.key !== key} aria-describedby={`run-reason-${listId}`}
          onClick={start}>{busy ? "Запускаем…" : active ? "Выполняется…" : `▶ Запустить выбранное (${steps.length})`}</button>
        <small id={`run-reason-${listId}`} className={css.disabledReason} aria-live="polite">{disabledReason}</small>
        {estimate?.key === key && <span className={css.estimate}><EstimateHint estimate={estimate.data}/>
          {estimate.data.cost && <> · <SourceCost cost={estimate.data.cost}/></>}
          {estimate.data.ai_tokens != null && <> · ИИ: ≈ {estimate.data.ai_tokens.toLocaleString("ru-RU")} токенов</>}
        </span>}
      </div>
      {actions}
    </div>
    <div className={css.runStrip}>
      {error && !finished && <p role="alert" className={css.cellError}>{error}</p>}
      {estimateError && <button type="button" className={css.smallButton}
        onClick={() => setRetry(value => value + 1)}>Повторить оценку</button>}
      {run && active && <div className={css.progressActive}>
        <TaskProgress key={run.id} url={`${base}/runs/${run.id}/progress`} active={active}
          since={run.since} onFinish={snapshot => {
            setActive(false); running(false); setFailed(snapshot.state === "error"); setFinished(true); changed();
          }}/>
      </div>}
      {finished && <div className={css.finished} role="status"><span>{completedText}</span>
        <Popover label="Итог запуска" trigger="Подробнее" align="start">
          <div className={css.runDetails}>
            {error && <p role="alert" className={css.cellError}>{error}
              <button type="button" className={css.smallButton} onClick={() => setRetry(value => value + 1)}>Повторить</button>
            </p>}
            {summary && <ChainResult chain={summary} title="Итог по функциям"
              names={Object.fromEntries(sources.map(source => [source.code, source.title]))}/>}
            {result?.result?.errors && Object.entries(result.result.errors).map(([code, reason]) =>
              <p key={code} className={css.cellError}>{shortError(new Error(reason))}</p>)}
          </div>
        </Popover>
      </div>}
    </div>
  </section>;
}
