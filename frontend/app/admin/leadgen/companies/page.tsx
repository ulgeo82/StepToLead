"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, API_URL } from "@/lib/api";
import {
  Advertisement,
  base,
  blocked,
  Company,
  CompanyDetail,
  date,
  Filters,
  Fit,
  fitLabels,
  message,
  money,
  Outreach,
  PageHeader,
  Pipeline,
  post,
  safeUrl,
  Score,
  Segment,
  signalLabels,
  stages,
} from "../shared";
import styles from "../leadgen.module.css";
import EnrollDialog from "../../outreach/email/EnrollDialog";

const channels: Record<string, string> = {
  whatsapp: "WhatsApp",
  telegram: "Telegram",
  email: "Email",
  phone: "Телефон",
  messenger: "Мессенджер",
};
type Action = "enrich" | "outreach" | "dnc" | "assess";
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
function signalText(kind: string, payload: unknown): string {
  const p = (payload && typeof payload === "object" ? payload : {}) as Record<string, unknown>;
  const extra = [
    p.reason,
    Array.isArray(p.keywords) ? `ключей: ${p.keywords.length}` : null,
    p.count != null ? `×${p.count}` : null,
  ]
    .filter((v) => typeof v === "string" || typeof v === "number")
    .join(" · ");
  return extra ? `${signalLabels[kind] || kind} — ${extra}` : signalLabels[kind] || kind;
}
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
  };
}
function filtersQuery(filters: Filters) {
  const q = new URLSearchParams();
  for (const key of ["q", "niche", "min_score", "new_days", "needs_review", "fit"] as const)
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
function useDialog(close: () => void) {
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

function CompanyCard({
  id,
  onClose,
  revision,
  busy,
  act,
  pipeline,
  feedback,
}: {
  id: number;
  onClose: () => void;
  revision: number;
  busy: boolean;
  act: (action: Action, ids: number[]) => Promise<void>;
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
            <h2 id="company-card-title">{company?.display_name || "Карточка компании"}</h2>
            {company?.domain && safeUrl(company.domain) && (
              <a href={safeUrl(company.domain)} target="_blank" rel="noopener noreferrer">
                {company.domain} ↗
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
                  <strong>{signalText(signal.kind, signal.payload)}</strong>
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
                В аутрич
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
                      `Добавить «${company.display_name}» в стоп-лист? Компания будет исключена из аутрича.`,
                    )
                  )
                    act("dnc", [id]);
                }}
              >
                В стоп-лист
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
  const router = useRouter();
  const search = useSearchParams();
  const raw = search.toString();
  const filters = useMemo(() => readFilters(new URLSearchParams(raw)), [raw]);
  const segmentId = search.get("segment_id");
  const runId = search.get("run_id");
  const sort = ["score", "new", "seen", "name"].includes(search.get("sort") || "")
    ? search.get("sort")!
    : "score";
  const offset = Math.max(0, Math.floor(Number(search.get("offset")) || 0));
  const companyId = Number(search.get("company")) || null;
  const requestQuery = useMemo(() => {
    const q = filtersQuery(filters);
    if (segmentId) q.set("segment_id", segmentId);
    if (runId) q.set("run_id", runId);
    q.set("sort", sort);
    q.set("limit", "50");
    q.set("offset", String(offset));
    return q.toString();
  }, [filters, segmentId, runId, sort, offset]);
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
  const closeEnroll = useCallback(() => setEnrollIds(null), []);
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
  const exportHref = (() => {
    const q = filtersQuery(filters);
    if (segmentId) q.set("segment_id", segmentId);
    if (runId) q.set("run_id", runId);
    q.set("sort", sort);
    selected.forEach((id) => q.append("ids", String(id)));
    return `${API_URL}/api${base}/companies/export?${q}`;
  })();
  const eligible = companies.filter((company) => !blocked(company.stage));
  const allSelected = !!eligible.length && eligible.every((company) => selected.includes(company.id));
  return (
    <div className={`page ${styles.page}`}>
      <PageHeader
        title="База компаний"
        subtitle="Компании, рекламные сигналы и публичные контакты — от поиска до аутрича."
      >
        <Link className={styles.primary} href="/admin/leadgen/search">
          Поиск компаний
        </Link>
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
                {companies.find((company) => company.id === result.company_id)?.display_name ||
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
              {signal}
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
          <a
            className={styles.secondary}
            href={exportHref}
            download
            aria-disabled={!total}
            onClick={(e) => {
              if (!total) e.preventDefault();
            }}
            title={selected.length ? "Только выбранные компании" : "Все компании по текущим фильтрам"}
          >
            {selected.length ? `Скачать Excel · ${selected.length}` : `Скачать Excel · ${total}`}
          </a>
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
            className={styles.primary}
            disabled={busy || loading || !selected.length}
            onClick={() => act("outreach", selected)}
          >
            {busy ? "Выполняем…" : "В аутрич"}
          </button>
          <button
            type="button"
            className={styles.secondary}
            disabled={busy || loading || !selected.length}
            onClick={() => setEnrollIds([...selected])}
          >
            Запустить цепочку
          </button>
          <button
            type="button"
            className={styles.secondary}
            disabled={busy || loading || !selected.length}
            onClick={() => act("assess", selected)}
          >
            Оценить ИИ
          </button>
        </div>
        {loading ? (
          <p role="status" className={styles.empty}>
            Загружаем компании…
          </p>
        ) : error && !companies.length ? (
          <p className={styles.empty}>Не удалось загрузить базу. Нажмите «Обновить».</p>
        ) : !companies.length ? (
          <div className={styles.empty}>
            <h2>
              {filtersQuery(filters).size || segmentId || runId
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
                        aria-label={`Выбрать ${company.display_name}`}
                        checked={selected.includes(company.id)}
                        disabled={busy || blocked(company.stage)}
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
                        {company.display_name}
                      </button>
                      <small>{company.domain || "Домен не найден"}</small>
                      {company.needs_review && <span className={styles.badge}>На проверке</span>}
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
