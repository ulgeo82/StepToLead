"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { count, money } from "@/components/reporting";

type Totals = { spend: number; impressions: number; clicks: number; calls: number; routes: number; site: number; has_data: boolean };
type MapDetail = { id: number; platform: "yandex_maps" | "2gis"; platform_name: string; name: string; card_url: string; utm_source: string; utm_link: string;
  budgets: { month: string; amount: number }[]; manual: Record<string, Record<string, number>>; tracking_numbers: string[];
  totals: Totals; last_upload: { days: number; first_date?: string; last_date?: string; metrics?: string[]; file?: string } | null };

const monthName = (m: string) => { const [y, mo] = m.split("-").map(Number); return new Date(y, mo - 1, 1).toLocaleDateString("ru-RU", { month: "long", year: "numeric" }); };
const recentMonths = () => { const out: string[] = []; const d = new Date(); d.setDate(1); for (let i = 0; i < 6; i++) { out.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`); d.setMonth(d.getMonth() - 1); } return out; };
const phone = (n: string) => n.length === 11 ? `+${n[0]} ${n.slice(1, 4)} ${n.slice(4, 7)}-${n.slice(7, 9)}-${n.slice(9)}` : n;
const how: Record<string, string> = {
  yandex_maps: "Яндекс Бизнес → Статистика → кнопка загрузки у графика (или «Собрать статистику» в рекламной кампании) → Excel. Выгружайте по дням.",
  "2gis": "2ГИС не даёт выгрузку статистики — введите цифры из кабинета за месяц в форму ниже. Если у вас есть Excel-отчёт от менеджера 2ГИС с датами, его тоже можно загрузить." };

/** Ads → Yandex Maps / 2GIS channel: card actions, monthly spend, statistics upload and lead attribution. */
export function MapsPanel({ connectionId, start, end, canManage, revision, onChanged }: { connectionId: number; start: string; end: string; canManage: boolean;
  revision: number; onChanged: () => void }) {
  const [data, setData] = useState<MapDetail | null>(null);
  const [month, setMonth] = useState(recentMonths()[0]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api<MapDetail>(`/ads/maps/${connectionId}?start=${start}&end=${end}`).then(setData).catch(e => setError((e as Error).message)), [connectionId, start, end]);
  useEffect(() => { load(); }, [load, revision]);

  async function act(run: () => Promise<unknown>, message: string) {
    setBusy(true); setError(""); setNotice("");
    try { await run(); await load(); onChanged(); setNotice(message); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function saveMonth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget);
    const num = (k: string) => { const v = String(f.get(k) ?? "").trim(); return v === "" ? undefined : Number(v); };
    const body = { month, budget: num("budget"), impressions: num("impressions"), clicks: num("clicks"), calls: num("calls"), routes: num("routes"), site: num("site") };
    await act(() => api(`/ads/maps/${connectionId}/month`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }), `Сохранено за ${monthName(month)}`);
  }
  async function upload(file: File) {
    const body = new FormData(); body.append("file", file);
    await act(async () => { const r = await api<{ days: number; first_date: string; last_date: string }>(`/ads/maps/${connectionId}/stats`, { method: "POST", body, timeoutMs: 120_000 });
      setNotice(`Загружено дней: ${r.days} (${r.first_date} — ${r.last_date})`); }, "");
  }
  async function copy(text: string) { try { await navigator.clipboard.writeText(text); setNotice("Ссылка скопирована"); } catch { window.prompt("Ссылка", text); } }

  if (!data) return error ? <p className="adsAccountError">{error}</p> : null;
  const t = data.totals;
  const budget = data.budgets.find(b => b.month === month)?.amount;
  const manual = data.manual[month] || {};
  return <section className="resultPanel mapsPanel">
    <div className="mapsHead"><div><h3>Карточка на картах</h3><p>{data.card_url ? <a href={data.card_url} target="_blank" rel="noopener noreferrer">Открыть карточку ↗</a> : "Ссылка на карточку не указана"}</p></div></div>
    <div className="mapsTiles">{[["Показы", t.impressions], ["Переходы в карточку", t.clicks], ["Нажали «Позвонить»", t.calls], ["Маршруты", t.routes], ["Переходы на сайт", t.site]].map(([label, value]) =>
      <div key={label as string}><span>{label}</span><strong>{t.has_data ? count(value as number) : "—"}</strong></div>)}
      <div><span>Расход за период</span><strong>{money(t.spend)}</strong></div></div>
    {!t.has_data && <p className="mapsHint">Статистики карточки за выбранный период ещё нет — загрузите выгрузку или введите цифры за месяц.</p>}
    {error && <p className="adsAccountError">{error}</p>}{notice && <p className="mapsOk">{notice}</p>}

    <div className="mapsGrid">
      <div className="mapsBox"><h4>Откуда заявки</h4>
        <p><b>Звонки.</b> {data.tracking_numbers.length ? <>Номер в карточке: {data.tracking_numbers.map(phone).join(", ")} — звонки на него попадают в этот канал.</>
          : <>Поставьте в карточку отдельный номер Mango и привяжите его к «{data.name}» в CRM → Звонки → Настройки → Коллтрекинг. Тогда каждый звонок с карт станет заявкой этого канала.</>}</p>
        <p><b>Сайт.</b> В поле «Сайт» карточки укажите ссылку с меткой — визиты и заявки с неё засчитаются картам:</p>
        <div className="mapsLink"><code>{data.utm_link}</code><button type="button" onClick={() => copy(data.utm_link)}>Копировать</button></div></div>
      <div className="mapsBox"><h4>Статистика карточки</h4><p>{how[data.platform]}</p>
        {canManage && <label className="mapsUpload">{busy ? "Загружаем…" : "Загрузить XLSX / CSV"}<input type="file" accept=".xlsx,.csv" disabled={busy} onChange={e => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }}/></label>}
        {data.last_upload && <p className="mapsMuted">Последняя загрузка: {data.last_upload.file || "файл"} · {data.last_upload.days} дн. · {data.last_upload.first_date} — {data.last_upload.last_date}</p>}</div>
    </div>

    <form className="mapsMonth" onSubmit={saveMonth} key={`${month}-${budget}-${JSON.stringify(manual)}`}>
      <div className="mapsMonthHead"><h4>Расход и цифры за месяц</h4>
        <select value={month} onChange={e => setMonth(e.target.value)}>{recentMonths().map(m => <option key={m} value={m}>{monthName(m)}</option>)}</select></div>
      <p className="mapsMuted">Стоимость продвижения за месяц (подписка Яндекс Бизнеса, пакет 2ГИС) разложится по дням — так считаются цена заявки и ROMI.</p>
      <div className="mapsFields">
        <label>Расход, ₽<input name="budget" type="number" min="0" step="0.01" defaultValue={budget ?? ""} disabled={!canManage} placeholder="15000"/></label>
        <label>Показы<input name="impressions" type="number" min="0" defaultValue={manual.impressions ?? ""} disabled={!canManage}/></label>
        <label>Переходы<input name="clicks" type="number" min="0" defaultValue={manual.clicks ?? ""} disabled={!canManage}/></label>
        <label>«Позвонить»<input name="calls" type="number" min="0" defaultValue={manual.calls ?? ""} disabled={!canManage}/></label>
        <label>Маршруты<input name="routes" type="number" min="0" defaultValue={manual.routes ?? ""} disabled={!canManage}/></label>
        <label>На сайт<input name="site" type="number" min="0" defaultValue={manual.site ?? ""} disabled={!canManage}/></label></div>
      {canManage && <button className="adsBlueButton" disabled={busy}>Сохранить за {monthName(month)}</button>}
      {!!data.budgets.length && <p className="mapsMuted">Расход по месяцам: {data.budgets.map(b => `${monthName(b.month)} — ${money(b.amount)}`).join(" · ")}</p>}
    </form>
  </section>;
}
