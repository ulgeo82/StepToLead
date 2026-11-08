"use client";

/* Прогресс долгой операции: полоса, текущий шаг, сколько прошло и сколько осталось.
   Универсально: любой экран, у бэкенда которого есть снимок app/services/progress.py. */

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import css from "./task-progress.module.css";

export type ProgressStep = {
  code: string; name: string; state: "pending" | "running" | "done" | "skipped" | "error";
  estimate: number; seconds: number | null; done: number; total: number; note: string;
};
export type ProgressSnapshot = {
  state: "idle" | "queued" | "running" | "done" | "error";
  title?: string; message?: string; percent?: number; elapsed?: number; waited?: number; remaining?: number;
  estimate?: number; current?: string; started_at?: number; steps?: ProgressStep[];
};
export type Estimate = { seconds: number; steps: { code: string; name: string; seconds: number }[] };

/** 42 -> «42 с», 95 -> «1 мин 35 с», 600 -> «10 мин». */
export function formatSeconds(value: number): string {
  const s = Math.max(0, Math.round(value));
  if (s < 60) return `${s} с`;
  const m = Math.floor(s / 60), rest = s % 60;
  return m >= 10 || !rest ? `${m} мин` : `${m} мин ${rest} с`;
}

/** «Займёт ≈ 1 мин» и разбивка по шагам во всплывающей подсказке. */
export function EstimateHint({ estimate, prefix = "Займёт" }: { estimate: Estimate | null; prefix?: string }) {
  if (!estimate) return null;
  const parts = estimate.steps.filter(s => s.seconds >= 1).map(s => `${s.name}: ${formatSeconds(s.seconds)}`);
  return <small className={css.hint} title={parts.join("\n")}>
    ⏱ {prefix} ≈ {formatSeconds(Math.max(1, estimate.seconds))}
  </small>;
}

const icons: Record<ProgressStep["state"], string> = {
  pending: "○", running: "◐", done: "✓", skipped: "–", error: "✕",
};

/** Опрашивает url раз в секунду, пока active. since — время нажатия (сек): старый снимок прошлого запуска
 *  не принимаем за новый. onFinish вызывается один раз, когда операция закончилась. */
export function TaskProgress({ url, active, since, onFinish }: {
  url: string; active: boolean; since: number; onFinish?: (snapshot: ProgressSnapshot) => void;
}) {
  const [snapshot, setSnapshot] = useState<ProgressSnapshot | null>(null);
  const finished = useRef(false);
  const done = useRef(onFinish);
  done.current = onFinish;
  useEffect(() => {
    if (!active) return;
    finished.current = false;
    setSnapshot(null);
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await api<ProgressSnapshot>(url, { signal: controller.signal, timeoutMs: 10_000 });
        if (controller.signal.aborted) return;
        const fresh = value.state !== "idle" && (value.started_at ?? 0) >= since - 2;
        if (fresh) {
          setSnapshot(value);
          if ((value.state === "done" || value.state === "error") && !finished.current) {
            finished.current = true;
            done.current?.(value);
            return;
          }
        }
      } catch { /* сеть моргнула — следующий опрос покажет */ }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1000);
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [url, active, since]);

  if (!active && !snapshot) return null;
  const percent = snapshot?.percent ?? 0;
  const queued = snapshot?.state === "queued";
  const failed = snapshot?.state === "error";
  const label = !snapshot ? "Запускаем…"
    : queued ? "В очереди: сейчас идёт другая проверка, начнём сразу после неё"
    : snapshot.state === "done" || failed ? snapshot.message || "Готово"
    : snapshot.current || snapshot.title || "Идёт проверка";
  return <div className={css.box} aria-live="polite">
    <div className={css.top}>
      <b>{percent}%</b>
      <span className={failed ? css.error : ""}>{label}</span>
    </div>
    <div className={`${css.track} ${queued ? css.queued : ""}`} role="progressbar" aria-valuemin={0}
      aria-valuemax={100} aria-valuenow={percent} aria-label={snapshot?.title || "Прогресс"}>
      <div className={`${css.fill} ${failed ? css.fillError : ""}`} style={{ width: `${Math.max(percent, 2)}%` }}/>
    </div>
    {snapshot && snapshot.state !== "idle" && <small className={css.meta}>
      Прошло {formatSeconds(snapshot.elapsed ?? 0)}
      {snapshot.state === "running" || queued ? ` · осталось ≈ ${formatSeconds(snapshot.remaining ?? 0)}` : ""}
      {(snapshot.waited ?? 0) >= 3 ? ` · ждали очереди ${formatSeconds(snapshot.waited ?? 0)}` : ""}
    </small>}
    {!!snapshot?.steps?.length && <details className={css.steps}>
      <summary>Шаги ({snapshot.steps.filter(s => s.state !== "pending" && s.state !== "running").length}
        {" из "}{snapshot.steps.length})</summary>
      <ol>{snapshot.steps.map(step => <li key={step.code} data-state={step.state}>
        <span aria-hidden="true">{icons[step.state]}</span>
        <span>{step.name}{step.state === "running" && step.total ? ` · ${Math.min(Math.max(step.done, 1), step.total)} из ${step.total}` : ""}
          {step.note ? <small> · {step.note}</small> : null}</span>
        <small>{step.seconds != null ? formatSeconds(step.seconds) : `≈ ${formatSeconds(step.estimate)}`}</small>
      </li>)}</ol>
    </details>}
  </div>;
}
