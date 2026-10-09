"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Advertisement, base, date, message, money, PageHeader, post, Run, Score } from "../shared";
import styles from "../leadgen.module.css";

const statusNames: Record<string, string> = {
  queued: "В очереди",
  running: "Идёт поиск",
  done: "Завершён",
  failed: "Ошибка",
};
const pending = (run: Run) => ["queued", "running"].includes(run.status);

export default function SearchPage() {
  const [text, setText] = useState("");
  const [niche, setNiche] = useState("");
  const [city, setCity] = useState("");
  const [region, setRegion] = useState("");
  const [enrich, setEnrich] = useState(true);
  const [repeat, setRepeat] = useState(false);
  const [organic, setOrganic] = useState(true);
  const [mobile, setMobile] = useState(false);
  const devices = useMemo(() => (mobile ? ["desktop", "mobile"] : ["desktop"]), [mobile]);
  const keywords = useMemo(
    () => [
      ...new Set(
        text
          .split(/\r?\n/)
          .map((k) => k.trim())
          .filter(Boolean),
      ),
    ],
    [text],
  );
  const [estimate, setEstimate] = useState<{ requests: number; cost_rub: number } | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [estimateError, setEstimateError] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<Run | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setEstimate(null);
    setEstimateError("");
    setEstimating(false);
    if (!keywords.length || keywords.length > 500) return () => controller.abort();
    setEstimating(true);
    const timer = setTimeout(() => {
      api<{ requests: number; cost_rub: number }>(`${base}/runs/estimate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keywords, devices }),
        signal: controller.signal,
      })
        .then((value) => {
          if (!controller.signal.aborted) setEstimate(value);
        })
        .catch((e) => {
          if (!controller.signal.aborted) setEstimateError(message(e));
        })
        .finally(() => {
          if (!controller.signal.aborted) setEstimating(false);
        });
    }, 500);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [keywords, devices]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const data = await api<{ items: Run[] }>(`${base}/runs?limit=30`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setRuns(data.items);
        setHistoryError("");
        if (data.items.some(pending)) timer = setTimeout(load, 3000);
      } catch (e) {
        if (!controller.signal.aborted) {
          setHistoryError(message(e));
          timer = setTimeout(load, 3000);
        }
      } finally {
        if (!controller.signal.aborted) setHistoryLoading(false);
      }
    };
    load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [refresh]);
  useEffect(() => {
    setDetail(null);
    setDetailError("");
    if (!selected) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setDetailLoading(true);
    const load = async () => {
      try {
        const run = await api<Run>(`${base}/runs/${selected}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setDetail(run);
        setDetailError("");
        if (pending(run)) timer = setTimeout(load, 3000);
      } catch (e) {
        if (!controller.signal.aborted) {
          setDetailError(message(e));
          timer = setTimeout(load, 3000);
        }
      } finally {
        if (!controller.signal.aborted) setDetailLoading(false);
      }
    };
    load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [selected, refresh]);
  async function start(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    if (busy) return;
    if (!keywords.length || keywords.length > 500) {
      setError("Введите от 1 до 500 ключевых слов, по одному на строку.");
      return;
    }
    if (region && (!Number.isInteger(Number(region)) || Number(region) <= 0)) {
      setError("Регион должен быть положительным целым числом.");
      return;
    }
    setBusy(true);
    try {
      const run = await post<Run>("/runs", {
        keywords,
        ...(region ? { region_code: Number(region) } : {}),
        ...(niche.trim() ? { niche: niche.trim() } : {}),
        ...(city.trim() ? { city: city.trim() } : {}),
        enrich,
        repeat,
        organic,
        devices,
      });
      setSelected(run.id);
      setRefresh((n) => n + 1);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className={`page ${styles.page}`}>
      <PageHeader
        title="Поиск компаний"
        subtitle="Найдите компании, которые рекламируются в Яндекс Директе, и соберите публичные контакты."
      >
        <Link className={styles.secondary} href="/admin/leadgen/companies">
          База компаний →
        </Link>
      </PageHeader>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={styles.grid}>
        <form className={styles.card} onSubmit={start}>
          <h2>Новый поиск</h2>
          <fieldset className={styles.sources}>
            <legend>Источник</legend>
            <button type="button" className={styles.source} aria-pressed="true">
              Яндекс Директ
            </button>
            {["2ГИС", "HH"].map((source) => (
              <button key={source} type="button" className={styles.source} disabled>
                {source} <span className={styles.badge}>скоро</span>
              </button>
            ))}
          </fieldset>
          <div className={styles.fields}>
            <label className={styles.field}>
              Ниша
              <input
                value={niche}
                onChange={(e) => setNiche(e.target.value)}
                maxLength={120}
                placeholder="Кухни на заказ"
              />
            </label>
            <label className={styles.field}>
              Город
              <input
                value={city}
                onChange={(e) => setCity(e.target.value)}
                maxLength={120}
                placeholder="Самара"
              />
            </label>
            <label className={`${styles.field} ${styles.wide}`}>
              Регион Яндекса (lr)
              <input
                type="number"
                min="1"
                step="1"
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                placeholder="51"
              />
              <small>Самара — 51, Москва — 213, Екатеринбург — 54</small>
            </label>
            <label className={`${styles.field} ${styles.wide}`}>
              Ключевые слова
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder={"кухни на заказ Самара\nкорпусная мебель Самара"}
                required
              />
              <small>{keywords.length} уникальных ключевых слов · по одному на строку · максимум 500</small>
            </label>
          </div>
          <label className={styles.check}>
            <input type="checkbox" checked={enrich} onChange={(e) => setEnrich(e.target.checked)} />
            Сразу собрать контакты с сайтов
          </label>
          <label className={styles.check}>
            <input type="checkbox" checked={organic} onChange={(e) => setOrganic(e.target.checked)} />
            Сохранить и компании из топ-10 поиска (органика) — в тех же запросах, бесплатно
          </label>
          <label className={styles.check}>
            <input type="checkbox" checked={mobile} onChange={(e) => setMobile(e.target.checked)} />
            Искать рекламу и на телефонах — другие рекламодатели, запросов ×2
          </label>
          <label className={styles.check}>
            <input type="checkbox" checked={repeat} onChange={(e) => setRepeat(e.target.checked)} />
            Повторять раз в неделю — ловить новых и пропавших рекламодателей
          </label>
          <p className={styles.estimate} aria-live="polite">
            {estimating
              ? "Рассчитываем стоимость…"
              : estimate
                ? `≈ ${estimate.requests} запросов · ${money(estimate.cost_rub)}`
                : "Введите ключевые слова для оценки стоимости"}
          </p>
          {estimateError && (
            <p className={styles.error} role="alert">
              {estimateError}
            </p>
          )}
          <button
            type="submit"
            className={styles.primary}
            disabled={busy || !keywords.length || keywords.length > 500}
          >
            {busy ? "Запускаем…" : "Запустить поиск"}
          </button>
        </form>
        <section className={`${styles.card} ${styles.history}`} aria-label="История запусков">
          <div className={styles.row}>
            <h2>История запусков</h2>
            <button
              type="button"
              className={styles.secondary}
              onClick={() => setRefresh((n) => n + 1)}
              aria-label="Обновить историю запусков"
            >
              Обновить
            </button>
          </div>
          {historyError && (
            <p className={styles.error} role="alert">
              {historyError}
            </p>
          )}
          {historyLoading ? (
            <p className={styles.muted} role="status">
              Загружаем историю…
            </p>
          ) : !runs.length ? (
            <p className={styles.empty}>Запусков пока нет. Начните с нового поиска.</p>
          ) : (
            runs.map((run) => (
              <button
                type="button"
                className={styles.run}
                aria-pressed={selected === run.id}
                key={run.id}
                onClick={() => setSelected(run.id)}
              >
                <span className={styles.row}>
                  <strong>
                    #{run.id} ·{" "}
                    {run.source === "import"
                      ? `Импорт${run.params.filename ? `: ${run.params.filename}` : ""}`
                      : run.params.niche || "Поиск компаний"}
                  </strong>
                  <span className={`${styles.badge} ${styles[run.status] || ""}`}>
                    {statusNames[run.status] || run.status}
                  </span>
                </span>
                <small>
                  {date(run.created_at)}
                  {run.params.city ? ` · ${run.params.city}` : ""}
                </small>
                {run.status === "done" && run.source === "import" && (
                  <span>
                    {run.stats.companies || 0} компаний · {run.stats.created || 0} новых
                    {run.stats.invalid ? ` · ${run.stats.invalid} строк пропущено` : ""}
                  </span>
                )}
                {run.status === "done" && run.source !== "import" && (
                  <span>
                    {run.stats.companies || 0} в рекламе · {run.stats.new_advertisers || 0} новых
                    {run.stats.organic_companies ? ` · ${run.stats.organic_companies} из поиска` : ""} ·{" "}
                    {run.stats.stopped || 0} пропали
                  </span>
                )}
                {run.error && <small>{run.error}</small>}
              </button>
            ))
          )}
        </section>
      </div>
      {selected && (
        <section className={styles.card} aria-label="Результаты запуска">
          <div className={styles.row}>
            <h2>Результаты запуска #{selected}</h2>
            <Link className={styles.secondary} href={`/admin/leadgen/companies?run_id=${selected}`}>
              Открыть в базе →
            </Link>
          </div>
          {detailLoading && <p role="status">Загружаем результаты…</p>}
          {detailError && (
            <p className={styles.error} role="alert">
              {detailError}
            </p>
          )}
          {detail?.error && <p className={styles.error}>{detail.error}</p>}
          {!!detail?.stats.failed && Object.keys(detail.stats.failed).length > 0 && (
            <div className={styles.error}>
              Ошибки по ключевым словам:
              {Object.entries(detail.stats.failed).map(([keyword, reason]) => (
                <p key={keyword}>
                  {keyword}: {reason}
                </p>
              ))}
            </div>
          )}
          {detail &&
            (!detail.companies?.length ? (
              <p className={styles.empty}>
                {pending(detail)
                  ? "Поиск выполняется. Результаты появятся автоматически."
                  : "Компании не найдены для этого запуска."}
              </p>
            ) : (
              <div className={styles.scroll}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th scope="col">Компания</th>
                      <th scope="col">Что крутят</th>
                      <th scope="col">Ключей</th>
                      <th scope="col">В базе</th>
                      <th scope="col">Скоринг</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.companies.map((company) => (
                      <tr key={company.id}>
                        <td>
                          <Link href={`/admin/leadgen/companies?company=${company.id}`}>
                            {company.display_name}
                          </Link>
                          <small>{company.domain || "Домен не найден"}</small>
                        </td>
                        <td>
                          {company.ad ? (
                            <Advertisement ad={company.ad} />
                          ) : (
                            <span className={styles.muted}>
                              {company.found_in === "organic"
                                ? "Не рекламируется по этим ключам — найдена в поиске"
                                : "Объявление не получено"}
                            </span>
                          )}
                        </td>
                        <td>{company.ad?.keywords?.length || 0}</td>
                        <td>
                          <span className={`${styles.badge} ${company.is_new ? styles.new : ""}`}>
                            {company.is_new ? "Новый" : "Уже был"}
                          </span>
                          {company.found_in === "organic" && <span className={styles.chip}>из поиска</span>}
                        </td>
                        <td>
                          <Score value={company.score} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
        </section>
      )}
    </div>
  );
}
