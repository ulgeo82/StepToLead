"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, API_URL } from "@/lib/api";
import EnrollDialog from "../../outreach/email/EnrollDialog";
import ExportDialog from "../ExportDialog";
import { base, date, ExportRecord, LeadList, message, PageHeader, plural, post } from "../shared";
import styles from "../leadgen.module.css";

type Tab = "active" | "archived";

const pct = (part: number, total: number) => (total ? Math.round((part / total) * 100) : 0);

export default function ListsPage() {
  const [tab, setTab] = useState<Tab>("active");
  const [items, setItems] = useState<LeadList[]>([]);
  const [archivedCount, setArchivedCount] = useState(0);
  const [allActive, setAllActive] = useState<LeadList[]>([]);
  const [exports, setExports] = useState<ExportRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [renaming, setRenaming] = useState<{ id: number; name: string } | null>(null);
  const [exportFor, setExportFor] = useState<number[] | null>(null);
  const [enrollFor, setEnrollFor] = useState<LeadList | null>(null);
  const [deleting, setDeleting] = useState<LeadList | null>(null);
  const [withCompanies, setWithCompanies] = useState(false);
  const [merging, setMerging] = useState<string | null>(null);
  const [creating, setCreating] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((n) => n + 1), []);
  const closeExport = useCallback(() => setExportFor(null), []);
  const closeEnroll = useCallback(() => setEnrollFor(null), []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    Promise.all([
      api<{ items: LeadList[]; archived_count: number }>(`${base}/lists?archived=${tab === "archived"}`, { signal: controller.signal }),
      tab === "archived"
        ? api<{ items: LeadList[] }>(`${base}/lists`, { signal: controller.signal })
        : Promise.resolve(null),
      api<{ items: ExportRecord[] }>(`${base}/exports?limit=20`, { signal: controller.signal }),
    ])
      .then(([current, active, history]) => {
        if (controller.signal.aborted) return;
        setItems(current.items);
        setArchivedCount(current.archived_count);
        setAllActive(active ? active.items : current.items);
        setExports(history.items);
        setError("");
      })
      .catch((e) => !controller.signal.aborted && setError(message(e)))
      .finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
  }, [tab, revision]);

  useEffect(() => setSelected([]), [tab]);

  async function run(action: () => Promise<unknown>, done: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await action();
      setNotice(typeof result === "string" ? result : done);
      refresh();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  const patch = (id: number, body: object) =>
    api(`${base}/lists/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  const totals = items.reduce(
    (acc, l) => ({ total: acc.total + l.total, fresh: acc.fresh + l.not_exported }),
    { total: 0, fresh: 0 },
  );

  return (
    <div className={`page ${styles.page}`}>
      <PageHeader
        title="Списки и выгрузки"
        subtitle="Каждый поиск — отдельный список. База общая и без дублей: одна компания может быть в нескольких списках."
      >
        <div className={styles.actions}>
          <button type="button" className={styles.secondary} onClick={() => setCreating("")}>
            ＋ Новый список
          </button>
          <button type="button" className={styles.primary} onClick={() => setExportFor([])}>
            Выгрузка в Excel
          </button>
        </div>
      </PageHeader>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className={styles.notice} role="status">
          {notice}
        </p>
      )}

      <div className={styles.tabs} aria-label="Списки">
        <button type="button" className={styles.tab} aria-pressed={tab === "active"} onClick={() => setTab("active")}>
          Активные
          <span>{tab === "active" ? items.length : allActive.length}</span>
        </button>
        <button type="button" className={styles.tab} aria-pressed={tab === "archived"} onClick={() => setTab("archived")}>
          Архив
          <span>{archivedCount}</span>
        </button>
      </div>

      <section className={styles.card} aria-label="Списки компаний">
        <div className={styles.row}>
          <h2>
            {tab === "active" ? "Активные списки" : "Архив"} · {items.length}
          </h2>
          {tab === "active" && (
            <span className={styles.muted}>
              Компаний в списках: {totals.total} · ещё не выгружались: {totals.fresh}
            </span>
          )}
        </div>
        {selected.length > 0 && (
          <div className={styles.actions}>
            <span>Выбрано: {selected.length}</span>
            <button type="button" className={styles.primary} disabled={busy} onClick={() => setExportFor([...selected])}>
              Выгрузить выбранные
            </button>
            {selected.length > 1 && (
              <button type="button" className={styles.secondary} disabled={busy} onClick={() => setMerging("")}>
                Объединить в один список
              </button>
            )}
            <button
              type="button"
              className={styles.secondary}
              disabled={busy}
              onClick={() =>
                run(
                  () => Promise.all(selected.map((id) => patch(id, { archived: tab === "active" }))),
                  tab === "active" ? "Списки перенесены в архив" : "Списки возвращены",
                )
              }
            >
              {tab === "active" ? "В архив" : "Вернуть из архива"}
            </button>
            <button type="button" className={styles.secondary} onClick={() => setSelected([])}>
              Снять выбор
            </button>
          </div>
        )}
        {loading ? (
          <p role="status" className={styles.empty}>
            Загружаем списки…
          </p>
        ) : !items.length ? (
          <div className={styles.empty}>
            <h2>{tab === "active" ? "Списков пока нет" : "Архив пуст"}</h2>
            {tab === "active" && (
              <>
                <p className={styles.muted}>Запустите поиск — каждый запуск автоматически станет списком.</p>
                <Link className={styles.primary} href="/admin/leadgen/search">
                  Поиск компаний
                </Link>
              </>
            )}
          </div>
        ) : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">
                    <input
                      type="checkbox"
                      aria-label="Выбрать все списки"
                      checked={items.every((l) => selected.includes(l.id))}
                      onChange={(e) => setSelected(e.target.checked ? items.map((l) => l.id) : [])}
                    />
                  </th>
                  <th scope="col">Список</th>
                  <th scope="col">Компаний</th>
                  <th scope="col">С контактами</th>
                  <th scope="col">Подходят (ИИ)</th>
                  <th scope="col">Не выгружались</th>
                  <th scope="col">Действия</th>
                </tr>
              </thead>
              <tbody>
                {items.map((list) => (
                  <tr key={list.id} className={selected.includes(list.id) ? styles.selected : ""}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Выбрать ${list.name}`}
                        checked={selected.includes(list.id)}
                        onChange={(e) =>
                          setSelected((ids) => (e.target.checked ? [...ids, list.id] : ids.filter((x) => x !== list.id)))
                        }
                      />
                    </td>
                    <td>
                      {renaming?.id === list.id ? (
                        <form
                          className={styles.inlineForm}
                          onSubmit={(e) => {
                            e.preventDefault();
                            const name = renaming.name.trim();
                            if (name) run(() => patch(list.id, { name }), "Список переименован").then(() => setRenaming(null));
                          }}
                        >
                          <input
                            autoFocus
                            value={renaming.name}
                            maxLength={180}
                            aria-label="Название списка"
                            onChange={(e) => setRenaming({ id: list.id, name: e.target.value })}
                            onKeyDown={(e) => e.key === "Escape" && setRenaming(null)}
                          />
                          <button type="submit" className={styles.secondary} disabled={busy}>
                            Сохранить
                          </button>
                        </form>
                      ) : (
                        <>
                          <Link className={styles.companyButton} href={`/admin/leadgen/companies?list_id=${list.id}`}>
                            {list.name}
                          </Link>
                          <small>
                            {list.kind === "run" ? `Запуск #${list.run_id}` : "Свой список"} · {date(list.created_at)}
                            {list.new > 0 && ` · ${list.new} новых в базе`}
                          </small>
                          {list.note && <small className={styles.muted}>{list.note}</small>}
                        </>
                      )}
                    </td>
                    <td>
                      <b>{list.total}</b>
                    </td>
                    <td>
                      {list.with_contacts}
                      <small className={styles.muted}> · {pct(list.with_contacts, list.total)}%</small>
                    </td>
                    <td>{list.fit || <span className={styles.muted}>—</span>}</td>
                    <td>
                      {list.not_exported ? (
                        <span className={`${styles.badge} ${styles.fitYes}`}>{list.not_exported}</span>
                      ) : (
                        <span className={styles.muted}>все выгружены</span>
                      )}
                    </td>
                    <td>
                      <div className={styles.rowActions}>
                        <button type="button" className={styles.secondary} disabled={!list.total} onClick={() => setExportFor([list.id])}>
                          Скачать
                        </button>
                        <details className={styles.menu}>
                          <summary aria-label={`Ещё действия со списком ${list.name}`}>⋯</summary>
                          <div>
                            <Link href={`/admin/leadgen/companies?list_id=${list.id}`}>Открыть в базе</Link>
                            <button type="button" onClick={() => setRenaming({ id: list.id, name: list.name })}>
                              Переименовать
                            </button>
                            <button
                              type="button"
                              disabled={busy || !list.total}
                              onClick={() =>
                                run(() => post(`/lists/${list.id}/assess`, {}), "ИИ-оценка списка запущена — обновите страницу через пару минут")
                              }
                            >
                              Оценить ИИ
                            </button>
                            <button type="button" disabled={!list.total} onClick={() => setEnrollFor(list)}>
                              Запустить цепочку
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                run(
                                  () => patch(list.id, { archived: !list.archived }),
                                  list.archived ? "Список возвращён" : "Список в архиве — компании остались в базе",
                                )
                              }
                            >
                              {list.archived ? "Вернуть из архива" : "В архив"}
                            </button>
                            <button type="button" className={styles.danger} onClick={() => { setWithCompanies(false); setDeleting(list); }}>
                              Удалить…
                            </button>
                          </div>
                        </details>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className={styles.card} aria-label="История выгрузок">
        <div className={styles.row}>
          <h2>История выгрузок</h2>
          <span className={styles.muted}>Повтор скачивает ровно тот же набор компаний и не меняет отметки «выгружено»</span>
        </div>
        {!exports.length ? (
          <p className={styles.muted}>Выгрузок пока не было.</p>
        ) : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Когда</th>
                  <th scope="col">Что</th>
                  <th scope="col">Компаний</th>
                  <th scope="col">Колонки</th>
                  <th scope="col">Кто</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {exports.map((item) => (
                  <tr key={item.id}>
                    <td>{date(item.created_at)}</td>
                    <td>{item.title}</td>
                    <td>{item.count}</td>
                    <td>{item.preset === "outreach" ? "Для рассылки" : "Полная"}</td>
                    <td>{item.created_by || "—"}</td>
                    <td>
                      <a className={styles.secondary} href={`${API_URL}/api${base}/exports/${item.id}/download`} download>
                        Скачать снова
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {exportFor && (
        <ExportDialog close={closeExport} done={refresh} lists={allActive} initialLists={exportFor} />
      )}
      {enrollFor && (
        <EnrollDialog
          ids={Array.from({ length: enrollFor.total }, (_, i) => i)}
          listId={enrollFor.id}
          listName={enrollFor.name}
          close={closeEnroll}
          completed={refresh}
        />
      )}
      {(merging !== null || creating !== null) && (
        <NameDialog
          title={merging !== null ? `Объединить ${selected.length} ${plural(selected.length, "список", "списка", "списков")}` : "Новый список"}
          hint={
            merging !== null
              ? "Дубли склеятся сами. Исходные списки уйдут в архив — компании никуда не денутся."
              : "Пустой список. Компании добавляются из «Базы компаний»: выделите и нажмите «В список»."
          }
          busy={busy}
          close={() => {
            setMerging(null);
            setCreating(null);
          }}
          save={(name) =>
            run(
              () => (merging !== null ? post("/lists/merge", { list_ids: selected, name }) : post("/lists", { name })),
              merging !== null ? `Списки объединены в «${name}»` : `Список «${name}» создан`,
            ).then(() => {
              setMerging(null);
              setCreating(null);
              setSelected([]);
            })
          }
        />
      )}
      {deleting && (
        <>
          <button type="button" className={styles.overlay} aria-label="Закрыть" tabIndex={-1} onClick={() => setDeleting(null)} />
          <div role="dialog" aria-modal="true" aria-labelledby="delete-list" className={`${styles.card} ${styles.modal}`}>
            <h2 id="delete-list">Удалить список «{deleting.name}»?</h2>
            <p className={styles.muted}>Сам список удалится. Компании по умолчанию остаются в базе и в других списках.</p>
            <label className={styles.check}>
              <input type="checkbox" checked={withCompanies} onChange={(e) => setWithCompanies(e.target.checked)} />
              <span>
                Удалить и компании, которых нет в других списках
                <small className={styles.hint}>Компании со сделкой в CRM или в идущей цепочке не удаляются</small>
              </span>
            </label>
            <div className={styles.actions}>
              <button
                type="button"
                className={`${styles.secondary} ${styles.danger}`}
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const r = await post<{ deleted: number; skipped: unknown[] }>(`/lists/${deleting.id}/delete`, {
                      with_companies: withCompanies,
                    });
                    return `Список удалён${withCompanies ? ` · компаний удалено: ${r.deleted}${r.skipped.length ? `, оставлено (заняты): ${r.skipped.length}` : ""}` : ""}`;
                  }, "Список удалён").then(() => setDeleting(null))
                }
              >
                {busy ? "Удаляем…" : "Удалить"}
              </button>
              <button type="button" className={styles.secondary} onClick={() => setDeleting(null)}>
                Отмена
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function NameDialog({ title, hint, busy, close, save }: { title: string; hint: string; busy: boolean; close: () => void; save: (name: string) => void }) {
  const [name, setName] = useState("");
  return (
    <>
      <button type="button" className={styles.overlay} aria-label="Закрыть" tabIndex={-1} onClick={close} />
      <div role="dialog" aria-modal="true" aria-labelledby="name-dialog" className={`${styles.card} ${styles.modal}`}>
        <h2 id="name-dialog">{title}</h2>
        <p className={styles.muted}>{hint}</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim() && !busy) save(name.trim());
          }}
        >
          <label className={styles.field}>
            Название
            <input autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={180} required />
          </label>
          <div className={styles.actions}>
            <button type="submit" className={styles.primary} disabled={busy || !name.trim()}>
              {busy ? "Сохраняем…" : "Сохранить"}
            </button>
            <button type="button" className={styles.secondary} onClick={close}>
              Отмена
            </button>
          </div>
        </form>
      </div>
    </>
  );
}
