"use client";

import { useState } from "react";
import { request } from "./shared";

type Found = { channel_id: number; kind: string; name: string; found: boolean;
  title: string | null; has_history: boolean; error?: string; reason?: string };

export function ReachContact({ dealId, onOpen }: { dealId: number; onOpen: (id: number) => void }) {
  const [items, setItems] = useState<Found[] | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  async function lookup() {
    setBusy(true);
    setError("");
    try {
      const result = await request<{ items: Found[]; message: string | null }>(
        `/crm/deals/${dealId}/reach/lookup`, "POST", {});
      setItems(result.items);
      setNotice(result.message || (!result.items.length ? "Подключите канал в настройках чатов." : ""));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function open(channelId: number) {
    setBusy(true);
    setError("");
    try {
      const result = await request<{ conversation_id: number }>(`/crm/deals/${dealId}/reach/open`,
        "POST", { channel_id: channelId });
      onOpen(result.conversation_id);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <div><button className="crmLinkButton" disabled={busy} onClick={() => void lookup()}>
    {busy ? "Ищем…" : "Найти и написать"}</button>
    {error && <p role="alert" className="crmFormError">{error}</p>}
    {notice && <p role="status" className="crmMuted">{notice}</p>}
    {items?.map(item => <div key={item.channel_id} className="crmMuted">
      <b>{item.name}</b> · {item.found ? item.title : item.error || item.reason || "Не найден"}
      {item.found && <button disabled={busy} onClick={() => void open(item.channel_id)}>
        {item.has_history ? "Открыть с историей" : "Открыть диалог"}</button>}
    </div>)}
  </div>;
}
