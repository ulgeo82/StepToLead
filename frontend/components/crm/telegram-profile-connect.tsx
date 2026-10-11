"use client";

import { FormEvent, useState } from "react";
import { request } from "./shared";

export function TelegramProfileConnect({ projectId, onChanged }: {
  projectId: number; onChanged: () => void;
}) {
  const [profile, setProfile] = useState<{ id: number; status: string } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      const next = profile && ["code_required", "password_required"].includes(profile.status)
        ? await request<typeof profile>(`/crm/channels/${profile.id}/telegram-confirm`, "POST", {
            code: form.get("code") || null, password: form.get("password") || null,
          })
        : await request<typeof profile>(`/crm/projects/${projectId}/telegram-profile`, "POST", {
            name: form.get("name"), phone: form.get("phone"), mode: form.get("mode"),
          });
      setProfile(next);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return <form className="chatAddChannel" onSubmit={submit}>
    <h3 className="crmSub">Telegram-профиль</h3>
    <p className="crmMuted">Рабочий аккаунт, не бот. IP закрепляется за профилем без автоматической смены.</p>
    {error && <p role="alert" className="crmFormError">{error}</p>}
    {!profile || profile.status === "waiting_proxy" ? <>
      <label>Название<input name="name" required minLength={2}/></label>
      <label>Телефон<input name="phone" required type="tel" placeholder="+79001234567" pattern="\+[0-9]{8,15}"/></label>
      <label>Режим<select name="mode"><option value="corporate">Корпоративный — все личные заявки</option>
        <option value="personal">Личный — только контакты CRM</option></select></label>
      {profile?.status === "waiting_proxy" && <p role="status">Нет свободных прокси. Обратитесь к агентству.</p>}
    </> : profile.status === "code_required" ? <label>Код из Telegram
      <input name="code" required autoComplete="one-time-code"/></label>
      : profile.status === "password_required" ? <label>Пароль двухэтапной защиты
        <input name="password" type="password" required autoComplete="off"/></label>
        : <p role="status" className="crmOk">Telegram-профиль подключён.</p>}
    {profile?.status !== "connected" && <button className="resultPrimary" disabled={busy}>
      {busy ? "Подключаем…" : profile ? "Продолжить" : "Получить код"}</button>}
  </form>;
}
