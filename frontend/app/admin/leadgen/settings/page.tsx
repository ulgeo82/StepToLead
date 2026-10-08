"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { base, message, PageHeader, signalLabels } from "../shared";
import styles from "../leadgen.module.css";

type Settings = {
  weights: Record<string, number>;
  platform_domains: string[];
  recontact_days: number;
  outreach_pipeline_id: number | null;
  icp: string;
  fit_ai_available: boolean;
};
type Patch = Partial<Pick<Settings, "weights" | "platform_domains" | "recontact_days" | "icp">>;

export default function Page() {
  const [saved, setSaved] = useState<Settings | null>(null);
  const [weights, setWeights] = useState<Record<string, number>>({});
  const [domains, setDomains] = useState("");
  const [days, setDays] = useState("30");
  const [icp, setIcp] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const request = useRef<AbortController | null>(null);

  function populate(result: Settings) {
    setSaved(result);
    setWeights({ ...result.weights });
    setDomains(result.platform_domains.join("\n"));
    setDays(String(result.recontact_days));
    setIcp(result.icp);
  }

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<Settings>(`${base}/settings`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) populate(result);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(message(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => () => request.current?.abort(), []);

  const platformDomains = [
    ...new Set(
      domains
        .split(/\r?\n/)
        .map((domain) => domain.trim())
        .filter(Boolean),
    ),
  ];
  const patch: Patch = {};
  if (saved) {
    if (icp !== saved.icp) patch.icp = icp;
    if (Number(days) !== saved.recontact_days) patch.recontact_days = Number(days);
    if (JSON.stringify(platformDomains) !== JSON.stringify(saved.platform_domains)) {
      patch.platform_domains = platformDomains;
    }
    const changedWeights = Object.fromEntries(
      Object.entries(weights).filter(([kind, weight]) => weight !== saved.weights[kind]),
    );
    if (Object.keys(changedWeights).length) patch.weights = changedWeights;
  }
  const changed = Object.keys(patch).length > 0;

  async function save() {
    if (!saved || busy || !changed) return;
    if (patch.icp != null && (icp.length < 20 || icp.length > 3000)) {
      setError("Портрет клиента: от 20 до 3000 символов.");
      return;
    }
    if (
      patch.recontact_days != null &&
      (!Number.isInteger(patch.recontact_days) || patch.recontact_days < 1 || patch.recontact_days > 3650)
    ) {
      setError("Повторный контакт: от 1 до 3650 дней.");
      return;
    }
    if (
      patch.weights &&
      Object.values(patch.weights).some((weight) => !Number.isInteger(weight) || weight < -5 || weight > 5)
    ) {
      setError("Вес сигнала: целое число от −5 до 5.");
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`${base}/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
        signal: controller.signal,
      });
      const result = await api<Settings>(`${base}/settings`, { signal: controller.signal });
      if (!controller.signal.aborted) {
        populate(result);
        setNotice("Настройки сохранены.");
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(message(reason));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <div className={styles.page}>
      <PageHeader title="Настройки" subtitle="ИИ-оценка, скоринг и повторные контакты." />
      {loading && <p role="status">Загружаем настройки…</p>}
      {busy && <p role="status">Сохраняем настройки…</p>}
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
      {!loading && !saved && (
        <button type="button" className={styles.secondary} onClick={() => setRevision((value) => value + 1)}>
          Повторить
        </button>
      )}
      {saved && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <fieldset className={styles.formFieldset} disabled={loading || busy}>
            <section className={styles.card}>
              <h2>Портрет клиента для ИИ-оценки</h2>
              {!saved.fit_ai_available && (
                <p className={styles.notice}>ИИ не подключён в портале — оценка выполняться не будет</p>
              )}
              <label className={styles.field}>
                Портрет клиента
                <textarea
                  value={icp}
                  minLength={20}
                  maxLength={3000}
                  required
                  aria-describedby="icp-count"
                  onChange={(event) => setIcp(event.target.value)}
                />
                <small id="icp-count">{icp.length} / 3000 символов</small>
              </label>
            </section>
            <section className={styles.card}>
              <h2>Веса скоринга</h2>
              <div className={styles.scroll}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th scope="col">Сигнал</th>
                      <th scope="col">Вес</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(weights).map(([kind, weight]) => (
                      <tr key={kind}>
                        <td>{signalLabels[kind] || kind}</td>
                        <td>
                          <input
                            type="number"
                            min={-5}
                            max={5}
                            step={1}
                            required
                            value={Number.isFinite(weight) ? weight : ""}
                            aria-label={`Вес: ${signalLabels[kind] || kind}`}
                            className={styles.weightInput}
                            onChange={(event) =>
                              setWeights((current) => ({
                                ...current,
                                [kind]: event.target.value === "" ? NaN : Number(event.target.value),
                              }))
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className={styles.muted}>Итоговый скоринг ограничен диапазоном 0–10.</p>
            </section>
            <section className={styles.card}>
              <label className={styles.field}>
                Повторный контакт не раньше чем через N дней
                <input
                  type="number"
                  min={1}
                  max={3650}
                  step={1}
                  required
                  value={days}
                  onChange={(event) => setDays(event.target.value)}
                />
              </label>
            </section>
            <section className={styles.card}>
              <h2>Домены-площадки</h2>
              <label className={styles.field}>
                Один домен на строку
                <textarea value={domains} onChange={(event) => setDomains(event.target.value)} />
                <small>Не считаются сайтами компаний.</small>
              </label>
            </section>
            <button className={styles.primary} disabled={!changed || loading || busy}>
              {busy ? "Сохраняем…" : "Сохранить"}
            </button>
          </fieldset>
        </form>
      )}
    </div>
  );
}
