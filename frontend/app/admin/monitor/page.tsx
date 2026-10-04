"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

type Problem = { key: string; title: string; detail: string; client: string | null };
type Overview = { problems: Problem[]; telegram: { configured: boolean; linked: boolean; username: string | null }; workers: { name: string; ok: boolean }[] };
const post = (path: string, method = "POST") => api<Overview["telegram"] & { url?: string }>(path, { method });

export default function MonitorPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [waiting, setWaiting] = useState(false);
  const load = useCallback(() => api<Overview>("/admin/monitor").then(setData).catch(e => setError((e as Error).message)), []);
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);
  async function link() {
    setError(""); try { const r = await post("/admin/monitor/telegram/link"); if (r.url) window.open(r.url, "_blank", "noopener"); setWaiting(true); } catch (e) { setError((e as Error).message); }
  }
  async function check() {
    setError(""); try { const t = await post("/admin/monitor/telegram/check"); setWaiting(!t.linked); setNotice(t.linked ? "Telegram подключён — уведомления о сбоях будут приходить туда." : "Бот ещё не получил /start — нажмите Start в Telegram и проверьте снова."); load(); } catch (e) { setError((e as Error).message); }
  }
  async function act(path: string, method: string, message: string) { setError(""); try { await post(path, method); setNotice(message); load(); } catch (e) { setError((e as Error).message); } }
  const t = data?.telegram;
  return <div className="page monitorPage"><header className="topbar"><div className="crumbs"><span>StepToLead</span><b>/</b><strong>Мониторинг</strong></div><span className="userAvatar">ЕД</span></header>
    <section className="pageHeader"><div><p className="eyebrow">Надёжность</p><h1>Мониторинг</h1><p className="subtitle">Что сломалось в портале прямо сейчас. О новых сбоях и восстановлении портал сам пишет вам в Telegram.</p></div></section>
    {error && <p className="notice error">{error}</p>}{notice && <p className="notice success">{notice}</p>}
    {data && <>
      <section className={`panel monitorStatus ${data.problems.length ? "bad" : "good"}`}>
        <b>{data.problems.length ? `Проблем: ${data.problems.length}` : "Всё работает"}</b>
        <span>{data.problems.length ? "Каждая проблема пришла вам в Telegram один раз; если не исправить — напоминание через сутки." : "Рекламные кабинеты, чаты, телефония и фоновые задачи в порядке."}</span></section>
      {!!data.problems.length && <section className="panel monitorList">{data.problems.map(p => <article key={p.key}><i>!</i><div><strong>{p.title}</strong>
        {p.client && <small>Клиент: {p.client}</small>}{p.detail && <p>{p.detail}</p>}</div></article>)}</section>}
      <div className="monitorGrid">
        <section className="panel monitorBox"><p className="eyebrow">Куда приходят сбои</p><h2>Telegram</h2>
          {!t?.configured ? <p className="mutedText">Бот не настроен: задайте TELEGRAM_BOT_TOKEN в .env.prod на сервере.</p>
            : t.linked ? <><p>Подключён{t.username ? ` (@${t.username})` : ""}.</p><div className="monitorActions">
                <button className="button secondary" onClick={() => act("/admin/monitor/telegram/test", "POST", "Тестовое сообщение отправлено")}>Отправить тест</button>
                <button className="button secondary" onClick={() => act("/admin/monitor/telegram", "DELETE", "Telegram отключён")}>Отключить</button></div></>
            : <><p>Подключите свой Telegram — бот будет писать о сбоях и об их исправлении.</p><div className="monitorActions">
                <button className="button primary" onClick={link}>Подключить Telegram</button>{waiting && <button className="button secondary" onClick={check}>Я нажал Start — проверить</button>}</div></>}</section>
        <section className="panel monitorBox"><p className="eyebrow">Фоновые задачи</p><h2>Сервер</h2>
          <ul className="monitorWorkers">{data.workers.map(w => <li key={w.name} className={w.ok ? "ok" : "bad"}><i>{w.ok ? "●" : "✕"}</i>{w.name}</li>)}</ul>
          <p className="mutedText">Если сервер выключится целиком, он не сможет сообщить об этом сам. Добавьте внешнюю проверку адреса <code>/api/health</code> раз в 5 минут — например, в UptimeRobot или Яндекс Мониторинге.</p></section>
      </div></>}
  </div>;
}
