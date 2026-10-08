"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { base, message, PageHeader, Score } from "../shared";
import { statusNames } from "../../outreach/email/types";
import styles from "../leadgen.module.css";

type Rate = number | null;
type Analytics = {
  days: number;
  niche: string | null;
  enriched: number;
  funnel: { key: string; label: string; count: number; from_previous: Rate }[];
  niches: {
    niche: string;
    companies: number;
    in_outreach: number;
    replied: number;
    reply_rate: Rate;
    avg_score: number;
  }[];
  sequences: {
    id: number;
    name: string;
    is_active: boolean;
    enrolled: number;
    statuses: Record<string, number>;
    replied: number;
    reply_rate: Rate;
    unsubscribed: number;
    emails: { email: number; sent: number; replies: number; bounced: number; reply_rate: Rate }[];
  }[];
  mailboxes: {
    id: number;
    email: string;
    is_active: boolean;
    sent_7d: number;
    bounced_7d: number;
    bounce_rate: Rate;
    replies_7d: number;
    reply_rate: Rate;
    warning: string | null;
    last_error: string | null;
  }[];
};

const percent = (value: Rate) =>
  value == null ? "—" : `${value.toLocaleString("ru-RU", { maximumFractionDigits: 2 })}%`;

function AnalyticsPage() {
  const params = useSearchParams();
  const router = useRouter();
  const daysValue = Number(params.get("days"));
  const days = [7, 30, 90].includes(daysValue) ? daysValue : 30;
  const niche = params.get("niche") || "";
  const [nicheInput, setNicheInput] = useState(niche);
  const [data, setData] = useState<Analytics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => setNicheInput(niche), [niche]);
  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({ days: String(days) });
    if (niche) query.set("niche", niche);
    setLoading(true);
    setError("");
    setData(null);
    api<Analytics>(`${base}/analytics?${query}`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setData(result);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [days, niche, revision]);

  function filter(period: number, value: string) {
    const query = new URLSearchParams(params.toString());
    query.set("days", String(period));
    if (value.trim()) query.set("niche", value.trim());
    else query.delete("niche");
    router.replace(`/admin/leadgen/analytics?${query}`, { scroll: false });
  }

  return (
    <div className={styles.page}>
      <PageHeader title="Аналитика" subtitle="Воронка компаний, цепочки и почтовые ящики." />
      <section className={styles.card}>
        <form
          className={styles.actions}
          onSubmit={(event) => {
            event.preventDefault();
            filter(days, nicheInput);
          }}
        >
          <div className={styles.actions} aria-label="Период">
            {[7, 30, 90].map((period) => (
              <button
                key={period}
                type="button"
                className={styles.source}
                aria-pressed={days === period}
                disabled={loading}
                onClick={() => filter(period, nicheInput)}
              >
                {period} дней
              </button>
            ))}
          </div>
          <label className={styles.field}>
            Ниша
            <input value={nicheInput} onChange={(event) => setNicheInput(event.target.value)} />
          </label>
          <button className={styles.primary} disabled={loading}>
            Применить
          </button>
          <button
            type="button"
            className={styles.secondary}
            disabled={loading}
            onClick={() => setRevision((value) => value + 1)}
          >
            Обновить
          </button>
        </form>
      </section>
      {loading && <p role="status">Загружаем аналитику…</p>}
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      {data && (data.funnel[0]?.count || 0) === 0 && (
        <section className={styles.card}>
          <h2>Данных пока нет</h2>
          <Link href="/admin/leadgen/search">Поиск компаний →</Link>
        </section>
      )}
      {data && (data.funnel[0]?.count || 0) > 0 && (
        <>
          <section className={styles.card}>
            <h2>Воронка</h2>
            <p className={styles.muted}>Обогащено: {data.enriched}</p>
            <div className={styles.funnel}>
              {data.funnel.map((stage) => (
                <div className={styles.funnelRow} key={stage.key}>
                  <span>{stage.label}</span>
                  <div className={styles.funnelTrack} aria-hidden="true">
                    <div
                      className={styles.funnelBar}
                      style={{ width: `${Math.min(100, (stage.count / data.funnel[0].count) * 100)}%` }}
                    />
                  </div>
                  <div className={styles.funnelValue}>
                    <strong>{stage.count}</strong>
                    <small>
                      {stage.from_previous == null ? "—" : `↓ ${percent(stage.from_previous)} от предыдущего`}
                    </small>
                  </div>
                </div>
              ))}
            </div>
          </section>
          <section className={styles.card}>
            <h2>Ниши</h2>
            {!data.niches.length ? (
              <p className={styles.empty}>Нет данных по нишам.</p>
            ) : (
              <div className={styles.scroll}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      {["Ниша", "Компании", "В аутриче", "Ответили", "% ответов", "Средний скоринг"].map(
                        (label) => (
                          <th scope="col" key={label}>
                            {label}
                          </th>
                        ),
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {data.niches.map((item) => (
                      <tr key={item.niche}>
                        <td>
                          <Link href={`/admin/leadgen/companies?niche=${encodeURIComponent(item.niche)}`}>
                            {item.niche}
                          </Link>
                        </td>
                        <td>{item.companies}</td>
                        <td>{item.in_outreach}</td>
                        <td>{item.replied}</td>
                        <td>{percent(item.reply_rate)}</td>
                        <td>
                          <Score value={item.avg_score} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <h2 className={styles.sectionTitle}>Цепочки</h2>
          {!data.sequences.length && <p className={styles.empty}>Цепочек пока нет.</p>}
          {data.sequences.map((sequence) => {
            const rates = sequence.emails.flatMap((email) =>
              email.reply_rate == null ? [] : [email.reply_rate],
            );
            const best = rates.length ? Math.max(...rates) : null;
            return (
              <section key={sequence.id} className={styles.card}>
                <h3>
                  {sequence.name}{" "}
                  <span className={styles.badge}>{sequence.is_active ? "Включена" : "Выключена"}</span>
                </h3>
                <dl className={styles.metrics}>
                  <div>
                    <dt>Участники</dt>
                    <dd>{sequence.enrolled}</dd>
                  </div>
                  <div>
                    <dt>Ответили</dt>
                    <dd>{sequence.replied}</dd>
                  </div>
                  <div>
                    <dt>Ответы</dt>
                    <dd>{percent(sequence.reply_rate)}</dd>
                  </div>
                  <div>
                    <dt>Отписались</dt>
                    <dd>{sequence.unsubscribed}</dd>
                  </div>
                </dl>
                <p className={styles.muted}>
                  {Object.entries(sequence.statuses)
                    .map(([status, count]) => `${statusNames[status] || status}: ${count}`)
                    .join(" · ")}
                </p>
                {!sequence.emails.length ? (
                  <p className={styles.empty}>Письма ещё не отправлялись.</p>
                ) : (
                  <div className={styles.scroll}>
                    <table className={styles.table}>
                      <thead>
                        <tr>
                          {["Письмо", "Отправлено", "Ответы", "% ответов", "Возвраты"].map((label) => (
                            <th scope="col" key={label}>
                              {label}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {sequence.emails.map((email) => (
                          <tr key={email.email}>
                            <td>Письмо {email.email}</td>
                            <td>{email.sent}</td>
                            <td>{email.replies}</td>
                            <td>
                              {email.reply_rate != null && email.reply_rate === best ? (
                                <strong className={styles.bestRate}>{percent(email.reply_rate)}</strong>
                              ) : (
                                percent(email.reply_rate)
                              )}
                            </td>
                            <td>{email.bounced}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            );
          })}
          <h2 className={styles.sectionTitle}>Ящики · последние 7 дней</h2>
          {!data.mailboxes.length && <p className={styles.empty}>Ящиков пока нет.</p>}
          <div className={styles.summaryGrid}>
            {data.mailboxes.map((mailbox) => (
              <section className={styles.card} key={mailbox.id}>
                <h3>{mailbox.email}</h3>
                <p className={styles.muted}>{mailbox.is_active ? "Включён" : "Выключен"}</p>
                {mailbox.warning != null && <p className={styles.error}>{mailbox.warning}</p>}
                <dl className={styles.metrics}>
                  <div>
                    <dt>Отправлено</dt>
                    <dd>{mailbox.sent_7d}</dd>
                  </div>
                  <div>
                    <dt>Возвраты</dt>
                    <dd>
                      {mailbox.bounced_7d} · {percent(mailbox.bounce_rate)}
                    </dd>
                  </div>
                  <div>
                    <dt>Ответы</dt>
                    <dd>
                      {mailbox.replies_7d} · {percent(mailbox.reply_rate)}
                    </dd>
                  </div>
                </dl>
                {mailbox.last_error && <small className={styles.errorText}>{mailbox.last_error}</small>}
              </section>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<p role="status">Загружаем аналитику…</p>}>
      <AnalyticsPage />
    </Suspense>
  );
}
