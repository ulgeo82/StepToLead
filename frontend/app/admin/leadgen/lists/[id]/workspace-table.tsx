"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { base, date, Fit, fitLabels, money, safeUrl, Score, stages } from "../../shared";
import { EmailStatus } from "../../email-status";
import { shortError } from "../../sources/source-types";
import { Column, TableRow, validInn, valueOf, View } from "./workspace-utils";
import css from "./workspace.module.css";

const editable: Record<string, string> = {
  display_name: "display_name", company: "display_name", domain: "domain", inn: "inn", city: "city", niche: "niche",
  personal_line: "personal_line",
};
export const emailLabels: Record<string, string> = {
  valid: "Проверена", invalid: "Некорректная", risky: "Есть риск", unknown: "Не проверена", none: "Нет почты",
};
export function ColumnFilter({ column, value, changed, labels, sourceLabels = {} }: {
  column: Column; value: string; changed: (value: string) => void; labels: Record<string, string>;
  sourceLabels?: Record<string, string>;
}) {
  const mode = ["empty", "notempty"].includes(value) ? value : value.startsWith("in:") ? "in"
    : value ? "contains" : "";
  const [localMode, setLocalMode] = useState(mode);
  useEffect(() => { if (mode) setLocalMode(mode); }, [mode]);
  const options = /^(fit|fit_label)$/.test(column.key) ? fitLabels : column.key === "stage" ? stages
    : column.key === "email_status" ? emailLabels : /^(signal|signals)$/.test(column.key) ? labels
      : column.key === "messengers" ? { whatsapp: "WhatsApp", telegram: "Telegram", vk: "VK" }
        : column.key === "source" ? sourceLabels : {};
  return <div className={css.filter} onClick={e => e.stopPropagation()}>
    <select aria-label={`Условие: ${column.label}`} value={localMode} onChange={e => {
      setLocalMode(e.target.value);
      changed(["empty", "notempty"].includes(e.target.value) ? e.target.value : "");
    }}>
      <option value="">Любое значение</option>
      {column.filter !== "empty" && <option value="contains">Содержит</option>}
      {column.filter === "values" && <option value="in">Выбор значений</option>}
      <option value="empty">Пусто</option>
      <option value="notempty">Не пусто</option>
    </select>
    {localMode && !["empty", "notempty"].includes(localMode) && (localMode === "in" && Object.keys(options).length ?
      <select multiple aria-label={`Фильтр: ${column.label}`} value={value.replace(/^in:/, "").split(",")}
        onChange={e => {
          const values = Array.from(e.target.selectedOptions, option => option.value);
          changed(values.length ? `in:${values.join(",")}` : "");
        }}>
        {Object.entries(options).map(([code, label]) => <option key={code} value={code}>{label}</option>)}
      </select> : <input aria-label={`Фильтр: ${column.label}`} value={value.replace(/^(contains:|in:)/, "")}
        placeholder={localMode === "in" ? "Значения через запятую" : "Содержит…"}
        onChange={e => changed(e.target.value ? `${localMode}:${e.target.value}` : "")}/>)}
  </div>;
}

function Cell({ row, column, labels }: { row: TableRow; column: Column; labels: Record<string, string> }) {
  const value = valueOf(row, column.key);
  if (column.key === "domain" && row.domain && safeUrl(row.domain)) return <a
    onClick={e => e.stopPropagation()} href={safeUrl(row.domain)} target="_blank" rel="noopener noreferrer">
    {row.domain_display || row.domain} ↗</a>;
  if (["email", "best_email"].includes(column.key)) return row.best_email ? <>
    {row.best_email.value} <EmailStatus status={row.best_email.verify_status}/>
    {row.emails > 1 && <small> · ещё {row.emails - 1}</small>}</> : <>—</>;
  if (column.key === "email_status") return <EmailStatus status={row.best_email?.verify_status}/>;
  if (["signal", "signals"].includes(column.key)) return <>{row.signals.map(code =>
    <span key={code} className={css.hint}>[{labels[code] || code}] </span>)}</>;
  if (["fit", "fit_label"].includes(column.key)) return <Fit label={row.fit_label}/>;
  if (column.key === "score") return <Score value={row.score}/>;
  if (column.key === "stage") return <>{stages[row.stage] || row.stage}</>;
  if (column.key === "revenue_rub") return <>{money(row.revenue_rub)}</>;
  if (column.key === "added_at") return <>{date(row.added_at)}</>;
  return <>{value || "—"}</>;
}

