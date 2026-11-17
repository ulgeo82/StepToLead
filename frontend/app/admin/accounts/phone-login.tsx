"use client";

/* Добавить аккаунт по номеру телефона: номер → код из Telegram → (если включён) облачный пароль.
   Подключение — только через прокси: выбранный или свободный прокси клиента. */

import { FormEvent, useState } from "react";
import type { InfraAccount, InfraProxy } from "../telegram-types";
import { isFreeProxy, json } from "../telegram-infra";
import css from "../telegram-infra.module.css";

type Step = "phone" | "code" | "password";

export function PhoneLoginDialog({ proxies, onClose, onChanged }: {
  proxies: InfraProxy[]; onClose: () => void; onChanged: () => void;
}) {
  const [step, setStep] = useState<Step>("phone");
  const [phone, setPhone] = useState("");
  const [proxyId, setProxyId] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [account, setAccount] = useState<InfraAccount | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const free = proxies.filter(isFreeProxy);

  async function run(fn: () => Promise<void>) {
    setBusy(true); setError("");
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : "Не удалось"); }
    finally { setBusy(false); }
  }
  function requestCode(event?: FormEvent) {
    event?.preventDefault();
    void run(async () => {
      const result = await json<InfraAccount>("/telegram-accounts/auth/request-code",
        { phone, proxy_id: proxyId ? Number(proxyId) : null });
      setAccount(result); setStep("code"); onChanged();
    });
  }
  function confirm(event: FormEvent) {
    event.preventDefault();
    if (!account) return;
    void run(async () => {
      const result = await json<{ account: InfraAccount; requires_2fa: boolean }>("/telegram-accounts/auth/confirm",
        { account_id: account.id, code: step === "code" ? code.trim() : null, password: step === "password" ? password : null });
      setPassword("");
      if (result.requires_2fa) { setStep("password"); return; }
      onChanged(); onClose();
    });
  }

  return <div className="modalBackdrop" onMouseDown={onClose}>
    <form className={`modal ${css.modal}`} onMouseDown={e => e.stopPropagation()}
      onSubmit={step === "phone" ? requestCode : confirm}>
      <div className="modalHead"><div><p className="eyebrow">Telegram</p><h2>Добавить по номеру</h2></div>
        <button type="button" className="close" onClick={onClose} aria-label="Закрыть">×</button></div>
      {step === "phone" && <>
        <label>Номер телефона<input required inputMode="tel" autoComplete="off" placeholder="+7 900 000-00-00"
          value={phone} onChange={e => setPhone(e.target.value)}/></label>
        <label>Прокси<select value={proxyId} onChange={e => setProxyId(e.target.value)}>
          <option value="">Автоматически — свободный ({free.length})</option>
          {free.map(p => <option key={p.id} value={p.id}>{p.host}:{p.port} · {p.scheme.toUpperCase()}</option>)}
        </select><small>Без прокси аккаунт не подключается. Аккаунт попадёт к выбранному сейчас клиенту.</small></label>
      </>}
      {step === "code" && <>
        <p>Telegram отправил код в приложение на {account?.phone} (или СМС).</p>
        <label>Код<input required autoFocus inputMode="numeric" autoComplete="one-time-code" value={code}
          onChange={e => setCode(e.target.value.replace(/\D/g, ""))}/></label>
        <button type="button" className="button secondary" disabled={busy} onClick={() => requestCode()}>Отправить код ещё раз</button>
      </>}
      {step === "password" && <label>Облачный пароль (двухэтапная проверка)
        <input required autoFocus type="password" autoComplete="off" value={password} onChange={e => setPassword(e.target.value)}/>
        <small>Пароль не сохраняется — нужен только для входа.</small></label>}
      {error && <p className="notice" role="alert">{error}</p>}
      <button className="button primary full" disabled={busy}>
        {busy ? "Подождите…" : step === "phone" ? "Получить код" : "Войти"}
      </button>
    </form>
  </div>;
}
