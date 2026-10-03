"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import "./plan.css";

type PlanInfo = { code: string | null; name: string; users: number | null; features: string[]; price: number | null };
type Overview = PlanInfo & { users_used: number; all_features: { key: string; name: string; included: boolean; from: string }[]; plans: PlanInfo[] };
const rub = (n: number) => `${n.toLocaleString("ru-RU")} ₽/мес`;

/** Admin → client card: the agency picks the client's tariff; the portal limits follow it. */
export function AdminPlanPanel({ workspaceId }: { workspaceId: number }) {
  const [data, setData] = useState<{ plans: PlanInfo[]; features: Record<string, string>; workspaces: { workspace_id: number; plan: string | null }[] } | null>(null);
  const [plan, setPlan] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => { api<NonNullable<typeof data>>("/portal/admin/plans").then(d => { setData(d); setPlan(d.workspaces.find(w => w.workspace_id === workspaceId)?.plan ?? null); })
    .catch(e => setMessage((e as Error).message)); }, [workspaceId]);
  async function choose(code: string | null) {
    setBusy(true); setMessage("");
    try { await api(`/portal/admin/workspaces/${workspaceId}/plan`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan: code }) });
      setPlan(code); setMessage("Тариф сохранён — ограничения в портале клиента применились сразу"); }
    catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  if (!data) return message ? <p className="notice error">{message}</p> : null;
  return <section className="panel planPanel"><div className="panelHead"><div><p className="eyebrow">Тариф</p><h2>Что входит в портал клиента</h2></div></div>
    <div className="planChoices">
      {data.plans.map(p => <button key={p.code} className={plan === p.code ? "active" : ""} disabled={busy} onClick={() => choose(p.code)}>
        <b>{p.name}</b><span>{p.price ? rub(p.price) : ""}</span><small>{p.users ? `до ${p.users} сотрудников` : "без лимита сотрудников"}</small>
        <ul>{Object.entries(data.features).map(([k, v]) => <li key={k} className={p.features.includes(k) ? "on" : ""}>{p.features.includes(k) ? "✓" : "—"} {v}</li>)}</ul></button>)}
      <button className={plan === null ? "active" : ""} disabled={busy} onClick={() => choose(null)}><b>Индивидуальный</b><span>по договору</span><small>без ограничений</small>
        <ul><li className="on">Всё включено — для клиентов, подключённых до тарифов</li></ul></button></div>
    {message && <p className="planMessage">{message}</p>}</section>;
}

/** Settings → «Компания»: the client sees their tariff, seats used and what is not included. */
export function ClientPlanCard() {
  const [data, setData] = useState<Overview | null>(null);
  useEffect(() => { api<Overview>("/crm/plan").then(setData).catch(() => setData(null)); }, []);
  if (!data) return null;
  const locked = data.all_features.filter(f => !f.included);
  return <section className="resultPanel settingsCard planCard"><h2>Тариф: {data.name}</h2>
    <p>{data.users ? `Сотрудников в портале: ${data.users_used} из ${data.users}.` : `Сотрудников в портале: ${data.users_used}, без ограничения.`}
      {data.price ? ` Ведение — ${rub(data.price)}.` : ""}</p>
    <ul className="planFeatures">{data.all_features.map(f => <li key={f.key} className={f.included ? "on" : ""}>
      <i>{f.included ? "✓" : "🔒"}</i>{f.name}{!f.included && <small>в тарифе «{f.from}»</small>}</li>)}</ul>
    {!!locked.length && <p className="planHint">Чтобы подключить закрытые функции, напишите вашему маркетологу StepToLead — тариф меняется без переноса данных.</p>}</section>;
}
