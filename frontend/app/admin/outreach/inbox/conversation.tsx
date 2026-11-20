"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { date, message } from "../../leadgen/shared";
import styles from "../../leadgen/leadgen.module.css";
import { Formatted, hasMarkup } from "../email/format";
import { inboxMutation, inboxRequest } from "./inbox-api";
import type {
  Conversation, InboxConversation, InboxLabel, InboxMessage, Operator, ReplyTemplate,
} from "./inbox-types";
import { activeLock, channelNames, inboxLabels, labelKeys, renderReplyTemplate } from "./inbox-utils";
import { useReplyLock } from "./reply-lock";
import { RichComposer } from "@/components/rich-composer";
import { inboxRequest as rawRequest } from "./inbox-api";
import { useVisiblePoll } from "./visible-poll";
import css from "./inbox.module.css";

const authors = {
  lead: "Контакт", agency: "Оператор агентства", client: "Сотрудник клиента", auto: "Автоматически", system: "Система",
};
const delivery = { sent: "Отправлено", delivered: "Доставлено", failed: "Не отправлено", received: "Получено", unknown: "Неизвестно, ушло ли" };

export function ConversationPanel({ path, workspace, onChanged, onNext }: {
  path: string;
  workspace: number | null;
  onChanged: () => void;
  onNext: () => void;
}) {
  const [full, setFull] = useState<InboxConversation | null>(null);
  const [revision, setRevision] = useState(0);
  const [templates, setTemplates] = useState<ReplyTemplate[]>([]);
  const [operators, setOperators] = useState<Operator[]>([]);
  const [assigning, setAssigning] = useState(false);
  const [assignee, setAssignee] = useState("");
  const [draft, setDraft] = useState("");
  const [editor, setEditor] = useState(true);
  const [focusRequested, setFocusRequested] = useState(false);
  const [templateId, setTemplateId] = useState<number>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const replyRef = useRef<HTMLTextAreaElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const busyRef = useRef(false);
  const fromClient = full?.client.id ?? workspace;
  const lock = useReplyLock(path, fromClient, !!(editor && draft.trim() && full?.can_reply));
  const other = lock.until > Date.now() ? null : activeLock(full?.locked_by ?? null);

  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const value = await inboxRequest<InboxConversation>(path, workspace, { signal });
      if (!signal.aborted) { setFull(value); setLoadError(""); }
    } catch (e) { if (!signal.aborted) setLoadError(message(e)); }
  // revision invalidates requests that started before a mutation.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, workspace, revision]);
  useVisiblePoll(load, 5000);

  useEffect(() => {
    if (fromClient == null) return;
    const controller = new AbortController();
    inboxRequest<ReplyTemplate[]>("/admin/outreach/reply-templates", fromClient, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setTemplates(value); })
      .catch(() => { if (!controller.signal.aborted) setTemplates([]); });
    return () => controller.abort();
  }, [fromClient]);

  useEffect(() => {
    if (!assigning) return;
    const controller = new AbortController();
    inboxRequest<Operator[]>("/admin/outreach/operators", null, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setOperators(value); })
      .catch(e => { if (!controller.signal.aborted) setError(message(e)); });
    return () => controller.abort();
  }, [assigning]);

  useEffect(() => {
    if (focusRequested && editor) {
      replyRef.current?.focus();
      setFocusRequested(false);
    }
  }, [editor, focusRequested]);

  function merge(update: Conversation) {
    setFull(current => current ? { ...current, ...update } : current);
  }
  function refreshed() {
    setRevision(n => n + 1);
    onChanged();
  }
  async function action(run: () => Promise<void>) {
    if (!full || busyRef.current) return;
    busyRef.current = true;
    setBusy(true); setError(""); setNotice("");
    try { await run(); refreshed(); }
    catch (e) { setError(message(e)); setRevision(n => n + 1); }
    finally { busyRef.current = false; setBusy(false); }
  }
  function classify(data: { label?: InboxLabel | null; handled?: boolean }) {
    void action(async () => {
      const update = await inboxMutation<Conversation>(path, fromClient, "PATCH", data);
      merge(update);
      setNotice(data.handled === true ? "Разобрано" : data.handled === false ? "Возвращено в неразобранные" : "Метка сохранена");
      if (!draft.trim() && (data.label != null || data.handled)) onNext();
    });
  }
  function send() {
    if (!full?.can_reply || other || !editor || (!draft.trim() && !file)) return;
    void action(async () => {
      const sent = await lock.withLock(() => {
        if (!file) return inboxMutation<InboxMessage>(`${path}/reply`, fromClient, "POST", {
          text: draft.trim(), ...(templateId === undefined ? {} : { template_id: templateId }),
        });
        const form = new FormData();
        form.set("text", draft.trim()); form.set("file", file);
        return rawRequest<InboxMessage>(`${path}/reply-rich`, fromClient, { method: "POST", body: form });
      });
      setFile(null);
      setFull(current => current && !current.messages.some(item => item.id === sent.id)
        ? { ...current, messages: [...current.messages, sent] } : current);
      setDraft(""); setTemplateId(undefined); setNotice("Ответ отправлен");
    });
  }

  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.repeat ||
          target?.isContentEditable || target?.closest("input, textarea, select, [role=dialog]")) return;
      const key = event.key.toLowerCase();
      const label = labelKeys[Number(key) - 1];
      if (/^[1-4]$/.test(key) && label) { event.preventDefault(); classify({ label }); }
      else if (key === "e" || key === "у") { event.preventDefault(); classify({ handled: true }); }
      else if (key === "r" || key === "к") {
        event.preventDefault(); setEditor(true); setFocusRequested(true);
      }
    };
    document.addEventListener("keydown", handle);
    return () => document.removeEventListener("keydown", handle);
  });

  if (!full) return <p role={loadError ? "alert" : "status"}>{loadError || "Загружаем переписку…"}</p>;
  const contact = full.contact.name || full.contact.address;
  const applicable = templates.filter(template => template.channel === "any" || template.channel === full.channel);
  const canCompose = full.can_reply && !other && !busy;
  return <>
    <header className={styles.inboxHead}>
      <div><h2>{full.hot ? "🔥 " : ""}{contact}</h2>
        <p className={styles.muted}>{channelNames[full.channel]} · {full.client.name}
          {full.campaign && ` · ${full.campaign.name}`}</p>
        <p>{full.contact.address}{full.contact.company && ` · ${full.contact.company}`}</p>
        <p className={styles.muted}>{full.sender.kind === "mailbox" ? "Ящик" : "Аккаунт"}: {full.sender.label}</p>
      </div>
      <div className={styles.actions}>
        {full.crm ? <Link className={styles.secondary} href={`/crm?deal=${full.crm.deal_id}`}>
          Сделка №{full.crm.deal_id}{full.crm.stage && ` · ${full.crm.stage}`}
        </Link> : <button type="button" className={styles.secondary} disabled={busy}
          onClick={() => void action(async () => {
            const result = await inboxMutation<{ deal_id: number }>(`${path}/to-crm`, fromClient);
            setFull(current => current ? { ...current, crm: { deal_id: result.deal_id, stage: "" } } : current);
            setNotice("Сделка создана в CRM клиента");
          })}>В CRM</button>}
      </div>
    </header>
    {(full.summary || full.hot || full.interest != null) &&
      <div className={`${styles.inboxSummary} ${full.hot ? styles.inboxSummaryHot : ""}`}>
        {full.hot && <b>🔥 Горячий ответ</b>}{full.summary && <p>{full.summary}</p>}
        {full.interest != null && <p>Интерес: {full.interest}/100</p>}
      </div>}
    <div className={styles.inboxActions}>
      <span>Назначено: {full.assignee?.name ?? "никому"}</span>
      <button type="button" className={styles.secondary} disabled={busy} onClick={() => void action(async () => {
        merge(await inboxMutation<Conversation>(`${path}/claim`, fromClient));
      })}>Взять себе</button>
      <button type="button" className={styles.secondary} disabled={busy} aria-expanded={assigning} onClick={() => {
        setAssignee(String(full.assignee?.id ?? "")); setAssigning(value => !value);
      }}>Назначить…</button>
      {assigning && <>
        <select aria-label="Ответственный" value={assignee} disabled={busy} onChange={e => setAssignee(e.target.value)}>
          <option value="">Никому</option>
          {operators.map(operator => <option key={operator.id} value={operator.id}>{operator.name}</option>)}
          {!!full.assignee && !operators.some(operator => operator.id === full.assignee?.id) &&
            <option value={full.assignee.id}>{full.assignee.name}</option>}
        </select>
        <button type="button" className={styles.primary} disabled={busy} onClick={() => void action(async () => {
          merge(await inboxMutation<Conversation>(`${path}/assign`, fromClient, "POST", {
            user_id: assignee ? Number(assignee) : null,
          }));
          setAssigning(false);
        })}>Сохранить назначение</button>
      </>}
    </div>
    <div className={styles.inboxActions}>
      {labelKeys.map((label, i) => <button type="button" key={label}
        className={full.label === label ? styles.primary : styles.secondary}
        aria-pressed={full.label === label} title={`Клавиша ${i + 1}`} disabled={busy}
        onClick={() => classify({ label })}>{inboxLabels[label]}</button>)}
      <select aria-label="Метка переписки" value={full.label ?? ""} disabled={busy}
        onChange={e => classify({ label: (e.target.value || null) as InboxLabel | null })}>
        <option value="">Без метки</option>
        {Object.entries(inboxLabels).map(([key, label]) => <option value={key} key={key}>{label}</option>)}
      </select>
      <button type="button" className={styles.secondary} title="Клавиша E" disabled={busy}
        onClick={() => classify({ handled: !full.handled })}>{full.handled ? "Вернуть" : "Разобрано ✓"}</button>
    </div>
    <div className={styles.thread}>{full.messages.map(item =>
      <article key={item.id} className={item.direction === "in" ? styles.msgIn : styles.msgOut}>
        <header><b>{item.author.name || (item.author.kind === "lead" ? contact : authors[item.author.kind])}</b>
          <small>{date(item.at)} · {delivery[item.status]}</small>
          {item.status === "unknown" && full.channel === "telegram" && <button type="button" className={styles.secondary} disabled={busy}
            onClick={() => void action(async () => {
              const r = await inboxMutation<{ sent: number; not_sent: number }>(`${path}/verify`, fromClient);
              setNotice(r.sent ? "Проверено в Telegram: сообщение ушло" : "Проверено в Telegram: сообщения нет — отправьте ещё раз");
            })}>Проверить, ушло ли</button>}</header>
        {item.direction === "out" && full.channel === "email" && hasMarkup(item.text)
          ? <Formatted text={item.text}/> : <p>{item.text}</p>}
      </article>)}
    </div>
    {(error || loadError || (editor && lock.error && !other)) &&
      <p role="alert" className={styles.error}>{error || loadError || lock.error}</p>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}
    {other && <p role="status" className={css.lock}>Сейчас отвечает {other.name}</p>}
    {!full.can_reply && <p className={styles.muted}>{full.reply_blocker || "Ответить в этой переписке нельзя."}</p>}
    {editor ? <form className={styles.replyBox} onSubmit={e => { e.preventDefault(); send(); }}>
      <div className={styles.actions}>
        <select aria-label="Шаблон ответа" value={templateId ?? ""} disabled={!canCompose || !applicable.length}
          onChange={e => {
            const template = applicable.find(item => item.id === Number(e.target.value));
            if (!template) { setTemplateId(undefined); return; }
            if (draft.trim() && !window.confirm("Заменить черновик выбранным шаблоном?")) return;
            setTemplateId(template.id); setDraft(renderReplyTemplate(template.text, full));
          }}>
          <option value="">{applicable.length ? "Шаблон ответа" : "Нет шаблонов для канала"}</option>
          {applicable.map(template => <option key={template.id} value={template.id}>{template.title}</option>)}
        </select>
        <button type="button" className={styles.secondary} disabled={!canCompose} onClick={() => {
          if (draft.trim() && !window.confirm("Заменить черновик подсказкой ИИ?")) return;
          void action(async () => {
            const result = await inboxMutation<{ text: string }>(`${path}/suggest`, fromClient);
            setDraft(result.text); setTemplateId(undefined); setFocusRequested(true);
          });
        }}>✨ Подсказать ответ</button>
        <button type="button" className={styles.secondary} disabled={busy}
          onClick={() => setEditor(false)}>Закрыть редактор</button>
      </div>
      <RichComposer inputRef={replyRef} value={draft} onChange={setDraft} onSubmit={send} readOnly={!canCompose}
        placeholder={`Ответ с «${full.sender.label}»`} formatting={full.channel === "telegram"}
        maxLength={full.channel === "telegram" ? 4096 : undefined}
        file={file} onFile={full.channel === "telegram" ? setFile : undefined} busy={busy}
        canSubmit={canCompose && (!!draft.trim() || !!file)} submitLabel="Отправить · ⌘/Ctrl+Enter"/>
    </form> : <button type="button" className={styles.primary} onClick={() => {
      setEditor(true); setFocusRequested(true);
    }}>Открыть редактор</button>}
  </>;
}
