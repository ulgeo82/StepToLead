"use client";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { BOT_STEPS } from "@/components/client-telegram-bot";

type Request = { lead_id: number; name: string | null; company: string | null; what: string | null; city: string | null;
  contact: string | null; launch: { answers: Record<string, string>; at: string }; converted_workspace_id: number | null; demo_workspace_id: number | null };
type Result = { workspace_id: number; ad_connections?: number; pipelines: number; brief_answers: number; demo_deleted: boolean; bot: string | null };

// «Хотят свой портал» — заявки из демо. Одна кнопка создаёт новый чистый кабинет и удаляет демо.
export function LaunchRequests({ onCreated }: { onCreated: () => void }) {
  const [rows, setRows] = useState<Request[]>([]);
  const [open, setOpen] = useState<Request | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(() => api<Request[]>("/admin/launch-requests").then(setRows).catch(() => setRows([])), []);
  useEffect(() => { void load(); }, [load]);
  const waiting = rows.filter(r => !r.converted_workspace_id);
  if (!waiting.length && !result) return null;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!open) return; setBusy(true); setError("");
    const f = new FormData(event.currentTarget);
    try {
      setResult(await api<Result>(`/admin/launch-requests/${open.lead_id}/convert`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company_name: f.get("company_name"), bot_token: f.get("bot_token") || null, delete_demo: f.get("delete_demo") === "on" }) }));
      setOpen(null); await load(); onCreated();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось создать кабинет"); } finally { setBusy(false); }
  }
  return <section className="panel launchRequests"><div className="panelHead"><div><p className="eyebrow">Из демо-портала</p><h2>Хотят свой портал · {waiting.length}</h2></div></div>
    {waiting.map(r => { const a = r.launch.answers; return <div className="launchRow" key={r.lead_id}>
      <div><strong>{r.company || r.what || "Без названия"}</strong> <span>{r.name} · {a.contact || r.contact}{r.city ? ` · ${r.city}` : ""}</span>
        <small>Направления: {a.directions?.split("\n").filter(Boolean).join(", ")} · бюджет {a.budget} · старт {a.start} · созвон {a.call} · заявки в: {a.crm} · команда {a.team}</small></div>
      <button className="button primary" onClick={() => { setError(""); setOpen(r); }}>Создать кабинет</button></div>; })}
    {result && <div className="credentialBox launchResult"><span>Кабинет создан</span><strong>Воронок: {result.pipelines} · ответов в брифе: {result.brief_answers}{result.ad_connections ? ` · рекламных кабинетов перенесено: ${result.ad_connections}` : ""}{result.demo_deleted ? " · демо удалено" : ""}{result.bot ? ` · бот: ${result.bot}` : ""}</strong>
      <span>Доступы клиенту выдаёте вы, когда решите</span><strong><a href={`/admin/clients/${result.workspace_id}`}>Открыть карточку клиента →</a></strong>
      <button className="button secondary" onClick={() => setResult(null)}>Скрыть</button></div>}
    {open && <div className="modalBackdrop"><form className="modal" onSubmit={submit}><div className="modalHead"><div><p className="eyebrow">Новый клиент из демо</p><h2>Создать кабинет</h2></div><button type="button" className="close" onClick={() => setOpen(null)}>×</button></div>
      <p className="formHint">Создадим новый чистый кабинет: бриф с перенесёнными ответами, воронки по направлениям, база знаний ИИ. Демо-данные не переносятся. Пользователей не создаём — доступы выдаёте вы в карточке клиента.</p>
      <label>Название компании<input name="company_name" required minLength={2} defaultValue={open.company || ""} autoFocus/></label>
      <label>Токен Telegram-бота компании (необязательно)<input name="bot_token" autoComplete="off" placeholder="1234567890:AAH…"/></label><p className="formHint">{BOT_STEPS}</p>
      <label className="launchCheck"><input type="checkbox" name="delete_demo" defaultChecked/> Удалить демо-кабинет после создания</label>
      {error && <p className="notice error">{error}</p>}
      <button className="button primary full" disabled={busy}>{busy ? "Создаём…" : "Создать кабинет"}</button></form></div>}
  </section>;
}
