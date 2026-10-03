"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Widget = { enabled: boolean; title: string; text: string; button: string; color: string; position: "right" | "left"; delay_sec: number;
  callback: boolean; whatsapp: string; telegram: string; max: string; phone: string; privacy_url: string; success: string };

/** Analytics → site card → «Виджет»: callback form + messenger buttons for the client's site. */
export function SiteWidgetEditor({ siteId, siteName, canManage, onClose }: { siteId: number; siteName: string; canManage: boolean; onClose: () => void }) {
  const [w, setW] = useState<Widget | null>(null);
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api<{ widget: Widget; public_key: string }>(`/website/sites/${siteId}/widget`).then(d => { setW(d.widget); setKey(d.public_key); })
    .catch(e => setError((e as Error).message)); }, [siteId]);
  const set = (patch: Partial<Widget>) => { setW(v => v && { ...v, ...patch }); setNotice(""); };
  async function save(event: FormEvent) {
    event.preventDefault(); if (!w) return; setBusy(true); setError(""); setNotice("");
    try {
      const d = await api<{ widget: Widget }>(`/website/sites/${siteId}/widget`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(w) });
      setW(d.widget); setNotice(d.widget.enabled ? "Сохранено. Виджет появится на сайте в течение 5 минут." : "Сохранено. Виджет выключен.");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const origin = typeof window === "undefined" ? "https://portal.steptolead.ru" : window.location.origin;
  const off = !canManage;
  return <div className="widgetBackdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <form className="widgetModal" onSubmit={save}>
      <header><h2>Виджет на сайте · {siteName}</h2><button type="button" onClick={onClose} aria-label="Закрыть">×</button></header>
      {error && <p className="widgetError">{error}</p>}
      {w && <div className="widgetBody"><div className="widgetFields">
        <label className="widgetSwitch"><input type="checkbox" checked={w.enabled} disabled={off} onChange={e => set({ enabled: e.target.checked })}/> <b>Показывать виджет на сайте</b></label>
        <fieldset><legend>Форма «Перезвоните мне»</legend>
          <label className="widgetSwitch"><input type="checkbox" checked={w.callback} disabled={off} onChange={e => set({ callback: e.target.checked })}/> Форма обратного звонка</label>
          <label>Заголовок<input value={w.title} maxLength={60} disabled={off} onChange={e => set({ title: e.target.value })}/></label>
          <label>Подзаголовок<input value={w.text} maxLength={200} disabled={off} onChange={e => set({ text: e.target.value })}/></label>
          <label>Текст кнопки<input value={w.button} maxLength={30} disabled={off} onChange={e => set({ button: e.target.value })}/></label>
          <label>После отправки<input value={w.success} maxLength={160} disabled={off} onChange={e => set({ success: e.target.value })}/></label>
          <label>Ссылка на политику конфиденциальности<input type="url" value={w.privacy_url} placeholder="https://romax63.ru/privacy" disabled={off} onChange={e => set({ privacy_url: e.target.value })}/>
            <small>По 152-ФЗ посетитель отмечает согласие на обработку данных — без галочки заявка не отправится.</small></label></fieldset>
        <fieldset><legend>Мессенджеры и телефон</legend>
          <div className="widgetGrid"><label>WhatsApp (номер)<input value={w.whatsapp} placeholder="+7 927 000-11-01" disabled={off} onChange={e => set({ whatsapp: e.target.value })}/></label>
          <label>Telegram (@username)<input value={w.telegram} placeholder="@romax63" disabled={off} onChange={e => set({ telegram: e.target.value })}/></label>
          <label>MAX (ссылка)<input type="url" value={w.max} placeholder="https://max.ru/..." disabled={off} onChange={e => set({ max: e.target.value })}/></label>
          <label>Телефон для звонка<input value={w.phone} placeholder="+7 846 200-00-00" disabled={off} onChange={e => set({ phone: e.target.value })}/></label></div></fieldset>
        <fieldset><legend>Вид</legend><div className="widgetGrid">
          <label>Цвет<input type="color" value={w.color} disabled={off} onChange={e => set({ color: e.target.value })}/></label>
          <label>Сторона<select value={w.position} disabled={off} onChange={e => set({ position: e.target.value as Widget["position"] })}><option value="right">Справа</option><option value="left">Слева</option></select></label>
          <label>Показать через, сек<input type="number" min={0} max={120} value={w.delay_sec} disabled={off} onChange={e => set({ delay_sec: Number(e.target.value) || 0 })}/></label></div></fieldset>
        <details className="widgetInstall" open={w.enabled}><summary>Код установки</summary>
          <p>Вставьте один раз перед закрывающим тегом &lt;/body&gt; на всех страницах (в Tilda — «Настройки сайта → Ещё → HTML-код для вставки внутрь head»).</p>
          <code>{`<script async src="${origin}/stl-widget.js" data-stl-key="${key}"></script>`}</code>
          <p>Если на сайте стоит счётчик StepToLead и посетитель дал согласие на аналитику, заявка привяжется к визиту и рекламной кампании. Метки UTM и yclid передаются в любом случае.</p></details>
        {notice && <p className="widgetOk">{notice}</p>}
        {canManage && <button className="websitePrimary" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить"}</button>}
      </div>
      <WidgetPreview w={w}/></div>}
    </form></div>;
}

function WidgetPreview({ w }: { w: Widget }) {
  const links = [w.whatsapp && ["WhatsApp", "#25D366"], w.telegram && ["Telegram", "#229ED9"], w.max && ["MAX", "#6E46F5"], w.phone && ["Позвонить", "#0F2140"]]
    .filter(Boolean) as [string, string][];
  return <aside className={`widgetPreview ${w.position}`} aria-label="Предпросмотр"><small>Так увидит посетитель</small>
    <div className="wpPanel"><b>{w.title}</b>{w.text && <p>{w.text}</p>}
      {w.callback && <><span className="wpInput">Имя</span><span className="wpInput">+7 (___) ___-__-__</span>
        <span className="wpConsent">☐ Согласен на обработку персональных данных</span>
        <span className="wpSend" style={{ background: w.color }}>{w.button}</span></>}
      {!!links.length && <div className="wpLinks">{links.map(([name, bg]) => <span key={name} style={{ background: bg }}>{name}</span>)}</div>}</div>
    <span className="wpFab" style={{ background: w.color }}>☎</span></aside>;
}
