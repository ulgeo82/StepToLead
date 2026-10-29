"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { base, message, post } from "../../leadgen/shared";
import styles from "../../leadgen/leadgen.module.css";
import type { Mailbox, Sequence } from "../email/types";
import { Campaign, clientLabel, OutreachClient, STATUS } from "./types";

export default function CampaignsPage() {
  const router = useRouter();
  const [items, setItems] = useState<Campaign[] | null>(null);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [templates, setTemplates] = useState<Sequence[]>([]);
  const [boxes, setBoxes] = useState<Mailbox[]>([]);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("");
  const [chosen, setChosen] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [clients, setClients] = useState<OutreachClient[]>([]);
  const [owner, setOwner] = useState("");   // "" — агентство, иначе project_id клиента
  const [allBoxes, setAllBoxes] = useState<Mailbox[]>([]);

  useEffect(() => {
    api<{ items: Campaign[] }>(`${base}/campaigns`).then((d) => setItems(d.items)).catch((e) => setError(message(e)));
  }, []);
  useEffect(() => {
    if (!creating) return;
    api<{ items: Sequence[] }>(`${base}/sequences`).then((d) => setTemplates(d.items)).catch(() => {});
    api<{ items: OutreachClient[] }>(`${base}/clients`).then((d) => setClients(d.items)).catch(() => {});
    api<{ items: Mailbox[] }>(`${base}/mailboxes`).then((d) => setAllBoxes(d.items.filter((m) => m.role !== "helper" && m.is_active))).catch(() => {});
  }, [creating]);
  useEffect(() => {   // ящики клиента — только в его кампаниях, ящики агентства — только в кампаниях агентства
    const mine = allBoxes.filter((m) => (m.client?.project_id ? String(m.client.project_id) : "") === owner);
    setBoxes(mine);
    setChosen(mine.map((m) => m.id));
  }, [allBoxes, owner]);

  async function create(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      const c = await post<Campaign>("/campaigns", { name: name.trim(), template_sequence_id: template ? Number(template) : null, mailbox_ids: chosen, client_project_id: owner ? Number(owner) : null });
      router.push(`/admin/outreach/campaigns/${c.id}`);
    } catch (err) { setError(message(err)); setBusy(false); }
  }

  return (
    <div className={`page ${styles.page}`}>
      <header className="topbar"><div className="crumbs"><span>StepToLead</span><b>/</b><strong>Кампании</strong></div><span className="userAvatar">ЕД</span></header>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Аутрич</p>
          <h1>Кампании</h1>
          <p className="subtitle">Кампания — свой список лидов, свои письма и свои ящики. Ответы — во «Входящих», цепочка по ответившим останавливается сама.</p>
        </div>
        <div className={styles.actions}><button type="button" className={styles.primary} onClick={() => setCreating(true)}>＋ Новая кампания</button></div>
      </section>
      {error && <p className={styles.error} role="alert">{error}</p>}

      <section className={styles.card}>
        {!items ? <p className={styles.muted} role="status">Загружаем…</p> : !items.length ? (
          <div className={styles.empty}>
            <h2>Кампаний пока нет</h2>
            <p className={styles.muted}>Создайте первую: название → загрузите CSV с лидами → проверьте письма → выберите ящики → «Запустить».</p>
          </div>
        ) : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead><tr><th>Кампания</th><th>Статус</th><th>Лиды</th><th>Отправлено</th><th>Ответили</th><th>🔥 Горячие</th><th>Передано клиенту</th><th>Не доставлено</th></tr></thead>
              <tbody>
                {items.map((c) => {
                  const s = c.stats;
                  return (
                    <tr key={c.id}>
                      <td><Link href={`/admin/outreach/campaigns/${c.id}`}><b>{c.name}</b></Link><small>{c.client ? `для ${clientLabel(c.client)} · ` : ""}{c.steps} шаг. · {c.mailbox_ids.length} ящ.</small></td>
                      <td><span className={`${styles.badge} ${STATUS[c.status].tone ? styles[STATUS[c.status].tone] : ""}`}>{STATUS[c.status].name}</span></td>
                      <td>{s?.leads ?? 0}<small>{s?.waiting ? `в работе ${s.waiting}` : ""}</small></td>
                      <td>{s?.sent ?? 0}</td>
                      <td>{s?.replied ?? 0}<small>{s?.contacted ? `${s.reply_rate}%` : ""}</small></td>
                      <td>{s?.hot ?? 0}</td>
                      <td>{c.client ? s?.handed ?? 0 : "—"}</td>
                      <td>{s?.bounced ?? 0}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {creating && <>
        <button type="button" className={styles.overlay} aria-label="Закрыть" tabIndex={-1} onClick={() => setCreating(false)} />
        <form role="dialog" aria-modal="true" aria-labelledby="new-campaign" className={`${styles.card} ${styles.modal} ${styles.modalWide}`} onSubmit={create}>
          <h2 id="new-campaign">Новая кампания</h2>
          <div className={styles.fields}>
            <label className={styles.field}>Название<input autoFocus required minLength={2} maxLength={180} placeholder="Кухни Самара — октябрь" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className={styles.field}>Для кого
              <select value={owner} onChange={(e) => setOwner(e.target.value)}>
                <option value="">Для агентства (наши продажи)</option>
                {clients.map((c) => <option key={c.project_id} value={c.project_id}>Клиент: {clientLabel(c)}</option>)}
              </select>
              <small>{owner ? "Ответы «Интересно» и горячие сами упадут в CRM клиента (воронка «Аутрич») с задачей «Связаться»; остальные — кнопкой «Передать клиенту». Клиент видит только CRM и отчёт." : "Ответившие — во «Входящих», в CRM агентства заводим вручную."}</small>
            </label>
            <label className={styles.field}>Письма — взять из шаблона
              <select value={template} onChange={(e) => setTemplate(e.target.value)}>
                <option value="">Пустая цепочка</option>
                {templates.map((t) => <option key={t.id} value={t.id}>{t.name} · {t.steps.length} шаг.</option>)}
              </select>
              <small>Кампания получит свою копию писем: правки не заденут шаблон и другие кампании.</small>
            </label>
            <fieldset className={styles.formFieldset}>
              <legend className={styles.legend}>С каких ящиков слать</legend>
              {!boxes.length && <p className={styles.muted}>{owner ? "У клиента нет своих ящиков — добавьте ящики на его домене и укажите в карточке ящика «Клиент»." : "Ящиков нет — добавьте их в «Ящики и шаблоны писем»."}</p>}
              {boxes.map((m) => (
                <label key={m.id} className={styles.check}>
                  <input type="checkbox" checked={chosen.includes(m.id)} onChange={(e) => setChosen(e.target.checked ? [...chosen, m.id] : chosen.filter((x) => x !== m.id))} />
                  <span>{m.email}<small className={styles.hint}>холодных сегодня можно ещё {m.left_today}</small></span>
                </label>
              ))}
              <p className={styles.hint}>Кампания шлёт столько, сколько позволяют прогрев и лимиты выбранных ящиков. Если ящик в нескольких кампаниях — его лимит делится поровну.</p>
            </fieldset>
          </div>
          <div className={styles.actions}>
            <button type="submit" className={styles.primary} disabled={busy || name.trim().length < 2}>{busy ? "Создаём…" : "Создать и загрузить лидов"}</button>
            <button type="button" className={styles.secondary} onClick={() => setCreating(false)}>Отмена</button>
          </div>
        </form>
      </>}
    </div>
  );
}
