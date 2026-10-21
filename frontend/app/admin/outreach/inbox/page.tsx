"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { base, date, message, PageHeader, post } from "../../leadgen/shared";
import styles from "../../leadgen/leadgen.module.css";

type Item = {
  id: number; company_id: number; company: string; domain: string | null; stage: string; score: number;
  fit_label: string | null; crm_deal_id: number | null; address: string | null; mailbox: string | null;
  sequence: string | null; subject: string | null; snippet: string; label: string | null; label_name: string | null;
  label_source: string | null; summary: string | null; handled: boolean; happened_at: string | null;
  hot: boolean; hot_at: string | null; intent_score: number | null; intent_signals: string[];
};
type Full = Item & {
  body: string; can_reply: boolean;
  thread: { id: number; direction: "in" | "out"; subject: string | null; body: string | null; address: string | null;
    manual: boolean; happened_at: string | null; label: string | null }[];
};
type Listing = { total: number; unhandled: number; hot: number; counts: Record<string, number>;
  labels: { key: string; name: string }[]; items: Item[]; ai_available: boolean };

const SIGNALS: Record<string, string> = { phone: "телефон", call: "созвон", price: "цена", trial: "тест", meeting: "время встречи", details: "подробности" };
const TONE: Record<string, string> = { interested: styles.fitYes, question: styles.fitMaybe, later: styles.fitMaybe, not_interested: styles.fitNo, unsubscribe: styles.fitNo };
const ACTIONS: { label: string; title: string; key: string }[] = [
  { label: "interested", title: "Интересно", key: "1" },
  { label: "question", title: "Вопрос", key: "2" },
  { label: "later", title: "Позже", key: "3" },
  { label: "not_interested", title: "Не интересно", key: "4" },
];
const when = (iso: string | null) => {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  return d.toDateString() === now.toDateString() ? d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
};

