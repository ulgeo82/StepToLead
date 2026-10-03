"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import "./launch.css";

type Item = { key: string; label: string; done: boolean; agency: boolean; href: string };
type PlanItem = { id: string; title: string; status: "todo" | "doing" | "done"; due: string | null; note?: string | null };
type Marketer = { name: string; role?: string | null; telegram?: string | null; phone?: string | null; hours?: string | null; photo_url?: string | null };
type Goals = { leads?: number; cpl?: number; meetings?: number; sales?: number };
export type Launch = { project_id: number; checklist: Item[]; done: number; total: number; complete: boolean; dismissed: boolean;
  marketer: Marketer | null; plan: PlanItem[]; statuses: Record<string, string>; goals: Goals;
  fact: { leads: number | null; cpl: number | null; meetings: number | null; sales: number | null; spend: number | null;
    target_share: number | null; month: string }; weekly_note: string; can_edit_goals: boolean };

const rub = (n: number | null | undefined) => n == null ? "—" : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(n)} ₽`;
const json = (method: string, body?: unknown) => ({ method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
const tgLink = (value: string) => `https://t.me/${value.replace(/^@/, "").replace(/^https?:\/\/t\.me\//, "")}`;

function GoalBar({ label, goal, fact, money, lowerIsBetter }: { label: string; goal?: number; fact: number | null; money?: boolean; lowerIsBetter?: boolean }) {
  if (!goal) return null;
  const share = fact == null ? 0 : Math.min(100, (lowerIsBetter ? goal / Math.max(fact, 1) : fact / goal) * 100);
  const ok = fact != null && (lowerIsBetter ? fact <= goal : fact >= goal);
  return <div className="launchGoal"><div><span>{label}</span><b>{money ? rub(fact) : fact ?? 0}<small> / {money ? rub(goal) : goal}</small></b></div>
    <i><em className={ok ? "ok" : ""} style={{ width: `${share}%` }}/></i></div>;
}

/** «Запуск» on the result page: what is set up, what the agency is doing, who the marketer is, goals of the month. */
export function LaunchPanel({ projectId }: { projectId: number | null }) {
  const [data, setData] = useState<Launch | null>(null);
  const [editGoals, setEditGoals] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(() => projectId ? api<Launch>(`/crm/projects/${projectId}/launch`).then(setData).catch(() => setData(null)) : Promise.resolve(), [projectId]);
  useEffect(() => { setData(null); load(); }, [load]);
  if (!data) return null;
  const hasGoals = Object.values(data.goals || {}).some(Boolean);
  const month = new Date(data.fact.month).toLocaleDateString("ru-RU", { month: "long" });
  async function saveGoals(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget);
    const value = (k: string) => f.get(k) ? Number(f.get(k)) : null;
    try { setData(await api<Launch>(`/crm/projects/${projectId}/launch/goals`, json("PUT", { leads: value("leads"), cpl: value("cpl"), meetings: value("meetings"), sales: value("sales") }))); setEditGoals(false); }
    catch (e) { setError((e as Error).message); }
  }
  const goals = <div className="launchCard launchGoals"><div className="launchCardHead"><h3>Цели на {month}</h3>
      {data.can_edit_goals && <button className="launchLink" onClick={() => setEditGoals(v => !v)}>{editGoals ? "Отмена" : hasGoals ? "Изменить" : "Задать цели"}</button>}</div>
    {editGoals ? <form className="launchGoalForm" onSubmit={saveGoals}>
        <label>Заявок<input name="leads" type="number" min="0" defaultValue={data.goals.leads ?? ""}/></label>
        <label>Цена заявки, ₽<input name="cpl" type="number" min="0" defaultValue={data.goals.cpl ?? ""}/></label>
        <label>Встреч / замеров<input name="meetings" type="number" min="0" defaultValue={data.goals.meetings ?? ""}/></label>
        <label>Продаж<input name="sales" type="number" min="0" defaultValue={data.goals.sales ?? ""}/></label>
        <button className="launchPrimary">Сохранить</button>{error && <p className="launchError">{error}</p>}</form>
      : hasGoals ? <><GoalBar label="Заявки" goal={data.goals.leads} fact={data.fact.leads}/><GoalBar label="Цена заявки" goal={data.goals.cpl} fact={data.fact.cpl} money lowerIsBetter/>
          <GoalBar label="Встречи" goal={data.goals.meetings} fact={data.fact.meetings}/><GoalBar label="Продажи" goal={data.goals.sales} fact={data.fact.sales}/>
          {data.fact.target_share != null && <p className="launchMuted">Целевых заявок: {Math.round(data.fact.target_share)}% по отметкам менеджеров</p>}</>
        : <p className="launchMuted">Цели согласуем с маркетологом на старте: сколько заявок, по какой цене и сколько продаж ждём в этом месяце.</p>}</div>;
  if (data.dismissed) return hasGoals ? <section className="launchStrip">{goals}</section> : null;
  const percentDone = Math.round(data.done / Math.max(1, data.total) * 100);
  const m = data.marketer;
  return <section className="startPanel" aria-label="Запуск проекта">
    <header><div><h2>{data.complete ? "Всё настроено" : "Запуск проекта"}</h2><p>{data.complete ? "Реклама и CRM работают. Этот блок можно скрыть — цели месяца останутся на странице." : "Что уже готово, что делает агентство и что осталось сделать вам."}</p></div>
      <div className="launchProgress"><b>{data.done} из {data.total}</b><i><em style={{ width: `${percentDone}%` }}/></i></div>
      <button className="launchLink" onClick={async () => { await api(`/crm/projects/${projectId}/launch/dismiss`, json("POST")).catch(() => undefined); load(); }}>Скрыть</button></header>
    <div className="launchGrid">
      <div className="launchCard"><h3>Чек-лист</h3><ol className="launchChecklist">{data.checklist.map(item => <li key={item.key} className={item.done ? "done" : ""}>
        <span className="launchTick">{item.done ? "✓" : ""}</span>
        {item.done ? <span>{item.label}</span> : <Link href={item.href}>{item.label}</Link>}{item.agency && <small>агентство</small>}</li>)}</ol></div>
      <div className="launchCard"><h3>Что делает агентство</h3><ol className="launchPlan">{data.plan.map(step => <li key={step.id} className={step.status}>
        <i/><div><b>{step.title}</b>{(step.due || step.note) && <small>{step.due ? `до ${new Date(step.due).toLocaleDateString("ru-RU", { day: "numeric", month: "long" })}` : ""}{step.due && step.note ? " · " : ""}{step.note || ""}</small>}</div>
        <span className={`launchStatus ${step.status}`}>{data.statuses[step.status]}</span></li>)}</ol>
        {data.weekly_note && <p className="launchNote"><b>Комментарий маркетолога:</b> {data.weekly_note}</p>}</div>
      <div className="launchSide">
        <div className="launchCard launchMarketer">{m ? <>{m.photo_url ? <img src={m.photo_url} alt=""/> : <span className="launchAvatar">{m.name.slice(0, 1)}</span>}
            <div><small>{m.role || "Ваш маркетолог"}</small><b>{m.name}</b>{m.hours && <small>{m.hours}</small>}</div>
            <div className="launchContacts">{m.telegram && <a href={tgLink(m.telegram)} target="_blank" rel="noopener noreferrer">Telegram</a>}{m.phone && <a href={`tel:${m.phone.replace(/[^\d+]/g, "")}`}>{m.phone}</a>}</div></>
          : <p className="launchMuted">Здесь появятся контакты вашего маркетолога.</p>}</div>
        {goals}
      </div>
    </div>
  </section>;
}
