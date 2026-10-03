"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Item = { name: string; kind: string; status: string; detail: string | null };
type Group = { key: string; title: string; href: string; items: Item[] };
type Source = { id: number; name: string; active: boolean; auto_accept: boolean; auto_assign: boolean };
type Data = { can_manage: boolean; groups: Group[]; sources: Source[] };

const statusLabel: Record<string, string> = { connected: "работает", error: "ошибка", pending: "проверяется", off: "выключено" };
const empty: Record<string, string> = { ads: "Кабинеты подключает агентство", site: "Трекинг сайта и формы Tilda ещё не подключены",
  chats: "Подключите Авито, Telegram-бот и WhatsApp", phone: "Подключите Mango Office — звонки будут попадать в сделки" };

/** Settings → «Подключения»: everything connected to the project and how each lead source is handled. */
export function IntegrationsCard({ projectId }: { projectId: number }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api<Data>(`/crm/projects/${projectId}/integrations`).then(setData).catch(e => setError((e as Error).message)), [projectId]);
  useEffect(() => { load(); }, [load]);
  async function toggle(source: Source, key: "auto_accept" | "auto_assign") {
    setError("");
    try { await api(`/crm/inbound-sources/${source.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [key]: !source[key] }) }); await load(); }
    catch (e) { setError((e as Error).message); }
  }
  if (!data) return <section className="resultPanel settingsCard">{error || "Загружаем подключения…"}</section>;
  return <>
    <section className="resultPanel settingsCard"><h2>Подключения</h2><p>Всё, откуда в проект приходят данные и заявки. Нажмите на раздел, чтобы настроить.</p>
      <div className="integrationGroups">{data.groups.map(group => <div key={group.key} className="integrationGroup">
        <div className="integrationGroupHead"><b>{group.title}</b><Link href={group.href}>Настроить →</Link></div>
        {group.items.length ? group.items.map((item, i) => <div key={i} className="integrationItem"><span className={`integrationDot ${item.status}`}/>
          <div><b>{item.name}</b><small>{item.kind}{item.detail ? ` · ${item.detail}` : ""}</small></div><em>{statusLabel[item.status] || item.status}</em></div>)
          : <p className="integrationEmpty">{empty[group.key]}</p>}</div>)}</div></section>
    <section className="resultPanel settingsCard"><h2>Как обрабатывать заявки</h2>
      <p>«Сразу в сделки» — для надёжных источников (сайт, звонки): заявка минует «Неразобранное», а повторное обращение клиента попадает в его открытую сделку. Для чатов лучше оставить ручной разбор — там бывает спам.</p>
      {error && <p className="crmFormError">{error}</p>}
      <div className="integrationSources">{data.sources.map(source => <div key={source.id} className={source.active ? "" : "off"}>
        <b>{source.name}</b>
        <label><input type="checkbox" checked={source.auto_accept} disabled={!data.can_manage} onChange={() => toggle(source, "auto_accept")}/> Сразу в сделки</label>
        <label><input type="checkbox" checked={source.auto_assign} disabled={!data.can_manage} onChange={() => toggle(source, "auto_assign")}/> Назначать менеджеров по очереди</label></div>)}
        {!data.sources.length && <p className="integrationEmpty">Источников заявок пока нет.</p>}</div></section>
  </>;
}
