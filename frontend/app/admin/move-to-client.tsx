"use client";

/* «Перенести к клиенту»: у каждой компании свои Telegram-аккаунты и прокси. Аккаунт переезжает вместе со своим
   прокси; занятый в кампании другого клиента аккаунт сервер не перенесёт и объяснит почему. */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { currentSpace } from "@/lib/space";
import { json } from "./telegram-infra";

type Space = { id: number; name: string; is_home: boolean };

export function MoveToClient({ path, ids, disabled, onDone }: {
  path: "/telegram-accounts/move" | "/proxies/move"; ids: number[]; disabled?: boolean;
  onDone: (message: string) => void;
}) {
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api<{ items: Space[] }>("/admin/leadgen/spaces", { noSpace: true }).then(r => setSpaces(r.items)).catch(() => setSpaces([]));
  }, []);
  const here = currentSpace() ?? spaces.find(s => s.is_home)?.id;
  const options = spaces.filter(s => s.id !== here);
  if (!options.length) return null;

  async function move() {
    const space = options.find(s => String(s.id) === target);
    if (!space || !ids.length) return;
    if (!window.confirm(`Перенести отмеченные (${ids.length}) к клиенту «${space.name}»?`)) return;
    setBusy(true);
    try {
      const r = await json<{ accounts?: number; proxies: number; without_proxy?: number }>(path, { ids, workspace_id: space.id });
      onDone(r.accounts != null
        ? `Перенесено к «${space.name}»: аккаунтов ${r.accounts}, прокси вместе с ними ${r.proxies}` +
          (r.without_proxy ? `, без прокси ${r.without_proxy} — раздайте им прокси у клиента` : "")
        : `Перенесено к «${space.name}»: прокси ${r.proxies}`);
      setTarget("");
    } catch (error) {
      onDone(error instanceof Error ? error.message : "Не удалось перенести");
    } finally { setBusy(false); }
  }

  return <label>Перенести к клиенту
    <select value={target} disabled={disabled || busy || !ids.length} onChange={e => setTarget(e.target.value)}>
      <option value="">Выберите клиента…</option>
      {options.map(s => <option key={s.id} value={s.id}>{s.name}{s.is_home ? " — агентство" : ""}</option>)}
    </select>
    <button className="button secondary" disabled={disabled || busy || !ids.length || !target} onClick={() => void move()}>
      {busy ? "Переносим…" : "Перенести"}
    </button>
  </label>;
}
