"use client";

// Форматирование писем: та же разметка, что и на сервере (core/sequence.py: to_html):
// **жирный**, *курсив*, __подчёркнутый__, [текст](https://…), строки «- …» / «1. …» — списки.
import styles from "../../leadgen/leadgen.module.css";

const LINK = /\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:|tel:)[^\s)]+)\)/g;
const BOLD = /\*\*(?=\S)(.+?)(?<=\S)\*\*/g;
const UNDER = /__(?=\S)(.+?)(?<=\S)__/g;
const ITALIC = /(?<![*\p{L}\p{N}_])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\p{L}\p{N}_])/gu;
const BULLET = /^\s*[-•]\s+(.*)$/;
const NUMBER = /^\s*\d{1,2}[.)]\s+(.*)$/;

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export function hasMarkup(text: string): boolean {
  return [LINK, BOLD, UNDER, ITALIC].some((re) => { re.lastIndex = 0; const ok = re.test(text || ""); re.lastIndex = 0; return ok; });
}

function inline(line: string): string {
  let out = "", last = 0;
  for (const m of line.matchAll(LINK)) {
    out += esc(line.slice(last, m.index)) + `<a href="${esc(m[2])}" target="_blank" rel="noopener noreferrer">${esc(m[1])}</a>`;
    last = (m.index || 0) + m[0].length;
  }
  out += esc(line.slice(last));
  return out.replace(BOLD, "<b>$1</b>").replace(UNDER, "<u>$1</u>").replace(ITALIC, "<i>$1</i>");
}

export function toHtml(text: string): string {
  const blocks: string[] = [];
  let para: string[] = [];
  let list: { tag: "ul" | "ol"; items: string[] } | null = null;
  const flushPara = () => { if (para.length) { blocks.push(`<p>${para.join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list) { blocks.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join("")}</${list.tag}>`); list = null; } };
  for (const raw of (text || "").split("\n")) {
    const b = raw.match(BULLET), n = raw.match(NUMBER);
    if (b || n) {
      flushPara();
      const tag = b ? "ul" : "ol";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push(inline((b || n)![1]));
    } else if (!raw.trim()) { flushPara(); flushList(); }
    else { flushList(); para.push(inline(raw)); }
  }
  flushPara(); flushList();
  return blocks.join("");
}

/** Текст письма с форматированием — безопасно (всё экранировано, кроме наших тегов). */
export function Formatted({ text, className }: { text: string; className?: string }) {
  return <div className={`${styles.formatted} ${className || ""}`} dangerouslySetInnerHTML={{ __html: toHtml(text) }} />;
}

type Wrap = { before: string; after: string; placeholder: string } | { line: string };
export const TOOLS: { key: string; label: string; title: string; wrap: Wrap }[] = [
  { key: "bold", label: "Ж", title: "Жирный (**текст**)", wrap: { before: "**", after: "**", placeholder: "жирный текст" } },
  { key: "italic", label: "К", title: "Курсив (*текст*)", wrap: { before: "*", after: "*", placeholder: "курсив" } },
  { key: "under", label: "Ч", title: "Подчёркнутый (__текст__)", wrap: { before: "__", after: "__", placeholder: "подчёркнутый" } },
  { key: "link", label: "🔗", title: "Ссылка [текст](https://…)", wrap: { before: "[", after: "](https://)", placeholder: "текст ссылки" } },
  { key: "ul", label: "• Список", title: "Маркированный список", wrap: { line: "- " } },
  { key: "ol", label: "1. Список", title: "Нумерованный список", wrap: { line: "1. " } },
];

/** Применить инструмент к тексту с выделением [start, end). Возвращает новый текст и позицию курсора. */
export function applyTool(text: string, start: number, end: number, key: string): { text: string; start: number; end: number } {
  const tool = TOOLS.find((t) => t.key === key)!;
  if ("line" in tool.wrap) {
    const lineStart = text.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
    const block = text.slice(lineStart, end || start);
    const lines = (block || "").split("\n");
    const prefixed = lines.map((l, i) => (tool.key === "ol" ? `${i + 1}. ` : tool.wrap && "line" in tool.wrap ? tool.wrap.line : "") + l.replace(/^\s*([-•]|\d+[.)])\s+/, "")).join("\n");
    const next = text.slice(0, lineStart) + prefixed + text.slice(end || start);
    return { text: next, start: lineStart, end: lineStart + prefixed.length };
  }
  const { before, after, placeholder } = tool.wrap;
  const selected = text.slice(start, end) || placeholder;
  const next = text.slice(0, start) + before + selected + after + text.slice(end);
  if (tool.key === "link") {   // курсор — на «https://», чтобы сразу вставить адрес
    const pos = start + before.length + selected.length + 2;
    return { text: next, start: pos, end: pos + "https://".length };
  }
  return { text: next, start: start + before.length, end: start + before.length + selected.length };
}
