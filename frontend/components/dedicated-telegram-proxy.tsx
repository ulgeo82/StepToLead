"use client";

import { useEffect, useState } from "react";
import { api, Proxy } from "@/lib/api";

export function DedicatedTelegramProxy({ workspaceId }: { workspaceId: number }) {
  const [proxies, setProxies] = useState<Proxy[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () => api<Proxy[]>("/proxies").then(setProxies).catch(e => setError(e.message));
  useEffect(() => { void load(); }, [workspaceId]);
  async function select(value: string) {
    setBusy(true);
    setError("");
    try {
      const old = proxies.find(p => p.dedicated_workspace_id === workspaceId);
      const next = proxies.find(p => String(p.id) === value);
      for (const [proxy, dedicated] of [[old, null], [next, workspaceId]] as const) {
        if (proxy) await api(`/proxies/${proxy.id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pool: "client", max_accounts: proxy.max_accounts,
            dedicated_workspace_id: dedicated }),
        });
      }
      await load();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="tablePanel"><h2>Выделенный IP для Telegram</h2>
    <p>Выбор применяется к новым профилям. Закреплённые IP автоматически не меняются.</p>
    {error && <p role="alert">{error}</p>}
    <select disabled={busy} aria-label="Выделенный IP для Telegram"
      value={proxies.find(p => p.dedicated_workspace_id === workspaceId)?.id || ""}
      onChange={e => void select(e.target.value)}>
      <option value="">Общий пул клиентов</option>
      {proxies.filter(p => p.pool === "client" && (!p.dedicated_workspace_id ||
        p.dedicated_workspace_id === workspaceId)).map(p => <option key={p.id} value={p.id}>
        {p.host}:{p.port} · {p.client_accounts}/{p.max_accounts}</option>)}
    </select>
  </section>;
}
