"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export type TelegramStatus = { configured: boolean; linked: boolean; telegram_username: string | null; bot_username: string | null; company_bot?: boolean; relink_suggested?: boolean };

// Личный Telegram сотрудника: доступно любой роли (МОП, РОП, собственник) — в «Настройках» и в «Мои уведомления».
export function TelegramCard({ configured, onChanged }: { configured: boolean; onChanged: () => void }) {
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [busy, setBusy] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [message, setMessage] = useState("");
  const call = async (path: string, method: string) => api<TelegramStatus>(`/portal/telegram${path}`, { method });
  useEffect(() => {
    if (!waiting) return;
    let active = true; let pending = false;
    const started = Date.now();
    const timer = setInterval(async () => {
      if (Date.now() - started >= 120000) { clearInterval(timer); if (active) { setWaiting(false); setMessage("Проверка завершена. Если Telegram не подключён, откройте ссылку ещё раз."); } return; }
      if (pending) return;
      pending = true;
      try {
        const next = await api<TelegramStatus>("/portal/telegram");
        if (active) { setStatus(next); if (next.linked && !next.relink_suggested) { setWaiting(false); setMessage("Telegram подключён."); onChanged(); } }
      } catch { /* keep checking until the timeout */ } finally { pending = false; }
    }, 3000);
    return () => { active = false; clearInterval(timer); };
  }, [waiting]);
  useEffect(() => { api<TelegramStatus>("/portal/telegram").then(setStatus).catch(() => setStatus(null)); }, [configured]);
  async function run(name: string, action: () => Promise<void>) { setBusy(name); setMessage("");
    try { await action(); } catch (e) { setMessage((e as Error).message); } finally { setBusy(""); } }
  const link = () => run("link", async () => { const result = await api<{ url: string }>("/portal/telegram/link", { method: "POST" });
    window.open(result.url, "_blank", "noopener,noreferrer"); setWaiting(true);
    setMessage("В Telegram нажмите «Запустить» (Start). Подключение подтвердится здесь автоматически."); });
  const check = () => run("check", async () => { const next = await call("/check", "POST"); setStatus(next);
    if (next.linked && !next.relink_suggested) { setWaiting(false); setMessage("Telegram подключён."); onChanged(); }
    else setMessage("Бот пока не получил команду Start. Откройте ссылку ещё раз и нажмите «Запустить»."); });
  const test = () => run("test", async () => { await api("/portal/telegram/test", { method: "POST" }); setMessage("Тестовое сообщение отправлено."); });
  const unlink = () => run("unlink", async () => { setStatus(await call("", "DELETE")); setMessage("Telegram отключён."); onChanged(); });
  return <section className="resultPanel settingsCard settingsTelegram"><div className="settingsCardHead"><div><h2>Мой Telegram</h2>
    <p>{!configured ? "Бот для уведомлений ещё не подключён — обратитесь к вашему маркетологу StepToLead." : status?.relink_suggested ? `У компании теперь свой бот @${status.bot_username}. Нажмите «Перейти в бот компании», чтобы уведомления приходили туда.` : status?.linked ? `Подключён${status.telegram_username ? ` @${status.telegram_username}` : ""}. Сюда приходят уведомления, если они включены для вас в правилах ниже, и код для восстановления пароля.` : `Подключите Telegram, чтобы получать уведомления через бота${status?.bot_username ? ` @${status.bot_username}` : ""} — и сами восстанавливать пароль, если забудете.`}</p></div>
    {configured && <div className="settingsTelegramActions">{status?.relink_suggested && <button className="settingsPrimary" disabled={!!busy} onClick={link}>{busy === "link" ? "Готовим ссылку…" : `Перейти в бот компании${status.bot_username ? ` @${status.bot_username}` : ""}`}</button>}{status?.linked ? <><button className="settingsOutline" disabled={!!busy} onClick={test}>{busy === "test" ? "Отправляем…" : "Отправить тест"}</button><button className="settingsOutline" disabled={!!busy} onClick={unlink}>Отключить</button></> : <><button className="settingsPrimary" disabled={!!busy} onClick={link}>{busy === "link" ? "Готовим ссылку…" : "Подключить Telegram"}</button>{waiting && <button className="settingsOutline" disabled={!!busy} onClick={check}>{busy === "check" ? "Проверяем…" : "Проверить"}</button>}</>}</div>}</div>
    {message && <p className="settingsHint" role="status">{message}</p>}</section>;
}
