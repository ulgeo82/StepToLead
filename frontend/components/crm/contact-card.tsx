"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { money } from "@/components/reporting";
import { contactLinks, Deal, day, request, Task, typeLabels, when } from "./shared";

type Card = { id: number; name: string; phones: string[]; emails: string[]; telegram: string | null; company: string | null;
  position: string | null; notes: string | null; tags: string[]; deals: Deal[]; open_tasks: Task[];
  lifetime_value: number; purchases: number; duplicates: { id: number; name: string; phones: string[]; emails: string[] }[] };

const list = (value: FormDataEntryValue | null) => String(value || "").split(/[,;\n]/).map(v => v.trim()).filter(Boolean);

export function ContactCard({ contactId, can, onClose, onOpenDeal, onChanged }: { contactId: number;
  can: (name: string) => boolean; onClose: () => void; onOpenDeal: (id: number) => void; onChanged: () => void }) {
  const [card, setCard] = useState<Card | null>(null);
  const [edit, setEdit] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => request<Card>(`/crm/contacts/${contactId}`, "GET").then(setCard).catch(e => setError(e.message)), [contactId]);
  useEffect(() => { setCard(null); setEdit(false); load(); }, [load]);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget); setBusy(true); setError("");
    try { await request(`/crm/contacts/${contactId}`, "PATCH", { name: f.get("name"), phones: list(f.get("phones")), emails: list(f.get("emails")),
      telegram: f.get("telegram") || null, company: f.get("company") || null, position: f.get("position") || null,
      notes: f.get("notes") || null, tags: list(f.get("tags")) });
      setEdit(false); await load(); onChanged(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function merge(sourceId: number, name: string) {
    if (!window.confirm(`Объединить «${name}» с этим контактом? Сделки, задачи и заявки перейдут сюда, дубль будет удалён.`)) return;
    setBusy(true); setError("");
    try { await request(`/crm/contacts/${contactId}/merge`, "POST", { source_contact_id: sourceId }); await load(); onChanged(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="resultModal crmContactModal" role="dialog"><header><h2>{card?.name || "Контакт"}</h2><button type="button" onClick={onClose}>×</button></header>
      {!card ? <p>{error || "Загружаем…"}</p> : <>
        {error && <p className="crmFormError">{error}</p>}
        <div className="crmContactStats"><div><span>Сделок</span><b>{card.deals.length}</b></div><div><span>Покупок</span><b>{card.purchases}</b></div><div><span>Принёс выручки</span><b>{money(card.lifetime_value)}</b></div></div>
        {card.duplicates.length > 0 && <div className="crmDupes"><b>Похоже, есть дубли:</b>{card.duplicates.map(d => <span key={d.id}>{d.name} · {d.phones[0] || d.emails[0]}{can("view_all_deals") && can("edit_deal") && <button disabled={busy} onClick={() => merge(d.id, d.name)}>Объединить</button>}</span>)}</div>}
        {edit ? <form className="crmContactForm" onSubmit={save}>
          <label>Имя<input name="name" required minLength={2} defaultValue={card.name}/></label>
          <div className="resultModalGrid"><label>Компания<input name="company" defaultValue={card.company || ""}/></label><label>Должность<input name="position" defaultValue={card.position || ""}/></label></div>
          <label>Телефоны (через запятую)<input name="phones" defaultValue={card.phones.join(", ")}/></label>
          <label>Email (через запятую)<input name="emails" defaultValue={card.emails.join(", ")}/></label>
          <div className="resultModalGrid"><label>Telegram<input name="telegram" defaultValue={card.telegram || ""} placeholder="@username"/></label><label>Теги (через запятую)<input name="tags" defaultValue={card.tags.join(", ")}/></label></div>
          <label>Заметки<textarea name="notes" defaultValue={card.notes || ""} placeholder="Важное о клиенте: ЛПР, бюджет, предпочтения"/></label>
          <div className="crmFormActions"><button type="button" className="crmGhost" onClick={() => setEdit(false)}>Отмена</button><button className="resultPrimary" disabled={busy}>Сохранить</button></div></form>
        : <div className="crmContactInfo">
          {(card.company || card.position) && <p className="crmMuted">{[card.position, card.company].filter(Boolean).join(" · ")}</p>}
          {card.phones.map(p => <p key={p}><a href={`tel:${p}`}>{p}</a></p>)}{card.emails.map(m => <p key={m}><a href={`mailto:${m}`}>{m}</a></p>)}
          {card.telegram && <p>Telegram: {card.telegram}</p>}
          <div className="crmQuick">{(() => { const l = contactLinks(card); return <>{l.call && <a href={l.call}>📞 Позвонить</a>}{l.whatsapp && <a href={l.whatsapp} target="_blank" rel="noopener noreferrer">WhatsApp</a>}{l.telegram && <a href={l.telegram} target="_blank" rel="noopener noreferrer">Telegram</a>}{l.email && <a href={l.email}>✉ Email</a>}</>; })()}
            {can("edit_deal") && <button onClick={() => setEdit(true)}>✎ Редактировать</button>}</div>
          {card.tags.length > 0 && <div className="crmTags">{card.tags.map(t => <span key={t}>#{t}</span>)}</div>}
          {card.notes && <p className="crmPre crmNotes">{card.notes}</p>}</div>}
        <h3 className="crmSub">Сделки клиента</h3>
        <div className="crmContactDeals">{card.deals.map(d => <button key={d.id} onClick={() => onOpenDeal(d.id)}><span><b>{d.name}</b><small>{day(d.created_at)} · {d.stage_name}</small></span><strong>{money(d.amount)}</strong></button>)}
          {!card.deals.length && <p className="crmMuted">Сделок нет</p>}</div>
        {card.open_tasks.length > 0 && <><h3 className="crmSub">Открытые задачи</h3>{card.open_tasks.map(t => <p key={t.id} className="crmKV"><span>{when(t.due_at)}</span>{typeLabels[t.type_code] || t.type_code}: {t.title}</p>)}</>}
      </>}</div></div>;
}
