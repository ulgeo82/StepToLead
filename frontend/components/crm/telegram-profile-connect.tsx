"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { request } from "./shared";

type Qr = { status: string; error: string | null; svg?: string; expires_at?: string; deadline?: string };
type Profile = { id: number; status: string };

// Подключение рабочего Telegram-аккаунта: по умолчанию по QR-коду (как «Подключить устройство» в Telegram Desktop),
// запасной вариант — по номеру и коду.
export function TelegramProfileConnect({ projectId, onChanged }: { projectId: number; onChanged: () => void }) {
  const [way, setWay] = useState<"qr" | "phone">("qr");
  return <div className="chatAddChannel">
    <h3 className="crmSub">Telegram-профиль</h3>
    <p className="crmMuted">Рабочий аккаунт отдела продаж, не бот: можно писать клиентам первыми, переписка остаётся у компании.</p>
    <div className="crmChips"><button type="button" className={way === "qr" ? "active" : ""} onClick={() => setWay("qr")}>По QR-коду</button>
      <button type="button" className={way === "phone" ? "active" : ""} onClick={() => setWay("phone")}>По номеру и коду</button></div>
    {way === "qr" ? <QrConnect projectId={projectId} onChanged={onChanged}/> : <PhoneConnect projectId={projectId} onChanged={onChanged}/>}
  </div>;
}

function QrConnect({ projectId, onChanged }: { projectId: number; onChanged: () => void }) {
  const [channelId, setChannelId] = useState<number | null>(null);
  const [qr, setQr] = useState<Qr | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const form = useRef<{ name: string; mode: string }>({ name: "", mode: "corporate" });

  useEffect(() => {
    if (!channelId || !qr || !["waiting", "password_required"].includes(qr.status)) return;
    const timer = setInterval(() => {
      request<Qr>(`/crm/channels/${channelId}/telegram-qr`, "GET").then(next => {
        setQr(next);
        if (next.status === "connected") onChanged();
      }).catch(e => setError((e as Error).message));
    }, 2000);
    return () => clearInterval(timer);
  }, [channelId, qr, onChanged]);

  async function show(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    if (event) {
      const data = new FormData(event.currentTarget);
      form.current = { name: String(data.get("name") || ""), mode: String(data.get("mode") || "corporate") };
    }
    setBusy(true); setError("");
    try {
      const result = await request<{ channel: Profile; qr: Qr }>(`/crm/projects/${projectId}/telegram-profile/qr`, "POST",
        { name: form.current.name, mode: form.current.mode, channel_id: channelId && qr?.status !== "connected" ? channelId : null });
      setChannelId(result.channel.id); setQr(result.qr); onChanged();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function password(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      await request(`/crm/channels/${channelId}/telegram-qr/password`, "POST", { password: data.get("password") });
      setQr({ status: "connected", error: null }); onChanged();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function cancel() {
    if (channelId) await request(`/crm/channels/${channelId}/telegram-qr`, "DELETE").catch(() => undefined);
    setChannelId(null); setQr(null); setError(""); onChanged();
  }

  if (qr?.status === "connected") return <p role="status" className="crmOk">Telegram-профиль подключён. Новые сообщения появятся в чатах.</p>;
  return <>
    {error && <p role="alert" className="crmFormError">{error}</p>}
    {!qr ? <form onSubmit={show}>
      <label>Название<input name="name" required minLength={2} placeholder="Telegram отдела продаж" defaultValue={form.current.name}/></label>
      <label>Режим<select name="mode" defaultValue={form.current.mode}><option value="corporate">Корпоративный — каждый новый чат = заявка</option>
        <option value="personal">Личный — только переписка с клиентами из CRM</option></select></label>
      <button className="resultPrimary" disabled={busy}>{busy ? "Готовим QR-код…" : "Показать QR-код"}</button></form>
    : qr.status === "waiting" ? <div className="tgQr">
      {qr.svg && <div className="tgQrCode" dangerouslySetInnerHTML={{ __html: qr.svg }}/>}
      <ol><li>Откройте Telegram на телефоне с рабочим аккаунтом.</li>
        <li>Настройки → Устройства → «Подключить устройство».</li>
        <li>Наведите камеру на этот код. Код обновляется сам.</li></ol>
      <p className="crmMuted">Первые сутки после входа Telegram ограничивает некоторые настройки нового устройства — на переписку это не влияет.</p>
      <button type="button" className="crmLinkButton" onClick={() => void cancel()}>Отмена</button></div>
    : qr.status === "password_required" ? <form onSubmit={password}>
      <p className="crmMuted">На аккаунте включён облачный пароль (двухэтапная защита). Введите его, чтобы завершить вход.</p>
      <label>Облачный пароль Telegram<input name="password" type="password" required autoComplete="off" autoFocus/></label>
      <button className="resultPrimary" disabled={busy}>{busy ? "Проверяем…" : "Войти"}</button>
      <button type="button" className="crmLinkButton" onClick={() => void cancel()}>Отмена</button></form>
    : <div><p className="crmFormError">{qr.error || "QR-код устарел"}</p>
      <button type="button" className="resultPrimary" disabled={busy} onClick={() => void show()}>Показать новый QR-код</button>
      <button type="button" className="crmLinkButton" onClick={() => void cancel()}>Отмена</button></div>}
  </>;
}

function PhoneConnect({ projectId, onChanged }: { projectId: number; onChanged: () => void }) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      const next = profile && ["code_required", "password_required"].includes(profile.status)
        ? await request<Profile>(`/crm/channels/${profile.id}/telegram-confirm`, "POST", {
            code: form.get("code") || null, password: form.get("password") || null })
        : await request<Profile>(`/crm/projects/${projectId}/telegram-profile`, "POST", {
            name: form.get("name"), phone: form.get("phone"), mode: form.get("mode") });
      setProfile(next); onChanged();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <form onSubmit={submit}>
    <p className="crmMuted">Если код не приходит — используйте вход по QR-коду: Telegram часто не отправляет коды при входе с сервера.</p>
    {error && <p role="alert" className="crmFormError">{error}</p>}
    {!profile || profile.status === "waiting_proxy" ? <>
      <label>Название<input name="name" required minLength={2}/></label>
      <label>Телефон<input name="phone" required type="tel" placeholder="+79001234567" pattern="\+[0-9]{8,15}"/></label>
      <label>Режим<select name="mode"><option value="corporate">Корпоративный — каждый новый чат = заявка</option>
        <option value="personal">Личный — только переписка с клиентами из CRM</option></select></label>
      {profile?.status === "waiting_proxy" && <p role="status">Для компании не настроен прокси. Обратитесь к агентству.</p>}
    </> : profile.status === "code_required" ? <label>Код из Telegram<input name="code" required autoComplete="one-time-code"/></label>
      : profile.status === "password_required" ? <label>Облачный пароль Telegram<input name="password" type="password" required autoComplete="off"/></label>
        : <p role="status" className="crmOk">Telegram-профиль подключён.</p>}
    {profile?.status !== "connected" && <button className="resultPrimary" disabled={busy}>{busy ? "Подключаем…" : profile ? "Продолжить" : "Получить код"}</button>}
  </form>;
}
