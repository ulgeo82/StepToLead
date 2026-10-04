"use client";
import { FormEvent, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import "../../growth/growth.css";

const json = (body: unknown) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

/** «Забыли пароль?»: a one-time code from the StepToLead Telegram bot, then a new password. */
export default function PortalReset() {
  const [step, setStep] = useState<"login" | "code" | "done">("login");
  const [username, setUsername] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function start(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { const r = await api<{ message: string }>("/portal/auth/reset/start", json({ username })); setMessage(r.message); setStep("code"); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function finish(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const f = new FormData(event.currentTarget); setError("");
    if (f.get("new_password") !== f.get("repeat")) { setError("Пароли не совпадают"); return; }
    setBusy(true);
    try { await api("/portal/auth/reset/finish", json({ username, code: String(f.get("code") || "").trim(), new_password: f.get("new_password") })); setStep("done"); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <main className="growth g-login"><div className="g-card"><Link className="g-brand" href="/growth"><span>↗</span> StepToLead</Link><p className="g-kicker">Клиентский портал</p>
    <h1>{step === "done" ? "Пароль изменён" : "Восстановить доступ"}</h1>
    {step === "login" && <><p className="g-muted">Пришлём код в Telegram-бот StepToLead — если вы подключили его в настройках уведомлений.</p>
      <form onSubmit={start}><label className="g-field">Логин<input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" required autoFocus /></label>
        {error && <p className="g-error">{error}</p>}<button className="g-button" disabled={busy}>{busy ? "Отправляем…" : "Получить код"}</button></form></>}
    {step === "code" && <><p className="g-muted">{message}</p>
      <form onSubmit={finish}><label className="g-field">Код из Telegram<input name="code" inputMode="numeric" pattern="\d{6}" maxLength={6} autoComplete="one-time-code" required autoFocus /></label>
        <label className="g-field">Новый пароль (от 12 символов)<input name="new_password" type="password" minLength={12} autoComplete="new-password" required /></label>
        <label className="g-field">Повторите пароль<input name="repeat" type="password" minLength={12} autoComplete="new-password" required /></label>
        {error && <p className="g-error">{error}</p>}<button className="g-button" disabled={busy}>{busy ? "Сохраняем…" : "Сменить пароль"}</button></form>
      <p className="g-small"><button type="button" className="g-linkButton" onClick={() => { setStep("login"); setError(""); }}>Запросить код ещё раз</button></p></>}
    {step === "done" && <><p className="g-muted">Готово. На других устройствах вы вышли из кабинета — войдите с новым паролем.</p><Link className="g-button" href="/portal/login">Войти</Link></>}
    {step !== "done" && <p className="g-small"><Link href="/portal/login">← Вернуться ко входу</Link></p>}</div></main>;
}
