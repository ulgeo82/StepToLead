"use client";

import { FormEvent, useEffect, useState } from "react";
import { request } from "./shared";

type Preset = { imap_host: string; smtp_host: string };

export function EmailChannelConnect({ projectId, onChanged }: { projectId: number; onChanged: () => void }) {
  const [presets, setPresets] = useState<Record<string, Preset>>({});
  const [imap, setImap] = useState("imap.yandex.ru");
  const [smtp, setSmtp] = useState("smtp.yandex.ru");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { request<Record<string, Preset>>("/crm/email-presets", "GET")
    .then(setPresets).catch(e => setError(e.message)); }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true);
    setError("");
    try {
      await request(`/crm/projects/${projectId}/email-channel`, "POST", {
        name: data.get("name"), email: data.get("email"), login: data.get("login") || data.get("email"),
        password: data.get("password"), imap_host: imap, smtp_host: smtp,
        imap_port: Number(data.get("imap_port")), smtp_port: Number(data.get("smtp_port")),
        signature: data.get("signature") || "",
      });
      form.reset();
      onChanged();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <form className="chatAddChannel" onSubmit={submit}><h3 className="crmSub">Почта</h3>
    <p className="crmMuted">Пароль приложения. Проверяется вход в IMAP и SMTP; новые письма — раз в минуту.</p>
    {error && <p className="crmFormError" role="alert">{error}</p>}
    <label>Пресет<select onChange={e => {
      const preset = presets[e.target.value];
      if (preset) { setImap(preset.imap_host); setSmtp(preset.smtp_host); }
    }}><option value="yandex">Яндекс</option><option value="mailru">Mail.ru</option>
      <option value="gmail">Gmail</option><option value="custom">Другой сервер</option></select></label>
    <div className="resultModalGrid"><label>Название<input name="name" required minLength={2}/></label>
      <label>Email<input name="email" type="email" required/></label></div>
    <div className="resultModalGrid"><label>IMAP<input value={imap} required
      onChange={e => setImap(e.target.value)}/></label><label>Порт IMAP
      <input name="imap_port" type="number" defaultValue={993} min={1} max={65535} required/></label></div>
    <div className="resultModalGrid"><label>SMTP<input value={smtp} required
      onChange={e => setSmtp(e.target.value)}/></label><label>Порт SMTP
      <input name="smtp_port" type="number" defaultValue={465} min={1} max={65535} required/></label></div>
    <label>Логин (если отличается от email)<input name="login"/></label>
    <label>Пароль приложения<input name="password" type="password" required autoComplete="off"/></label>
    <label>Подпись менеджера<textarea name="signature" maxLength={2000}/></label>
    <button className="resultPrimary" disabled={busy}>{busy ? "Проверяем…" : "Проверить и подключить"}</button>
  </form>;
}
