"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { GrowthInputs, GrowthResult, money, statusCopy } from "@/lib/growth";

type Entry = { id: number; name: string; email: string | null; phone: string | null; telegram: string | null; status: GrowthResult["status"]; created_at: string; inputs: GrowthInputs; results: GrowthResult };
export default function GrowthLeads() {
  const [items, setItems] = useState<Entry[]>([]);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => { let active = true; setLoading(true); api<Entry[]>(`/admin/growth/leads?offset=${offset}`).then(v => { if (active) { setItems(v); setError(""); } }).catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); }); return () => { active = false; }; }, [offset]);
  return <div className="page"><header className="pageHeader"><div><p className="eyebrow">Публичный калькулятор</p><h1>Заявки роста</h1><p className="subtitle">Отдельно от базы Telegram. В рассылки автоматически не попадают.</p></div></header>{error && <p className="notice error" role="alert">{error}</p>}{loading ? <p>Загрузка…</p> : <div style={{ display: "grid", gap: 18 }}>{items.length === 0 && <div className="empty"><h3>Заявок пока нет</h3><p>Здесь появятся контакты и расчёты, сохранённые на /growth.</p></div>}{items.map(item => <article className="panel" key={item.id} style={{ padding: 24, overflowWrap: "anywhere" }}><p className="eyebrow">#{item.id} · {new Date(item.created_at).toLocaleString("ru-RU")}</p><h2>{item.name}</h2><p>{[item.phone, item.telegram, item.email].filter(Boolean).join(" · ")}</p><p>{statusCopy[item.status].title}</p><p>Текущая прибыль: <strong>{money(item.results.metrics.currentOperatingProfit)}</strong> · После вложения: <strong>{money(item.results.metrics.profitAfterInvestment)}</strong></p><details><summary style={{ cursor: "pointer", padding: "12px 0" }}>Все исходные данные и результаты</summary><pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify({ inputs: item.inputs, results: item.results }, null, 2)}</pre></details></article>)}</div>}<div style={{ display: "flex", gap: 12, marginTop: 24 }}><button className="button" disabled={offset === 0 || loading} onClick={() => setOffset(n => Math.max(0, n - 50))}>← Назад</button><button className="button" disabled={items.length < 50 || loading} onClick={() => setOffset(n => n + 50)}>Далее →</button></div></div>;
}
