"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, Campaign, Lead, LeadStatus } from "@/lib/api";

const labels: Record<LeadStatus, string> = {
  new: "Новый", queued: "В работе", sent: "Отправлено", replied: "Ответил",
  interested: "Интерес", not_interested: "Не интересно", handoff: "Менеджеру", failed: "Ошибка",
};
const statuses = Object.keys(labels) as LeadStatus[];

export default function LeadsPage() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [campaign, setCampaign] = useState("");
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [notice, setNotice] = useState("");
  const [activeLead, setActiveLead] = useState<Lead | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const query = new URLSearchParams();
      if (campaign) query.set("campaign_id", campaign);
      if (status) query.set("lead_status", status);
      const [leadData, campaignData] = await Promise.all([
        api<Lead[]>(`/leads?${query}`), api<Campaign[]>("/campaigns"),
      ]);
      setLeads(leadData); setCampaigns(campaignData);
    } catch (error) { setNotice((error as Error).message); }
  }, [campaign, status]);

  useEffect(() => { load(); }, [load]);

  const visible = useMemo(() => {
    const query = search.toLowerCase().replace("@", "").trim();
    return leads.filter((lead) => lead.username.includes(query));
  }, [leads, search]);

  const summary = useMemo(() => ({
    new: leads.filter((lead) => ["new", "queued"].includes(lead.status)).length,
    sent: leads.filter((lead) => lead.status === "sent").length,
    replies: leads.filter((lead) => ["replied", "interested", "handoff"].includes(lead.status)).length,
  }), [leads]);

  const campaignName = (id: number) => campaigns.find((item) => item.id === id)?.name || `#${id}`;

  async function changeStatus(id: number, next: LeadStatus) {
    try {
      const updated = await api<Lead>(`/leads/${id}/status`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: next }),
      });
      setLeads((current) => current.map((lead) => lead.id === id ? updated : lead));
      setActiveLead((current) => current?.id === id ? updated : current);
    } catch (error) { setNotice((error as Error).message); }
  }

  function openComposer(lead: Lead) { setActiveLead(lead); setDraft(lead.first_message); setNotice(""); }

  async function saveDraft() {
    if (!activeLead || draft.trim() === activeLead.first_message) return activeLead;
    const updated = await api<Lead>(`/leads/${activeLead.id}/message`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ first_message: draft.trim() }),
    });
    setLeads((current) => current.map((lead) => lead.id === updated.id ? updated : lead));
    setActiveLead(updated);
    return updated;
  }

  async function writeClipboard(text: string) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
    const helper = document.createElement("textarea");
    helper.value = text; helper.style.position = "fixed"; helper.style.opacity = "0";
    document.body.appendChild(helper); helper.select(); document.execCommand("copy"); helper.remove();
  }

  async function copyMessage() {
    if (!activeLead || !draft.trim()) return;
    setBusy(true);
    try {
      await saveDraft();
      await writeClipboard(draft.trim());
      setNotice(`Сообщение для @${activeLead.username} скопировано`);
    } catch (error) { setNotice((error as Error).message || "Не удалось скопировать сообщение"); }
    finally { setBusy(false); }
  }

  async function openTelegram() {
    if (!activeLead) return;
    setBusy(true);
    try {
      const copyOperation = writeClipboard(draft.trim());
      window.open(`https://t.me/${encodeURIComponent(activeLead.username)}`, "_blank");
      await saveDraft();
      await copyOperation;
      if (activeLead.status === "new") await changeStatus(activeLead.id, "queued");
      setNotice(`Чат @${activeLead.username} открыт, сообщение уже скопировано`);
    } catch (error) { setNotice((error as Error).message || "Не удалось открыть Telegram"); }
    finally { setBusy(false); }
  }

  async function markSent() {
    if (!activeLead) return;
    await changeStatus(activeLead.id, "sent");
    setNotice(`@${activeLead.username} отмечен как отправленный`);
    setActiveLead(null);
  }

  function openNext() {
    const next = visible.find((lead) => ["new", "queued"].includes(lead.status));
    if (next) openComposer(next); else setNotice("В текущем списке нет новых лидов");
  }

  async function removeLead(lead: Lead) {
    if (!window.confirm(`Удалить @${lead.username}?`)) return;
    try { await api(`/leads/${lead.id}`, { method: "DELETE" }); await load(); }
    catch (error) { setNotice((error as Error).message); }
  }

  return <div className="page manualLeadsPage">
    <header className="pageHeader"><div><p className="eyebrow">Ручной запуск</p><h1>Рабочая очередь</h1><p className="subtitle">Открывайте чат, вставляйте готовое сообщение и фиксируйте результат.</p></div><button className="button primary" onClick={openNext}>Начать со следующего <span>→</span></button></header>
    {notice && <div className="notice">{notice}<button onClick={() => setNotice("")}>×</button></div>}
    <section className="manualSummary"><article><span className="summaryIcon violet">01</span><div><small>Ожидают отправки</small><strong>{summary.new}</strong></div></article><article><span className="summaryIcon blue">02</span><div><small>Отправлено</small><strong>{summary.sent}</strong></div></article><article><span className="summaryIcon lime">03</span><div><small>Есть ответы</small><strong>{summary.replies}</strong></div></article><div className="manualMode"><i />Без Telegram API</div></section>
    <section className="tablePanel manualTable"><div className="filters"><div className="search"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по username" /></div><select value={campaign} onChange={(event) => setCampaign(event.target.value)}><option value="">Все кампании</option>{campaigns.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">Все статусы</option>{statuses.map((item) => <option key={item} value={item}>{labels[item]}</option>)}</select></div>
      <div className="tableWrap"><table><thead><tr><th>Лид</th><th>Сообщение</th><th>Кампания</th><th>Статус</th><th>Действие</th></tr></thead><tbody>{visible.map((lead) => <tr key={lead.id}><td><div className="leadName"><span>{lead.username[0]?.toUpperCase()}</span><div><strong>@{lead.username}</strong><small>Telegram</small></div></div></td><td className="messageCell">{lead.first_message}</td><td><span className="campaignTag">{campaignName(lead.campaign_id)}</span></td><td><select className={`status status-${lead.status}`} value={lead.status} onChange={(event) => changeStatus(lead.id, event.target.value as LeadStatus)}>{statuses.map((item) => <option key={item} value={item}>{labels[item]}</option>)}</select></td><td><div className="leadActions"><button className="writeButton" onClick={() => openComposer(lead)}>Написать <span>↗</span></button><button className="rowDelete" onClick={() => removeLead(lead)} aria-label={`Удалить @${lead.username}`}>×</button></div></td></tr>)}</tbody></table>{!visible.length && <div className="empty compact"><span>◎</span><h3>Лиды не найдены</h3><p>Импортируйте CSV/XLSX на странице кампаний.</p></div>}</div>
    </section>
    {activeLead && <div className="modalBackdrop" onMouseDown={() => setActiveLead(null)}><section className="modal composeModal" onMouseDown={(event) => event.stopPropagation()}><div className="modalHead"><div><p className="eyebrow">Сообщение лиду</p><h2>@{activeLead.username}</h2></div><button type="button" className="close" onClick={() => setActiveLead(null)}>×</button></div><div className="composeCampaign"><span>Кампания</span><strong>{campaignName(activeLead.campaign_id)}</strong><span className={`liveStatus ${activeLead.status}`}><i />{labels[activeLead.status]}</span></div><label className="composeLabel">Текст сообщения<textarea value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={4096} autoFocus /></label><div className="composeHint"><span>1</span><p><strong>Откройте чат</strong><small>Текст автоматически скопируется</small></p><i>→</i><span>2</span><p><strong>Вставьте и отправьте</strong><small>Вернитесь сюда после отправки</small></p></div><div className="composeActions"><button className="button secondary" onClick={copyMessage} disabled={busy || !draft.trim()}>Скопировать</button><button className="button telegramButton" onClick={openTelegram} disabled={busy || !draft.trim()}>Открыть Telegram ↗</button><button className="button primary" onClick={markSent} disabled={busy}>Готово, отправлено</button></div></section></div>}
  </div>;
}
