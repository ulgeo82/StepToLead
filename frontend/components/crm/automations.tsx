"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { Pipeline, request, Source, Team, typeLabels, when } from "./shared";

type Rule = { id: number; pipeline_id: number; stage_id: number | null; name: string; trigger: string; delay_minutes: number;
  conditions: Record<string, unknown>; action: string; params: Record<string, unknown>; active: boolean;
  fired_count: number; last_fired_at: string | null; summary: string };

const triggerLabels: Record<string, string> = { DEAL_CREATED: "Новая сделка", STAGE_ENTER: "Сделка перешла на этап", NO_ACTIVITY: "Нет активности" };
const actionLabels: Record<string, string> = { CREATE_TASK: "Поставить задачу", SET_RESPONSIBLE: "Назначить ответственного",
  ADD_TAG: "Добавить тег", NOTIFY: "Уведомить" };
const notifyTo: Record<string, string> = { owner: "Ответственному", heads: "Руководителям", owner_and_heads: "Ответственному и руководителям" };
const delayOptions = [[60, "1 час"], [180, "3 часа"], [24 * 60, "1 день"], [2 * 24 * 60, "2 дня"], [3 * 24 * 60, "3 дня"],
  [7 * 24 * 60, "7 дней"], [14 * 24 * 60, "14 дней"]] as const;
const dueOptions = [[0, "Сразу"], [15, "Через 15 минут"], [60, "Через час"], [24 * 60, "Через день"], [3 * 24 * 60, "Через 3 дня"]] as const;
const duration = (minutes: number) => minutes >= 1440 ? `${Math.round(minutes / 1440)} дн.` : minutes >= 60 ? `${Math.round(minutes / 60)} ч` : `${minutes} мин`;

export function Automations({ projectId, pipelines, pipelineId, team, sources, canManage }: { projectId: number;
  pipelines: Pipeline[]; pipelineId: number | null; team: Team[]; sources: Source[]; canManage: boolean }) {
  const [rules, setRules] = useState<Rule[]>([]);
  const [selectedPipeline, setSelectedPipeline] = useState<number | null>(pipelineId);
  const [editing, setEditing] = useState<Rule | "new" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(() => request<Rule[]>(`/crm/projects/${projectId}/automations`, "GET").then(setRules).catch(e => setError(e.message)), [projectId]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (!selectedPipeline && pipelines[0]) setSelectedPipeline(pipelineId || pipelines[0].id); }, [pipelines, pipelineId, selectedPipeline]);
  const pipeline = pipelines.find(p => p.id === selectedPipeline);
  const shown = rules.filter(r => r.pipeline_id === selectedPipeline);
  const stageName = (id: number | null) => pipeline?.stages.find(s => s.id === id)?.name;

  async function call(action: () => Promise<unknown>, message: string) {
    setError(""); setNotice("");
    try { await action(); await load(); setNotice(message); return true; } catch (e) { setError((e as Error).message); return false; }
  }
  const toggle = (rule: Rule) => call(() => request(`/crm/automations/${rule.id}`, "PUT", { ...strip(rule), active: !rule.active }), rule.active ? "Правило выключено" : "Правило включено");
  const remove = (rule: Rule) => window.confirm(`Удалить правило «${rule.name}»?`) && call(() => request(`/crm/automations/${rule.id}`, "DELETE"), "Правило удалено");
  const install = () => selectedPipeline && call(() => request(`/crm/projects/${projectId}/automations/recommended?pipeline_id=${selectedPipeline}`, "POST", {}), "Рекомендуемые правила добавлены");

  return <section className="crmList resultPanel crmAutomation"><div className="crmSectionHead"><div><h2>Автоматизация воронки</h2>
    <p>Правила срабатывают сами: ставят задачи, назначают менеджеров, ставят теги и предупреждают о зависших сделках. Так ни один лид не теряется.</p></div>
    <div className="crmHeadActions">{pipelines.length > 1 && <select value={selectedPipeline || ""} onChange={e => setSelectedPipeline(Number(e.target.value))}>{pipelines.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>}
      {canManage && <><button className="crmGhost" onClick={install}>✨ Рекомендуемые правила</button><button className="crmPrimary" onClick={() => setEditing("new")}>＋ Правило</button></>}</div></div>
    {error && <p className="crmFormError">{error}</p>}{notice && <p className="crmOk">{notice}</p>}
    {pipeline && <div className="crmAutoStages">{[{ id: 0, name: "При создании сделки", color: "#7a8ca5" }, ...pipeline.stages].map(stage => {
      const list = shown.filter(r => stage.id === 0 ? r.trigger === "DEAL_CREATED" : r.stage_id === stage.id && r.trigger !== "DEAL_CREATED");
      const anyStageIdle = stage.id === 0 ? shown.filter(r => r.trigger === "NO_ACTIVITY" && !r.stage_id) : [];
      return <div className="crmAutoColumn" key={stage.id} style={{ ["--stage" as string]: stage.color }}><header>{stage.name}</header>
        {[...list, ...anyStageIdle].map(rule => <article key={rule.id} className={rule.active ? "" : "off"}>
          <small>{rule.trigger === "NO_ACTIVITY" ? `Нет активности ${duration(rule.delay_minutes)}${rule.stage_id ? "" : " (любой этап)"}` : triggerLabels[rule.trigger]}</small>
          <b>{rule.name}</b><span>⚡ {actionText(rule, team)}</span>
          <em>Сработало {rule.fired_count} раз{rule.last_fired_at ? ` · последний ${when(rule.last_fired_at)}` : ""}</em>
          {canManage && <div><button onClick={() => toggle(rule)}>{rule.active ? "Выключить" : "Включить"}</button><button onClick={() => setEditing(rule)}>Изменить</button><button onClick={() => remove(rule)}>Удалить</button></div>}</article>)}
        {!list.length && !anyStageIdle.length && <p className="crmEmpty">Нет правил</p>}</div>; })}</div>}
    {!shown.length && <div className="crmAutoHint"><b>С чего начать.</b> Нажмите «Рекомендуемые правила»: задача «позвонить за 15 минут» на каждый новый лид (скорость реакции — главный фактор конверсии), КП после квалификации, сигнал о лидах без движения сутки и дожим квалифицированных через 3 дня.</div>}
    {editing && pipeline && <RuleForm projectId={projectId} pipeline={pipeline} rule={editing === "new" ? null : editing} team={team} sources={sources}
      onClose={() => setEditing(null)} onSaved={async () => { setEditing(null); await load(); setNotice("Правило сохранено"); }} stageName={stageName}/>}
  </section>;
}