export function WorkspaceTable({ rows, columns, view, setView, selected, allSelected, toggle,
  selectPage, open, changed, labels, sourceLabels, wrap, duplicates, locked }: {
  rows: TableRow[]; columns: Column[]; view: View; setView: (value: View) => void;
  selected: number[]; allSelected: boolean; toggle: (id: number, checked: boolean) => void;
  selectPage: (checked: boolean) => void; open: (id: number) => void; changed: () => void;
  labels: Record<string, string>; wrap: boolean; duplicates: boolean; locked: boolean;
  sourceLabels?: Record<string, string>;
}) {
  const [edit, setEdit] = useState<{ id: number; key: string; value: string; error: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);
  const check = useRef<HTMLInputElement>(null);
  useEffect(() => () => { if (pending.current) clearTimeout(pending.current); }, []);
  const picked = rows.filter(row => allSelected || selected.includes(row.id)).length;
  useEffect(() => { if (check.current) check.current.indeterminate = picked > 0 && picked < rows.length; },
    [picked, rows.length]);
  function begin(row: TableRow, column: Column) {
    if (pending.current) clearTimeout(pending.current);
    if (locked || saving || !editable[column.key]) return;
    setEdit({ id: row.id, key: column.key, value: valueOf(row, column.key), error: "" });
  }
  async function save() {
    if (!edit || saving) return;
    const value = edit.value.trim();
    if (editable[edit.key] === "display_name" && !value) {
      setEdit({ ...edit, error: "Укажите название" });
      return;
    }
    if (edit.key === "inn" && value && !validInn(value)) {
      setEdit({ ...edit, error: "Проверьте ИНН и контрольную сумму" });
      return;
    }
    if (edit.key === "personal_line" && value.length > 200) {
      setEdit({ ...edit, error: "Не больше 200 символов" });
      return;
    }
    setSaving(true);
    try {
      await api(`${base}/companies/${edit.id}`, { method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [editable[edit.key]]: value || null }) });
      setEdit(null);
      changed();
    } catch (e) { setEdit({ ...edit, error: shortError(e) }); }
    finally { setSaving(false); }
  }
  return <div className={css.scroll} role="region" aria-label="Таблица компаний" tabIndex={0}>
    <table className={`${css.table} ${wrap ? css.wrap : ""}`}>
      <thead><tr>
        <th scope="col"><input ref={check} type="checkbox" aria-label="Выбрать все на странице"
          disabled={locked || !rows.length} checked={rows.length > 0 && picked === rows.length}
          onChange={e => selectPage(e.target.checked)}/></th>
        {columns.map(column => <th scope="col" key={column.key}
          aria-sort={view.sort === column.key ? view.dir === "asc" ? "ascending" : "descending" : "none"}>
          <button type="button" onClick={() => setView({ ...view, page: 1, sort: column.key,
            dir: view.sort === column.key && view.dir === "asc" ? "desc" : "asc" })}>
            {column.label} {view.sort === column.key ? view.dir === "asc" ? "↑" : "↓" : "↕"}
          </button>
          <ColumnFilter column={column} value={view.filters[column.key] || ""} labels={labels} sourceLabels={sourceLabels}
            changed={value => setView({ ...view, page: 1, filters: { ...view.filters, [column.key]: value } })}/>
        </th>)}
      </tr></thead>
      <tbody>{rows.map(row => <tr key={row.id} tabIndex={0}
        className={[allSelected || selected.includes(row.id) ? css.selected : "",
          duplicates && row.duplicate_group ? css.duplicate : ""].join(" ")}
        title={duplicates && row.duplicate_group ? `Возможное совпадение: ${row.duplicate_group}` : undefined}
        onClick={() => {
          if (pending.current) clearTimeout(pending.current);
          pending.current = setTimeout(() => open(row.id), 250);
        }} onKeyDown={e => { if (e.target === e.currentTarget && e.key === "Enter") open(row.id); }}>
        <td onClick={e => e.stopPropagation()}><input type="checkbox" disabled={locked}
          aria-label={`Выбрать ${row.display_name || row.id}`} checked={allSelected || selected.includes(row.id)}
          onChange={e => toggle(row.id, e.target.checked)}/></td>
        {columns.map(column => <td key={column.key} onDoubleClick={e => {
          e.stopPropagation();
          begin(row, column);
        }}>
          {edit?.id === row.id && edit.key === column.key ?
            <form className={css.cellEdit} onClick={e => e.stopPropagation()}
              onDoubleClick={e => e.stopPropagation()} onSubmit={e => { e.preventDefault(); void save(); }}>
              <input autoFocus aria-label={`Изменить: ${column.label}`} value={edit.value} disabled={saving}
                onChange={e => setEdit({ ...edit, value: e.target.value, error: "" })}
                onKeyDown={e => { if (e.key === "Escape" && !saving) setEdit(null); }}/>
              <div><button type="submit" disabled={saving}>Сохранить</button>{" "}
                <button type="button" disabled={saving} onClick={() => setEdit(null)}>Отмена</button></div>
              {edit.error && <span role="alert" className={css.cellError}>{edit.error}</span>}
            </form> : <Cell row={row} column={column} labels={labels}/>}
        </td>)}
      </tr>)}</tbody>
    </table>
    {!rows.length && <p role="status">Компаний по этим условиям нет.</p>}
  </div>;
}
