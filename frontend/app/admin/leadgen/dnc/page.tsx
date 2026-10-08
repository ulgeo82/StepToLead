"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { base, date, message, PageHeader, post } from "../shared";
import styles from "../leadgen.module.css";

const kinds: Record<string, string> = {
  company: "Компания (id)",
  domain: "Домен",
  email: "Email",
  phone: "Телефон",
  telegram: "Telegram",
};
const reasons: Record<string, string> = {
  unsubscribed: "Отписались",
  refused: "Отказ",
  client: "Клиент",
  competitor: "Конкурент",
  bounced: "Недоставка",
  legal: "Юридически",
};
type Entry = {
  id: number;
  kind: string;
  value: string;
  reason: string;
  note: string | null;
  until: string | null;
  created_at: string;
};

export default function Page() {
  const [items, setItems] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [kindFilter, setKindFilter] = useState("");
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  const [now, setNow] = useState(Date.now);
  const form = useRef<HTMLFormElement>(null);
  const mutation = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    api<{ items: Entry[] }>(`${base}/dnc`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setItems(result.items);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60000);
    return () => {
      clearInterval(timer);
      mutation.current?.abort();
    };
  }, []);

  async function add() {
    if (!form.current || busy) return;
    const fields = new FormData(form.current);
    const until = String(fields.get("until") || "");
    setBusy(true);
    setError("");
    setNotice("");
    const controller = new AbortController();
    mutation.current = controller;
    try {
      await post(
        "/dnc",
        {
          kind: fields.get("kind"),
          value: String(fields.get("value") || "").trim(),
          reason: fields.get("reason"),
          note: String(fields.get("note") || "").trim() || undefined,
          until: until ? new Date(until).toISOString() : undefined,
        },
        controller.signal,
      );
      if (!controller.signal.aborted) {
        form.current?.reset();
        setNotice("Запись добавлена.");
        setRevision((value) => value + 1);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(message(reason));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function remove(entry: Entry) {
    if (busy || !confirm(`Удалить из стоп-листа: ${entry.value}?`)) return;
    const controller = new AbortController();
    mutation.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`${base}/dnc/${entry.id}`, { method: "DELETE", signal: controller.signal });
      if (!controller.signal.aborted) {
        setNotice("Запись удалена.");
        setRevision((value) => value + 1);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(message(reason));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const visible = items.filter(
    (entry) =>
      (!kindFilter || entry.kind === kindFilter) &&
      entry.value.toLocaleLowerCase("ru").includes(query.trim().toLocaleLowerCase("ru")),
  );

  return (
    <div className={styles.page}>
      <PageHeader title="Стоп-лист" subtitle="Исключения из аутрича." />
      {loading && <p role="status">Загружаем стоп-лист…</p>}
      {busy && <p role="status">Сохраняем изменения…</p>}
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className={styles.notice}>
          {notice}
        </p>
      )}
      <section className={styles.card}>
        <h2>Добавить запись</h2>
        <form
          ref={form}
          onSubmit={(event) => {
            event.preventDefault();
            void add();
          }}
        >
          <fieldset className={`${styles.formFieldset} ${styles.fields}`} disabled={loading || busy}>
            <label className={styles.field}>
              Тип
              <select name="kind" defaultValue="company">
                {Object.entries(kinds).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.field}>
              Значение
              <input name="value" required maxLength={300} />
            </label>
            <label className={styles.field}>
              Причина
              <select name="reason" defaultValue="refused">
                {Object.entries(reasons).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className={styles.field}>
              До (необязательно)
              <input name="until" type="datetime-local" />
            </label>
            <label className={`${styles.field} ${styles.wide}`}>
              Заметка
              <textarea name="note" maxLength={1000} />
            </label>
            <div className={styles.actions}>
              <button className={styles.primary} disabled={loading || busy}>
                Добавить
              </button>
            </div>
          </fieldset>
        </form>
      </section>
      <section className={styles.card}>
        <h2>Записи</h2>
        <div className={styles.filters}>
          <label className={styles.field}>
            Тип записи
            <select value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}>
              <option value="">Все типы</option>
              {Object.entries(kinds).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            Поиск по значению
            <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
        </div>
        <button
          type="button"
          className={styles.secondary}
          disabled={loading || busy}
          onClick={() => {
            setError("");
            setRevision((value) => value + 1);
          }}
        >
          Обновить
        </button>
        {!loading && !visible.length ? (
          <p className={styles.empty}>{items.length ? "По фильтрам записей нет." : "Стоп-лист пуст."}</p>
        ) : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  {["Тип", "Значение", "Причина", "Заметка", "До", "Добавлено", "Действия"].map((label) => (
                    <th scope="col" key={label}>
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visible.map((entry) => {
                  const expired = entry.until != null && new Date(entry.until).getTime() < now;
                  return (
                    <tr key={entry.id} className={expired ? styles.expired : undefined}>
                      <td>{kinds[entry.kind] || entry.kind}</td>
                      <td>
                        {entry.kind === "company" ? (
                          <Link href={`/admin/leadgen/companies?company=${encodeURIComponent(entry.value)}`}>
                            {entry.value}
                          </Link>
                        ) : (
                          entry.value
                        )}
                      </td>
                      <td>{reasons[entry.reason] || entry.reason}</td>
                      <td>{entry.note || "—"}</td>
                      <td>
                        {date(entry.until)}
                        {expired && <small className={styles.expiredLabel}>истёк</small>}
                      </td>
                      <td>{date(entry.created_at)}</td>
                      <td>
                        <button
                          type="button"
                          className={styles.secondary}
                          disabled={loading || busy}
                          aria-label={`Удалить ${entry.value}`}
                          onClick={() => void remove(entry)}
                        >
                          Удалить
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
