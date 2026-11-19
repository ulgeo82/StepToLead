"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { TaskProgress } from "@/components/task-progress";
import { notifyTasks } from "@/components/tasks";
import type { Health, HealthLevel, InfraProxy } from "./telegram-types";
import css from "./telegram-infra.module.css";

export const pools = { agency: "Аутрич", client: "CRM-профили клиентов" };
export const healthLabels = { ok: "В порядке", warn: "Предупреждение", dead: "Не работает", unknown: "Не проверено" };
export const proxyStatuses: Record<string, string> = {
  untested: "Не проверен", working: "Работает", error: "Не работает",
};
export const accountStatuses: Record<string, string> = {
  online: "В сети", offline: "Не в сети", checking: "Проверяем", connecting: "Подключаем",
  disconnected: "Отключён", flood_wait: "Пауза Telegram", error: "Ошибка", waiting_proxy: "Ждёт прокси",
  spamblock: "Спам-блок", code_sent: "Ожидает код", password_required: "Нужен пароль 2FA",
  unauthorized: "Сессия слетела — войдите заново", banned: "Заблокирован Telegram",
};

export function isFreeProxy(proxy: InfraProxy) {
  return proxy.pool === "agency" && !proxy.dedicated_workspace_id && proxy.status === "working"
    && proxy.health?.level !== "dead" && !proxy.rotating && proxy.duplicate_of == null
    && !!proxy.exit_ip && proxy.assigned_accounts < proxy.max_accounts;
}

const proxyInputErrors = new Set([
  "пустая строка", "некорректный URL прокси", "некорректный host или port",
  "ожидается login:pass@host:port или host:port@login:pass",
  "ожидается host:port, host:port:login:pass, login:pass@host:port или scheme://…",
]);

export function proxyImportError(message: string, index: number) {
  const match = message.match(/^Строка\s+(\d+):\s*(.*)$/iu);
  if (!match) return `Ошибка добавления №${index + 1}.`;
  const reason = proxyInputErrors.has(match[2]) ? match[2] : "не удалось добавить прокси";
  return `Строка ${match[1]}: ${reason}.`;
}

export function json<T>(url: string, body: unknown, method = "POST") {
  return api<T>(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

// Старые ответы и диагностика сторонних сервисов могут содержать реквизиты подключения.
export function safeDiagnostic(value: string) {
  return value
    .replace(/\b\w+:\/\/[^\s,;]+/g, "[адрес скрыт]")
    .replace(/[\w.%+\-]+:[^\s]+@[^\s]+|[^\s]+:\d{1,5}(?::[^\s]+|@[^\s]+)/g, "[реквизиты скрыты]")
    .replace(/\b(?:password|session|passwd)\s*[:=]\s*\S+/gi, "[секрет скрыт]")
    .slice(0, 240);
}

export function HealthBadge({ health, level }: { health?: Health | null; level?: HealthLevel }) {
  const state = health?.level ?? level ?? "unknown";
  return <span className={css.health} data-level={state}
    title={health?.reasons?.map(safeDiagnostic).join("\n") || undefined}>
    {healthLabels[state]}{health && state !== "unknown" && health.score != null ? ` · ${health.score}/100` : ""}
  </span>;
}

export function Stats({ values }: { values: [string, number | undefined][] }) {
  return <div className={css.stats}>{values.map(([label, value]) =>
    <div className="statPill" key={label}><strong>{value ?? "—"}</strong><span>{label}</span></div>)}</div>;
}

export function useInfraTask() {
  const [task, setTask] = useState<{ url: string; active: boolean } | null>(null);
  return {
    task,
    start(url: string) { setTask({ url, active: true }); notifyTasks(); },
    finish() { setTask(current => current ? { ...current, active: false } : null); },
  };
}

export function InfraProgress({ job, onFinish }: {
  job: ReturnType<typeof useInfraTask>;
  onFinish?: () => void;
}) {
  if (!job.task) return null;
  return <TaskProgress key={job.task.url} url={job.task.url} active={job.task.active}
    since={0} onFinish={() => { job.finish(); notifyTasks(); onFinish?.(); }}/>;
}

export function toggleId(ids: number[], id: number) {
  return ids.includes(id) ? ids.filter(current => current !== id) : [...ids, id];
}
