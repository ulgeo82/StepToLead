"use client";

import { FormEvent, useState } from "react";
import { duePresets, localInput, request, Task, Team, typeLabels } from "./shared";

const quickResults = ["Не дозвонился", "Договорились о встрече", "Отправил КП", "Клиент думает", "Перезвонить позже", "Клиент отказался"];

/** Complete a task with a result and (by default) schedule the next step — no deal is left without a task. */
export function CompleteTaskModal({ task, team, onClose, onDone }: { task: Task; team: Team[];
  onClose: () => void; onDone: () => void }) {
  const [result, setResult] = useState("");
  const [next, setNext] = useState(true);
  const [due, setDue] = useState(duePresets()[2].value);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const f = new FormData(event.currentTarget);
    try {
      await request(`/crm/tasks/${task.id}/complete`, "POST", { result, next_task: next ? {
        type_code: f.get("type_code"), title: f.get("title"), due_at: new Date(due).toISOString(),
        responsible_user_id: Number(f.get("owner")) || null } : null });
      onDone();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <form className="resultModal crmModal" onSubmit={submit}><header><h2>Завершить задачу</h2><button type="button" onClick={onClose}>×</button></header>
      <p className="crmModalLead">{typeLabels[task.type_code] || task.type_code}: <b>{task.title}</b></p>
      <label>Результат<textarea required value={result} onChange={e => setResult(e.target.value)} placeholder="Что получилось? Это увидит вся команда в истории сделки"/></label>
      <div className="crmChips">{quickResults.map(text => <button type="button" key={text} onClick={() => setResult(r => r ? `${r}. ${text}` : text)}>{text}</button>)}</div>
      <label className="crmCheck"><input type="checkbox" checked={next} onChange={e => setNext(e.target.checked)}/> Сразу поставить следующую задачу <small>— у сделки всегда должен быть следующий шаг</small></label>
      {next && <div className="crmNextTask"><div className="resultModalGrid"><label>Тип<select name="type_code" defaultValue="CALL">{Object.entries(typeLabels).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label>Ответственный<select name="owner" defaultValue={task.responsible_user_id || ""}><option value="">Как в задаче</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label></div>
        <label>Что сделать<input name="title" required minLength={2} defaultValue="Связаться с клиентом"/></label>
        <label>Когда<input type="datetime-local" required value={due} onChange={e => setDue(e.target.value)}/></label>
        <div className="crmChips">{duePresets().map(p => <button type="button" key={p.label} className={p.value === due ? "active" : ""} onClick={() => setDue(p.value)}>{p.label}</button>)}</div></div>}
      {error && <p className="crmFormError">{error}</p>}
      <button className="resultPrimary" disabled={busy || !result.trim()}>{busy ? "Сохраняем…" : next ? "Завершить и запланировать" : "Завершить"}</button></form></div>;
}

/** New task for a deal (or a contact) with quick due presets. */
export function NewTaskModal({ projectId, dealId, contactId, team, deals, onClose, onDone }: { projectId: number;
  dealId?: number | null; contactId?: number | null; team: Team[]; deals?: { id: number; name: string }[];
  onClose: () => void; onDone: () => void }) {
  const [due, setDue] = useState(localInput(new Date(Date.now() + 3600000)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const f = new FormData(event.currentTarget);
    try {
      await request("/crm/tasks", "POST", { project_id: projectId, deal_id: dealId || Number(f.get("deal_id")) || null,
        contact_id: dealId ? null : contactId || null, type_code: f.get("type_code"), title: f.get("title"),
        due_at: new Date(due).toISOString(), responsible_user_id: Number(f.get("owner")) || null,
        priority: f.get("priority"), description: f.get("description") || null,
        duration_minutes: f.get("duration_minutes") ? Number(f.get("duration_minutes")) : null });
      onDone();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <form className="resultModal crmModal" onSubmit={submit}><header><h2>Новая задача</h2><button type="button" onClick={onClose}>×</button></header>
      {!dealId && !contactId && <label>Сделка<select name="deal_id" required><option value="">Выберите сделку</option>{(deals || []).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>}
      <div className="resultModalGrid"><label>Тип<select name="type_code" defaultValue="CALL">{Object.entries(typeLabels).map(([k, v]) => <option value={k} key={k}>{v}</option>)}</select></label>
        <label>Приоритет<select name="priority" defaultValue="NORMAL"><option value="HIGH">Высокий</option><option value="NORMAL">Обычный</option><option value="LOW">Низкий</option></select></label></div>
      <label>Что сделать<input name="title" required minLength={2} placeholder="Например, перезвонить и уточнить бюджет"/></label>
      <label>Когда<input type="datetime-local" required value={due} onChange={e => setDue(e.target.value)}/></label>
      <div className="crmChips">{duePresets().map(p => <button type="button" key={p.label} className={p.value === due ? "active" : ""} onClick={() => setDue(p.value)}>{p.label}</button>)}</div>
      <div className="resultModalGrid"><label>Ответственный<select name="owner"><option value="">Ответственный сделки</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
        <label>Длительность, мин<input name="duration_minutes" type="number" min="1"/></label></div>
      <label>Описание<textarea name="description"/></label>
      {error && <p className="crmFormError">{error}</p>}
      <button className="resultPrimary" disabled={busy}>{busy ? "Сохраняем…" : "Поставить задачу"}</button></form></div>;
}
