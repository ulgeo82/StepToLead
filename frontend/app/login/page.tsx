"use client";
import { FormEvent, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import "../growth/growth.css";

export default function Login() {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); setBusy(true);
    const data = new FormData(event.currentTarget);
    try {
      await api("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(data)) });
      const next = new URLSearchParams(window.location.search).get("next");
      window.location.assign(next && /^\/admin(?:\/|$)/.test(next) && !next.includes("\\") ? next : "/admin");
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось войти"); setBusy(false); }
  }
  return <main className="growth g-login"><div className="g-card"><Link className="g-brand" href="/growth"><span>↗</span> StepToLead</Link><h1>Вход в управление</h1><p className="g-muted">Кампании, аккаунты и заявки доступны только администратору.</p><form onSubmit={submit}><label className="g-field">Логин<input name="username" autoComplete="username" required maxLength={254} /></label><label className="g-field">Пароль<input name="password" type="password" autoComplete="current-password" required maxLength={256} /></label>{error && <p className="g-error" role="alert">{error}</p>}<button className="g-button" disabled={busy}>{busy ? "Входим…" : "Войти"}</button></form><p className="g-small">Доступ создаётся владельцем портала. Публичной регистрации нет.</p><Link className="g-link" href="/growth">← К калькулятору</Link></div></main>;
}
