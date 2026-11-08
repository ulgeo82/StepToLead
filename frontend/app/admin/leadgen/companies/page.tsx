"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  base,
  blocked,
  Company,
  date,
  Filters,
  Fit,
  fitLabels,
  LeadList,
  message,
  Outreach,
  PageHeader,
  Pipeline,
  post,
  safeUrl,
  Score,
  Segment,
  companyName,
  signalLabels,
  stages,
} from "../shared";
import styles from "../leadgen.module.css";
import EnrollDialog from "../../outreach/email/EnrollDialog";
import AddToCampaignDialog from "../../outreach/campaigns/AddToCampaignDialog";
import ExportDialog from "../ExportDialog";
import ImportDialog from "../ImportDialog";
import { CompanyCard, AddToListDialog, useDialog } from "../company-dialogs";
import type { CompanySource } from "../sources/source-types";

const channels: Record<string, string> = {
  whatsapp: "WhatsApp",
  telegram: "Telegram",
  email: "Email",
  phone: "Телефон",
  messenger: "Мессенджер",
};
type Action = "enrich" | "outreach" | "dnc" | "assess" | "hide" | "restore";
function readFilters(params: URLSearchParams): Filters {
  const number = (key: string) =>
    params.has(key) && Number.isFinite(Number(params.get(key))) ? Number(params.get(key)) : undefined;
  return {
    q: params.get("q") || undefined,
    niche: params.get("niche") || undefined,
    cities: params.getAll("city"),
    min_score: number("min_score"),
    stages: params.getAll("stage"),
    channels: params.getAll("channel"),
    signals: params.getAll("signal"),
    new_days: number("new_days"),
    needs_review: params.get("needs_review") === "true" ? true : undefined,
    fit: (["fit", "maybe", "no"] as const).find((v) => v === params.get("fit")),
    verify_status: params.get("verify_status") === "valid" ? "valid" : undefined,
  };
}
function filtersQuery(filters: Filters) {
  const q = new URLSearchParams();
  for (const key of ["q", "niche", "min_score", "new_days", "needs_review", "fit", "verify_status"] as const)
    if (filters[key] !== undefined && filters[key] !== "") q.set(key, String(filters[key]));
  for (const [key, values] of [
    ["city", filters.cities],
    ["stage", filters.stages],
    ["channel", filters.channels],
    ["signal", filters.signals],
  ] as const)
    values?.forEach((value) => q.append(key, value));
  return q;
}
function SegmentDialog({
  close,
  save,
  busy,
  error,
}: {
  close: () => void;
  save: (name: string) => Promise<void>;
  busy: boolean;
  error: string;
}) {
  const ref = useDialog(close);
  const [name, setName] = useState("");
  return (
    <>
      <button
        type="button"
        className={styles.overlay}
        aria-label="Закрыть создание сегмента"
        tabIndex={-1}
        onClick={close}
      />
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="segment-title"
        className={`${styles.card} ${styles.modal}`}
      >
        <h2 id="segment-title">Сохранить сегмент</h2>
        <p className={styles.muted}>Сегмент сохранит текущие фильтры базы.</p>
        {error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim() && !busy) save(name.trim());
          }}
        >
          <label className={styles.field}>
            Название
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required />
          </label>
          <div className={styles.actions}>
            <button type="submit" className={styles.primary} disabled={busy || !name.trim()}>
              {busy ? "Сохраняем…" : "Сохранить"}
            </button>
            <button type="button" className={styles.secondary} disabled={busy} onClick={close}>
              Отмена
            </button>
          </div>
        </form>
      </div>
    </>
  );
}

