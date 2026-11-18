"use client";

import { useEffect, useState } from "react";
import { ProjectSidebar, count } from "@/components/reporting";
import { api } from "@/lib/api";
import type { Channel, Contacted, Conversation, Listing, Summary, Thread } from "./types";
import "../../result/result.css";
import css from "./outreach.module.css";
import { ReplyBox } from "./reply-box";

const base = "/portal/outreach";
const limit = 50;
const labels = { sent: "Написали", replied: "Ответил", interested: "Интерес", refused: "Отказ" };
const responseLabels: Record<string, string> = {
  interested: "Интерес", question: "Вопрос", later: "Позже", not_interested: "Отказ", unsubscribe: "Отписка",
};
const channels = { email: "Email", telegram: "Telegram" };
const metrics: [keyof Summary, string][] = [
  ["companies", "Охвачено компаний"], ["people", "Охвачено контактов"], ["sent", "Отправлено"],
  ["replied", "Ответили"], ["hot", "Горячие"],
];
const date = (value: string | null) => value
  ? new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—";
const name = (row: Thread) => row.contact.company || row.contact.name || row.contact.address || "Контакт";

export default function OutreachPage() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryError, setSummaryError] = useState("");
  const [tab, setTab] = useState<"contacted" | "inbox">("contacted");
  const [channel, setChannel] = useState<Channel | "">("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ route: string; result: Listing<Contacted | Thread> } | null>(null);
  const [listError, setListError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Conversation | null>(null);
  const [detailError, setDetailError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    api<Summary>(`${base}/summary`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setSummary(value);
    }).catch(error => { if (!controller.signal.aborted) setSummaryError(error.message); });
    return () => controller.abort();
  }, []);
  const params = new URLSearchParams({ page: String(page), per_page: String(limit) });
  if (channel) params.set("channel", channel);
  if (query.trim()) params.set("q", query.trim());
  const route = `${base}/${tab}?${params}`;
  useEffect(() => {
    if (!summary) return;
    setListError("");
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api<Listing<Contacted | Thread>>(route, { signal: controller.signal }).then(value => {
        if (!controller.signal.aborted) setData({ route, result: value });
      }).catch(error => { if (!controller.signal.aborted) setListError(error.message); });
    }, 180);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [route, summary]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    api<Conversation>(`${base}/inbox/${encodeURIComponent(selected)}`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setDetail(value);
    }).catch(error => { if (!controller.signal.aborted) setDetailError(error.message); });
    return () => controller.abort();
  }, [selected]);

  function select(key: string | null) {
    if (key === selected) return;
    setDetailError(""); setDetail(null); setSelected(key);
    if (window.matchMedia("(max-width: 700px)").matches) window.scrollTo(0, 0);
  }

  const result = data?.route === route ? data.result : null;
  return <div className="resultShell"><ProjectSidebar projectId={null} active="outreach"/>
    <main className={`resultMain ${css.page}`}>
      <header className="resultHeader"><div><h1>Аутрич</h1><p>Email и Telegram — работа агентства для вашей компании.</p></div></header>
      {!summary && (summaryError ? <p className="resultError" role="alert">{summaryError}</p>
        : <p role="status">Загружаем сводку…</p>)}
      {summary && <>
        <section className={css.metrics} aria-label="Сводка аутрича">
          {metrics.map(([key, title]) => <div className={css.panel} key={key}>
            <small>{title}</small><strong>{count(summary[key])}</strong>
          </div>)}
        </section>
        <div className={css.actions} aria-label="Разделы аутрича">
          {([["contacted", "Кому писали"], ["inbox", "Переписки"]] as const).map(([value, title]) =>
            <button key={value} type="button" aria-pressed={tab === value}
              onClick={() => { if (tab !== value) { select(null); setPage(1); setTab(value); } }}>{title}</button>)}
        </div>
        <div className={tab === "inbox" ? css.inbox : ""} data-selected={!!selected}>
          <section className={`${css.panel} ${css.list}`} aria-label={tab === "inbox" ? "Переписки" : "Кому писали"}>
            <div className={css.actions}>
              <select aria-label="Канал" value={channel} onChange={event => {
                setChannel(event.target.value as Channel | ""); setPage(1);
              }}>
                <option value="">Все каналы</option>
                <option value="email">Email</option><option value="telegram">Telegram</option>
              </select>
              <input type="search" aria-label="Поиск по аутричу" placeholder="Компания или контакт"
                maxLength={200} value={query} onChange={event => { setQuery(event.target.value); setPage(1); }}/>
            </div>
            {listError && <p className="resultError" role="alert">{listError}</p>}
            {!result && !listError && <p role="status">Загружаем…</p>}
            {result && !result.items.length && <p>Ничего не найдено.</p>}
            {tab === "contacted" && !!result?.items.length && <div className={css.scroll} tabIndex={0}
              aria-label="Таблица контактов"><table><thead><tr>
                <th>Компания</th><th>Контакт</th><th>Канал</th><th>Кампания</th><th>Первое касание</th><th>Статус</th>
              </tr></thead><tbody>{(result.items as Contacted[]).map((row, i) => <tr key={i}>
                <td>{row.company ?? "—"}</td><td>{row.contact ?? "—"}</td><td>{channels[row.channel]}</td>
                <td>{row.campaign ?? "—"}</td><td>{date(row.first_contact_at)}</td><td>{labels[row.status]}</td>
              </tr>)}</tbody></table></div>}
            {tab === "inbox" && <ul className={css.threads}>{(result?.items as Thread[] | undefined)?.map(row =>
              <li key={row.key}><button type="button" aria-current={row.key === selected ? "true" : undefined}
                onClick={() => select(row.key)}>
                <strong>{row.hot ? "🔥 " : ""}{name(row)}</strong>
                <small>{channels[row.channel]} · {date(row.last.at)}</small><span>{row.last.text}</span>
              </button></li>)}</ul>}
            {result && (page > 1 || result.total > limit) && <div className={css.actions}>
              <button type="button" disabled={page === 1} onClick={() => setPage(value => value - 1)}>Назад</button>
              <span>Страница {page}</span>
              <button type="button" disabled={page * limit >= result.total}
                onClick={() => setPage(value => value + 1)}>Далее</button>
            </div>}
          </section>
          {tab === "inbox" && <section className={`${css.panel} ${css.detail}`} aria-label="Переписка">
            {selected ? <>
              <button type="button" className={css.back} onClick={() => select(null)}>← К перепискам</button>
              {detailError && <p className="resultError" role="alert">{detailError}</p>}
              {!detail && !detailError && <p role="status">Загружаем переписку…</p>}
              {detail && <>
                <h2>{name(detail)}</h2>
                <p>{detail.contact.address} · {channels[detail.channel]}{detail.campaign && ` · ${detail.campaign}`}</p>
                <p>Отправитель: {detail.sender ?? "Недоступен"}</p>
                {detail.label && responseLabels[detail.label] && <p>{responseLabels[detail.label]}</p>}
                <div className={css.messages}>{detail.messages.map(message =>
                  <article key={message.id} data-direction={message.direction}>
                    <header><strong>{message.author}</strong><small>{date(message.at)}</small></header>
                    <p>{message.text}</p>
                  </article>)}</div>
                <ReplyBox conv={detail} onSent={message =>
                  setDetail(current => current ? { ...current, messages: [...current.messages, message] } : current)}/>
              </>}
            </> : <p>Выберите переписку, чтобы прочитать сообщения.</p>}
          </section>}
        </div>
      </>}
    </main>
  </div>;
}
