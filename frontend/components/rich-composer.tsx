"use client";

/* Поле ответа лиду: панель разметки Telegram, вложение и «развернуть на весь экран».
   Разметка хранится маркерами в тексте (**ж**, __к__, ++ч++, ~~з~~, ||спойлер||, `код`, ```блок```, > цитата,
   [текст](url)) — сервер (services/tg_format.py) превращает их в форматирование Telegram, в письмо уходит чистый текст. */

import { useEffect, useRef, useState, type KeyboardEvent, type Ref } from "react";
import css from "./rich-composer.module.css";

type Wrap = { label: string; title: string; before: string; after?: string; line?: boolean; style?: React.CSSProperties };

const TOOLS: Wrap[] = [
  { label: "Ж", title: "Жирный — Ctrl+B", before: "**", style: { fontWeight: 700 } },
  { label: "К", title: "Курсив — Ctrl+I", before: "__", style: { fontStyle: "italic" } },
  { label: "Ч", title: "Подчёркнутый — Ctrl+U", before: "++", style: { textDecoration: "underline" } },
  { label: "З", title: "Зачёркнутый", before: "~~", style: { textDecoration: "line-through" } },
  { label: "Спойлер", title: "Скрытый текст", before: "||" },
  { label: "</>", title: "Код в строке", before: "`" },
  { label: "{ }", title: "Блок кода", before: "```\n", after: "\n```" },
  { label: "❝", title: "Цитата", before: "> ", line: true },
];

const MAX_FILE = 20 * 1024 * 1024;

export function RichComposer({ value, onChange, onSubmit, inputRef, placeholder, readOnly, maxLength, file, onFile,
  formatting = true, submitLabel, busy, canSubmit, extra }: {
  value: string; onChange: (v: string) => void; onSubmit: () => void; inputRef?: Ref<HTMLTextAreaElement>;
  placeholder?: string; readOnly?: boolean; maxLength?: number;
  file: File | null; onFile?: (f: File | null) => void; formatting?: boolean;
  submitLabel: string; busy?: boolean; canSubmit: boolean; extra?: React.ReactNode;
}) {
  const [full, setFull] = useState(false);
  const [error, setError] = useState("");
  const local = useRef<HTMLTextAreaElement | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!full) return;
    const esc = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") setFull(false); };
    document.addEventListener("keydown", esc);
    document.body.style.overflow = "hidden";
    local.current?.focus();
    return () => { document.removeEventListener("keydown", esc); document.body.style.overflow = ""; };
  }, [full]);

  function setRef(el: HTMLTextAreaElement | null) {
    local.current = el;
    if (typeof inputRef === "function") inputRef(el);
    else if (inputRef) (inputRef as React.MutableRefObject<HTMLTextAreaElement | null>).current = el;
  }

  function apply(next: string, start: number, end: number) {
    onChange(next);
    requestAnimationFrame(() => { local.current?.focus(); local.current?.setSelectionRange(start, end); });
  }

  function wrap(tool: Wrap) {
    const el = local.current;
    if (!el || readOnly) return;
    const { selectionStart: a, selectionEnd: b } = el;
    if (tool.line) {
      const from = value.lastIndexOf("\n", a - 1) + 1;
      const block = value.slice(from, b);
      const quoted = block.split("\n").map(l => tool.before + l).join("\n");
      apply(value.slice(0, from) + quoted + value.slice(b), from, from + quoted.length);
      return;
    }
    const after = tool.after ?? tool.before;
    const picked = value.slice(a, b);
    apply(value.slice(0, a) + tool.before + picked + after + value.slice(b), a + tool.before.length, a + tool.before.length + picked.length);
  }

  function link() {
    const el = local.current;
    if (!el || readOnly) return;
    const url = window.prompt("Ссылка (https://…)", "https://");
    if (!url || !/^https?:\/\/\S+$/i.test(url.trim())) return;
    const { selectionStart: a, selectionEnd: b } = el;
    const label = value.slice(a, b) || "текст ссылки";
    const piece = `[${label}](${url.trim()})`;
    apply(value.slice(0, a) + piece + value.slice(b), a + 1, a + 1 + label.length);
  }

  function keys(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (canSubmit) onSubmit(); return; }
    if (!formatting || !(e.metaKey || e.ctrlKey)) return;
    const hot = { b: 0, i: 1, u: 2 }[e.key.toLowerCase() as "b" | "i" | "u"];
    if (hot !== undefined) { e.preventDefault(); wrap(TOOLS[hot]); }
  }

  function pick(f: File | undefined) {
    setError("");
    if (!f || !onFile) return;
    if (f.size > MAX_FILE) { setError("Файл больше 20 МБ"); return; }
    onFile(f);
  }

  return <div className={full ? css.full : css.inline}>
    {full && <div className={css.head}>
      <strong>Ответ</strong>
      <button type="button" className={css.tool} onClick={() => setFull(false)} title="Свернуть — Esc">Свернуть</button>
    </div>}
    <div className={css.bar} role="toolbar" aria-label="Форматирование">
      {formatting && <>
        {TOOLS.map(t => <button key={t.title} type="button" className={css.tool} title={t.title} aria-label={t.title}
          style={t.style} disabled={readOnly} onMouseDown={e => e.preventDefault()} onClick={() => wrap(t)}>{t.label}</button>)}
        <button type="button" className={css.tool} title="Ссылка" aria-label="Ссылка" disabled={readOnly}
          onMouseDown={e => e.preventDefault()} onClick={link}>🔗</button>
      </>}
      {onFile && <>
        <button type="button" className={css.tool} title="Прикрепить файл (до 20 МБ)" disabled={readOnly}
          onClick={() => fileInput.current?.click()}>📎 Файл</button>
        <input ref={fileInput} type="file" hidden onChange={e => { pick(e.target.files?.[0]); e.target.value = ""; }}/>
      </>}
      <span className={css.grow}/>
      {!full && <button type="button" className={css.tool} title="Развернуть на весь экран" onClick={() => setFull(true)}>⤢ Развернуть</button>}
    </div>
    <textarea ref={setRef} className={css.area} aria-label="Ответ" rows={full ? 20 : 5} value={value} readOnly={readOnly}
      maxLength={maxLength} placeholder={placeholder} onChange={e => onChange(e.target.value)} onKeyDown={keys}
      onDragOver={e => { if (onFile) e.preventDefault(); }} onDrop={e => { if (!onFile) return; e.preventDefault(); pick(e.dataTransfer.files[0]); }}
      onPaste={e => { const f = e.clipboardData.files[0]; if (f && onFile) { e.preventDefault(); pick(f); } }}/>
    {file && <div className={css.file}>
      📎 {file.name} <span className={css.muted}>{(file.size / 1024 / 1024).toFixed(1)} МБ</span>
      <button type="button" className={css.tool} onClick={() => onFile?.(null)} aria-label="Убрать файл">✕</button>
    </div>}
    {error && <p role="alert" className={css.error}>{error}</p>}
    <div className={css.foot}>
      {extra}
      <span className={css.grow}/>
      {maxLength && <span className={css.muted}>{value.length}/{maxLength}</span>}
      <button type="button" className={css.send} disabled={!canSubmit || busy} onClick={onSubmit}>
        {busy ? "Отправляем…" : submitLabel}
      </button>
    </div>
  </div>;
}