function strip(rule: Rule) {
  return { pipeline_id: rule.pipeline_id, stage_id: rule.stage_id, name: rule.name, trigger: rule.trigger,
    delay_minutes: rule.delay_minutes, conditions: rule.conditions, action: rule.action, params: rule.params, active: rule.active };
}

function actionText(rule: Rule, team: Team[]) {
  const p = rule.params;
  if (rule.action === "CREATE_TASK") return `Задача «${p.title}»${p.due_minutes ? `, срок ${duration(Number(p.due_minutes))}` : ""}`;
  if (rule.action === "SET_RESPONSIBLE") return p.mode === "round_robin"
    ? `По очереди: ${((p.user_ids as number[]) || []).map(id => team.find(m => m.id === id)?.display_name).filter(Boolean).join(", ")}`
    : `Ответственный: ${team.find(m => m.id === Number(p.user_id))?.display_name || "—"}`;
  if (rule.action === "ADD_TAG") return `Тег #${p.tag}`;
  return `${notifyTo[String(p.to || "owner")]}: «${p.text || rule.name}»`;
}

function RuleForm({ projectId, pipeline, rule, team, sources, onClose, onSaved }: { projectId: number; pipeline: Pipeline;
  rule: Rule | null; team: Team[]; sources: Source[]; onClose: () => void; onSaved: () => void; stageName: (id: number | null) => string | undefined }) {
  const [trigger, setTrigger] = useState(rule?.trigger || "STAGE_ENTER");
  const [action, setAction] = useState(rule?.action || "CREATE_TASK");
  const [mode, setMode] = useState(String(rule?.params.mode || "user"));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const p = rule?.params || {};
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget); setBusy(true); setError("");
    const params: Record<string, unknown> = action === "CREATE_TASK" ? { type_code: f.get("type_code"), title: f.get("title"),
      due_minutes: Number(f.get("due_minutes")), priority: f.get("priority"), responsible: f.get("responsible") || "owner" }
      : action === "SET_RESPONSIBLE" ? (mode === "round_robin" ? { mode, user_ids: f.getAll("user_ids").map(Number) } : { mode, user_id: Number(f.get("user_id")) })
      : action === "ADD_TAG" ? { tag: f.get("tag") } : { to: f.get("to"), text: f.get("text") };
    const body = { pipeline_id: pipeline.id, stage_id: Number(f.get("stage_id")) || null, name: f.get("name"), trigger,
      delay_minutes: trigger === "NO_ACTIVITY" ? Number(f.get("delay_minutes")) : 0,
      conditions: f.get("source_id") ? { source_id: Number(f.get("source_id")) } : {}, action, params, active: rule?.active ?? true };
    try { await request(rule ? `/crm/automations/${rule.id}` : `/crm/projects/${projectId}/automations`, rule ? "PUT" : "POST", body); onSaved(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const openStages = pipeline.stages.filter(s => trigger !== "NO_ACTIVITY" || s.analytics_type === "LEAD" || s.analytics_type === "QUALIFIED");
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}><form className="resultModal crmModal" onSubmit={submit}>
    <header><h2>{rule ? "Изменить правило" : "Новое правило"}</h2><button type="button" onClick={onClose}>×</button></header>
    <label>Название<input name="name" required minLength={2} defaultValue={rule?.name || ""} placeholder="Например, звонок новому лиду за 15 минут"/></label>
    <fieldset className="crmFieldset"><legend>Когда</legend>
      <select value={trigger} onChange={e => setTrigger(e.target.value)}>{Object.entries(triggerLabels).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
      {trigger !== "DEAL_CREATED" && <label>Этап<select name="stage_id" defaultValue={rule?.stage_id || ""} required={trigger === "STAGE_ENTER"}>{trigger === "NO_ACTIVITY" && <option value="">Любой открытый этап</option>}{openStages.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
      {trigger === "NO_ACTIVITY" && <label>Без активности дольше<select name="delay_minutes" defaultValue={rule?.delay_minutes || 24 * 60}>{delayOptions.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>}
      <label>Только для источника<select name="source_id" defaultValue={String(rule?.conditions.source_id || "")}><option value="">Любой источник</option>{sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label></fieldset>
    <fieldset className="crmFieldset"><legend>Что сделать</legend>
      <select value={action} onChange={e => setAction(e.target.value)}>{Object.entries(actionLabels).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
      {action === "CREATE_TASK" && <><div className="resultModalGrid"><label>Тип<select name="type_code" defaultValue={String(p.type_code || "CALL")}>{Object.entries(typeLabels).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label>Срок<select name="due_minutes" defaultValue={String(p.due_minutes ?? 15)}>{dueOptions.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label></div>
        <label>Текст задачи<input name="title" required minLength={2} defaultValue={String(p.title || "Связаться с клиентом")}/></label>
        <div className="resultModalGrid"><label>Кому<select name="responsible" defaultValue={String(p.responsible || "owner")}><option value="owner">Ответственному сделки</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
          <label>Приоритет<select name="priority" defaultValue={String(p.priority || "NORMAL")}><option value="HIGH">Высокий</option><option value="NORMAL">Обычный</option><option value="LOW">Низкий</option></select></label></div></>}
      {action === "SET_RESPONSIBLE" && <><select value={mode} onChange={e => setMode(e.target.value)}><option value="user">Конкретный сотрудник</option><option value="round_robin">По очереди между сотрудниками</option></select>
        {mode === "user" ? <label>Сотрудник<select name="user_id" required defaultValue={String(p.user_id || "")}><option value="">Выберите</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
          : <div className="crmChecks">{team.map(m => <label key={m.id}><input type="checkbox" name="user_ids" value={m.id} defaultChecked={((p.user_ids as number[]) || []).includes(m.id)}/> {m.display_name}</label>)}</div>}</>}
      {action === "ADD_TAG" && <label>Тег<input name="tag" required maxLength={40} defaultValue={String(p.tag || "")} placeholder="горячий"/></label>}
      {action === "NOTIFY" && <><label>Кому<select name="to" defaultValue={String(p.to || "owner")}>{Object.entries(notifyTo).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label>Текст<input name="text" required defaultValue={String(p.text || "")} placeholder="Лид без движения больше суток"/></label><p className="crmModalLead">Придёт в кабинет и в Telegram, если он подключён.</p></>}</fieldset>
    {error && <p className="crmFormError">{error}</p>}
    <button className="resultPrimary" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить правило"}</button></form></div>;
}
