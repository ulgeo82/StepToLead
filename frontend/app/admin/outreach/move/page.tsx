"use client";

/* «Перенести аутрич к клиенту»: кампании (email и Telegram) со всем содержимым, ящики и аккаунты из текущего
   пространства — к другому клиенту. Сначала проверка (что переедет и что мешает), потом перенос. */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { currentSpace } from "@/lib/space";

type Options = {
  email_campaigns: { id: number; name: string; status: string }[];
  mailboxes: { id: number; email: string }[];
  telegram_campaigns: { id: number; name: string }[];
  accounts: { id: number; phone: string; username: string | null }[];
};
type Result = {
  email_campaigns: number; sequences: number; companies: number; enrollments: number; mailboxes: number;
  telegram_campaigns: number; accounts: number; proxies: number; problems: string[]; dry_run: boolean;
};
type Space = { id: number; name: string; is_home: boolean };
type Group = "email_campaign_ids" | "mailbox_ids" | "telegram_campaign_ids" | "account_ids";

const base = "/admin/leadgen";

export default function MoveOutreachPage() {
  const [options, setOptions] = useState<Options | null>(null);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [target, setTarget] = useState("");
  const [picked, setPicked] = useState<Record<Group, number[]>>({
    email_campaign_ids: [], mailbox_ids: [], telegram_campaign_ids: [], account_ids: [] });
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Options>(`${base}/move-outreach/options`).then(setOptions).catch(e => setError(e.message));
    api<{ items: Space[] }>(`${base}/spaces`, { noSpace: true }).then(r => setSpaces(r.items)).catch(() => {});
  }, []);
  const here = currentSpace() ?? spaces.find(s => s.is_home)?.id;
  const targets = spaces.filter(s => s.id !== here);

  function toggle(group: Group, id: number) {
    setResult(null);
    setPicked(p => ({ ...p, [group]: p[group].includes(id) ? p[group].filter(x => x !== id) : [...p[group], id] }));
  }
  async function run(dry: boolean) {
    if (!target) return;
    if (!dry && !window.confirm(`Перенести выбранное к «${targets.find(t => String(t.id) === target)?.name}»?`)) return;
    setBusy(true); setError("");
    try {
      const r = await api<Result>(`${base}/move-outreach`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: Number(target), ...picked, dry_run: dry }) });
      setResult(r);
      if (!dry) {
        setPicked({ email_campaign_ids: [], mailbox_ids: [], telegram_campaign_ids: [], account_ids: [] });
        setOptions(await api<Options>(`${base}/move-outreach/options`));
      }
    } catch (e) { setError(e instanceof Error ? e.message : "Не удалось"); }
    finally { setBusy(false); }
  }

  const list = (title: string, group: Group, items: { id: number; label: string }[]) => <section className="tablePanel">
    <div className="tableTitle"><h2>{title}</h2></div>
    {items.length ? items.map(item => <label key={item.id} className="checkLine" style={{ display: "flex", gap: 8 }}>
      <input type="checkbox" checked={picked[group].includes(item.id)} onChange={() => toggle(group, item.id)}/>{item.label}
    </label>) : <p className="mutedText">Нет</p>}
  </section>;
  const any = Object.values(picked).some(v => v.length);

  return <div className="page">
    <header className="pageHeader"><div><p className="eyebrow">Аутрич</p><h1>Перенести к клиенту</h1>
      <p className="subtitle">Завели у себя то, что относится к клиенту? Выберите кампании, ящики и аккаунты — они
        переедут со всем содержимым: компании и переписка кампаний, цепочки, лиды и диалоги Telegram, прокси аккаунтов.
        Сделки в CRM агентства и расходы на поиск остаются у агентства.</p></div></header>
    {error && <p className="notice error" role="alert">{error}</p>}
    {!targets.length && spaces.length > 0 && <p className="notice">Других клиентов нет — сначала заведите клиента в «Клиентах».</p>}
    <label>Кому перенести <select value={target} onChange={e => { setTarget(e.target.value); setResult(null); }}>
      <option value="">Выберите клиента…</option>
      {targets.map(s => <option key={s.id} value={s.id}>{s.name}{s.is_home ? " — агентство" : ""}</option>)}
    </select></label>
    {!options ? <p className="mutedText">Загружаем…</p> : <>
      {list("Email-кампании", "email_campaign_ids", options.email_campaigns.map(c => ({ id: c.id, label: c.name })))}
      {list("Почтовые ящики", "mailbox_ids", options.mailboxes.map(m => ({ id: m.id, label: m.email })))}
      {list("Telegram-кампании", "telegram_campaign_ids", options.telegram_campaigns.map(c => ({ id: c.id, label: c.name })))}
      {list("Telegram-аккаунты", "account_ids", options.accounts.map(a => ({ id: a.id,
        label: `${a.username ? "@" + a.username + " · " : ""}${a.phone}` })))}
    </>}
    {result && <section className="notice" role="status">
      <strong>{result.dry_run ? "Переедет:" : "Перенесено:"}</strong> email-кампаний {result.email_campaigns}, цепочек {result.sequences},
      компаний {result.companies}, ящиков {result.mailboxes}, Telegram-кампаний {result.telegram_campaigns},
      аккаунтов {result.accounts}, прокси {result.proxies}.
      {result.problems.length > 0 && <ul>{result.problems.map(p => <li key={p}>{p}</li>)}</ul>}
    </section>}
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
      <button className="button secondary" disabled={busy || !target || !any} onClick={() => void run(true)}>Проверить</button>
      <button className="button primary" disabled={busy || !target || !result?.dry_run || result.problems.length > 0}
        onClick={() => void run(false)}>Перенести</button>
    </div>
  </div>;
}
