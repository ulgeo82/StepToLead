"use client";

/* Общий индикатор долгих операций админки. Операции идут на сервере, что бы ни происходило со вкладкой;
   здесь — их видимость: плашка в углу на любой странице (сколько идёт, сколько осталось, ссылка туда, где
   запустили) и восстановление прогресса, когда возвращаешься на страницу (useResumeTask).
   Данные — GET /api/admin/tasks (снимки app/services/progress.py). */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { formatSeconds, ProgressSnapshot } from "./task-progress";
import css from "./tasks.module.css";

export type TaskItem = ProgressSnapshot & {
  key: string; link: string | null; page: string; run_id: number | null; source: string | null;
};
type TasksState = { items: TaskItem[]; refresh: () => void };

const TasksContext = createContext<TasksState>({ items: [], refresh: () => undefined });
const ACTIVE_POLL = 2000, IDLE_POLL = 8000, SHOW_DONE_MS = 3 * 60 * 1000;
const DISMISSED = "stl:tasks:dismissed";

export const isActive = (t: ProgressSnapshot) => t.state === "queued" || t.state === "running";

/** Сообщить индикатору, что запущена новая операция, — чтобы он показал её сразу, а не через несколько секунд. */
export function notifyTasks() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("stl:tasks"));
}

export function useTasks() { return useContext(TasksContext); }

/** Идущая операция, подходящая под условие, — чтобы страница снова показала её прогресс после возвращения. */
export function useResumeTask(match: (task: TaskItem) => boolean): TaskItem | null {
  const { items } = useTasks();
  return items.find(task => isActive(task) && match(task)) ?? null;
}

function readDismissed(): string[] {
  try { return JSON.parse(window.sessionStorage.getItem(DISMISSED) || "[]"); } catch { return []; }
}

export function TasksProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<TaskItem[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const inFlight = useRef(false);
  const poll = useCallback(async () => {
    clearTimeout(timer.current);
    if (inFlight.current) return;
    inFlight.current = true;
    let active = false;
    try {
      const data = await api<{ items: TaskItem[] }>("/admin/tasks", { timeoutMs: 10_000 });
      setItems(data.items);
      active = data.items.some(isActive);
    } catch { /* сеть моргнула — следующий опрос покажет */ }
    finally { inFlight.current = false; }
    // Вкладка скрыта — не опрашиваем зря; работа на сервере идёт, при возвращении покажем свежее.
    if (document.hidden) return;
    timer.current = setTimeout(() => void poll(), active ? ACTIVE_POLL : IDLE_POLL);
  }, []);
  useEffect(() => {
    void poll();
    const wake = () => { if (!document.hidden) void poll(); };
    const soon = () => { setTimeout(() => void poll(), 400); };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("focus", wake);
    window.addEventListener("stl:tasks", soon);
    return () => {
      clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("focus", wake);
      window.removeEventListener("stl:tasks", soon);
    };
  }, [poll]);
  return <TasksContext.Provider value={{ items, refresh: () => void poll() }}>
    {children}
    <TasksDock items={items}/>
  </TasksContext.Provider>;
}

function TasksDock({ items }: { items: TaskItem[] }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState<string[]>([]);
  // Законченные показываем, только если видели их идущими в этой вкладке, и недолго — как уведомление.
  const seenActive = useRef(new Set<string>());
  const doneAt = useRef(new Map<string, number>());
  const [, setTick] = useState(0);
  useEffect(() => setDismissed(readDismissed()), []);
  useEffect(() => {
    for (const task of items) {
      if (isActive(task)) seenActive.current.add(task.key);
      else if (seenActive.current.has(task.key) && !doneAt.current.has(task.key)) doneAt.current.set(task.key, Date.now());
    }
    const id = setTimeout(() => setTick(n => n + 1), SHOW_DONE_MS + 500);
    return () => clearTimeout(id);
  }, [items]);
  const now = Date.now();
  const shown = items.filter(task => !dismissed.includes(task.key) && (isActive(task)
    || (doneAt.current.has(task.key) && now - (doneAt.current.get(task.key) ?? 0) < SHOW_DONE_MS)));
  if (!shown.length) return null;
  const running = shown.filter(isActive);
  const percent = running.length
    ? Math.round(running.reduce((sum, t) => sum + (t.percent ?? 0), 0) / running.length) : 100;
  const left = Math.max(0, ...running.map(t => t.remaining ?? 0));
  const failed = shown.some(t => t.state === "error");
  function dismiss(key: string) {
    const next = [...dismissed, key].slice(-50);
    setDismissed(next);
    try { window.sessionStorage.setItem(DISMISSED, JSON.stringify(next)); } catch { /* приватный режим */ }
  }
  const label = running.length
    ? `${running.length === 1 ? running[0].title : `Идёт задач: ${running.length}`} · ${percent}%`
    : failed ? "Задача завершилась с ошибкой" : `Готово: ${shown.length === 1 ? shown[0].title : shown.length}`;
  return <aside className={css.dock} aria-label="Фоновые задачи">
    {open && <div className={css.panel} role="region" aria-label="Список задач">
      {shown.map(task => <article key={task.key} className={css.item} data-state={task.state}>
        <div className={css.itemHead}>
          <strong>{task.title}</strong>
          <span>{isActive(task) ? `${task.percent ?? 0}%` : task.state === "error" ? "Ошибка" : "Готово"}</span>
        </div>
        <div className={css.track}><div className={css.fill} style={{ width: `${isActive(task) ? task.percent ?? 0 : 100}%` }}/></div>
        <p className={css.meta}>{isActive(task)
          ? (task.state === "queued" ? "В очереди" : task.current || "Выполняется")
            + (task.remaining ? ` · осталось ≈ ${formatSeconds(task.remaining)}` : "")
          : task.message || (task.state === "error" ? "Ошибка выполнения" : "Готово")}</p>
        <div className={css.itemActions}>
          {task.link && pathname !== task.link.split("?")[0] && <Link href={task.link} onClick={() => setOpen(false)}>Открыть →</Link>}
          {!isActive(task) && <button type="button" onClick={() => dismiss(task.key)}>Скрыть</button>}
        </div>
      </article>)}
    </div>}
    <button type="button" className={css.pill} data-state={running.length ? "running" : failed ? "error" : "done"}
      aria-expanded={open} onClick={() => setOpen(value => !value)}>
      <i aria-hidden="true">{running.length ? "◐" : failed ? "✕" : "✓"}</i>
      <span>{label}</span>
      {running.length > 0 && left > 0 && <small>≈ {formatSeconds(left)}</small>}
    </button>
  </aside>;
}
