"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Direction = { id: string; name: string; stages: string[]; qualified_from: number | null; check: string; cycle_days: string; template?: string };
type Template = { key: string; name: string; q: number; stages: string[] };
type PlanItem = { direction: string; pipeline: string | null; action: "create" | "update"; stages: { name: string; type: string; status: string }[]; kept: { name: string; deals: number }[]; archived: { name: string }[] };

const uid = () => Math.random().toString(36).slice(2, 10);
const TYPE: Record<string, string> = { LEAD: "лид", QUALIFIED: "целевой", WON: "продажа", LOST: "отказ" };

function read(value: string): Direction[] {
  try { const data = JSON.parse(value || "{}"); return Array.isArray(data.directions) ? data.directions : []; } catch { return []; }
}

/** Направления продаж в брифе: каждое станет воронкой CRM. «Заявка», «Продажа» и «Отказ» есть всегда. */
export function FunnelsEditor({ value, onChange, disabled, hintText, projectId, briefId, beforeApply }: {
  value: string; onChange: (value: string) => void; disabled: boolean; hintText?: string;
  projectId?: number; briefId: number | null; beforeApply: () => Promise<unknown>;
}) {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [plan, setPlan] = useState<PlanItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  useEffect(() => { api<{ templates: Template[] }>("/brief/funnel-templates").then(r => setTemplates(r.templates)).catch(() => undefined); }, []);
  const legacy = value && !value.trim().startsWith("{") ? value : "";
  const directions = read(value);
  const save = (next: Direction[]) => { setPlan(null); setDone(""); onChange(next.length ? JSON.stringify({ directions: next }) : ""); };
  const patch = (i: number, change: Partial<Direction>) => save(directions.map((d, k) => k === i ? { ...d, ...change } : d));
  const fromTemplate = (key: string, name?: string): Direction => {
    const t = templates.find(x => x.key === key) || templates[templates.length - 1];
    return { id: uid(), name: name || (directions.length ? `Направление ${directions.length + 1}` : t?.name || "Основное направление"), stages: [...(t?.stages || [])], qualified_from: t ? t.q : 0, check: "", cycle_days: "", template: t?.key };
  };
  const fromText = (text: string) => {
    const parts = text.split(/→|->|\n|;|,/).map(s => s.trim()).filter(Boolean).filter(s => !/^(заявка|продажа|оплата|отказ)$/i.test(s));
    return { id: uid(), name: "Основное направление", stages: parts.slice(0, 12), qualified_from: parts.length ? 0 : null, check: "", cycle_days: "" };
  };
  const moveStage = (i: number, s: number, delta: number) => {
    const stages = [...directions[i].stages]; const j = s + delta; if (j < 0 || j >= stages.length) return;
    [stages[s], stages[j]] = [stages[j], stages[s]];
    const q = directions[i].qualified_from; patch(i, { stages, qualified_from: q === s ? j : q === j ? s : q });
  };
  const removeStage = (i: number, s: number) => {
    const stages = directions[i].stages.filter((_, k) => k !== s); const q = directions[i].qualified_from;
    patch(i, { stages, qualified_from: !stages.length ? null : q == null ? null : q > s ? q - 1 : Math.min(q, stages.length - 1) });
  };
  async function preview() {
    setBusy(true); setError(""); setDone("");
    try { await beforeApply(); setPlan((await api<{ plan: PlanItem[] }>(`/brief/projects/${projectId}/${briefId}/funnels`)).plan); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось подготовить воронки"); } finally { setBusy(false); }
  }
  async function apply() {
    setBusy(true); setError("");
    try { await api(`/brief/projects/${projectId}/${briefId}/funnels`, { method: "POST" }); setPlan(null); setDone(directions.length > 1 ? "Воронки созданы в CRM. Теперь в «Настройки → Источники» выберите, в какую воронку идут заявки каждого источника — иначе всё попадёт в основную." : "Воронка обновлена в CRM — этапы уже на месте."); }
    catch (e) { setError(e instanceof Error ? e.message : "Не удалось создать воронки"); } finally { setBusy(false); }
  }
  const lock = disabled || busy;
  return <div className="funnels">
    {legacy && !directions.length && <div className="funnelsLegacy"><p>Сейчас здесь текст: «{legacy.slice(0, 200)}»</p><button type="button" disabled={lock} onClick={() => save([fromText(legacy)])}>Разложить на этапы</button></div>}
    {!directions.length && !legacy && <div className="funnelsStart"><p>Выберите нишу — подставим типовые этапы, их можно поправить.</p>
      <div className="briefOptions">{templates.map(t => <button type="button" key={t.key} disabled={lock} onClick={() => save([fromTemplate(t.key, t.key === "other" ? "Основное направление" : t.name)])}>{t.name}</button>)}</div>
      {hintText?.trim() && <button type="button" className="funnelsLink" disabled={lock} onClick={() => save([fromText(hintText)])}>Взять этапы из комментария к пути клиента</button>}</div>}
    {directions.map((d, i) => <div className="funnelsCard" key={d.id}>
      <div className="funnelsHead"><b>{i + 1}</b><input aria-label="Название направления" disabled={lock} value={d.name} maxLength={60} onChange={e => patch(i, { name: e.target.value })} placeholder="Например: Кухни на заказ"/>
        <button type="button" className="funnelsRemove" disabled={lock} onClick={() => save(directions.filter((_, k) => k !== i))} title="Убрать направление">×</button></div>
      <div className="funnelsMeta"><label>Средний чек, ₽<input disabled={lock} inputMode="decimal" value={d.check} maxLength={30} onChange={e => patch(i, { check: e.target.value })}/></label>
        <label>От заявки до оплаты, дней<input disabled={lock} inputMode="numeric" value={d.cycle_days} maxLength={10} onChange={e => patch(i, { cycle_days: e.target.value })}/></label></div>
      <ol className="funnelsStages"><li className="fixed">Заявка <small>всегда первая</small></li>
        {d.stages.map((s, k) => <li key={k} className={d.qualified_from != null && k >= d.qualified_from ? "qualified" : ""}>
          <input aria-label={`Этап ${k + 1}`} disabled={lock} value={s} maxLength={60} onChange={e => patch(i, { stages: d.stages.map((x, n) => n === k ? e.target.value : x) })}/>
          <label className="funnelsQ" title="С этого этапа лид считается целевым (квалифицированным)"><input type="radio" name={`q-${d.id}`} disabled={lock} checked={d.qualified_from === k} onChange={() => patch(i, { qualified_from: k })}/> с него целевой</label>
          <button type="button" disabled={lock || k === 0} onClick={() => moveStage(i, k, -1)} aria-label="Выше">↑</button>
          <button type="button" disabled={lock || k === d.stages.length - 1} onClick={() => moveStage(i, k, 1)} aria-label="Ниже">↓</button>
          <button type="button" disabled={lock} onClick={() => removeStage(i, k)} aria-label="Удалить этап">×</button></li>)}
        <li className="add"><button type="button" disabled={lock || d.stages.length >= 12} onClick={() => patch(i, { stages: [...d.stages, ""], qualified_from: d.qualified_from ?? 0 })}>＋ Этап</button></li>
        <li className="fixed won">Продажа</li><li className="fixed lost">Отказ <small>из любого этапа</small></li></ol>
    </div>)}
    {directions.length > 0 && <div className="funnelsFoot">
      <div className="briefOptions"><span>Ещё направление:</span>{templates.map(t => <button type="button" key={t.key} disabled={lock || directions.length >= 6} onClick={() => save([...directions, fromTemplate(t.key, t.key === "other" ? undefined : t.name)])}>{t.name}</button>)}</div>
      {projectId && briefId && <button type="button" className="funnelsApply" disabled={lock} onClick={() => void preview()}>{busy && !plan ? "Готовим…" : "Создать воронки в CRM"}</button>}</div>}
    {error && <p role="alert" className="briefError">{error}</p>}{done && <p className="funnelsDone">{done}</p>}
    {plan && <div className="funnelsPlan"><h4>Что изменится в CRM</h4>
      {plan.map(p => <div key={p.direction}><strong>{p.action === "create" ? "Новая воронка" : `Воронка «${p.pipeline}»`} → «{p.direction}»</strong>
        <p>{p.stages.map(s => `${s.name}${s.status === "new" ? " (новый)" : s.status === "renamed" ? " (переименуем)" : ""} · ${TYPE[s.type]}`).join("  →  ")}</p>
        {p.kept.length > 0 && <p className="warn">Останутся, потому что в них есть сделки: {p.kept.map(k => `${k.name} (${k.deals})`).join(", ")}. Перенесите сделки и уберите этап вручную, если он не нужен.</p>}
        {p.archived.length > 0 && <p>Уберём пустые этапы: {p.archived.map(a => a.name).join(", ")}.</p>}</div>)}
      <div className="funnelsPlanActions"><button type="button" className="funnelsApply" disabled={busy} onClick={() => void apply()}>{busy ? "Создаём…" : "Подтвердить"}</button><button type="button" disabled={busy} onClick={() => setPlan(null)}>Отмена</button></div></div>}
  </div>;
}
