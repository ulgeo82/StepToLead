"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { base, message } from "./shared";
import styles from "./leadgen.module.css";

type ImportResult = {
  id: number;
  stats: {
    rows?: number;
    companies?: number;
    created?: number;
    already_in_base?: number;
    needs_review?: number;
    invalid?: number;
    truncated?: boolean;
    columns?: Record<string, number>;
    errors?: { line: number; reason: string; value: string }[];
  };
  enrich_queued: number;
};

const columnNames: Record<string, string> = {
  domain: "сайт",
  name: "название",
  legal_name: "юрлицо",
  city: "город",
  niche: "ниша",
  phone: "телефон",
  email: "почта",
  inn: "ИНН",
  telegram: "Telegram",
  whatsapp: "WhatsApp",
};

export default function ImportDialog({ close, completed }: { close: () => void; completed: () => void }) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [niche, setNiche] = useState("");
  const [city, setCity] = useState("");
  const [enrich, setEnrich] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) close();
    };
    document.addEventListener("keydown", key);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, [close, busy]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || (!file && !text.trim())) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    if (file) form.append("file", file);
    else form.append("text", text);
    if (niche.trim()) form.append("niche", niche.trim());
    if (city.trim()) form.append("city", city.trim());
    form.append("enrich", String(enrich));
    try {
      const data = await api<ImportResult>(`${base}/companies/import`, {
        method: "POST",
        body: form,
        timeoutMs: 120_000,
      });
      setResult(data);
      completed();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  const s = result?.stats;
  return (
    <>
      <button type="button" className={styles.overlay} aria-label="Закрыть импорт" tabIndex={-1} onClick={close} />
      <div
        ref={ref}
        className={`${styles.card} ${styles.modal}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-title"
        tabIndex={-1}
      >
        <h2 id="import-title">Импорт базы</h2>
        {error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        {result && s ? (
          <div role="status" className={styles.notice}>
            <strong>
              Компаний: {s.companies ?? 0} · новых {s.created ?? 0} · уже были в базе {s.already_in_base ?? 0}
            </strong>
            {!!s.needs_review && <p>На проверке (возможный дубль): {s.needs_review}</p>}
            {!!s.invalid && <p>Пропущено строк: {s.invalid}</p>}
            {s.truncated && <p>Взяли первые 2000 строк — остальное загрузите отдельным файлом.</p>}
            {s.columns && Object.keys(s.columns).length > 0 && (
              <p>
                Узнали колонки:{" "}
                {Object.keys(s.columns)
                  .map((k) => columnNames[k] || k)
                  .join(", ")}
              </p>
            )}
            {result.enrich_queued > 0 && (
              <p>Сбор контактов с сайтов запущен для {result.enrich_queued} компаний — займёт несколько минут.</p>
            )}
            {s.errors?.slice(0, 10).map((item) => (
              <p key={`${item.line}-${item.value}`}>
                <small>
                  Строка {item.line}: {item.reason}
                  {item.value ? ` — ${item.value}` : ""}
                </small>
              </p>
            ))}
            <p>
              <Link href={`/admin/leadgen/companies?run_id=${result.id}`} onClick={close}>
                Открыть компании импорта →
              </Link>
            </p>
          </div>
        ) : (
          <form onSubmit={submit}>
            <p className={styles.muted}>
              CSV, Excel (.xlsx) или список сайтов — по одному на строку. Колонки узнаём по заголовкам: сайт,
              название, город, ниша, телефон, email, ИНН. Дубли с базой склеиваются.
            </p>
            <label className={styles.field}>
              Файл
              <input
                type="file"
                accept=".csv,.txt,.tsv,.xlsx,text/csv,text/plain"
                onChange={(e) => setFile(e.target.files?.[0] || null)}
              />
            </label>
            {!file && (
              <label className={styles.field}>
                Или вставьте список
                <textarea
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  rows={8}
                  placeholder={"romax63.ru\nhttps://agata63.ru/\nСайт;Город;Ниша"}
                />
              </label>
            )}
            <div className={styles.fields}>
              <label className={styles.field}>
                Ниша (если нет в файле)
                <input value={niche} onChange={(e) => setNiche(e.target.value)} maxLength={120} />
              </label>
              <label className={styles.field}>
                Город (если нет в файле)
                <input value={city} onChange={(e) => setCity(e.target.value)} maxLength={120} />
              </label>
            </div>
            <label className={styles.check}>
              <input type="checkbox" checked={enrich} onChange={(e) => setEnrich(e.target.checked)} />
              Сразу собрать контакты с сайтов
            </label>
            <div className={styles.actions}>
              <button type="submit" className={styles.primary} disabled={busy || (!file && !text.trim())}>
                {busy ? "Загружаем…" : "Импортировать"}
              </button>
              <button type="button" className={styles.secondary} disabled={busy} onClick={close}>
                Отмена
              </button>
            </div>
          </form>
        )}
        {result && (
          <div className={styles.actions}>
            <button type="button" className={styles.secondary} onClick={close}>
              Закрыть
            </button>
          </div>
        )}
      </div>
    </>
  );
}
