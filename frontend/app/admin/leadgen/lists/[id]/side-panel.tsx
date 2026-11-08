"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { base, companyName, fitLabels, post, stages } from "../../shared";
import { shortError } from "../../sources/source-types";
import styles from "../../leadgen.module.css";
import css from "./workspace.module.css";
import { emailLabels } from "./workspace-table";
import { Summary, TableRow } from "./workspace-utils";

const summaryColumns: Record<string, string> = {
  city: "Город", niche: "Ниша", domain: "Домен", source: "Источник", signal: "Сигнал",
  fit: "Оценка ИИ", email_status: "Статус почты", stage: "Стадия",
};
const cleanupRules: Record<string, string> = {
  no_email: "Скрыть без почты", bad_email: "Скрыть с невалидной почтой",
  not_fit: "Скрыть «не подходит» по ИИ", chains: "Скрыть сети", duplicates: "Отправить дубли на проверку",
};

export function SidePanel({ id, revision, locked, changed, filter, labels }: {
  id: number; revision: number; locked: boolean; changed: () => void;
  filter: (column: string, value: string) => void; labels: Record<string, string>;
}) {
  const [column, setColumn] = useState("city");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryError, setSummaryError] = useState("");
  const [rule, setRule] = useState("no_email");
  const [revenueMax, setRevenueMax] = useState("");
  const [preview, setPreview] = useState<{ count: number; sample: TableRow[] } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setSummary(null);
    setSummaryError("");
    api<Summary>(`${base}/lists/${id}/summary?column=${column}`, { signal: controller.signal })
      .then(data => !controller.signal.aborted && setSummary(data))
      .catch(e => !controller.signal.aborted && setSummaryError(shortError(e)));
    return () => controller.abort();
  }, [id, column, revision]);
  useEffect(() => { setPreview(null); setError(""); }, [rule, revenueMax, revision, id]);
  const cleanupBody = { rule, ...(rule === "chains" && revenueMax ? { revenue_max: Number(revenueMax) } : {}) };
  const thresholdValid = rule !== "chains" || !revenueMax || Number.isSafeInteger(Number(revenueMax)) && Number(revenueMax) > 0;
  async function inspect() {
    setBusy(true);
    setError("");
    setNotice("");
    try { setPreview(await post(`/lists/${id}/cleanup/preview`, cleanupBody)); }
    catch (e) { setError(shortError(e)); }
    finally { setBusy(false); }
  }
  async function apply() {
    if (!preview?.count || locked || busy || !window.confirm(
      `${cleanupRules[rule]}: затронет ${preview.count} компаний. Применить ко всей базе?`,
    )) return;
    setBusy(true);
    setError("");
    try {
      const result = await post<{ updated: number }>(`/lists/${id}/cleanup`, cleanupBody);
      setNotice(`Обновлено: ${result.updated}`);
      setPreview(null);
      changed();
    } catch (e) { setError(shortError(e)); }
    finally { setBusy(false); }
  }
  const valueLabels = column === "fit" ? fitLabels : column === "stage" ? stages
    : column === "signal" ? labels : column === "email_status" ? emailLabels : {};
  return <aside className={css.aside} aria-label="Сводка и очистка">
    <section className={styles.card}><h2>Сводка</h2>
      <label className={styles.field}>Колонка<select value={column} onChange={e => setColumn(e.target.value)}>
        {Object.entries(summaryColumns).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
      </select></label>
      {summaryError ? <p role="alert" className={styles.error}>{summaryError}</p>
        : !summary ? <p role="status">Загружаем сводку…</p> : <>
          <p>Всего: {summary.total}</p>
          <div className={css.summary}>{summary.items.map(item => <button type="button"
            className={styles.secondary} key={item.value ?? "__empty"} disabled={locked}
            onClick={() => filter(column, item.value == null || item.value === "" ? "empty"
              : ["signal", "fit", "email_status", "stage"].includes(column) ? `in:${item.value}`
                : `contains:${item.value}`)}>
            <span>{item.value == null || item.value === "" ? "Не указано" : valueLabels[item.value] || item.value}</span>
            <b>{item.count}</b></button>)}</div>
        </>}
    </section>
    <section className={styles.card}><h2>Очистка</h2>
      <p className={css.hint}>Применяется ко всей базе, независимо от фильтров таблицы.</p>
      <div className={css.cleanup}>
        <label className={styles.field}>Набор<select value={rule} disabled={busy || locked}
          onChange={e => setRule(e.target.value)}>
          {Object.entries(cleanupRules).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
        </select></label>
        {rule === "chains" && <label className={styles.field}>Порог выручки, ₽ (необязательно)
          <input type="number" min="1" step="1" value={revenueMax} disabled={busy || locked}
            onChange={e => setRevenueMax(e.target.value)}/>
          <small className={css.hint}>Порог филиалов в API не предусмотрен. Без суммы применяется порог сервера.</small>
        </label>}
        <button type="button" className={styles.secondary} disabled={busy || locked || !thresholdValid} onClick={inspect}>
          {busy ? "Выполняется…" : "Узнать, сколько затронет"}</button>
        {preview && <div role="status"><p>Затронет {preview.count} компаний</p>
          <ul>{preview.sample.map(row => <li key={row.id}>{companyName(row)}</li>)}</ul>
          <button type="button" className={styles.primary} disabled={!preview.count || busy || locked}
            onClick={apply}>Подтвердить очистку</button>
        </div>}
        {error && <p role="alert" className={styles.error}>{error}</p>}
        {notice && <p role="status">{notice}</p>}
      </div>
    </section>
  </aside>;
}
