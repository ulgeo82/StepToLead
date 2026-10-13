"use client";
import { FormEvent, useState } from "react";
import { useParams } from "next/navigation";
import { BrandMark } from "@/components/brand-mark";
import "../../growth/growth.css";

// Тестовая заявка из демо: человек сканирует QR своим телефоном и видит, как заявка за секунду появляется в портале и в Telegram.
export default function TryLead() {
  const { token } = useParams<{ token: string }>();
  const [state, setState] = useState<"form" | "busy" | "done">("form");
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setState("busy"); setError("");
    const f = new FormData(event.currentTarget);
    const response = await fetch(`/api/public/try/${encodeURIComponent(token)}`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: f.get("name"), phone: f.get("phone"), comment: f.get("comment") || "" }) }).catch(() => null);
    if (response?.ok) { setState("done"); return; }
    const body = await response?.json().catch(() => null);
    setError(typeof body?.detail === "string" ? body.detail : "Не удалось отправить, попробуйте ещё раз"); setState("form");
  }
  return <main className="growth g-login"><div className="g-card"><div className="g-brand"><BrandMark size={36}/></div>
    {state === "done" ? <><h1>Готово ✅</h1><p className="g-muted">Посмотрите на экран портала: заявка уже в колонке «Неразобранное». Если вы подключили Telegram — уведомление пришло и туда.</p></>
      : <><p className="g-kicker">Тестовая заявка</p><h1>Оставьте заявку как ваш клиент</h1><p className="g-muted">Так заявки с сайта, рекламы и карт попадают в портал — за секунду.</p>
        <form onSubmit={submit}><label className="g-field">Имя<input name="name" required minLength={2} maxLength={120} autoComplete="name"/></label>
          <label className="g-field">Телефон<input name="phone" required minLength={5} maxLength={40} inputMode="tel" autoComplete="tel"/></label>
          <label className="g-field">Что интересует<input name="comment" maxLength={500} placeholder="Например: хочу узнать цену"/></label>
          {error && <p className="g-error" role="alert">{error}</p>}
          <button className="g-button" disabled={state === "busy"}>{state === "busy" ? "Отправляем…" : "Отправить заявку"}</button></form></>}
  </div></main>;
}
