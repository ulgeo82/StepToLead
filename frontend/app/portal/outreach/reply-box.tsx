"use client";

/* Ответ лиду из кабинета клиента (право «может отвечать» выдаёт агентство). Пока редактор открыт и в нём есть
   текст — держим «пишу ответ» (lock 60 с, продление каждые 20 с), чтобы два человека не ответили одновременно. */

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Conversation } from "./types";

const base = "/portal/outreach";

export function ReplyBox({ conv, onSent }: { conv: Conversation; onSent: (message: Conversation["messages"][number]) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const held = useRef(false);
  const path = `${base}/inbox/${encodeURIComponent(conv.key)}`;
  const otherWriting = conv.locked_by && !conv.locked_by.me ? conv.locked_by.name : null;

  useEffect(() => {
    if (!text.trim() || otherWriting) return;
    const take = () => api(`${path}/lock`, { method: "POST" }).then(() => { held.current = true; })
      .catch(e => setError(e instanceof Error ? e.message : "Сейчас отвечает другой сотрудник"));
    void take();
    const timer = window.setInterval(() => void take(), 20_000);
    return () => window.clearInterval(timer);
  }, [path, text.trim().length > 0, otherWriting]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { if (held.current) void api(`${path}/lock`, { method: "DELETE" }).catch(() => {}); }, [path]);

  if (!conv.can_reply) return conv.reply_blocker ? <p className="resultError">{conv.reply_blocker}</p> : null;
  if (otherWriting) return <p role="status">Сейчас отвечает {otherWriting}</p>;

  async function send() {
    if (!text.trim() || busy) return;
    setBusy(true); setError("");
    try {
      const message = await api<Conversation["messages"][number]>(`${path}/reply`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: text.trim() }),
      });
      held.current = false; setText(""); onSent(message);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось отправить"); }
    finally { setBusy(false); }
  }

  return <div>
    <textarea rows={4} maxLength={4096} value={text} placeholder={`Ответ с «${conv.sender ?? "того же отправителя"}»`}
      onChange={e => setText(e.target.value)}
      onKeyDown={e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void send(); }}
      style={{ width: "100%", boxSizing: "border-box" }}/>
    {error && <p className="resultError" role="alert">{error}</p>}
    <button className="button primary" disabled={busy || !text.trim()} onClick={() => void send()}>
      {busy ? "Отправляем…" : "Отправить"}
    </button>
  </div>;
}
