"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { Pipeline } from "./shared";

type Preview = { total: number; no_contact: number; with_amount: number; already_imported: number; pipeline_id: number; pipelines: string[];
  columns: Record<string, string | null>; stages: { name: string; count: number; suggested: number | null }[];
  users: { name: string; count: number; suggested: number | null }[]; target_stages: { id: number; name: string; analytics_type: string }[];
  team: { id: number; name: string }[]; sample: { name: string; contact: string; amount: number | null; stage: string; responsible: string | null; phone: string | null }[] };
type Result = { created: number; skipped_duplicates: number; skipped_no_contact: number; contacts_new: number; contacts_merged: number; sales: number };

const columnNames: Record<string, string> = { name: "Название", amount: "Бюджет", stage: "Этап", responsible: "Ответственный", created: "Дата создания",
  contact: "Контакт", phones: "Телефоны", emails: "Email", tags: "Теги", note: "Примечание", amo_id: "ID" };

/** CRM → «Импорт из amoCRM»: upload the deals export, check the mapping, import. */
export function AmoImport({ projectId, pipelines, pipelineId, onClose, onDone }: { projectId: number; pipelines: Pipeline[]; pipelineId: number | null;
  onClose: () => void; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [target, setTarget] = useState<number | null>(pipelineId);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [stages, setStages] = useState<Record<string, number>>({});
  const [users, setUsers] = useState<Record<string, number | null>>({});
  const [defaultUser, setDefaultUser] = useState<number | null>(null);
  const [sales, setSales] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<Result | null>(null);

  async function load(f: File, pipeline: number | null) {
    setBusy(true); setError(""); setPreview(null);
    const body = new FormData(); body.append("file", f); if (pipeline) body.append("pipeline_id", String(pipeline));
    try {
      const p = await api<Preview>(`/crm/projects/${projectId}/import/amocrm/preview`, { method: "POST", body, timeoutMs: 120_000 });
      setPreview(p); setTarget(p.pipeline_id);
      setStages(Object.fromEntries(p.stages.map(s => [s.name, s.suggested ?? p.target_stages[0]?.id])));
      setUsers(Object.fromEntries(p.users.map(u => [u.name, u.suggested])));
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function run() {
    if (!file || !preview) return;
    setBusy(true); setError("");
    const body = new FormData(); body.append("file", file);
    body.append("options", JSON.stringify({ pipeline_id: preview.pipeline_id, stages, users, default_user_id: defaultUser, create_sales: sales }));
    try { setResult(await api<Result>(`/crm/projects/${projectId}/import/amocrm`, { method: "POST", body, timeoutMs: 600_000 })); onDone(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const toImport = preview ? preview.total - preview.no_contact - preview.already_imported : 0;
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget && !busy) onClose(); }}>
    <div className="resultModal crmModal amoImport"><header><h2>Импорт из amoCRM</h2><button type="button" onClick={onClose} disabled={busy}>×</button></header>
      {result ? <div className="amoResult">
        <p className="crmOk">Готово: перенесено сделок — <b>{result.created}</b>{result.sales ? `, подтверждено продаж — ${result.sales}` : ""}.</p>
        <ul><li>Новых контактов: {result.contacts_new}, объединено с существующими: {result.contacts_merged}</li>
          {!!result.skipped_duplicates && <li>Уже были перенесены раньше: {result.skipped_duplicates}</li>}
          {!!result.skipped_no_contact && <li>Пропущено без телефона и email: {result.skipped_no_contact}</li>}</ul>
        <button className="resultPrimary" onClick={onClose}>Открыть воронку</button></div>
      : <>
        <ol className="amoSteps"><li>В amoCRM откройте «Сделки» → список → «⋯» → <b>Экспорт</b> → Excel (все поля).</li>
          <li>Загрузите файл сюда — проверим колонки, этапы и менеджеров.</li><li>Нажмите «Перенести». Повторная загрузка того же файла не создаст дублей.</li></ol>
        <div className="amoPick"><label className="crmGhost amoFile">{file ? file.name : "Выбрать файл XLSX / CSV"}
          <input type="file" accept=".xlsx,.csv" onChange={e => { const f = e.target.files?.[0] || null; setFile(f); if (f) load(f, target); }}/></label>
          {pipelines.length > 1 && <label>В воронку<select value={target || ""} onChange={e => { const v = Number(e.target.value); setTarget(v); if (file) load(file, v); }}>
            {pipelines.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}</div>
        {busy && !preview && <p className="crmMuted">Читаем файл…</p>}
        {error && <p className="crmFormError">{error}</p>}
        {preview && <>
          <p className="amoSummary">В файле <b>{preview.total}</b> сделок{preview.with_amount ? `, с бюджетом — ${preview.with_amount}` : ""}.
            {preview.no_contact ? ` Без телефона и email — ${preview.no_contact} (их пропустим).` : ""}
            {preview.already_imported ? ` Уже перенесены — ${preview.already_imported}.` : ""}</p>
          <p className="crmMuted amoCols">Нашли колонки: {Object.entries(preview.columns).filter(([, v]) => v).map(([k, v]) => `${columnNames[k] || k} ← «${v}»`).join(" · ")}</p>
          <h3>Этапы</h3><div className="amoMap">{preview.stages.map(s => <div key={s.name}><span>{s.name} <small>{s.count}</small></span>
            <select value={stages[s.name] || ""} onChange={e => setStages({ ...stages, [s.name]: Number(e.target.value) })}>
              {preview.target_stages.map(t => <option key={t.id} value={t.id}>{t.name}{t.analytics_type === "WON" ? " (успех)" : t.analytics_type === "LOST" ? " (отказ)" : ""}</option>)}</select></div>)}</div>
          {!!preview.users.length && <><h3>Менеджеры</h3><div className="amoMap">{preview.users.map(u => <div key={u.name}><span>{u.name} <small>{u.count}</small></span>
            <select value={users[u.name] ?? ""} onChange={e => setUsers({ ...users, [u.name]: Number(e.target.value) || null })}>
              <option value="">— как ниже —</option>{preview.team.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></div>)}</div></>}
          <label>Если менеджер не найден<select value={defaultUser || ""} onChange={e => setDefaultUser(Number(e.target.value) || null)}>
            <option value="">Без ответственного</option>{preview.team.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label>
          <label className="crmCheck"><input type="checkbox" checked={sales} onChange={e => setSales(e.target.checked)}/> Успешные сделки с бюджетом — подтверждённые продажи <small>(выручка за прошлые месяцы появится в аналитике; в рекламу не отправляется)</small></label>
          <div className="crmTableScroll"><table className="amoSample"><thead><tr><th>Сделка</th><th>Контакт</th><th>Телефон</th><th>Бюджет</th><th>Этап</th></tr></thead>
            <tbody>{preview.sample.map((r, i) => <tr key={i}><td>{r.name}</td><td>{r.contact}</td><td>{r.phone || "—"}</td><td>{r.amount ? r.amount.toLocaleString("ru-RU") : "—"}</td><td>{r.stage}</td></tr>)}</tbody></table></div>
          <button className="resultPrimary" disabled={busy || toImport <= 0} onClick={run}>{busy ? "Переносим… это может занять пару минут" : `Перенести ${toImport} ${toImport % 10 === 1 && toImport % 100 !== 11 ? "сделку" : [2, 3, 4].includes(toImport % 10) && ![12, 13, 14].includes(toImport % 100) ? "сделки" : "сделок"}`}</button>
        </>}</>}
    </div></div>;
}