function Inbox() {
  const router = useRouter();
  const search = useSearchParams();
  const [tab, setTab] = useState<string>(search.get("tab") || "hot");
  const [status, setStatus] = useState<"unhandled" | "all">("unhandled");
  const [q, setQ] = useState("");
  const [data, setData] = useState<Listing | null>(null);
  const [selected, setSelected] = useState<number | null>(Number(search.get("touch")) || null);
  const [full, setFull] = useState<Full | null>(null);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const replyRef = useRef<HTMLTextAreaElement>(null);
  const refresh = useCallback(() => setRevision((n) => n + 1), []);

  const query = useMemo(() => {
    const p = new URLSearchParams({ status: status === "all" ? "all" : "unhandled", limit: "100" });
    if (tab === "hot") p.set("hot", "true");
    else if (tab !== "all") p.set("label", tab);
    if (q.trim()) p.set("q", q.trim());
    return p.toString();
  }, [tab, status, q]);

  useEffect(() => {
    const controller = new AbortController();
    const t = setTimeout(() => {
      api<Listing>(`${base}/inbox?${query}`, { signal: controller.signal })
        .then((d) => { if (!controller.signal.aborted) { setData(d); setError(""); } })
        .catch((e) => !controller.signal.aborted && setError(message(e)));
    }, q ? 250 : 0);
    return () => { clearTimeout(t); controller.abort(); };
  }, [query, revision, q]);

  // Новые ответы подтягиваются сами раз в минуту.
  useEffect(() => { const t = setInterval(refresh, 60_000); return () => clearInterval(t); }, [refresh]);

  useEffect(() => {
    if (!selected) { setFull(null); return; }
    const controller = new AbortController();
    api<Full>(`${base}/inbox/${selected}`, { signal: controller.signal })
      .then((d) => !controller.signal.aborted && setFull(d))
      .catch((e) => !controller.signal.aborted && setError(message(e)));
    const p = new URLSearchParams(search.toString());
    p.set("touch", String(selected));
    router.replace(`/admin/outreach/inbox?${p}`, { scroll: false });
    setReply("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  async function patch(body: object, done: string) {
    if (!full || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const r = await api<Item & { effect: string | null }>(`${base}/inbox/${full.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      setNotice(done + (r.effect === "task:call" ? " · задача «Связаться» в CRM" : r.effect?.startsWith("task:later") ? " · напомним позже" : r.effect === "closed" ? " · стоп-лист и «Отказ» в CRM" : ""));
      setFull({ ...full, ...r });
      refresh();
      next();
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  function next() {
    const items = data?.items || [];
    const i = items.findIndex((x) => x.id === full?.id);
    const n = items[i + 1] || items[i - 1];
    if (n && n.id !== full?.id) setSelected(n.id);
  }
  async function send() {
    if (!full || !reply.trim() || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const r = await post<{ item: Full }>(`/inbox/${full.id}/reply`, { body: reply.trim() });
      setFull(r.item); setReply(""); setNotice(`Ответ отправлен с ${full.mailbox}`); refresh();
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function aiLabel() {
    if (!full || busy) return;
    setBusy(true); setError("");
    try { setFull({ ...full, ...(await post<Item>(`/inbox/${full.id}/classify`, {})) }); refresh(); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }

  // Горячие клавиши: 1–4 — метка, E — разобрано, R — ответить, J/K — следующий/предыдущий.
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || e.metaKey || e.ctrlKey) return;
      const action = ACTIONS.find((a) => a.key === e.key);
      if (action) { e.preventDefault(); void patch({ label: action.label }, `Метка «${action.title}»`); }
      else if (e.key === "e") { e.preventDefault(); void patch({ handled: true }, "Разобрано"); }
      else if (e.key === "r") { e.preventDefault(); replyRef.current?.focus(); }
      else if (e.key === "j" || e.key === "k") {
        const items = data?.items || [];
        const i = items.findIndex((x) => x.id === selected);
        const n = items[e.key === "j" ? i + 1 : i - 1];
        if (n) setSelected(n.id);
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  });

  const tabs: [string, string, number | undefined][] = [
    ["hot", "🔥 Горячие", data?.hot],
    ["all", "Все", undefined],
    ...((data?.labels || []).map((l) => [l.key, l.name, data?.counts[l.key]] as [string, string, number | undefined])),
  ];

  return (
    <div className={`page ${styles.page}`}>
      <PageHeader title="Входящие" subtitle="Ответы на аутрич со всех ящиков. Горячие — сверху, о них сразу приходит сообщение в Telegram.">
        <div className={styles.actions}>
          <span className={styles.muted}>Не разобрано: <b>{data?.unhandled ?? "—"}</b></span>
          <Link className={styles.secondary} href="/admin/leadgen/settings#hot">Настройки триггера</Link>
        </div>
      </PageHeader>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}

      <div className={styles.tabs} aria-label="Метки">
        {tabs.map(([key, name, count]) => (
          <button key={key} type="button" className={styles.tab} aria-pressed={tab === key} onClick={() => setTab(key)}>
            {name}{count ? <span>{count}</span> : null}
          </button>
        ))}
      </div>

      <div className={styles.inboxGrid}>
        <section className={`${styles.card} ${styles.inboxList}`} aria-label="Ответы">
          <div className={styles.inboxTools}>
            <input type="search" placeholder="Компания, тема, текст" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Поиск" />
            <select value={status} onChange={(e) => setStatus(e.target.value as "unhandled" | "all")} aria-label="Статус">
              <option value="unhandled">Не разобрано</option>
              <option value="all">Все</option>
            </select>
          </div>
          {!data ? <p className={styles.muted} role="status">Загружаем…</p> : !data.items.length ? (
            <div className={styles.empty}>
              <h2>{tab === "hot" ? "Горячих ответов нет" : "Здесь пусто"}</h2>
              <p className={styles.muted}>{status === "unhandled" ? "Всё разобрано. Новые ответы появятся сами." : "Ответов с такими условиями нет."}</p>
            </div>
          ) : (
            <ul>
              {data.items.map((item) => (
                <li key={item.id}>
                  <button type="button" className={`${styles.inboxItem} ${item.id === selected ? styles.inboxActive : ""} ${item.hot && !item.handled ? styles.inboxHot : ""} ${item.handled ? styles.inboxDone : ""}`}
                    onClick={() => setSelected(item.id)}>
                    <span className={styles.inboxTop}>
                      <b>{item.hot ? "🔥 " : ""}{item.company}</b>
                      <small>{when(item.happened_at)}</small>
                    </span>
                    <span className={styles.inboxSnippet}>{item.summary || item.snippet}</span>
                    <span className={styles.inboxMeta}>
                      {item.label_name && <span className={`${styles.badge} ${TONE[item.label || ""] || ""}`}>{item.label_name}{item.label_source === "user" ? "" : " · авто"}</span>}
                      {item.intent_score != null && <span className={styles.muted}>интерес {item.intent_score}</span>}
                      {item.sequence && <span className={styles.muted}>{item.sequence}</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className={`${styles.card} ${styles.inboxView}`} aria-label="Переписка">
          {!full ? <div className={styles.empty}><h2>Выберите ответ слева</h2><p className={styles.muted}>Клавиши: 1–4 — метка, E — разобрано, R — ответить, J/K — следующий и предыдущий.</p></div> : <>
            <header className={styles.inboxHead}>
              <div>
                <h2>{full.hot ? "🔥 " : ""}{full.company}</h2>
                <p className={styles.muted}>
                  {full.address} → {full.mailbox}{full.sequence ? ` · цепочка «${full.sequence}»` : ""}
                  {full.domain && <> · <a href={`https://${full.domain}`} target="_blank" rel="noopener noreferrer">{full.domain}</a></>}
                </p>
              </div>
              <div className={styles.actions}>
                <Link className={styles.secondary} href={`/admin/leadgen/companies?company=${full.company_id}`}>Карточка</Link>
                {full.crm_deal_id && <Link className={styles.secondary} href={`/crm?deal=${full.crm_deal_id}`}>Сделка в CRM</Link>}
              </div>
            </header>
            {(full.summary || full.hot) && (
              <div className={`${styles.inboxSummary} ${full.hot ? styles.inboxSummaryHot : ""}`}>
                {full.hot && <b>🔥 Горячий ответ{full.hot_at ? ` · ${date(full.hot_at)}` : ""}</b>}
                {full.summary && <p>{full.summary}</p>}
                {!!full.intent_signals.length && <p className={styles.muted}>Признаки: {full.intent_signals.map((s) => SIGNALS[s] || s).join(", ")}{full.intent_score != null ? ` · интерес ${full.intent_score}/100` : ""}</p>}
              </div>
            )}
            <div className={styles.inboxActions}>
              {ACTIONS.map((a) => (
                <button key={a.label} type="button" disabled={busy} className={full.label === a.label && full.label_source === "user" ? styles.primary : styles.secondary}
                  onClick={() => patch({ label: a.label }, `Метка «${a.title}»`)} title={`Клавиша ${a.key}`}>
                  {a.title}
                </button>
              ))}
              <button type="button" className={styles.secondary} disabled={busy} onClick={() => patch({ handled: !full.handled }, full.handled ? "Вернули в неразобранные" : "Разобрано")} title="Клавиша E">
                {full.handled ? "Вернуть" : "Разобрано ✓"}
              </button>
              {data?.ai_available && full.label_source !== "user" && (
                <button type="button" className={styles.secondary} disabled={busy} onClick={aiLabel}>Разметить ИИ</button>
              )}
            </div>
            <div className={styles.thread}>
              {full.thread.map((m) => (
                <article key={m.id} className={m.direction === "in" ? styles.msgIn : styles.msgOut}>
                  <header><b>{m.direction === "in" ? full.company : m.manual ? "Вы" : "Цепочка"}</b><small>{date(m.happened_at)}</small></header>
                  {m.subject && <strong>{m.subject}</strong>}
                  <p>{m.body}</p>
                </article>
              ))}
            </div>
            {full.can_reply ? (
              <form className={styles.replyBox} onSubmit={(e) => { e.preventDefault(); void send(); }}>
                <textarea ref={replyRef} rows={5} value={reply} onChange={(e) => setReply(e.target.value)}
                  placeholder={`Ответ уйдёт с ${full.mailbox} в эту же ветку`}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void send(); } }} />
                <div className={styles.actions}>
                  <button type="submit" className={styles.primary} disabled={busy || !reply.trim()}>{busy ? "Отправляем…" : "Отправить"}</button>
                  <span className={styles.muted}>⌘+Enter</span>
                </div>
              </form>
            ) : <p className={styles.muted}>Ответить отсюда нельзя: компания просила не писать или адрес в стоп-листе.</p>}
          </>}
        </section>
      </div>
    </div>
  );
}

export default function Page() {
  return <Suspense fallback={<div className={`page ${styles.page}`} role="status">Загружаем входящие…</div>}><Inbox /></Suspense>;
}
