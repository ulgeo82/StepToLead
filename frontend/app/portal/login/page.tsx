"use client";
import { FormEvent, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import "../../growth/growth.css";

export default function PortalLogin() {
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setBusy(true); setError(""); const data = new FormData(event.currentTarget); try { await api("/portal/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(data)) }); window.location.assign("/portal"); } catch (e) { setError(e instanceof Error ? e.message : "Не удалось войти"); setBusy(false); } }
  return <main className="growth g-login"><div className="g-card"><Link className="g-brand" href="/growth"><span>↗</span> StepToLead</Link><p className="g-kicker">Клиентский портал</p><h1>Вход в кабинет</h1><p className="g-muted">Реклама, лиды и работа отдела продаж в одном месте.</p><form onSubmit={submit}><label className="g-field">Логин<input name="username" autoComplete="username" required /></label><label className="g-field">Пароль<input name="password" type="password" autoComplete="current-password" required /></label>{error && <p className="g-error">{error}</p>}<button className="g-button" disabled={busy}>{busy ? "Входим…" : "Войти"}</button></form><p className="g-small">Доступ выдаёт администратор StepToLead. Публичной регистрации нет.</p></div></main>;
}
