"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api, API_URL } from "@/lib/api";
import { base, LeadList, message, plural } from "./shared";
import styles from "./leadgen.module.css";

type Source = "filter" | "selected" | "lists";

/**
 * Окно выгрузки: откуда (текущий фильтр / выбранные / списки), какие компании (только новые, с контактами,
 * подходящие по ИИ) и какие колонки. Перед скачиванием видно, сколько компаний попадёт в файл.
 * Стоп-лист, отказы и скрытые не выгружаются никогда — это делает сервер.
 */
export default function ExportDialog({
  close,
  done,
  lists,
  filterQuery,
  filterLabel,
  selectedIds = [],
  initialLists = [],
}: {
  close: () => void;
  done?: () => void;
  lists: LeadList[];
  /** Текущие фильтры базы (без ids) — если не задано, источника «фильтр» нет. */
  filterQuery?: string;
  filterLabel?: string;
  selectedIds?: number[];
  initialLists?: number[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [source, setSource] = useState<Source>(
    selectedIds.length ? "selected" : initialLists.length || filterQuery === undefined ? "lists" : "filter",
  );
  const [chosen, setChosen] = useState<number[]>(initialLists);
  const [onlyNew, setOnlyNew] = useState(true);
  const [withContacts, setWithContacts] = useState(true);
  const [onlyFit, setOnlyFit] = useState(false);
  const [preset, setPreset] = useState<"outreach" | "full">("outreach");
  const [count, setCount] = useState<{ count: number; total: number; max: number } | null>(null);
  const [counting, setCounting] = useState(false);
  const [error, setError] = useState("");
  const [started, setStarted] = useState(false);

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
    };
    document.addEventListener("keydown", key);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener("keydown", key);
      previous?.focus();
    };
  }, [close]);

  // Массивы из пропсов каждый рендер новые — зависим от строкового ключа, иначе пересчёт идёт по кругу.
  const selectedKey = selectedIds.join(",");
  const chosenKey = chosen.join(",");
  const query = useMemo(() => {
    const q = new URLSearchParams(source === "filter" ? filterQuery || "" : "");
    q.delete("limit");
    q.delete("offset");
    if (source === "selected") selectedKey.split(",").filter(Boolean).forEach((id) => q.append("ids", id));
    if (source === "lists") chosenKey.split(",").filter(Boolean).forEach((id) => q.append("list_id", id));
    if (onlyNew) q.set("not_exported", "true");
    if (withContacts) q.set("has_contacts", "true");
    if (onlyFit) q.set("fit", "fit");
    q.set("preset", preset);
    return q;
  }, [source, filterQuery, selectedKey, chosenKey, onlyNew, withContacts, onlyFit, preset]);
  const queryKey = query.toString();
  const ready = source !== "lists" || chosen.length > 0;

  useEffect(() => {
    setCount(null);
    setError("");
    if (!ready) return;
    const controller = new AbortController();
    setCounting(true);
    const timer = setTimeout(() => {
      const q = new URLSearchParams(queryKey);
      q.set("count_only", "true");
      api<{ count: number; total: number; max: number }>(`${base}/companies/export?${q}`, { signal: controller.signal })
        .then((value) => !controller.signal.aborted && setCount(value))
        .catch((e) => !controller.signal.aborted && setError(message(e)))
        .finally(() => !controller.signal.aborted && setCounting(false));
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [queryKey, ready]);

  const href = `${API_URL}/api${base}/companies/export?${query}`;
  const toggle = (id: number, on: boolean) => setChosen((ids) => (on ? [...ids, id] : ids.filter((x) => x !== id)));

  return (
    <>
      <button type="button" className={styles.overlay} aria-label="Закрыть выгрузку" tabIndex={-1} onClick={close} />
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-title"
        className={`${styles.card} ${styles.modal} ${styles.modalWide}`}
      >
        <h2 id="export-title">Выгрузка в Excel</h2>

        <fieldset className={styles.formFieldset}>
          <legend className={styles.legend}>Что выгрузить</legend>
          <div className={styles.segmented} role="radiogroup">
            {filterQuery !== undefined && (
              <button type="button" role="radio" aria-checked={source === "filter"} onClick={() => setSource("filter")}>
                Текущий фильтр
                <small>{filterLabel || "как сейчас в базе"}</small>
              </button>
            )}
            {selectedIds.length > 0 && (
              <button type="button" role="radio" aria-checked={source === "selected"} onClick={() => setSource("selected")}>
                Выбранные
                <small>
                  {selectedIds.length} {plural(selectedIds.length, "компания", "компании", "компаний")}
                </small>
              </button>
            )}
            <button type="button" role="radio" aria-checked={source === "lists"} onClick={() => setSource("lists")}>
              Списки
              <small>{chosen.length ? `выбрано ${chosen.length}` : "один или несколько"}</small>
            </button>
          </div>
          {source === "lists" && (
            <div className={styles.listPicker}>
              {!lists.length && <p className={styles.muted}>Списков пока нет — они появятся после первого поиска.</p>}
              {lists.map((list) => (
                <label key={list.id} className={styles.listOption}>
                  <input type="checkbox" checked={chosen.includes(list.id)} onChange={(e) => toggle(list.id, e.target.checked)} />
                  <span>
                    <b>{list.name}</b>
                    <small>
                      {list.total} {plural(list.total, "компания", "компании", "компаний")} · не выгружались {list.not_exported}
                    </small>
                  </span>
                </label>
              ))}
            </div>
          )}
        </fieldset>

        <fieldset className={styles.formFieldset}>
          <legend className={styles.legend}>Какие компании</legend>
          <label className={styles.check}>
            <input type="checkbox" checked={onlyNew} onChange={(e) => setOnlyNew(e.target.checked)} />
            <span>
              Только новые — ни разу не выгружались
              <small className={styles.hint}>Портал помнит все выгрузки: одна компания не попадёт в два файла</small>
            </span>
          </label>
          <label className={styles.check}>
            <input type="checkbox" checked={withContacts} onChange={(e) => setWithContacts(e.target.checked)} />
            Только с контактами
          </label>
          <label className={styles.check}>
            <input type="checkbox" checked={onlyFit} onChange={(e) => setOnlyFit(e.target.checked)} />
            Только «Подходит» по ИИ-оценке
          </label>
          <p className={styles.hint}>Стоп-лист, отказы и скрытые компании в файл не попадают никогда.</p>
        </fieldset>

        <fieldset className={styles.formFieldset}>
          <legend className={styles.legend}>Колонки</legend>
          <div className={styles.segmented} role="radiogroup">
            <button type="button" role="radio" aria-checked={preset === "outreach"} onClick={() => setPreset("outreach")}>
              Для рассылки
              <small>компания, сайт, город, контакты</small>
            </button>
            <button type="button" role="radio" aria-checked={preset === "full"} onClick={() => setPreset("full")}>
              Полная
              <small>всё: скоринг, юрлицо, реклама</small>
            </button>
          </div>
        </fieldset>

        <div className={styles.exportSummary} aria-live="polite">
          {!ready ? (
            "Выберите хотя бы один список"
          ) : counting || !count ? (
            error || "Считаем…"
          ) : count.count ? (
            <>
              В файл попадёт <b>{count.count}</b> {plural(count.count, "компания", "компании", "компаний")}
              {count.total > count.max && ` (из ${count.total} — максимум ${count.max} за раз)`}
            </>
          ) : onlyNew ? (
            "Новых компаний нет — все уже выгружались. Снимите «Только новые», чтобы скачать повторно."
          ) : (
            "Под эти условия компаний нет"
          )}
        </div>
        {error && count === null && !counting && <p className={styles.error}>{error}</p>}

        <div className={styles.actions}>
          <a
            className={styles.primary}
            href={href}
            download
            aria-disabled={!count?.count}
            onClick={(e) => {
              if (!count?.count) {
                e.preventDefault();
                return;
              }
              setStarted(true);
              setTimeout(() => done?.(), 1500);
            }}
          >
            {started ? "Скачивается…" : "Скачать Excel"}
          </a>
          <button type="button" className={styles.secondary} onClick={close}>
            {started ? "Готово" : "Отмена"}
          </button>
        </div>
      </div>
    </>
  );
}
