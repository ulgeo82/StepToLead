"use client";

import { FormEvent, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "@/lib/api";
import { base } from "./shared";
import styles from "./leadgen.module.css";
import { aiQueryBody, mergeQueries, QueryPurpose, QuerySuggestions } from "./ai-query-utils";
import { shortError } from "./sources/source-types";

export function AiQueryPicker({ purpose, added }: {
  purpose: QueryPurpose; added: (queries: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  return <>
    <button ref={button} type="button" className={styles.secondary} onClick={() => setOpen(true)}>
      ✨ Подобрать ИИ</button>
    {open && createPortal(<QueryDialog purpose={purpose} close={() => {
      setOpen(false); button.current?.focus();
    }} added={added}/>, document.body)}
  </>;
}

function QueryDialog({ purpose, close, added }: {
  purpose: QueryPurpose; close: () => void; added: (queries: string[]) => void;
}) {
  const id = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const request = useRef<AbortController | null>(null);
  const closeRef = useRef(close);
  closeRef.current = close;
  const [niche, setNiche] = useState("");
  const [region, setRegion] = useState("");
  const [brief, setBrief] = useState("");
  const [mode, setMode] = useState<"text" | "pdf">("text");
  const [file, setFile] = useState<File | null>(null);
  const [suggestions, setSuggestions] = useState<QuerySuggestions | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const all = !!suggestions?.queries.length && selected.length === suggestions.queries.length;

  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.current?.querySelector<HTMLInputElement>("input")?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); closeRef.current(); }
      if (event.key !== "Tab") return;
      const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]',
      ) || []).filter(node => node.getClientRects().length);
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keyboard);
    return () => {
      request.current?.abort();
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", keyboard);
    };
  }, []);

  function reset() { setSuggestions(null); setSelected([]); setError(""); }
  async function generate(event: FormEvent) {
    event.preventDefault();
    event.stopPropagation(); // A portal still bubbles React events into the source's launch form.
    if (busy) return;
    if (!niche.trim()) { setError("Укажите нишу"); return; }
    if (mode === "pdf" && (!file || !/\.pdf$/i.test(file.name))) {
      setError("Выберите файл PDF"); return;
    }
    if (mode === "pdf" && file && file.size > 10 * 1024 * 1024) {
      setError("Размер PDF — не больше 10 МБ"); return;
    }
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    reset();
    try {
      const body = aiQueryBody(purpose, niche, mode === "text" ? brief : "", region);
      let value: QuerySuggestions;
      if (mode === "pdf" && file) {
        const form = new FormData();
        form.append("file", file);
        Object.entries(body).forEach(([key, text]) => form.append(key, text));
        value = await api<QuerySuggestions>(`${base}/ai/queries/pdf`, {
          method: "POST", body: form, signal: controller.signal, timeoutMs: 90_000,
        });
      } else value = await api<QuerySuggestions>(`${base}/ai/queries`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        signal: controller.signal, timeoutMs: 90_000,
      });
      if (!controller.signal.aborted) {
        const queries = mergeQueries([], value.queries);
        setSuggestions({ ...value, queries });
        setSelected(queries);
      }
    } catch (e) { if (!controller.signal.aborted) setError(shortError(e)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  return <div className={`${styles.page} ${styles.aiQueryOverlay}`} onClick={event => {
    if (event.target === event.currentTarget) close();
  }}>
    <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={`${id}-title`}
      className={`${styles.card} ${styles.aiQueryDialog}`}>
      <div className={styles.sourceCardHead}><h2 id={`${id}-title`}>Подобрать запросы с ИИ</h2>
        <button type="button" className={styles.close} aria-label="Закрыть подбор ИИ" onClick={close}>×</button></div>
      <form onSubmit={generate}>
        <fieldset disabled={busy} className={styles.formFieldset}>
          <label className={styles.field}>Ниша *<input required value={niche}
            onChange={e => { setNiche(e.target.value); reset(); }}/></label>
          <label className={styles.field}>Город или регион (необязательно)<input value={region}
            onChange={e => { setRegion(e.target.value); reset(); }}/></label>
          <label className={styles.field}>Бриф<select value={mode} onChange={e => {
            setMode(e.target.value as "text" | "pdf"); reset();
          }}><option value="text">Текстом (необязательно)</option><option value="pdf">Загрузить PDF</option></select></label>
          {mode === "text" ? <label className={styles.field}>Текст брифа<textarea rows={4} value={brief}
            onChange={e => { setBrief(e.target.value); reset(); }}/></label>
            : <label className={styles.field}>PDF до 10 МБ<input type="file" accept=".pdf,application/pdf" required
              onChange={e => { setFile(e.target.files?.[0] || null); reset(); }}/></label>}
          <p className={styles.muted}>Подбор использует резерв ИИ агентства.</p>
          <button type="submit" className={styles.primary} disabled={!niche.trim()}>
            {busy ? "Подбираем…" : "Подобрать запросы"}</button>
        </fieldset>
      </form>
      {busy && <p role="status">ИИ подбирает предложения…</p>}
      {error && <p role="alert" className={styles.error}>{error}</p>}
      {suggestions && <section className={styles.aiSuggestions}>
        {suggestions.note && <p className={styles.muted}>{shortError(new Error(suggestions.note))}</p>}
        {!suggestions.queries.length ? <p role="status">Предложений нет. Уточните нишу или бриф.</p> : <>
          <label className={styles.check}><input type="checkbox" checked={all} onChange={e =>
            setSelected(e.target.checked ? suggestions.queries : [])}/>Выбрать все</label>
          <div className={styles.aiSuggestionList}>{suggestions.queries.map(query => <label key={query}
            className={styles.check}><input type="checkbox" checked={selected.includes(query)}
              onChange={e => setSelected(e.target.checked ? [...selected, query] : selected.filter(q => q !== query))}/>
            <span>{query}</span></label>)}</div>
          <button type="button" className={styles.primary} disabled={!selected.length}
            onClick={() => { added(selected); close(); }}>Добавить выбранные ({selected.length})</button>
        </>}
      </section>}
    </div>
  </div>;
}
