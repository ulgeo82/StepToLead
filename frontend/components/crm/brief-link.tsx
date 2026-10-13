"use client";
import { useState } from "react";
import { request } from "./shared";

type Stage = { id: number; name: string; analytics_type: string };

// Ссылка на бриф для клиента после созвона: без логина. Когда клиент отправит бриф, сделка сама переедет на выбранный этап.
export function BriefLink({ dealId, stages, canEdit }: { dealId: number; stages: Stage[]; canEdit: boolean }) {
  const open = stages.filter(s => s.analytics_type !== "WON" && s.analytics_type !== "LOST");
  const guess = open.find(s => /бриф/i.test(s.name))?.id;
  const [stageId, setStageId] = useState<number | "">(guess ?? "");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  if (!canEdit) return null;
  async function make() {
    setBusy(true); setError(""); setCopied(false);
    try { const r = await request<{ url: string }>(`/crm/deals/${dealId}/brief-link`, "POST", { stage_id: stageId || null }); setUrl(r.url); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <div className="crmBlock crmBriefLink"><h3>📝 Бриф клиенту</h3>
    <label>После заполнения перенести на этап<select value={stageId} onChange={e => { setStageId(e.target.value ? Number(e.target.value) : ""); setUrl(""); }}>
      <option value="">Не переносить</option>{open.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
    {url ? <div className="crmBriefLinkRow"><input readOnly value={url} onFocus={e => e.target.select()}/>
        <button type="button" onClick={() => { void navigator.clipboard.writeText(url).then(() => setCopied(true)); }}>{copied ? "Скопировано ✓" : "Копировать"}</button></div>
      : <button type="button" className="crmPrimary" disabled={busy} onClick={() => void make()}>{busy ? "Готовим…" : "Получить ссылку"}</button>}
    <small>Без логина и пароля. Черновик сохраняется сам. Когда клиент отправит бриф: сделка переедет на этап, контакт обновится, вам — уведомление и задача.</small>
    {error && <small className="crmError">{error}</small>}</div>;
}
