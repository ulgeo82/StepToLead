"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { currentSpace, spaceUrl } from "@/lib/space";
import { date, message, PageHeader } from "../../leadgen/shared";
import styles from "../../leadgen/leadgen.module.css";
import { ConversationPanel } from "./conversation";
import { inboxRequest } from "./inbox-api";
import type { Channel, Conversation, ConversationKey, InboxLabel, InboxListing, InboxStatus } from "./inbox-types";
import {
  activeLock, channelNames, conversationKey, countChangedEvent, inboxLabels, inboxPage, nextConversation,
} from "./inbox-utils";
import { useVisiblePoll } from "./visible-poll";
import css from "./inbox.module.css";

const PAGE_SIZE = 50;
const tabs: [InboxStatus, string][] = [
  ["unhandled", "Не разобрано"], ["mine", "Мои"], ["hot", "🔥 Горячие"], ["all", "Все"],
];

function positiveId(value: string | null) {
  return value && /^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null;
}

function Inbox() {
  const search = useSearchParams();
  const allClients = search.get("all_clients") === "1";
  const selected = conversationKey(search.get("conversation"), search.get("touch"));
  const [scope, setScope] = useState<number | null>();
  const [status, setStatus] = useState<InboxStatus>(() => {
    const tab = search.get("tab");
    return tabs.some(([key]) => key === tab) ? tab as InboxStatus : "unhandled";
  });
  const [channel, setChannel] = useState<Channel | "">("");
  const [label, setLabel] = useState<InboxLabel | "">("");
  const [campaign, setCampaign] = useState(search.get("campaign") ?? "");
  const [account, setAccount] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ key: string; listing: InboxListing } | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [known, setKnown] = useState<{ key: string; items: Conversation[] }>({ key: "", items: [] });

  const requestedWorkspace = search.get("ws");
  useEffect(() => { setScope(currentSpace()); }, [requestedWorkspace]);
  const params = new URLSearchParams({ status, page: String(page), per_page: String(PAGE_SIZE) });
  if (allClients) params.set("all_clients", "1");
  if (channel) params.set("channel", channel);
  if (label) params.set("label", label);
  if (q.trim()) params.set("q", q.trim());
  if (positiveId(campaign)) params.set("campaign", campaign);
  if (positiveId(account)) params.set("account", account);
  const query = params.toString();
  const listWorkspace = allClients ? null : scope ?? null;
  const scopeKey = `${allClients ? "all" : scope}:${channel}`;
  const requestKey = `${scopeKey}:${query}`;
  const listing = data?.key === requestKey ? data.listing : null;
  const items = listing?.items ?? [];

  const load = useCallback(async (signal: AbortSignal) => {
    if (scope === undefined) return;
    try {
      const value = await inboxRequest<InboxListing>(`${inboxPage}?${query}`, listWorkspace, { signal });
      if (signal.aborted) return;
      setError("");
      if (page > 1 && (page - 1) * PAGE_SIZE >= value.total) {
        setPage(Math.max(1, Math.ceil(value.total / PAGE_SIZE)));
        return;
      }
      setData({ key: requestKey, listing: value });
      setKnown(previous => {
        const candidates = new Map((previous.key === scopeKey ? previous.items : []).map(item => [item.key, item]));
        value.items.forEach(item => candidates.set(item.key, item));
        return { key: scopeKey, items: [...candidates.values()].slice(-500) };
      });
    } catch (e) { if (!signal.aborted) setError(message(e)); }
  // revision revalidates after a mutation without replacing the current list with a loading screen.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, listWorkspace, scope, revision, requestKey, scopeKey, page]);
  useVisiblePoll(load, 5000, q.trim() ? 250 : 0);

  function changeUrl(edit: (params: URLSearchParams) => void) {
    const next = new URLSearchParams(window.location.search);
    edit(next);
    window.history.replaceState(null, "", `${inboxPage}${next.size ? `?${next}` : ""}`);
  }

  function select(key: ConversationKey | null, workspace?: number) {
    changeUrl(next => {
      next.delete("touch"); next.delete("conversation"); next.delete("dialog_ws");
      if (key) next.set("conversation", key);
      if (key && allClients && workspace != null) next.set("dialog_ws", String(workspace));
    });
  }
  function selectItem(item: Conversation) { select(item.key, item.client.id); }
  function move(offset: 1 | -1, fallback = false) {
    const key = nextConversation(items, selected, offset)
      ?? (fallback ? nextConversation(items, selected, -1) : null);
    const item = items.find(row => row.key === key);
    if (item) selectItem(item);
  }
  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey ||
          target?.isContentEditable || target?.closest("input,textarea,select,[role=dialog]")) return;
      const key = event.key.toLowerCase();
      if (key === "j" || key === "о") { event.preventDefault(); move(1); }
      if (key === "k" || key === "л") { event.preventDefault(); move(-1); }
    };
    document.addEventListener("keydown", handle);
    return () => document.removeEventListener("keydown", handle);
  });

  function changeAll(value: boolean) {
    setPage(1); setCampaign(""); setAccount(""); setError("");
    changeUrl(next => {
      ["touch", "conversation", "dialog_ws", "campaign"].forEach(key => next.delete(key));
      if (value) next.set("all_clients", "1");
      else next.delete("all_clients");
    });
  }

  const candidates = known.key === scopeKey ? known.items : [];
  const campaigns = new Map(candidates.flatMap(item => item.campaign ? [[item.campaign.id, item.campaign.name] as const] : []));
  const senders = new Map(candidates.map(item => [item.sender.id, item.sender.label]));
  const itemWorkspace = allClients
    ? positiveId(search.get("dialog_ws")) ?? items.find(item => item.key === selected)?.client.id ?? null
    : scope ?? null;

  return <div className={`page ${styles.page} ${css.screen}`}>
    <PageHeader title="Входящие" subtitle="Ответы на аутрич из email и Telegram.">
      <div className={styles.actions}>
        <label className={styles.check}><input type="checkbox" checked={allClients}
          onChange={event => changeAll(event.target.checked)}/> Все клиенты</label>
        <Link className={styles.secondary} href={spaceUrl("/admin/leadgen/settings#hot", scope ?? null)}>
          Настройки триггера
        </Link>
      </div>
    </PageHeader>
    <div className={`${styles.inboxGrid} ${css.grid}`} data-selected={selected != null}>
      <section className={`${styles.card} ${styles.inboxList} ${css.list}`} aria-label="Список переписок">
        <div className={`${styles.tabs} ${css.tabs}`} aria-label="Статус переписки">
          {tabs.map(([key, title]) => <button key={key} type="button" className={styles.tab}
            aria-pressed={status === key} onClick={() => { setStatus(key); setPage(1); }}>
            {title}{key !== "all" && listing?.counts[key] != null ? <span>{listing.counts[key]}</span> : null}
          </button>)}
        </div>
        <div className={`${styles.inboxTools} ${css.filters}`}>
          <input type="search" aria-label="Поиск по перепискам" placeholder="Контакт, компания, текст" value={q}
            onChange={event => { setQ(event.target.value); setPage(1); }}/>
          <select aria-label="Канал" value={channel} onChange={event => {
            setChannel(event.target.value as Channel | ""); setPage(1); setCampaign(""); setAccount("");
          }}>
            <option value="">Все каналы</option>
            {Object.entries(channelNames).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
          </select>
          <select aria-label="Фильтр по метке" value={label} onChange={event => {
            setLabel(event.target.value as InboxLabel | ""); setPage(1);
          }}>
            <option value="">Все метки</option>
            {Object.entries(inboxLabels).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
          </select>
          <input aria-label="ID кампании" placeholder="Кампания (ID)" inputMode="numeric" list="inbox-campaigns"
            value={campaign} onChange={event => { setCampaign(event.target.value.replace(/\D/g, "")); setPage(1); }}/>
          <datalist id="inbox-campaigns">{[...campaigns].map(([id, name]) => <option key={id} value={id} label={name}/>)}</datalist>
          <input aria-label="ID аккаунта или ящика" placeholder="Аккаунт или ящик (ID)" inputMode="numeric"
            list="inbox-senders" value={account} onChange={event => {
              setAccount(event.target.value.replace(/\D/g, "")); setPage(1);
            }}/>
          <datalist id="inbox-senders">{[...senders].map(([id, name]) => <option key={id} value={id} label={name}/>)}</datalist>
        </div>
        {error && <p className={styles.error} role="alert">{error}</p>}
        {!listing && !error && <p className={css.empty} role="status">Загружаем переписки…</p>}
        {listing && !items.length && <p className={css.empty}>{status === "hot" ? "Горячих ответов нет." : "Переписок не найдено."}</p>}
        <ul>{items.map(item => {
          const lock = activeLock(item.locked_by);
          return <li key={item.key}>
            <button type="button" aria-current={selected === item.key ? "true" : undefined}
              className={`${styles.inboxItem} ${selected === item.key ? styles.inboxActive : ""} ${item.hot && !item.handled ? styles.inboxHot : ""}`}
              onClick={() => selectItem(item)}>
              <span className={styles.inboxTop}>
                <b>{item.hot ? "🔥 " : ""}{item.contact.name || item.contact.company || item.contact.address}</b>
                <small>{date(item.last.at)}</small>
              </span>
              <span className={styles.inboxMeta}>{channelNames[item.channel]} · {item.client.name}
                {item.unread && <strong className={css.unread}>Непрочитано</strong>}
              </span>
              <span className={styles.inboxSnippet}>{item.last.text}</span>
              <span className={styles.inboxMeta}>
                {item.label && <span className={styles.badge}>{inboxLabels[item.label]}</span>}
                {item.assignee && <span>Назначено: {item.assignee.name}</span>}
                {lock && <span>✍ пишет {lock.name}</span>}
              </span>
            </button>
          </li>;
        })}</ul>
        {(page > 1 || (listing?.total ?? 0) > PAGE_SIZE) && <div className={css.pagination}>
          <button type="button" className={styles.secondary} disabled={page === 1}
            onClick={() => setPage(n => n - 1)}>←</button>
          <span>Страница {page}</span>
          <button type="button" className={styles.secondary} disabled={!listing || page * PAGE_SIZE >= listing.total}
            onClick={() => setPage(n => n + 1)}>→</button>
        </div>}
      </section>
      <section className={`${styles.card} ${styles.inboxView} ${css.detail}`} aria-label="Переписка">
        {selected ? <>
          <button type="button" className={`${styles.secondary} ${css.back}`} onClick={() => select(null)}>← Назад</button>
          {scope !== undefined && <ConversationPanel key={`${itemWorkspace}:${selected}`}
            workspace={itemWorkspace} path={`${inboxPage}/${encodeURIComponent(selected)}`}
            onChanged={() => { setRevision(n => n + 1); window.dispatchEvent(new Event(countChangedEvent)); }}
            onNext={() => move(1, true)}/>}
        </> : <div className={styles.empty}><h2>Выберите переписку</h2>
          <p>1–4 — метка, E — разобрано, R — ответить, J/K — следующая/предыдущая.</p>
        </div>}
      </section>
    </div>
  </div>;
}

export default function Page() {
  return <Suspense fallback={<p role="status">Загружаем входящие…</p>}><Inbox/></Suspense>;
}
