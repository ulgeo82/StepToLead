"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

/** Registers the service worker so the portal can be installed on a phone and receive pushes. */
export function RegisterServiceWorker() {
  useEffect(() => {
    if ("serviceWorker" in navigator && window.location.protocol === "https:") navigator.serviceWorker.register("/sw.js").catch(() => undefined);
  }, []);
  return null;
}

const toBytes = (base64: string) => {
  const padded = (base64 + "=".repeat((4 - base64.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), c => c.charCodeAt(0));
};
const json = (method: string, body?: unknown) => ({ method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });

/** Settings → «Уведомления»: push notifications on this phone or computer. */
export function PushCard() {
  const [state, setState] = useState<"unsupported" | "insecure" | "ios" | "off" | "on" | "denied" | "loading">("loading");
  const [error, setError] = useState("");
  const check = useCallback(async () => {
    const ios = /iPhone|iPad/.test(navigator.userAgent) && !(window.matchMedia("(display-mode: standalone)").matches);
    if (window.location.protocol !== "https:" && window.location.hostname !== "localhost") return setState("insecure");
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return setState(ios ? "ios" : "unsupported");
    if (Notification.permission === "denied") return setState("denied");
    const registration = await navigator.serviceWorker.getRegistration();
    const subscription = await registration?.pushManager.getSubscription();
    setState(subscription ? "on" : "off");
  }, []);
  useEffect(() => { check().catch(() => setState("unsupported")); }, [check]);
  async function enable() {
    setError("");
    try {
      const registration = await navigator.serviceWorker.register("/sw.js");
      await navigator.serviceWorker.ready;
      if (await Notification.requestPermission() !== "granted") { setState("denied"); return; }
      const { public_key } = await api<{ public_key: string }>("/portal/push/key");
      const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: toBytes(public_key) });
      await api("/portal/push/subscribe", json("POST", subscription.toJSON()));
      await api("/portal/push/test", json("POST"));
      setState("on");
    } catch (e) { setError((e as Error).message || "Не удалось включить уведомления"); }
  }
  async function disable() {
    const registration = await navigator.serviceWorker.getRegistration();
    const subscription = await registration?.pushManager.getSubscription();
    if (subscription) { await api("/portal/push/unsubscribe", json("POST", { endpoint: subscription.endpoint })).catch(() => undefined); await subscription.unsubscribe(); }
    setState("off");
  }
  const text: Record<string, string> = {
    loading: "Проверяем…", on: "Включены на этом устройстве: новые заявки, пропущенные звонки и «заявка без ответа» приходят сразу.",
    off: "Новые заявки и напоминания будут приходить на этот телефон или компьютер, даже когда портал закрыт.",
    denied: "Уведомления запрещены в настройках браузера. Разрешите их для этого сайта и обновите страницу.",
    unsupported: "Этот браузер не поддерживает push-уведомления.", insecure: "Push-уведомления работают только на сайте с https.",
    ios: "На iPhone: откройте портал в Safari → «Поделиться» → «На экран „Домой“», запустите с иконки и включите уведомления здесь." };
  return <section className="resultPanel settingsCard"><div className="settingsCardHead"><div><h2>Уведомления на телефон</h2><p>{text[state]}</p></div>
    {state === "off" && <button className="crmPrimary" onClick={enable}>Включить</button>}{state === "on" && <button className="crmGhost" onClick={disable}>Выключить</button>}</div>
    {error && <p className="crmFormError">{error}</p>}
    <p className="convHint">Совет: установите портал как приложение — в Chrome «Установить приложение», на iPhone «На экран „Домой“». Откроется сразу CRM.</p></section>;
}
