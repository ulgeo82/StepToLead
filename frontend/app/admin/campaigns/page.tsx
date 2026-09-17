"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Campaign } from "@/lib/api";

export default function CampaignsPage() {
  const [items, setItems] = useState<Campaign[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [editing, setEditing] = useState<Campaign | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api<Campaign[]>("/campaigns").then(setItems).catch((e) => setMessage(e.message)), []);
  useEffect(() => { load(); }, [load]);

  function showCreate() { setEditing(null); setName(""); setDescription(""); setOpen(true); }
  function showEdit(item: Campaign) { setEditing(item); setName(item.name); setDescription(item.description || ""); setOpen(true); }

  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage("");
    try {
      await api(editing ? `/campaigns/${editing.id}` : "/campaigns", { method: editing ? "PATCH" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, description: description || null }) });
      setName(""); setDescription(""); setOpen(false); await load();
    } catch (error) { setMessage((error as Error).message); } finally { setBusy(false); }
  }

  async function remove(item: Campaign) {
    if (!window.confirm(`Удалить кампанию «${item.name}» и всех её лидов?`)) return;
    try { await api(`/campaigns/${item.id}`, { method: "DELETE" }); await load(); }
    catch (error) { setMessage((error as Error).message); }
  }

  async function upload(campaignId: number, file?: File) {
    if (!file) return;
    const data = new FormData(); data.append("file", file); setMessage("Импортируем базу…");
    try {
      const result = await api<{ imported: number; skipped: number }>(`/campaigns/${campaignId}/import`, { method: "POST", body: data });
      setMessage(`Готово: импортировано ${result.imported}, пропущено ${result.skipped}`); await load();
    } catch (error) { setMessage((error as Error).message); }
  }

  return <div className="page"><header className="pageHeader"><div><p className="eyebrow">Рабочее пространство</p><h1>Кампании</h1><p className="subtitle">Организуйте лидов по отдельным сценариям коммуникации.</p></div><button className="button primary" onClick={showCreate}>+ Новая кампания</button></header>
    {message && <div className="notice">{message}<button onClick={() => setMessage("")}>×</button></div>}
    <section className="section"><div className="sectionHead"><h2>Все кампании</h2><span>{items.length} всего</span></div>
      <div className="campaignGrid">{items.map((item, index) => <article className="campaignCard" key={item.id}><div className="campaignTop"><span className="campaignNo">{String(index + 1).padStart(2, "0")}</span><div className="cardActions"><button onClick={() => showEdit(item)} aria-label="Редактировать">✎</button><button onClick={() => remove(item)} aria-label="Удалить">×</button></div></div><h3><a href={`/admin/campaigns/${item.id}`} style={{ color: "inherit", textDecoration: "none" }}>{item.name} ↗</a></h3><a className="button secondary" href={`/admin/campaigns/${item.id}`} style={{ marginBottom: 16 }}>Настроить и запустить</a><p>{item.description || "Без описания"}</p><div className="campaignMeta"><div><strong>{item.leads_count}</strong><span>лидов</span></div><label className="uploadButton">Импорт CSV/XLSX<input type="file" accept=".csv,.xlsx" onChange={(e) => upload(item.id, e.target.files?.[0])} /></label></div></article>)}
      {!items.length && <div className="empty"><span>◫</span><h3>Кампаний пока нет</h3><p>Создайте первую — это займёт меньше минуты.</p></div>}</div>
    </section>
    {open && <div className="modalBackdrop" onMouseDown={() => setOpen(false)}><form className="modal" onSubmit={save} onMouseDown={(e) => e.stopPropagation()}><div className="modalHead"><div><p className="eyebrow">{editing ? "Редактирование" : "Новая сущность"}</p><h2>{editing ? "Изменить кампанию" : "Создать кампанию"}</h2></div><button type="button" className="close" onClick={() => setOpen(false)}>×</button></div><label>Название<input required minLength={2} value={name} onChange={(e) => setName(e.target.value)} placeholder="Например, SaaS-агентства · август" /></label><label>Описание<textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Кому и с каким предложением пишем" /></label><button className="button primary full" disabled={busy}>{busy ? "Сохраняем…" : editing ? "Сохранить изменения" : "Создать кампанию"}</button></form></div>}
  </div>;
}
