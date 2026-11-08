"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  Advertisement, base, blocked, CompanyDetail, date, Fit, LeadList, message, money, Pipeline,
  post, safeUrl, Score, companyName, stages,
} from "./shared";
import styles from "./leadgen.module.css";
import { CompanyTech } from "./company-tech";
import { EmailStatus } from "./email-status";

const channels: Record<string, string> = {
  whatsapp: "WhatsApp", telegram: "Telegram", email: "Email", phone: "Телефон", messenger: "Мессенджер",
};
export type CompanyAction = "enrich" | "outreach" | "dnc" | "assess" | "hide" | "restore";
const touchStatus: Record<string, string> = {
  sent: "отправлено",
  delivered: "доставлено",
  replied: "ответ",
  bounced: "не доставлено",
  auto_reply: "автоответ",
  task: "задача",
  done: "выполнено",
};
const channelLabel: Record<string, string> = {
  email: "Письмо",
  call: "Звонок",
  whatsapp: "WhatsApp",
  telegram: "Telegram",
};
function signalText(kind: string, payload: unknown, labels: Record<string, string>): string {
  const p = (payload && typeof payload === "object" ? payload : {}) as Record<string, unknown>;
  const extra = [
    p.reason,
    Array.isArray(p.keywords) ? `ключей: ${p.keywords.length}` : null,
    p.count != null ? `×${p.count}` : null,
  ]
    .filter((v) => typeof v === "string" || typeof v === "number")
    .join(" · ");
  return extra ? `${labels[kind] || kind} — ${extra}` : labels[kind] || kind;
}
export function useDialog(close: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
      if (event.key !== "Tab") return;
      const nodes = Array.from(
        ref.current?.querySelectorAll<HTMLElement>(
          "button:not([disabled]), a[href], input, select, textarea",
        ) || [],
      ).filter((node) => node.getClientRects().length);
      const first = nodes[0],
        last = nodes[nodes.length - 1];
      if (!nodes.length) {
        event.preventDefault();
        return;
      }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last || document.activeElement === ref.current)
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, [close]);
  return ref;
}

