"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Site = { id: number; name: string; origin: string };
type Connection = { id: number; public_id: string; project_id: number; website_id: number; is_active: boolean;
  last_received_at: string | null; form_id: string; form_name: string; secret_header: string; webhook_path: string };

export default function TildaIntegration({ projectId, sites, canManage }: { projectId: number; sites: Site[]; canManage: boolean }) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [websiteId, setWebsiteId] = useState("");
  const [revealed, setRevealed] = useState<{ id: number; secret: string } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const refresh = () => api<Connection[]>(`/integrations/tilda/connections?project_id=${projectId}`)
    .then(setConnections).catch(e => setError(e.message));
  useEffect(() => { setRevealed(null); setWebsiteId(""); refresh(); }, [projectId]);
  async function create() {
    setBusy(true); setError("");
    try {
      const row = await api<Connection & { secret: string }>("/integrations/tilda/connections", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, website_id: Number(websiteId) }) });
      setRevealed({ id: row.id, secret: row.secret }); setShowCreate(false); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось создать подключение"); }
    finally { setBusy(false); }
  }
  async function rotate(id: number) {
    if (!window.confirm("Старый секрет сразу перестанет работать. Перевыпустить?")) return;
    setBusy(true); setError("");
    try {
      const row = await api<Connection & { secret: string }>(`/integrations/tilda/connections/${id}/rotate-secret`, { method: "POST" });
      setRevealed({ id, secret: row.secret }); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось перевыпустить секрет"); }
    finally { setBusy(false); }
  }
  async function toggle(row: Connection) {
    setBusy(true); setError("");
    try {
      await api(`/integrations/tilda/connections/${row.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !row.is_active }) }); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось изменить статус"); }
    finally { setBusy(false); }
  }
  return <section className="resultPanel tildaIntegration">
    <div className="websiteTitle"><div><h3>Интеграция с Tilda</h3><p>Заявки разрешённой формы поступают в существующую входящую очередь CRM.</p></div>
      {canManage && <button className="websitePrimary" onClick={() => setShowCreate(!showCreate)}>Создать подключение</button>}</div>
    {error && <div className="resultError" role="alert">{error}</div>}
    {showCreate && <div className="websiteFilters"><label>Проект: текущий выбранный проект №{projectId}</label>
      <select aria-label="Сайт Tilda" value={websiteId} onChange={e => setWebsiteId(e.target.value)}><option value="">Выберите сайт</option>
        {sites.map(site => <option key={site.id} value={site.id}>{site.name} — {site.origin}</option>)}</select>
      <button className="websitePrimary" disabled={busy || !websiteId} onClick={create}>Создать</button></div>}
    {connections.map(row => <article key={row.id} className="tildaConnection"><strong>{sites.find(site => site.id === row.website_id)?.name || `Сайт №${row.website_id}`}</strong>
      <p>Статус: {row.is_active ? "Активно" : "Выключено"} · Последняя заявка: {row.last_received_at ? new Date(row.last_received_at).toLocaleString("ru-RU") : "ещё нет"}</p>
      <p>Webhook: <code>{typeof window === "undefined" ? row.webhook_path : `${window.location.origin}${row.webhook_path}`}</code></p>
      <p>Заголовок секрета: <code>{row.secret_header}</code></p>
      <p>Разрешённая форма: <code>{row.form_id}</code> — {row.form_name}</p>
      {revealed?.id === row.id && <div className="tildaSecret"><p>Сохраните секрет сейчас: повторно он не показывается.</p><code>{revealed.secret}</code>
        <button onClick={() => setRevealed(null)}>Скрыть</button></div>}
      {canManage && <div className="websiteFilters"><button disabled={busy} onClick={() => rotate(row.id)}>Перевыпустить секрет</button>
        <button disabled={busy} onClick={() => toggle(row)}>{row.is_active ? "Выключить" : "Включить"}</button></div>}
    </article>)}
    <details><summary>Как подключить Tilda</summary>
      <p>В Tilda откройте Настройки сайта → Формы → Webhook, укажите адрес Webhook и прикрепите его к форме «{connections[0]?.form_name || "Рассчитаем потенциал продвижения"}». Затем опубликуйте страницу.</p>
      <p><strong>Важное ограничение:</strong> штатный Webhook Tilda не позволяет задать HTTP-заголовок. Поэтому напрямую этот защищённый адрес не заработает: нужен доверенный промежуточный сервер, который добавит заголовок <code>{connections[0]?.secret_header || "X-StepToLead-Tilda-Secret"}</code>. Не помещайте секрет в URL или HTML сайта.</p>
      <p>Для связи с визитом передавайте скрытое поле <code>website_session_key</code> из <code>StepToLead.context()</code> после согласия посетителя. Без него заявка сохранится без связи с визитом.</p>
    </details>
  </section>;
}
