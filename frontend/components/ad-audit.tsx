"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { money } from "@/components/reporting";

type Campaign = {
  connection_id: number;
  external_campaign_id: string;
  campaign_name: string;
  connection_name: string;
};
type Audit = {
  findings: { code: string; title: string; detail: string; loss_rub: number; campaigns: Campaign[] }[];
  total_loss_rub: number;
  period_days: number;
  spend: number;
  leads: number;
};

export function AdAudit({ projectId }: { projectId: number }) {
  const [data, setData] = useState<Audit | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError("");
    setLoading(true);
    api<Audit>(`/result/audit?project_id=${projectId}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [projectId, revision]);

  return <section className="resultPanel resultAudit" aria-labelledby="ad-audit-title">
    <div className="resultPanelHead"><h2 id="ad-audit-title">Экспресс-аудит за 30 дней</h2></div>
    {loading && <p className="resultCalm" role="status">Проверяем рекламные данные…</p>}
    {error && <div className="resultError" role="alert">{error}
      <button className="resultOutlineLink" onClick={() => setRevision(n => n + 1)} disabled={loading}>
        Повторить проверку
      </button>
    </div>}
    {data && <>
      {data.findings.length > 0 ? <>
        <p className="resultAuditTotal">Нашли {data.findings.length} проблем ≈ {money(data.total_loss_rub)}</p>
        <p className="resultCalm">Сумма — наблюдаемые расходы без результата, не прогноз экономии.
          Пересечения между правилами не суммируются. Период аудита не зависит от фильтра отчёта.</p>
        <ul className="resultAuditFindings">
          {data.findings.map((finding, index) => <li key={`${finding.code}-${index}`}>
            <div className="resultPanelHead"><h3>{finding.title}</h3>
              <strong>{money(finding.loss_rub)}</strong></div>
            <p>{finding.detail}</p>
            <ul>{finding.campaigns.map(c => <li key={`${c.connection_id}-${c.external_campaign_id}`}>
              {c.campaign_name} · {c.connection_name}
            </li>)}</ul>
            {finding.loss_rub === 0 && <small>Денежная потеря не подтверждена.</small>}
          </li>)}
        </ul>
      </> : <p className="resultCalm">{data.spend === 0
        ? "Подключите рекламный кабинет — аудит построится автоматически"
        : "По доступным данным проблем не найдено. Правила без достаточных данных пропущены."}</p>}
    </>}
  </section>;
}
