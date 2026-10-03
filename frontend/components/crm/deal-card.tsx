"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { money } from "@/components/reporting";
import { DealChats } from "./inbox";
import { CallButton, DealCalls } from "./telephony";
import { CompleteTaskModal, NewTaskModal } from "./task-modals";
import { contactLinks, CustomField, Deal, day, days, describeActivity, Pipeline, priorityLabels, request,
  Source, stateLabels, Task, Team, typeLabels, when } from "./shared";

type Reason = { id: number; label: string };
type Composer = "COMMENT" | "CALL" | "MESSAGE" | "MEETING";
const composerLabels: Record<Composer, string> = { COMMENT: "Примечание", CALL: "Звонок", MESSAGE: "Сообщение", MEETING: "Встреча" };

export function clientFields(data: Record<string, unknown>) {
  const fields: [string, string[]][] = [
    ["Имя", ["name", "full_name"]], ["Способ связи", ["contact_method"]], ["Контакт", ["contact"]],
    ["Телефон", ["phone"]], ["Email", ["email"]], ["Сайт клиента", ["website"]], ["Комментарий", ["comment", "notes"]],
    ["Объявление Авито", ["item_title"]], ["Ссылка на объявление", ["item_url"]], ["Цена в объявлении", ["item_price"]],
    ["Профиль покупателя", ["buyer_profile_url"]], ["Запись звонка", ["call_record_url"]],
  ];
  const seen = new Set<string>();
  return fields.flatMap(([label, keys]) => {
    const value = keys.map(key => String(data[key] ?? "").trim()).find(Boolean);
    if (!value || seen.has(value)) return [];
    seen.add(value); return [{ label, value }];
  });
}

const platformNames: Record<string, string> = { avito_items: "Авито · Объявления", avito_ads: "Авито Реклама",
  yandex: "Яндекс Директ", vk_ads: "VK Реклама", meta: "Meta Ads" };
const linkify = (value: string) => /^https?:\/\//.test(value)
  ? <a href={value} target="_blank" rel="noopener noreferrer">{value}</a> : value;

