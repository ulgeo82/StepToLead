"use client";

/* Ответ лиду из кабинета клиента (право «может отвечать» выдаёт агентство). Пока редактор открыт и в нём есть
   текст — держим «пишу ответ» (lock 60 с, продление каждые 20 с), чтобы два человека не ответили одновременно. */

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Conversation } from "./types";
import { RichComposer } from "@/components/rich-composer";

const base = "/portal/outreach";

export function ReplyBox({ conv, onSent }: { conv: Conversation; onSent: (message: Conversation["messages"][number]) => void }) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
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
    if ((!text.trim() && !file) || busy) return;
    setBusy(true); setError("");
    try {
      const form = new FormData();
      form.set("text", text.trim());
      if (file) form.set("file", file);
      const message = await api<Conversation["messages"][number]>(`${path}/reply-rich`, { method: "POST", body: form });
      held.current = false; setText(""); setFile(null); onSent(message);
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось отправить"); }
    finally { setBusy(false); }
  }

  const tg = conv.channel === "telegram";
  return <div>
    <RichComposer value={text} onChange={setText} onSubmit={() => void send()} maxLength={4096}
      placeholder={`Ответ с «${conv.sender ?? "того же отправителя"}»`} formatting={tg}
      file={file} onFile={tg ? setFile : undefined} busy={busy} canSubmit={!!text.trim() || !!file} submitLabel="Отправить"/>
    {error && <p className="resultError" role="alert">{error}</p>}
  </div>;
}