export function CompanyCard({
  id,
  labels,
  onClose,
  revision,
  busy,
  act,
  pipeline,
  feedback,
}: {
  id: number;
  labels: Record<string, string>;
  onClose: () => void;
  revision: number;
  busy: boolean;
  act: (action: CompanyAction, ids: number[]) => Promise<void>;
  pipeline: Pipeline | null;
  feedback: React.ReactNode;
}) {
  const [company, setCompany] = useState<CompanyDetail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const ref = useDialog(onClose);
  useEffect(() => {
    const controller = new AbortController();
    setCompany(null);
    setLoading(true);
    setError("");
    api<CompanyDetail>(`${base}/companies/${id}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setCompany(data);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(message(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [id, revision]);
  return (
    <>
      <button
        type="button"
        className={styles.overlay}
        aria-label="Закрыть карточку компании"
        tabIndex={-1}
        onClick={onClose}
      />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="company-card-title"
        tabIndex={-1}
        className={styles.drawer}
      >
        <header>
          <div>
            <h2 id="company-card-title">{company ? companyName(company) : "Карточка компании"}</h2>
            {company?.legal_name && company.display_name !== company.legal_name && <small className={styles.muted}>
              Очищено из: {company.legal_name}</small>}
            {company?.domain && safeUrl(company.domain) && (
              <a href={safeUrl(company.domain)} target="_blank" rel="noopener noreferrer">
                {company.domain_display || company.domain} ↗
              </a>
            )}
          </div>
          <button type="button" className={styles.close} aria-label="Закрыть карточку" onClick={onClose}>
            ×
          </button>
        </header>
        {feedback}
        {loading && <p role="status">Загружаем карточку…</p>}
        {error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        {company && (
          <>
            <div className={styles.row}>
              <Score value={company.score} />
              <span className={styles.badge}>{stages[company.stage] || company.stage}</span>
              {company.needs_review && <span className={styles.badge}>На проверке</span>}
              <Fit label={company.fit_label} reason={company.fit_reason} />
            </div>
            {company.fit_reason && <p className={styles.fitReason}>ИИ: {company.fit_reason}</p>}
            <h3>О компании</h3>
            <dl className={styles.facts}>
              {[
                ["Юрлицо", company.legal_name],
                ["ИНН", company.inn],
                ["ОГРН", company.ogrn],
                ["Руководитель", company.director_name],
                ["Должность", company.director_post],
                ["ОКВЭД", company.okved],
                ["Статус юрлица", company.legal_status],
                ["Город", company.city],
                ["Ниша", company.niche],
                [
                  "Выручка",
                  company.revenue_rub != null
                    ? `${money(company.revenue_rub)}${company.revenue_year ? ` (${company.revenue_year})` : ""}`
                    : null,
                ],
              ]
                .filter(([, value]) => value != null && value !== "")
                .map(([label, value]) => (
                  <div key={label} className={styles.wide}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
            </dl>
            <p className={styles.muted}>
              Впервые замечена: {date(company.first_seen_at)}
              <br />
              Последний раз: {date(company.last_seen_at)}
            </p>
            <CompanyTech tech={company.tech}/>
            <h3>Почему {company.score}</h3>
            {company.score_reasons?.length ? (
              <ul className={styles.reasons}>
                {company.score_reasons.map((reason, index) => (
                  <li key={`${reason.kind}-${index}`}>
                    {reason.label}{" "}
                    <strong>
                      {reason.weight > 0 ? "+" : ""}
                      {reason.weight}
                    </strong>
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.muted}>Причины скоринга не получены.</p>
            )}
            <h3>Что крутят</h3>
            {company.ads?.length ? (
              company.ads.map((ad, index) => <Advertisement key={index} ad={ad} />)
            ) : (
              <p className={styles.muted}>Объявлений пока нет.</p>
            )}
            <h3>Контакты</h3>
            {company.contacts?.length ? (
              company.contacts.map((contact) => (
                <article key={contact.id} className={styles.contact}>
                  <span className={styles.badge}>{channels[contact.kind] || contact.kind}</span>
                  <strong>{contact.value}</strong>
                  {contact.kind === "email" && <EmailStatus status={contact.verify_status}/>}
                  {contact.is_personal && (
                    <p className={styles.personal}>
                      Личный — не пишем{contact.person_name ? ` · ${contact.person_name}` : ""}
                    </p>
                  )}
                  <small>
                    Источник: {contact.source || "Не указан"}
                    {safeUrl(contact.source_url) && (
                      <>
                        {" "}
                        ·{" "}
                        <a href={safeUrl(contact.source_url)} target="_blank" rel="noopener noreferrer">
                          Открыть источник ↗
                        </a>
                      </>
                    )}
                  </small>
                  <small>
                    Найден: {date(contact.found_at)} · Проверка: {contact.verify_status || "Нет данных"}
                    {contact.bounced ? " · Недоставлен" : ""}
                  </small>
                </article>
              ))
            ) : (
              <p className={styles.muted}>Контактов пока нет. Запустите сбор контактов.</p>
            )}
            <h3>Сигналы</h3>
            {company.signals?.length ? (
              company.signals.map((signal, index) => (
                <article key={index} className={styles.contact}>
                  <strong>{signalText(signal.kind, signal.payload, labels)}</strong>
                  <small>
                    {date(signal.observed_at)}
                    {signal.expires_at ? ` · До ${date(signal.expires_at)}` : ""}
                  </small>
                </article>
              ))
            ) : (
              <p className={styles.muted}>Сигналы не найдены.</p>
            )}
            <h3>История касаний</h3>
            {company.touches?.length ? (
              <ul className={styles.reasons}>
                {company.touches.map((touch, index) => (
                  <li key={index}>
                    {touch.direction === "in" ? "← " : "→ "}
                    {channelLabel[touch.channel] || touch.channel}
                    {touch.subject ? ` «${touch.subject}»` : ""} · {touchStatus[touch.status] || touch.status}{" "}
                    <small>{date(touch.happened_at)}</small>
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.muted}>Касаний пока не было.</p>
            )}
            <div className={styles.actions}>
              <button
                type="button"
                className={styles.primary}
                disabled={busy || blocked(company.stage)}
                onClick={() => act("outreach", [id])}
              >
                В CRM
              </button>
              <button
                type="button"
                className={styles.secondary}
                disabled={busy || blocked(company.stage)}
                onClick={() => act("enrich", [id])}
              >
                Собрать контакты
              </button>
              <button
                type="button"
                className={styles.secondary}
                disabled={busy}
                onClick={() => act("assess", [id])}
              >
                {company.fit_label ? "Переоценить ИИ" : "Оценить ИИ"}
              </button>
              <button
                type="button"
                className={styles.secondary}
                disabled={busy || company.stage === "dnc"}
                onClick={() => {
                  if (
                    window.confirm(
                      `Добавить «${companyName(company)}» в стоп-лист? Компания будет исключена из аутрича.`,
                    )
                  )
                    act("dnc", [id]);
                }}
              >
                В стоп-лист
              </button>
              <button
                type="button"
                className={styles.secondary}
                disabled={busy}
                onClick={() => act(company.stage === "hidden" ? "restore" : "hide", [id])}
              >
                {company.stage === "hidden" ? "Вернуть в базу" : "Скрыть"}
              </button>
            </div>
            {company.crm_deal_id && pipeline && safeUrl(pipeline.crm_url) && (
              <Link
                className={styles.secondary}
                href={`${pipeline.crm_url}${pipeline.crm_url.includes("?") ? "&" : "?"}deal=${company.crm_deal_id}`}
              >
                Открыть сделку →
              </Link>
            )}
          </>
        )}
      </div>
    </>
  );
}

export function AddToListDialog({
  ids,
  lists,
  close,
  done,
}: {
  ids: number[];
  lists: LeadList[];
  close: () => void;
  done: (text: string) => void;
}) {
  const ref = useDialog(close);
  const manual = lists.filter((l) => l.kind === "manual");
  const [target, setTarget] = useState<string>(manual[0] ? String(manual[0].id) : "new");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function save() {
    setBusy(true);
    setError("");
    try {
      if (target === "new") {
        const created = await post<LeadList>("/lists", { name: name.trim(), company_ids: ids });
        done(`Создан список «${created.name}» · ${created.total} компаний`);
      } else {
        const r = await post<LeadList & { added: number }>(`/lists/${target}/companies`, { company_ids: ids });
        done(`В список «${r.name}» добавлено ${r.added}${r.added < ids.length ? ` (остальные уже были в нём)` : ""}`);
      }
    } catch (e) {
      setError(message(e));
      setBusy(false);
    }
  }
  return (
    <>
      <button type="button" className={styles.overlay} aria-label="Закрыть" tabIndex={-1} onClick={close} />
      <div ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="add-list" className={`${styles.card} ${styles.modal}`}>
        <h2 id="add-list">Добавить {ids.length} в список</h2>
        <p className={styles.muted}>Компании останутся и в своих прежних списках — база одна, без дублей.</p>
        {error && <p className={styles.error}>{error}</p>}
        <label className={styles.field}>
          Список
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="new">＋ Новый список</option>
            {lists.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name} · {l.total}
              </option>
            ))}
          </select>
        </label>
        {target === "new" && (
          <label className={styles.field}>
            Название нового списка
            <input autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={180} placeholder="Например, «Горячие — позвонить»" />
          </label>
        )}
        <div className={styles.actions}>
          <button type="button" className={styles.primary} disabled={busy || (target === "new" && !name.trim())} onClick={save}>
            {busy ? "Добавляем…" : "Добавить"}
          </button>
          <button type="button" className={styles.secondary} onClick={close}>
            Отмена
          </button>
        </div>
      </div>
    </>
  );
}