export function DealCard({ dealId, projectId, pipelines, team, sources, fields, lostReasons, can, onClose, onChanged, onOpenContact, canDial = false }: {
  dealId: number; projectId: number; pipelines: Pipeline[]; team: Team[]; sources: Source[]; fields: CustomField[];
  lostReasons: Reason[]; can: (name: string) => boolean; onClose: () => void; onChanged: () => void;
  onOpenContact: (id: number) => void; canDial?: boolean }) {
  const [deal, setDeal] = useState<Deal | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState<"task" | "sale" | "lost" | null>(null);
  const [completing, setCompleting] = useState<Task | null>(null);
  const [pendingLost, setPendingLost] = useState<number | null>(null);
  const [composer, setComposer] = useState<Composer>("COMMENT");
  const [text, setText] = useState("");
  const [tag, setTag] = useState("");
  const load = useCallback(() => request<Deal>(`/crm/deals/${dealId}`, "GET").then(setDeal).catch(e => setError(e.message)), [dealId]);
  useEffect(() => { setDeal(null); setError(""); setNotice(""); load(); }, [load]);
  useEffect(() => { const esc = (e: KeyboardEvent) => { if (e.key === "Escape" && !modal && !completing) onClose(); };
    window.addEventListener("keydown", esc); return () => window.removeEventListener("keydown", esc); }, [modal, completing, onClose]);

  async function run<T>(action: () => Promise<T>, message?: string): Promise<T | null> {
    setBusy(true); setError(""); setNotice("");
    try { const value = await action(); await load(); onChanged(); if (message) setNotice(message); return value; }
    catch (e) { setError((e as Error).message); return null; } finally { setBusy(false); }
  }
  const patch = (body: Record<string, unknown>, message?: string) => run(() => request(`/crm/deals/${dealId}`, "PATCH", body), message);

  async function moveTo(stageId: number, lostReasonId?: number, lostComment?: string) {
    if (!deal || stageId === deal.stage_id) return;
    const stage = pipeline?.stages.find(s => s.id === stageId);
    if (stage?.analytics_type === "LOST" && !lostReasonId) { setPendingLost(stageId); setModal("lost"); return; }
    const result = await run(() => request<{ sale_required: boolean; automations?: string[] }>(`/crm/deals/${dealId}/move`, "POST",
      { stage_id: stageId, lost_reason_id: lostReasonId, lost_comment: lostComment }));
    if (result?.automations?.length) setNotice(`Автоматизация: ${result.automations.join("; ")}`);
    if (result?.sale_required) setModal("sale");
  }

  if (!deal) return <aside className="crmCardPanel"><div className="crmCardLoading">{error || "Загружаем сделку…"}</div></aside>;
  const pipeline = pipelines.find(p => p.id === deal.pipeline_id);
  const stages = (pipeline?.stages || []).filter(s => s.analytics_type !== "LOST");
  const lostStage = pipeline?.stages.find(s => s.analytics_type === "LOST");
  const currentIndex = stages.findIndex(s => s.id === deal.stage_id);
  const links = contactLinks(deal.contact);
  const openTasks = (deal.tasks || []).filter(t => t.status === "OPEN");
  const snap = deal.attribution_snapshot || {};
  const form = deal.form_data || {};
  const responseMinutes = deal.first_response_at ? Math.round((new Date(deal.first_response_at).getTime() - new Date(deal.created_at).getTime()) / 60000) : null;
  const closed = deal.analytics_type === "WON" || deal.analytics_type === "LOST";

  async function submitComposer(event: FormEvent) {
    event.preventDefault(); if (!text.trim()) return;
    const ok = composer === "COMMENT"
      ? await run(() => request(`/crm/deals/${dealId}/comments`, "POST", { text }))
      : await run(() => request(`/crm/deals/${dealId}/touch`, "POST", { kind: composer, text }));
    if (ok) setText("");
  }
  async function logTouch(kind: "CALL" | "MESSAGE") {
    await run(() => request(`/crm/deals/${dealId}/touch`, "POST", { kind, text: "" }),
      kind === "CALL" ? "Звонок записан в историю" : "Сообщение записано в историю");
  }
  async function submitSale(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget);
    const ok = await run(() => request(`/crm/deals/${dealId}/sales`, "POST", { amount: Number(f.get("amount")),
      occurred_at: f.get("occurred_at") ? new Date(String(f.get("occurred_at"))).toISOString() : null, comment: f.get("comment") || null }),
      "Продажа подтверждена — она попала в выручку и ROMI");
    if (ok) setModal(null);
  }
  async function submitLost(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!pendingLost) return; const f = new FormData(event.currentTarget);
    setModal(null); await moveTo(pendingLost, Number(f.get("reason")), String(f.get("comment") || "")); setPendingLost(null);
  }

  return <aside className="crmCardPanel" role="dialog" aria-label={`Сделка ${deal.name}`}>
    <header className="crmCardHeader">
      <div className="crmCardTitle"><small>Сделка #{deal.id} · создана {day(deal.created_at)}{deal.source_name ? ` · ${deal.source_name}` : ""}</small>
        {can("edit_deal") ? <input className="crmTitleInput" defaultValue={deal.name} key={`n${deal.name}`} onBlur={e => { const v = e.target.value.trim(); if (v.length >= 2 && v !== deal.name) patch({ name: v }); }}/> : <h2>{deal.name}</h2>}
        <div className="crmCardMeta"><strong>{money(deal.amount)}</strong><span className={`crmTaskBadge ${deal.task_state.toLowerCase()}`}>{stateLabels[deal.task_state]}</span>
          <span>на этапе {days(deal.days_in_stage)}</span>{(deal.idle_days ?? 0) >= 3 && !closed && <span className="crmIdle">без движения {days(deal.idle_days)}</span>}
          {responseMinutes != null && <span title="Время от появления сделки до первого действия менеджера">реакция {responseMinutes < 60 ? `${responseMinutes} мин` : `${Math.round(responseMinutes / 60)} ч`}</span>}
          {deal.archived_at && <span className="crmArchived">в архиве</span>}</div></div>
      <button className="crmClose" onClick={onClose} aria-label="Закрыть">×</button></header>

    <nav className="crmRibbon" aria-label="Этапы">{stages.map((stage, index) => <button key={stage.id} disabled={!can("move_deal") || busy || !!deal.archived_at}
      className={`${index < currentIndex ? "passed" : ""} ${stage.id === deal.stage_id ? "current" : ""}`} style={{ ["--stage" as string]: stage.color }}
      onClick={() => moveTo(stage.id)} title={`Перевести на этап «${stage.name}»`}>{stage.name}</button>)}
      {lostStage && <button className={`lost ${deal.stage_id === lostStage.id ? "current" : ""}`} disabled={!can("move_deal") || busy || !!deal.archived_at} onClick={() => moveTo(lostStage.id)}>✕ {lostStage.name}</button>}</nav>

    <div className="crmQuick">{links.call && (canDial ? <CallButton projectId={projectId} phone={deal.contact.phones[0]} dealId={deal.id} canDial onNotice={(t, failed) => failed ? setError(t) : setNotice(t)}/>
      : <a href={links.call} onClick={() => can("edit_deal") && logTouch("CALL")}>📞 Позвонить</a>)}
      {links.whatsapp && <a href={links.whatsapp} target="_blank" rel="noopener noreferrer" onClick={() => can("edit_deal") && logTouch("MESSAGE")}>WhatsApp</a>}
      {links.telegram && <a href={links.telegram} target="_blank" rel="noopener noreferrer" onClick={() => can("edit_deal") && logTouch("MESSAGE")}>Telegram</a>}
      {links.email && <a href={links.email}>✉ Email</a>}
      {typeof form.chat_url === "string" && <a href={form.chat_url} target="_blank" rel="noopener noreferrer">Чат Авито</a>}
      {can("manage_tasks") && <button onClick={() => setModal("task")}>＋ Задача</button>}
      {can("create_sale") && deal.analytics_type !== "LOST" && <button onClick={() => setModal("sale")}>₽ Продажа</button>}</div>
    {error && <div className="crmCardError">{typeof error === "string" ? error : "Ошибка"} <button onClick={() => setError("")}>×</button></div>}
    {notice && <div className="crmCardNotice">{notice}</div>}

    <div className="crmCardBody">
      <section className="crmCardMain">
        <div className="crmBlock"><div className="crmBlockHead"><h3>Контакт</h3><button className="crmLinkButton" onClick={() => onOpenContact(deal.contact.id)}>Карточка →</button></div>
          <button className="crmContactName" onClick={() => onOpenContact(deal.contact.id)}>{deal.contact.name}</button>
          {deal.contact.company && <p className="crmMuted">{deal.contact.company}</p>}
          {deal.contact.phones.map(p => <p key={p}><a href={`tel:${p}`}>{p}</a></p>)}
          {deal.contact.emails.map(e => <p key={e}><a href={`mailto:${e}`}>{e}</a></p>)}
          {deal.contact.telegram && <p>Telegram: {deal.contact.telegram}</p>}</div>

        <div className="crmBlock"><h3>Сделка</h3><div className="crmFieldGrid">
          <label>Ответственный<select disabled={!can("edit_deal")} value={deal.responsible_user_id || ""} onChange={e => patch({ responsible_user_id: Number(e.target.value) || null }, "Ответственный изменён")}><option value="">Не назначен</option>{team.map(m => <option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label>
          <label>Бюджет, ₽<input type="number" min="0" disabled={!can("edit_deal")} defaultValue={deal.amount ?? ""} key={`a${deal.amount}`} onBlur={e => { const v = e.target.value ? Number(e.target.value) : null; if (v !== deal.amount) patch({ amount: v }); }}/></label>
          <label>Источник<select disabled={!can("change_attribution")} value={deal.source_id || ""} onChange={e => patch({ source_id: Number(e.target.value) || null }, "Источник скорректирован")}><option value="">Не определено</option>{sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
          {fields.map(field => <label key={field.id}>{field.name}{field.field_type === "SELECT" ? <select disabled={!can("edit_deal")} value={String(deal.custom_fields[field.key] ?? "")} onChange={e => patch({ custom_fields: { ...deal.custom_fields, [field.key]: e.target.value || null } })}><option value="">—</option>{(field.options || []).map(o => <option key={o}>{o}</option>)}</select>
            : field.field_type === "BOOLEAN" ? <select disabled={!can("edit_deal")} value={deal.custom_fields[field.key] == null ? "" : String(deal.custom_fields[field.key])} onChange={e => patch({ custom_fields: { ...deal.custom_fields, [field.key]: e.target.value === "" ? null : e.target.value === "true" } })}><option value="">—</option><option value="true">Да</option><option value="false">Нет</option></select>
            : <input disabled={!can("edit_deal")} key={`${field.key}${String(deal.custom_fields[field.key] ?? "")}`} type={field.field_type === "NUMBER" || field.field_type === "MONEY" ? "number" : field.field_type === "DATE" ? "date" : "text"}
              defaultValue={Array.isArray(deal.custom_fields[field.key]) ? (deal.custom_fields[field.key] as string[]).join(", ") : String(deal.custom_fields[field.key] ?? "")}
              onBlur={e => { const raw = e.target.value; const value = !raw ? null : field.field_type === "NUMBER" || field.field_type === "MONEY" ? Number(raw) : field.field_type === "MULTISELECT" ? raw.split(",").map(v => v.trim()).filter(Boolean) : raw;
                if (JSON.stringify(value) !== JSON.stringify(deal.custom_fields[field.key] ?? null)) patch({ custom_fields: { ...deal.custom_fields, [field.key]: value } }); }}/>}</label>)}</div>
          <div className="crmTags">{deal.tags.map(t => <span key={t}>#{t}{can("edit_deal") && <button onClick={() => patch({ tags: deal.tags.filter(x => x !== t) })} aria-label={`Удалить тег ${t}`}>×</button>}</span>)}
            {can("edit_deal") && <form onSubmit={e => { e.preventDefault(); if (tag.trim()) { patch({ tags: [...deal.tags, tag.trim()] }); setTag(""); } }}><input value={tag} onChange={e => setTag(e.target.value)} placeholder="＋ тег" maxLength={40}/></form>}</div>
          {deal.analytics_type === "LOST" && <p className="crmLostNote">Причина отказа: {lostReasons.find(r => r.id === deal.lost_reason_id)?.label || "—"}{deal.lost_comment ? ` · ${deal.lost_comment}` : ""}</p>}
          {can("archive_deal") && (deal.archived_at || closed) && <button className="crmLinkButton" disabled={busy} onClick={() => run(() => request(`/crm/deals/${dealId}/${deal.archived_at ? "restore" : "archive"}`, "POST", {}), deal.archived_at ? "Сделка возвращена из архива" : "Сделка перенесена в архив")}>{deal.archived_at ? "Вернуть из архива" : "Перенести в архив"}</button>}</div>

        <div className="crmBlock"><h3>Откуда клиент</h3><p><b>{deal.source_name || "Источник не определён"}</b></p>
          {[["platform", "Платформа"], ["ad_account", "Кабинет"], ["utm_source", "utm_source"], ["utm_medium", "utm_medium"], ["utm_campaign", "Кампания"], ["utm_content", "Объявление"], ["utm_term", "Ключ"], ["landing_url", "Страница"], ["referer", "Откуда перешёл"]].map(([key, label]) => {
            const raw = snap[key] || form[key] || (key === "landing_url" ? form.page_url : null);
            const value = key === "platform" && raw ? platformNames[String(raw)] || raw : raw;
            return value ? <p key={key} className="crmKV"><span>{label}</span>{linkify(String(value))}</p> : null; })}</div>

        {deal.form_data && <div className="crmBlock"><h3>Что написал клиент</h3>{clientFields(deal.form_data).map(({ label, value }) => <p key={label} className="crmKV"><span>{label}</span><span className="crmPre">{linkify(value)}</span></p>)}</div>}

        <div className="crmBlock"><h3>Продажи</h3>{deal.sales?.length ? deal.sales.map(s => <p key={s.id} className="crmKV"><span>{day(s.occurred_at)}</span><b>{money(s.amount)}</b></p>) : <p className="crmMuted">Продажа ещё не подтверждена. Выручка в аналитике считается только по подтверждённым продажам.</p>}</div>
      </section>

      <section className="crmCardFeed">
        <div className="crmBlock"><div className="crmBlockHead"><h3>Задачи</h3>{can("manage_tasks") && <button className="crmLinkButton" onClick={() => setModal("task")}>＋ Задача</button>}</div>
          {!openTasks.length && !closed && <p className="crmWarnLine">У сделки нет следующего шага. Поставьте задачу — иначе клиент «остынет».</p>}
          {openTasks.map(t => { const overdue = new Date(t.due_at).getTime() < Date.now();
            return <div key={t.id} className={`crmTaskItem ${overdue ? "overdue" : ""}`}><div><b>{typeLabels[t.type_code] || t.type_code}: {t.title}</b>
              <small>{when(t.due_at)}{t.priority === "HIGH" ? ` · ${priorityLabels.HIGH}` : ""}{t.description ? ` · ${t.description}` : ""}</small></div>
              {can("manage_tasks") && <button onClick={() => setCompleting(t)}>Завершить</button>}</div>; })}</div>

        <DealCalls dealId={deal.id} refreshKey={deal.activities?.length}/>

        <DealChats dealId={deal.id} projectId={projectId} canWrite={can("edit_deal")} phone={deal.contact.phones[0] || null}/>

        <div className="crmBlock crmTimelineBlock"><h3>История</h3>
          {can("edit_deal") && <form className="crmComposer" onSubmit={submitComposer}><div className="crmComposerTabs">{(Object.keys(composerLabels) as Composer[]).map(k => <button type="button" key={k} className={composer === k ? "active" : ""} onClick={() => setComposer(k)}>{composerLabels[k]}</button>)}</div>
            <textarea value={text} onChange={e => setText(e.target.value)} placeholder={composer === "COMMENT" ? "Примечание для команды" : composer === "CALL" ? "Итог звонка: о чём договорились" : composer === "MESSAGE" ? "Что написали клиенту" : "Итог встречи"}/>
            <button className="crmPrimary" disabled={busy || !text.trim()}>Добавить</button></form>}
          <ol className="crmTimeline">{[...(deal.activities || [])].map(a => { const item = describeActivity(a);
            return <li key={a.id} className={item.tone || ""}><i>{item.icon}</i><div><p className="crmPre">{item.text}</p><small>{when(a.created_at)} · {a.actor_name || "Система"}</small></div></li>; })}
          </ol></div>
      </section>
    </div>

    {completing && <CompleteTaskModal task={completing} team={team} onClose={() => setCompleting(null)} onDone={async () => { setCompleting(null); await load(); onChanged(); setNotice("Задача завершена"); }}/>}
    {modal === "task" && <NewTaskModal projectId={projectId} dealId={deal.id} team={team} onClose={() => setModal(null)} onDone={async () => { setModal(null); await load(); onChanged(); }}/>}
    {modal === "sale" && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) setModal(null); }}><form className="resultModal crmModal" onSubmit={submitSale}><header><h2>Подтвердить продажу</h2><button type="button" onClick={() => setModal(null)}>×</button></header>
      <p className="crmModalLead">Выручка попадёт в аналитику, ROMI и рекламные отчёты по источнику сделки.</p>
      <label>Сумма продажи, ₽<input name="amount" type="number" min="0.01" step="0.01" required defaultValue={deal.amount ?? ""}/></label><label>Дата<input name="occurred_at" type="datetime-local"/></label><label>Комментарий<textarea name="comment"/></label>
      <button className="resultPrimary" disabled={busy}>Подтвердить</button></form></div>}
    {modal === "lost" && <div className="resultModalBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) { setModal(null); setPendingLost(null); } }}><form className="resultModal crmModal" onSubmit={submitLost}><header><h2>Почему сделка проиграна?</h2><button type="button" onClick={() => { setModal(null); setPendingLost(null); }}>×</button></header>
      <p className="crmModalLead">Причины отказов — главный источник роста конверсии. Сделка останется в истории и аналитике.</p>
      <label>Причина<select name="reason" required><option value="">Выберите причину</option>{lostReasons.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}</select></label><label>Комментарий<textarea name="comment" placeholder="Что именно не подошло?"/></label>
      <button className="resultPrimary" disabled={busy}>Сохранить</button></form></div>}
  </aside>;
}
