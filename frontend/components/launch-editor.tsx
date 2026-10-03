"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Launch } from "./launch-panel";
import "./launch.css";

type PlanItem = Launch["plan"][number];
type Row = { id: number; name: string; launch: Launch };

/** Admin → client card: the marketer shown to the client, the work plan with statuses, goals and a weekly note. */
export function LaunchEditor({ workspaceId }: { workspaceId: number }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [plan, setPlan] = useState<PlanItem[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const load = useCallback(() => api<Row[]>(`/portal/admin/projects?workspace_id=${workspaceId}`).then(list => {
    setRows(list); setProjectId(current => current && list.some(r => r.id === current) ? current : list[0]?.id ?? null);
  }).catch(e => setError((e as Error).message)), [workspaceId]);
  useEffect(() => { load(); }, [load]);
  const row = rows.find(r => r.id === projectId);
  useEffect(() => { setPlan(row?.launch.plan || []); }, [row]);
  if (!row) return null;
  const launch = row.launch, m = launch.marketer;
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget); setError(""); setNotice("");
    const text = (k: string) => String(f.get(k) || "").trim() || null;
    const num = (k: string) => f.get(k) ? Number(f.get(k)) : null;
    const name = text("name");
    try {
      await api(`/portal/admin/projects/${row!.id}/launch`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        marketer: name ? { name, role: text("role") || "Ваш маркетолог", telegram: text("telegram"), phone: text("phone"), hours: text("hours"), photo_url: text("photo_url") } : null,
        plan: plan.filter(p => p.title.trim().length >= 2).map(p => ({ id: p.id, title: p.title.trim(), status: p.status, due: p.due || null, note: p.note || null })),
        goals: { leads: num("leads"), cpl: num("cpl"), meetings: num("meetings"), sales: num("sales") }, weekly_note: text("weekly_note") || "" }) });
      setNotice("Сохранено — клиент увидит это на странице «Результат»"); await load();
    } catch (e) { setError((e as Error).message); }
  }
  const update = (i: number, patch: Partial<PlanItem>) => setPlan(list => list.map((p, j) => j === i ? { ...p, ...patch } : p));
  return <section className="panel launchEditor"><div className="panelHead"><div><p className="eyebrow">Онбординг клиента</p><h2>Экран «Запуск»</h2></div>
      {rows.length > 1 && <select value={projectId ?? ""} onChange={e => setProjectId(Number(e.target.value))}>{rows.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}</select>}
      <span className="launchEditorProgress">Чек-лист клиента: {launch.done} из {launch.total}</span></div>
    <form key={row.id} onSubmit={save}>
      <div className="launchEditorGrid">
        <fieldset><legend>Маркетолог проекта</legend>
          <label>Имя<input name="name" defaultValue={m?.name || ""} placeholder="Мария Иванова"/></label>
          <label>Подпись<input name="role" defaultValue={m?.role || "Ваш маркетолог"}/></label>
          <label>Telegram<input name="telegram" defaultValue={m?.telegram || ""} placeholder="@username"/></label>
          <label>Телефон<input name="phone" defaultValue={m?.phone || ""}/></label>
          <label>Часы связи<input name="hours" defaultValue={m?.hours || ""} placeholder="Пн–Пт, 9:00–19:00"/></label>
          <label>Фото (https://…)<input name="photo_url" defaultValue={m?.photo_url || ""}/></label></fieldset>
        <fieldset><legend>Цели на месяц</legend>
          <label>Заявок<input name="leads" type="number" min="0" defaultValue={launch.goals.leads ?? ""}/></label>
          <label>Цена заявки, ₽<input name="cpl" type="number" min="0" defaultValue={launch.goals.cpl ?? ""}/></label>
          <label>Встреч / замеров<input name="meetings" type="number" min="0" defaultValue={launch.goals.meetings ?? ""}/></label>
          <label>Продаж<input name="sales" type="number" min="0" defaultValue={launch.goals.sales ?? ""}/></label>
          <label>Комментарий недели (уходит и в еженедельный отчёт)<textarea name="weekly_note" defaultValue={launch.weekly_note} maxLength={1000}/></label></fieldset>
      </div>
      <fieldset className="launchEditorPlan"><legend>План работ агентства</legend>
        {plan.map((p, i) => <div key={p.id || i} className="launchEditorStep">
          <input value={p.title} onChange={e => update(i, { title: e.target.value })} aria-label="Шаг"/>
          <select value={p.status} onChange={e => update(i, { status: e.target.value as PlanItem["status"] })}>{Object.entries(launch.statuses).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
          <input type="date" value={p.due || ""} onChange={e => update(i, { due: e.target.value || null })} aria-label="Срок"/>
          <input value={p.note || ""} onChange={e => update(i, { note: e.target.value })} placeholder="Комментарий для клиента"/>
          <button type="button" onClick={() => setPlan(list => list.filter((_, j) => j !== i))} aria-label="Удалить шаг">×</button></div>)}
        <button type="button" className="button" onClick={() => setPlan(list => [...list, { id: "", title: "", status: "todo", due: null }])}>＋ Шаг</button></fieldset>
      {error && <p className="notice error">{error}</p>}{notice && <p className="notice">{notice}</p>}
      <button className="button primary">Сохранить</button>
    </form></section>;
}
