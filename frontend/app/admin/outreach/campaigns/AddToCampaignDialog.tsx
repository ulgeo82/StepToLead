"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { base, message, plural, post } from "../../leadgen/shared";
import styles from "../../leadgen/leadgen.module.css";
import type { Campaign, ImportReport } from "./types";

/** «В кампанию»: выбранные компании базы -> лиды кампании (берётся лучшая рабочая почта компании). */
export default function AddToCampaignDialog({ ids, initial, close, done }: {
  ids: number[]; initial?: number | null; close: () => void; done: (text: string) => void;
}) {
  const [items, setItems] = useState<Campaign[] | null>(null);
  const [chosen, setChosen] = useState<string>(initial ? String(initial) : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [report, setReport] = useState<ImportReport | null>(null);

  useEffect(() => {
    api<{ items: Campaign[] }>(`${base}/campaigns`).then((d) => {
      const open = d.items.filter((c) => c.status !== "done");
      setItems(open);
      if (!chosen && open[0]) setChosen(String(open[0].id));
    }).catch((e) => setError(message(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function add() {
    setBusy(true); setError("");
    try {
      const r = await post<ImportReport>(`/campaigns/${chosen}/leads/companies`, { company_ids: ids });
      setReport(r);
      done(`Добавлено в кампанию: ${r.added}${r.skipped ? `, пропущено ${r.skipped}` : ""}`);
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }

  return <>
    <button type="button" className={styles.overlay} aria-label="Закрыть" tabIndex={-1} onClick={close} />
    <div role="dialog" aria-modal="true" aria-labelledby="to-campaign" className={`${styles.card} ${styles.modal}`}>
      <h2 id="to-campaign">В кампанию</h2>
      <p className={styles.muted}>{ids.length} {plural(ids.length, "компания", "компании", "компаний")} станут лидами кампании. Письмо уйдёт на лучшую рабочую почту компании; стоп-лист и повторы портал отсеет сам.</p>
      {error && <p className={styles.error}>{error}</p>}
      {!items ? <p className={styles.muted}>Загружаем…</p> : !items.length ? (
        <p className={styles.muted}>Кампаний нет. <Link href="/admin/outreach/campaigns">Создать кампанию →</Link></p>
      ) : (
        <label className={styles.field}>Кампания
          <select value={chosen} onChange={(e) => setChosen(e.target.value)}>
            {items.map((c) => <option key={c.id} value={c.id}>{c.name} · {c.stats?.leads ?? 0} лидов</option>)}
          </select>
        </label>
      )}
      {report && (
        <div className={styles.exportSummary}>
          <p>Добавлено <b>{report.added}</b>{report.skipped ? `, пропущено ${report.skipped}` : ""}</p>
          {Object.entries(report.reasons).map(([r, n]) => <p key={r} className={styles.muted}>— {r}: {n}</p>)}
        </div>
      )}
      <div className={styles.actions}>
        {report ? <>
          <Link className={styles.primary} href={`/admin/outreach/campaigns/${chosen}`}>Открыть кампанию</Link>
          <button type="button" className={styles.secondary} onClick={close}>Готово</button>
        </> : <>
          <button type="button" className={styles.primary} disabled={busy || !chosen} onClick={add}>{busy ? "Добавляем…" : "Добавить"}</button>
          <button type="button" className={styles.secondary} onClick={close}>Отмена</button>
        </>}
      </div>
    </div>
  </>;
}
