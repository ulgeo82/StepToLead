"use client";
// Отчёт по ссылке для клиента: без входа, только снимок отчёта. PDF — печать браузера, таблицы — копирование.
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { BrandMark } from "@/components/brand-mark";
import { copyForSheets, ReportData, ReportView } from "@/components/report/report-view";
import "./public-report.css";

export default function PublicReport() {
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<{ title: string; report: ReportData } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    fetch(`/api/public/report/${encodeURIComponent(token)}`, { cache: "no-store" })
      .then(async r => { if (!r.ok) throw new Error("Отчёт не найден или ссылка устарела"); return r.json(); })
      .then(setData).catch(exc => setError(exc.message));
  }, [token]);
  useEffect(() => { if (data) document.title = `${data.title} — StepToLead`; }, [data]);
  async function copy() {
    if (!data) return;
    try { await copyForSheets(data.report); setNotice("Скопировано. Откройте Google Таблицу, встаньте в ячейку A1 и нажмите Ctrl+V (⌘V)."); }
    catch { setNotice("Браузер не дал скопировать — разрешите доступ к буферу обмена."); }
  }
  return <main className="prShell">
    <header className="prTop"><BrandMark size={32} />
      {data && <div className="prActions"><button type="button" onClick={copy}>Скопировать для Google Таблиц</button><button type="button" className="primary" onClick={() => window.print()}>Скачать PDF</button></div>}
    </header>
    {notice && <p className="prNotice" role="status">{notice}</p>}
    {error ? <div className="prError" role="alert"><h1>Отчёт недоступен</h1><p>{error}. Попросите менеджера StepToLead прислать новую ссылку.</p></div>
      : data ? <ReportView report={{ ...data.report, title: data.title }} /> : <p className="prMuted">Загружаем отчёт…</p>}
  </main>;
}
