"use client";

import styles from "../leadgen.module.css";
import type { Field, Values } from "./source-types";
import { AiQueryPicker } from "../ai-query-picker";
import { mergeQueries, queryLines } from "../ai-query-utils";

export function SourceFields({ schema, values, saved = {}, prefix, changed }: {
  schema: Field[]; values: Values; saved?: Values; prefix: string; changed: (value: Values) => void;
}) {
  return <div className={styles.sourceFields}>{schema.map(field => {
    const secret = field.type === "secret";
    const value = secret ? (typeof values[field.key] === "string" ? values[field.key] : "")
      : values[field.key] ?? saved[field.key];
    const text = Array.isArray(value) ? value.join("\n") : String(value ?? "");
    const set = (next: unknown) => changed({ ...values, [field.key]: next });
    const id = `${prefix}-${field.key}`;
    const configured = secret && typeof saved[field.key] === "object" &&
      (saved[field.key] as { set?: boolean } | null)?.set === true;
    if (field.type === "multi") {
      const selected = Array.isArray(value) ? value : [];
      return <fieldset key={field.key} className={styles.multiOptions}>
        <legend>{field.label}{field.required ? " *" : ""}</legend>
        {field.options?.map(option => <label key={String(option.value)} className={styles.check}>
          <input type="checkbox" checked={selected.includes(option.value)} onChange={e =>
            set(e.target.checked ? [...selected, option.value] : selected.filter(item => item !== option.value))}/>
          <span>{option.label}</span></label>)}
        {field.hint && <small className={styles.hint}>{field.hint}</small>}
      </fieldset>;
    }
    if (field.type === "bool") return <label key={field.key} className={styles.check}>
      <input type="checkbox" checked={!!value} onChange={e => set(e.target.checked)}/>
      <span>{field.label}{field.hint && <small className={styles.hint}>{field.hint}</small>}</span>
    </label>;
    return <div key={field.key} className={styles.field}>
      <label htmlFor={id}>{field.label}{field.required ? " *" : ""}</label>
      {secret && <small>{configured ? "Ключ сохранён" : "Ключ не задан"}</small>}
      {field.type === "list" ? <textarea id={id} rows={3} required={field.required} value={text}
        onChange={e => set(e.target.value)}/> : field.type === "select" ? <select id={id} value={text}
        required={field.required} onChange={e => {
          const option = field.options?.find(item => String(item.value) === e.target.value);
          set(option?.value ?? "");
        }}>
        <option value="">Выберите значение</option>
        {field.options?.map(option => <option key={String(option.value)} value={String(option.value)}>
          {option.label}</option>)}
      </select> : <input id={id} type={secret ? "password" : field.type === "number" ? "number" : "text"}
        required={field.required && !configured} value={text} min={field.min} max={field.max}
        autoComplete={secret ? "new-password" : "off"} spellCheck={!secret}
        onChange={e => set(e.target.value)}/>}
      {field.type === "list" && <small>Одно значение на строку.</small>}
      {field.type === "list" && field.ai && <AiQueryPicker purpose={field.ai.purpose}
        added={queries => set(mergeQueries(queryLines(value), queries).join("\n"))}/>}
      {field.hint && <small>{field.hint}</small>}
      {secret && configured && <button type="button" className={styles.secondary} onClick={() => set("")}>
        Удалить ключ при сохранении</button>}
      {secret && configured && field.key in values && value === "" && <small>
        Ключ будет удалён при сохранении. <button type="button" className={styles.secondary} onClick={() => {
          const next = { ...values };
          delete next[field.key];
          changed(next);
        }}>Отменить удаление</button></small>}
    </div>;
  })}</div>;
}