function CompaniesPage() {
  const [labels, setLabels] = useState<Record<string, string>>(signalLabels);
  useEffect(() => {
    const controller = new AbortController();
    api<{ items: CompanySource[] }>(`${base}/sources`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setLabels({ ...Object.fromEntries(value.items.flatMap(source =>
        source.signals.map(signal => [signal.kind, signal.label]))), ...signalLabels });
    }).catch(() => undefined);
    return () => controller.abort();
  }, []);
  const router = useRouter();
  const search = useSearchParams();
  const raw = search.toString();
  const filters = useMemo(() => readFilters(new URLSearchParams(raw)), [raw]);
  const segmentId = search.get("segment_id");
  const runId = search.get("run_id");
  const listIds = search.getAll("list_id");
  const listKey = listIds.join(",");
  const sort = ["score", "new", "seen", "name"].includes(search.get("sort") || "")
    ? search.get("sort")!
    : "score";
  const offset = Math.max(0, Math.floor(Number(search.get("offset")) || 0));
  const companyId = Number(search.get("company")) || null;
  const requestQuery = useMemo(() => {
    const q = filtersQuery(filters);
    if (segmentId) q.set("segment_id", segmentId);
    if (runId) q.set("run_id", runId);
    listKey.split(",").filter(Boolean).forEach((id) => q.append("list_id", id));
    q.set("sort", sort);
    q.set("limit", "50");
    q.set("offset", String(offset));
    return q.toString();
  }, [filters, segmentId, runId, listKey, sort, offset]);
  const [allLists, setAllLists] = useState<LeadList[]>([]);
  const [exportOpen, setExportOpen] = useState(false);
  const [addToList, setAddToList] = useState<number[] | null>(null);
  const closeExport = useCallback(() => setExportOpen(false), []);
  const closeAddToList = useCallback(() => setAddToList(null), []);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [segmentError, setSegmentError] = useState("");
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [outreach, setOutreach] = useState<Outreach | null>(null);
  const [revision, setRevision] = useState(0);
  const [segmentDialog, setSegmentDialog] = useState(false);
  const [segmentBusy, setSegmentBusy] = useState(false);
  const [enrollIds, setEnrollIds] = useState<number[] | null>(null);
  const [campaignIds, setCampaignIds] = useState<number[] | null>(null);
  const closeEnroll = useCallback(() => setEnrollIds(null), []);
  const [importOpen, setImportOpen] = useState(false);
  const [deleteIds, setDeleteIds] = useState<number[] | null>(null);
  const [blockDomains, setBlockDomains] = useState(false);
  const closeImport = useCallback(() => setImportOpen(false), []);
  const write = useCallback(
    (params: URLSearchParams) =>
      router.replace(`/admin/leadgen/companies${params.size ? `?${params}` : ""}`, { scroll: false }),
    [router],
  );
  const closeCard = useCallback(() => {
    const params = new URLSearchParams(raw);
    params.delete("company");
    write(params);
  }, [raw, write]);
  const closeSegment = useCallback(() => setSegmentDialog(false), []);
  useEffect(() => {
    const controller = new AbortController();
    api<{ items: LeadList[] }>(`${base}/lists`, { signal: controller.signal })
      .then((data) => !controller.signal.aborted && setAllLists(data.items))
      .catch(() => undefined);
    return () => controller.abort();
  }, [revision]);
  function change(key: string, value: string | string[]) {
    const q = new URLSearchParams(raw);
    q.delete(key);
    (Array.isArray(value) ? value : [value]).filter(Boolean).forEach((v) => q.append(key, v));
    if (!["company", "offset"].includes(key)) q.delete("offset");
    if (!["company", "offset", "sort"].includes(key)) q.delete("run_id");
    if (!["company", "offset", "sort"].includes(key)) q.delete("segment_id");
    write(q);
  }
  function multi(key: string, current: string[] | undefined, value: string, checked: boolean) {
    change(key, checked ? [...(current || []), value] : (current || []).filter((v) => v !== value));
  }
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setSelected([]);
    setError("");
    const timer = setTimeout(
      () =>
        api<{ total: number; items: Company[] }>(`${base}/companies?${requestQuery}`, {
          signal: controller.signal,
        })
          .then((data) => {
            if (!controller.signal.aborted) {
              setCompanies(data.items);
              setTotal(data.total);
            }
          })
          .catch((e) => {
            if (!controller.signal.aborted) {
              setError(message(e));
              setCompanies([]);
              setTotal(0);
            }
          })
          .finally(() => {
            if (!controller.signal.aborted) setLoading(false);
          }),
      250,
    );
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [requestQuery, revision]);
  useEffect(() => {
    const controller = new AbortController();
    api<{ items: Segment[] }>(`${base}/segments`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) {
          setSegments(data.items);
          setSegmentError("");
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted) setSegmentError(message(e));
      });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    const controller = new AbortController();
    api<Pipeline>(`${base}/outreach/pipeline`, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setPipeline(value);
      })
      .catch(() => {
        /* Retry on handoff; do not hide the companies table. */
      });
    return () => controller.abort();
  }, []);
  async function act(action: Action, ids: number[]) {
    if (busy || !ids.length) return;
    setBusy(true);
    setError("");
    setNotice("");
    setOutreach(null);
    try {
      if (action === "enrich") {
        const data = await post<{ queued: number; skipped: number }>("/companies/enrich", {
          company_ids: ids,
        });
        setNotice(
          `Сбор контактов поставлен в очередь: ${data.queued}. Пропущено: ${data.skipped}. Обновите базу после завершения сбора.`,
        );
      } else if (action === "assess") {
        const data = await post<{ queued: number; skipped: number }>("/companies/assess", {
          company_ids: ids,
          force: ids.length === 1,
        });
        setNotice(
          `ИИ-оценка поставлена в очередь: ${data.queued}${data.skipped ? `, пропущено ${data.skipped}` : ""}. Уже оценённые компании в массовой оценке пропускаются. Обновите базу через минуту.`,
        );
      } else if (action === "hide" || action === "restore") {
        const data = await post<{ done: number; skipped: { company_id: number; reason_text: string }[] }>(
          "/companies/hide",
          { company_ids: ids, hidden: action === "hide" },
        );
        const why = data.skipped.length ? ` Не тронули ${data.skipped.length}: ${data.skipped[0].reason_text}.` : "";
        setNotice(
          action === "hide"
            ? `Скрыто: ${data.done}. Найти их можно фильтром «Стадия → Скрыта».${why}`
            : `Возвращено в базу: ${data.done}.`,
        );
      } else if (action === "dnc") {
        await post("/dnc", { kind: "company", value: String(ids[0]), reason: "refused" });
        setNotice("Компания добавлена в стоп-лист.");
      } else {
        const data = await post<Outreach>("/companies/outreach", { company_ids: ids });
        setOutreach(data);
        try {
          setPipeline(await api<Pipeline>(`${base}/outreach/pipeline`));
        } catch {
          setPipeline({
            project_id: data.project_id,
            pipeline_id: data.pipeline_id,
            crm_url: `/crm?project_id=${data.project_id}`,
          });
        }
      }
      setSelected([]);
      setRevision((n) => n + 1);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!deleteIds || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const data = await post<{
        deleted: number;
        blocked_domains: number;
        skipped: { company_id: number; reason_text: string }[];
      }>("/companies/delete", { company_ids: deleteIds, block_domains: blockDomains });
      const parts = [`Удалено: ${data.deleted}.`];
      if (data.blocked_domains) parts.push(`В исключения поиска добавлено сайтов: ${data.blocked_domains}.`);
      if (data.skipped.length) parts.push(`Не удалили ${data.skipped.length}: ${data.skipped[0].reason_text}.`);
      setNotice(parts.join(" "));
      setDeleteIds(null);
      setBlockDomains(false);
      setSelected([]);
      setRevision((n) => n + 1);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function saveSegment(name: string) {
    setSegmentBusy(true);
    setError("");
    try {
      const created = await post<Segment>("/segments", { name, filters });
      setSegmentDialog(false);
      setRevision((n) => n + 1);
      const q = filtersQuery(created.filters);
      q.set("segment_id", String(created.id));
      write(q);
      setNotice(`Сегмент «${created.name}» сохранён.`);
    } catch (e) {
      setError(message(e));
    } finally {
      setSegmentBusy(false);
    }
  }
  function selectSegment(segment?: Segment) {
    if (!segment) {
      write(new URLSearchParams());
      return;
    }
    const q = filtersQuery(segment.filters);
    q.set("segment_id", String(segment.id));
    write(q);
  }
  const exportFilter = (() => {
    const q = filtersQuery(filters);
    if (segmentId) q.set("segment_id", segmentId);
    if (runId) q.set("run_id", runId);
    listIds.forEach((id) => q.append("list_id", id));
    q.set("sort", sort);
    return q.toString();
  })();
  const listName = (id: string) => allLists.find((l) => String(l.id) === id)?.name || `Список #${id}`;
  const eligible = companies;
  const viewingHidden = !!filters.stages?.includes("hidden");
  const allSelected = !!eligible.length && eligible.every((company) => selected.includes(company.id));
  return (
    <div className={`page ${styles.page}`}>
      <PageHeader
        title="База компаний"
        subtitle={search.get("campaign")
          ? "Отметьте компании галочками и нажмите «В кампанию…» внизу таблицы — они станут лидами кампании."
          : "Компании, рекламные сигналы и публичные контакты — от поиска до аутрича."}
      >
        <div className={styles.actions}>
          <button type="button" className={styles.secondary} onClick={() => setImportOpen(true)}>
            Импорт из файла
          </button>
          <Link className={styles.primary} href="/admin/leadgen/search">
            Поиск компаний
          </Link>
        </div>
      </PageHeader>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      {segmentError && (
        <p className={styles.error} role="alert">
          Сегменты: {segmentError}
        </p>
      )}
      {notice && (
        <p className={styles.notice} role="status">
          {notice}
        </p>
      )}
      {outreach && (
        <div className={styles.notice} role="status">
          <strong>
            Перенесено {outreach.moved}. Пропущено {outreach.skipped}.
          </strong>
          {outreach.results
            .filter((result) => !result.ok)
            .map((result) => (
              <p key={result.company_id}>
                {(() => {
                  const c = companies.find((company) => company.id === result.company_id);
                  return c ? companyName(c) : "";
                })() ||
                  `Компания #${result.company_id}`}
                : {result.reason_text || result.reason || "Причина не указана"}
              </p>
            ))}
          {pipeline && safeUrl(pipeline.crm_url) && <Link href={pipeline.crm_url}>Открыть воронку →</Link>}
        </div>
      )}
      <div className={styles.tabs} aria-label="Сегменты компаний">
        <button
          type="button"
          className={styles.tab}
          aria-pressed={!segmentId}
          onClick={() => selectSegment()}
        >
          Все компании
        </button>
        {segments.map((segment) => (
          <button
            type="button"
            className={styles.tab}
            key={segment.id}
            aria-pressed={segmentId === String(segment.id)}
            onClick={() => selectSegment(segment)}
          >
            {segment.name}
            <span>{segment.count}</span>
          </button>
        ))}
        <button type="button" className={styles.tab} onClick={() => setSegmentDialog(true)}>
          ＋ Сегмент
        </button>
      </div>
      <section className={styles.card} aria-label="Фильтры компаний">
        <div className={styles.filters}>
          <label className={styles.field}>
            Поиск
            <input
              type="search"
              value={filters.q || ""}
              onChange={(e) => change("q", e.target.value)}
              placeholder="Название, домен или ИНН"
            />
          </label>
          <label className={styles.field}>
            Ниша
            <input
              value={filters.niche || ""}
              onChange={(e) => change("niche", e.target.value)}
              placeholder="Любая ниша"
            />
          </label>
          <label className={styles.field}>
            Города
            <input
              key={(filters.cities || []).join(",")}
              defaultValue={(filters.cities || []).join(", ")}
              onBlur={(e) => {
                const value = e.target.value
                  .split(",")
                  .map((v) => v.trim())
                  .filter(Boolean);
                if (JSON.stringify(value) !== JSON.stringify(filters.cities)) change("city", value);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") e.currentTarget.blur();
              }}
              placeholder="Через запятую, затем Enter"
            />
          </label>
          <label className={styles.field}>
            Минимальный скоринг
            <select value={filters.min_score ?? ""} onChange={(e) => change("min_score", e.target.value)}>
              <option value="">Любой</option>
              {Array.from({ length: 11 }, (_, score) => (
                <option key={score} value={score}>
                  {score}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            Оценка ЦА
            <select value={filters.fit ?? ""} onChange={(e) => change("fit", e.target.value)}>
              <option value="">Любая</option>
              {Object.entries(fitLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <details>
            <summary>Списки{listIds.length ? ` · ${listIds.length}` : ""}</summary>
            {!allLists.length && <p className={styles.muted}>Списков пока нет</p>}
            {allLists.map((list) => (
              <label key={list.id} className={styles.check}>
                <input
                  type="checkbox"
                  checked={listIds.includes(String(list.id))}
                  onChange={(e) =>
                    change("list_id", e.target.checked ? [...listIds, String(list.id)] : listIds.filter((v) => v !== String(list.id)))
                  }
                />
                {list.name} <span className={styles.muted}>· {list.total}</span>
              </label>
            ))}
            <Link href="/admin/leadgen/lists">Управлять списками →</Link>
          </details>
          <details>
            <summary>Канал{filters.channels?.length ? ` · ${filters.channels.length}` : ""}</summary>
            {Object.entries(channels).map(([value, label]) => (
              <label key={value} className={styles.check}>
                <input
                  type="checkbox"
                  checked={filters.channels?.includes(value) || false}
                  onChange={(e) => multi("channel", filters.channels, value, e.target.checked)}
                />
                {label}
              </label>
            ))}
          </details>
          <details>
            <summary>Сигналы{filters.signals?.length ? ` · ${filters.signals.length}` : ""}</summary>
            {Object.entries(labels).map(([value, label]) => <label key={value} className={styles.check}>
              <input type="checkbox" checked={filters.signals?.includes(value) || false}
                onChange={e => multi("signal", filters.signals, value, e.target.checked)}/>{label}
            </label>)}
          </details>
          <details>
            <summary>Стадия{filters.stages?.length ? ` · ${filters.stages.length}` : ""}</summary>
            {Object.entries(stages).map(([value, label]) => (
              <label key={value} className={styles.check}>
                <input
                  type="checkbox"
                  checked={filters.stages?.includes(value) || false}
                  onChange={(e) => multi("stage", filters.stages, value, e.target.checked)}
                />
                {label}
              </label>
            ))}
          </details>
          <label className={styles.check}>
            <input type="checkbox" checked={filters.verify_status === "valid"}
              onChange={e => change("verify_status", e.target.checked ? "valid" : "")}/>
            Есть проверенная почта
          </label>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={filters.new_days === 7}
              onChange={(e) => change("new_days", e.target.checked ? "7" : "")}
            />
            Новые за 7 дней
          </label>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={filters.needs_review || false}
              onChange={(e) => change("needs_review", e.target.checked ? "true" : "")}
            />
            На проверке
          </label>
          <button type="button" className={styles.secondary} onClick={() => write(new URLSearchParams())}>
            Сбросить фильтры
          </button>
        </div>
        <div className={styles.row}>
          {runId && (
            <span className={styles.chip}>
              Запуск #{runId}{" "}
              <button
                type="button"
                className={styles.close}
                aria-label="Убрать фильтр по запуску"
                onClick={() => change("run_id", "")}
              >
                ×
              </button>
            </span>
          )}
          {listIds.map((id) => (
            <span key={id} className={styles.chip}>
              Список: {listName(id)}{" "}
              <button
                type="button"
                className={styles.close}
                aria-label="Убрать фильтр по списку"
                onClick={() => change("list_id", listIds.filter((v) => v !== id))}
              >
                ×
              </button>
            </span>
          ))}
          {filters.fit && <span className={styles.chip}>ЦА: {fitLabels[filters.fit]}</span>}
          {filters.cities?.map((city) => (
            <span key={city} className={styles.chip}>
              {city}
            </span>
          ))}
          {filters.channels?.map((channel) => (
            <span key={channel} className={styles.chip}>
              {channels[channel] || channel}
            </span>
          ))}
          {filters.stages?.map((stage) => (
            <span key={stage} className={styles.chip}>
              {stages[stage] || stage}
            </span>
          ))}
          {filters.signals?.map((signal) => (
            <span key={signal} className={styles.chip}>
              {labels[signal] || signal}
              <button type="button" className={styles.close} aria-label="Убрать фильтр по сигналу"
                onClick={() => multi("signal", filters.signals, signal, false)}>×</button>
            </span>
          ))}
        </div>
      </section>
      <section className={styles.card} aria-label="Список компаний">
        <div className={styles.row}>
          <h2>Компании · {total}</h2>
          <label className={styles.field}>
            Сортировка
            <select value={sort} onChange={(e) => change("sort", e.target.value)}>
              <option value="score">По скорингу</option>
              <option value="new">Сначала новые</option>
              <option value="seen">По последнему появлению</option>
              <option value="name">По названию</option>
            </select>
          </label>
          <button type="button" className={styles.secondary} onClick={() => setRevision((n) => n + 1)}>
            Обновить
          </button>
          <button type="button" className={styles.primary} disabled={!total} onClick={() => setExportOpen(true)}>
            Выгрузка в Excel
          </button>
        </div>
        <div className={styles.actions}>
          <span>Выбрано: {selected.length}</span>
          <button
            type="button"
            className={styles.secondary}
            disabled={busy || loading || !selected.length}
            onClick={() => act("enrich", selected)}
          >
            Собрать контакты
          </button>
          <button
            type="button"
            className={styles.secondary}
            disabled={busy || loading || !selected.length}
            onClick={() => setAddToList([...selected])}
          >
            В список…
          </button>
          <button
            type="button"
            className={styles.primary}
            disabled={busy || loading || !selected.length}
            onClick={() => setCampaignIds([...selected])}
            title="Сделать выбранные компании лидами кампании"
          >
            В кампанию…
          </button>
          <button
            type="button"
            className={styles.secondary}
            disabled={busy || loading || !selected.length}
            onClick={() => act("outreach", selected)}
            title="Создать сделки в CRM, воронка «Аутрич». Писем это не отправляет"
          >
            {busy ? "Выполняем…" : "В CRM"}
          </button>
          <button
            type="button"
            className={styles.secondary}
            disabled={busy || loading || !selected.length}
            onClick={() => act("assess", selected)}
          >
            Оценить ИИ
          </button>
          <button
            type="button"
            className={styles.secondary}
            disabled={busy || loading || !selected.length}
            onClick={() => act(viewingHidden ? "restore" : "hide", selected)}
            title="Убрать из списка: компания не попадёт в аутрич и останется скрытой при новых поисках"
          >
            {viewingHidden ? "Вернуть в базу" : "Скрыть"}
          </button>
          <button
            type="button"
            className={`${styles.secondary} ${styles.danger}`}
            disabled={busy || loading || !selected.length}
            onClick={() => setDeleteIds([...selected])}
          >
            Удалить
          </button>
        </div>
        {deleteIds && (
          <div className={styles.notice} role="dialog" aria-label="Удаление компаний">
            <strong>Удалить {deleteIds.length} компаний из базы насовсем?</strong>
            <p>Контакты, объявления и история по ним тоже удалятся. Компании с открытой сделкой в CRM не удаляются.</p>
            <label className={styles.check}>
              <input type="checkbox" checked={blockDomains} onChange={(e) => setBlockDomains(e.target.checked)} />
              Больше не добавлять эти сайты при новых поисках и импорте (для сетей, агрегаторов, конкурентов)
            </label>
            <div className={styles.actions}>
              <button type="button" className={`${styles.secondary} ${styles.danger}`} disabled={busy} onClick={() => remove()}>
                {busy ? "Удаляем…" : "Удалить"}
              </button>
              <button type="button" className={styles.secondary} disabled={busy} onClick={() => setDeleteIds(null)}>
                Отмена
              </button>
            </div>
          </div>
        )}
        {loading ? (
          <p role="status" className={styles.empty}>
            Загружаем компании…
          </p>
        ) : error && !companies.length ? (
          <p className={styles.empty}>Не удалось загрузить базу. Нажмите «Обновить».</p>
        ) : !companies.length ? (
          <div className={styles.empty}>
            <h2>
              {filtersQuery(filters).size || segmentId || runId || listIds.length
                ? "По этим фильтрам компаний нет."
                : "Пока пусто. Запустите поиск компаний"}
            </h2>
            <Link className={styles.primary} href="/admin/leadgen/search">
              Запустить поиск компаний
            </Link>
          </div>
        ) : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">
                    <input
                      type="checkbox"
                      aria-label="Выбрать все доступные компании на странице"
                      checked={allSelected}
                      disabled={busy || !eligible.length}
                      onChange={(e) =>
                        setSelected(e.target.checked ? eligible.map((company) => company.id) : [])
                      }
                    />
                  </th>
                  <th scope="col">Компания</th>
                  <th scope="col">Сигналы</th>
                  <th scope="col">Скоринг</th>
                  <th scope="col">ЦА</th>
                  <th scope="col">Стадия</th>
                </tr>
              </thead>
              <tbody>
                {companies.map((company) => (
                  <tr
                    key={company.id}
                    className={selected.includes(company.id) ? styles.selected : ""}
                    onClick={() => change("company", String(company.id))}
                  >
                    <td onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        aria-label={`Выбрать ${companyName(company)}`}
                        checked={selected.includes(company.id)}
                        disabled={busy}
                        onChange={(e) =>
                          setSelected((ids) =>
                            e.target.checked ? [...ids, company.id] : ids.filter((id) => id !== company.id),
                          )
                        }
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className={styles.companyButton}
                        onClick={(e) => {
                          e.stopPropagation();
                          change("company", String(company.id));
                        }}
                      >
                        {companyName(company)}
                      </button>
                      {company.display_name ? (
                        <small>{company.domain_display || company.domain || "Домен не найден"}</small>
                      ) : null}
                      {company.needs_review && <span className={styles.badge}>На проверке</span>}
                      {(company.lists?.length || !company.exported_at) && (
                        <span className={styles.listChips}>
                          {!company.exported_at && <span className={`${styles.listChip} ${styles.freshChip}`}>не выгружалась</span>}
                          {company.lists?.slice(0, 2).map((l) => (
                            <span key={l.id} className={styles.listChip} title={l.name}>
                              {l.name}
                            </span>
                          ))}
                          {(company.lists?.length || 0) > 2 && <span className={styles.listChip}>+{(company.lists?.length || 0) - 2}</span>}
                        </span>
                      )}
                    </td>
                    <td>
                      {company.score_reasons?.length ? (
                        company.score_reasons.slice(0, 4).map((reason, index) => (
                          <span
                            key={index}
                            className={styles.chip}
                            title={`${reason.label}: ${reason.weight > 0 ? "+" : ""}${reason.weight}`}
                          >
                            {reason.label}
                          </span>
                        ))
                      ) : (
                        <span className={styles.muted}>Нет сигналов</span>
                      )}
                    </td>
                    <td>
                      <Score value={company.score} />
                    </td>
                    <td>
                      <Fit label={company.fit_label} />
                    </td>
                    <td>
                      <span className={styles.badge}>{stages[company.stage] || company.stage}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className={styles.pagination}>
          <span>{total ? `${offset + 1}–${Math.min(offset + 50, total)} из ${total}` : "0 компаний"}</span>
          <div className={styles.row}>
            <button
              type="button"
              className={styles.secondary}
              disabled={loading || offset === 0}
              onClick={() => change("offset", String(Math.max(0, offset - 50)))}
            >
              ← Назад
            </button>
            <button
              type="button"
              className={styles.secondary}
              disabled={loading || offset + 50 >= total}
              onClick={() => change("offset", String(offset + 50))}
            >
              Далее →
            </button>
          </div>
        </div>
      </section>
      {companyId && (
        <CompanyCard
          id={companyId}
          labels={labels}
          onClose={closeCard}
          revision={revision}
          busy={busy}
          act={act}
          pipeline={pipeline}
          feedback={
            <>
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
              {outreach && (
                <div role="status" className={styles.notice}>
                  Перенесено {outreach.moved}. Пропущено {outreach.skipped}.
                  {outreach.results
                    .filter((result) => !result.ok)
                    .map((result) => (
                      <p key={result.company_id}>
                        {companies.find((c) => c.id === result.company_id)?.display_name ||
                          `#${result.company_id}`}
                        : {result.reason_text || result.reason}
                      </p>
                    ))}
                  {pipeline && safeUrl(pipeline.crm_url) && (
                    <Link href={pipeline.crm_url}>Открыть воронку →</Link>
                  )}
                </div>
              )}
            </>
          }
        />
      )}
      {segmentDialog && (
        <SegmentDialog close={closeSegment} save={saveSegment} busy={segmentBusy} error={error} />
      )}
      {importOpen && <ImportDialog close={closeImport} completed={() => setRevision((n) => n + 1)} />}
      {exportOpen && (
        <ExportDialog
          close={closeExport}
          done={() => setRevision((n) => n + 1)}
          lists={allLists}
          filterQuery={exportFilter}
          filterLabel={`${total} ${total === 1 ? "компания" : "компаний"} в базе`}
          selectedIds={selected}
          initialLists={listIds.map(Number)}
        />
      )}
      {addToList && (
        <AddToListDialog
          ids={addToList}
          lists={allLists}
          close={closeAddToList}
          done={(text) => {
            setNotice(text);
            setAddToList(null);
            setRevision((n) => n + 1);
          }}
        />
      )}
      {campaignIds && (
        <AddToCampaignDialog
          ids={campaignIds}
          initial={Number(search.get("campaign")) || null}
          close={() => setCampaignIds(null)}
          done={(text) => { setNotice(text); setSelected([]); setRevision((n) => n + 1); }}
        />
      )}
      {enrollIds && (
        <EnrollDialog
          ids={enrollIds}
          close={closeEnroll}
          completed={() => {
            setSelected([]);
            setRevision((n) => n + 1);
          }}
        />
      )}
    </div>
  );
}
export default function Page() {
  return (
    <Suspense
      fallback={
        <div className={`page ${styles.page}`} role="status">
          Загружаем базу компаний…
        </div>
      }
    >
      <CompaniesPage />
    </Suspense>
  );
}
