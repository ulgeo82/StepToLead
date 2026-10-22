"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { base, date, message, post } from "../../leadgen/shared";
import styles from "../../leadgen/leadgen.module.css";

type Check = { key: string; title: string; ok: boolean | null; detail: string | null; fix: string | null };
type Day = { day: string; sent: number; inbox: number; spam: number; missing: number };
type Card = {
  id: number; email: string; sender_name: string | null; role: "outreach" | "helper"; is_active: boolean;
  warmup_enabled: boolean; warmup_target: number;
  stage: { phase: "warming" | "ready" | "helper" | "off"; title: string; day?: number; of?: number };
  today: { warm_plan: number; warm_sent: number };
  health: { score: number | null; measured: number; inbox_rate: number | null; spam_rate: number | null;
    bounce_rate: number | null; cold_sent: number; checks: Check[]; checked_at: string | null };
  cold_paused: { at: string; reason: string } | null; error: string | null; days: Day[];
};
type Overview = { pool: { total: number; outreach: number; helpers: number; providers: string[] }; running: boolean;
  in_window: boolean; window: string; tips: string[]; items: Card[] };

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const tone = (score: number | null) => (score == null ? "" : score >= 80 ? styles.fitYes : score >= 50 ? styles.fitMaybe : styles.fitNo);

function Bars({ days }: { days: Day[] }) {
  const max = Math.max(1, ...days.map((d) => d.sent));
  return (
    <div className={styles.warmBars} role="img" aria-label="Прогревочные письма за 14 дней: во «Входящих», в «Спаме», не дошли">
      {days.map((d) => {
        const pending = Math.max(0, d.sent - d.inbox - d.spam - d.missing);
        return (
          <span key={d.day} title={`${new Date(d.day).toLocaleDateString("ru-RU", { day: "numeric", month: "short" })}: отправлено ${d.sent} · входящие ${d.inbox} · спам ${d.spam} · не дошли ${d.missing}`}>
            <i style={{ height: `${(d.missing / max) * 100}%` }} className={styles.barMissing} />
            <i style={{ height: `${(d.spam / max) * 100}%` }} className={styles.barSpam} />
            <i style={{ height: `${(pending / max) * 100}%` }} className={styles.barPending} />
            <i style={{ height: `${(d.inbox / max) * 100}%` }} className={styles.barInbox} />
          </span>
        );
      })}
    </div>
  );
}

function MailCard({ card, busy, act }: { card: Card; busy: boolean; act: (path: string, body?: object, method?: string) => void }) {
  const h = card.health;
  const progress = card.stage.day && card.stage.of ? Math.round((card.stage.day / card.stage.of) * 100) : card.stage.phase === "ready" ? 100 : 0;
  const problems = h.checks.filter((c) => c.ok === false);
  return (
    <article className={`${styles.card} ${styles.warmCard}`}>
      <header className={styles.warmHead}>
        <div>
          <h3>{card.email}</h3>
          <p className={styles.muted}>{card.role === "helper" ? "Помощник прогрева — холодные не шлёт" : card.sender_name || "Рабочий ящик"}</p>
        </div>
        <div className={`${styles.warmScore} ${tone(h.score)}`} title="Здоровье ящика: сколько прогревочных писем попадает во «Входящие» с учётом настроек домена">
          <b>{h.score ?? "—"}</b><small>здоровье</small>
        </div>
      </header>

      <div className={styles.warmStage}>
        <span>{card.stage.title}</span>
        {card.stage.phase !== "off" && card.stage.phase !== "helper" && <div className={styles.warmProgress}><i style={{ width: `${progress}%` }} /></div>}
      </div>

      {card.cold_paused && (
        <div className={`${styles.inboxSummary} ${styles.inboxSummaryHot}`}>
          <b>⏸ Холодные письма на паузе</b>
          <p>{card.cold_paused.reason}. Прогрев продолжается.</p>
          <button type="button" className={styles.secondary} disabled={busy} onClick={() => act(`/warmup/${card.id}/resume-cold`)}>Возобновить холодные</button>
        </div>
      )}

      <dl className={styles.warmFacts}>
        <div><dt>Прогрев сегодня</dt><dd>{card.today.warm_sent} / {card.today.warm_plan}</dd></div>
        <div><dt>Во «Входящих»</dt><dd>{h.measured >= 5 ? pct(h.inbox_rate) : <span className={styles.muted}>копим данные</span>}</dd></div>
        <div><dt>Недоставлено</dt><dd>{h.cold_sent >= 20 ? pct(h.bounce_rate) : <span className={styles.muted}>—</span>}</dd></div>
      </dl>

      <Bars days={card.days} />

      <ul className={styles.warmChecks}>
        {h.checks.map((c) => (
          <li key={c.key} className={c.ok === false ? styles.warmBad : c.ok ? styles.warmOk : styles.warmUnknown}>
            <span>{c.ok === false ? "✕" : c.ok ? "✓" : "?"}</span>
            <div><b>{c.title}</b>{c.detail && <small> · {c.detail}</small>}{c.fix && <p>{c.fix}</p>}</div>
          </li>
        ))}
        {!h.checks.length && <li className={styles.warmUnknown}><span>?</span><div>Проверим домен в течение нескольких минут или нажмите «Перепроверить».</div></li>}
      </ul>
      {card.error && <p className={styles.errorText}>{card.error}</p>}

      <div className={styles.actions}>
        <button type="button" className={styles.secondary} disabled={busy} onClick={() => act(`/warmup/${card.id}/check`)}>Перепроверить</button>
        <button type="button" className={styles.secondary} disabled={busy} onClick={() => act(`/mailboxes/${card.id}`, { warmup_enabled: !card.warmup_enabled }, "PATCH")}>
          {card.warmup_enabled ? "Выключить прогрев" : "Включить прогрев"}
        </button>
        {card.role === "outreach" && (
          <label className={styles.inlineForm} title="Сколько прогревочных писем в день к концу разгона">
            <small className={styles.muted}>до</small>
            <select value={card.warmup_target} disabled={busy} onChange={(e) => act(`/mailboxes/${card.id}`, { warmup_target: Number(e.target.value) }, "PATCH")}>
              {[15, 20, 25, 30, 40].map((n) => <option key={n} value={n}>{n} писем/день</option>)}
            </select>
          </label>
        )}
      </div>
      {problems.length > 0 && <p className={styles.muted}>Исправьте пункты с ✕ — после правки DNS нажмите «Перепроверить» (записи обновляются до часа).</p>}
      {h.checked_at && <p className={styles.muted}>Проверено {date(h.checked_at)}</p>}
    </article>
  );
}

export default function WarmupPage() {
  const [tab, setTab] = useState<"email" | "telegram">("email");
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);

  const load = useCallback((signal?: AbortSignal) =>
    api<Overview>(`${base}/warmup`, { signal }).then((d) => { if (!signal?.aborted) { setData(d); setError(""); } })
      .catch((e) => !signal?.aborted && setError(message(e))), []);
  useEffect(() => { const c = new AbortController(); void load(c.signal); return () => c.abort(); }, [load, revision]);
  useEffect(() => { const t = setInterval(() => setRevision((n) => n + 1), 120_000); return () => clearInterval(t); }, []);

  async function act(path: string, body?: object, method = "POST") {
    setBusy(true); setError(""); setNotice("");
    try {
      if (method === "POST") await post(path, body || {});
      else await api(`${base}${path}`, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      setNotice("Готово");
      setRevision((n) => n + 1);
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }

  const outreach = data?.items.filter((c) => c.role === "outreach") || [];
  const helpers = data?.items.filter((c) => c.role === "helper") || [];

  return (
    <div className={`page ${styles.page}`}>
      <header className="topbar"><div className="crumbs"><span>StepToLead</span><b>/</b><strong>Прогрев</strong></div><span className="userAvatar">ЕД</span></header>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Аутрич</p>
          <h1>Прогрев</h1>
          <p className="subtitle">Ящики переписываются между собой, достают письма из «Спама» и отвечают — так почтовики начинают доверять адресу.</p>
        </div>
      </section>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}

      <div className={styles.tabs}>
        <button type="button" className={styles.tab} aria-pressed={tab === "email"} onClick={() => setTab("email")}>Почта{data ? <span>{data.pool.total}</span> : null}</button>
        <button type="button" className={styles.tab} aria-pressed={tab === "telegram"} onClick={() => setTab("telegram")}>Telegram</button>
      </div>

      {tab === "telegram" ? (
        <section className={`${styles.card} ${styles.empty}`}>
          <h2>Прогрев Telegram — следующий этап</h2>
          <p className={styles.muted}>Для купленных аккаунтов: привыкание (профиль, каналы, чтение), переписка аккаунтов между собой, проверка у @SpamBot и постепенный рост лимита в кампаниях.</p>
          <Link href="/admin/accounts">Аккаунты Telegram →</Link>
        </section>
      ) : !data ? <p className={styles.muted} role="status">Загружаем…</p> : <>
        <section className={`${styles.card} ${styles.warmPool}`}>
          <div>
            <h2>{data.running ? (data.in_window ? "● Прогрев идёт" : "◐ Прогрев на паузе до утра") : "○ Прогрев не запущен"}</h2>
            <p className={styles.muted}>
              В пуле {data.pool.total}: рабочих {data.pool.outreach}, помощников {data.pool.helpers}
              {data.pool.providers.length ? ` · ${data.pool.providers.join(", ")}` : ""}. Письма уходят {data.window}, в выходные — вполовину.
            </p>
            {data.tips.map((tip) => <p key={tip} className={styles.hint}>💡 {tip}</p>)}
          </div>
          <Link className={styles.secondary} href="/admin/outreach/email?tab=mailboxes">＋ Добавить ящик или помощника</Link>
        </section>

        {!data.items.length ? (
          <section className={`${styles.card} ${styles.empty}`}><h2>Ящиков пока нет</h2><p className={styles.muted}>Добавьте рабочие ящики и 2–3 помощника в «Email-кампании → Ящики».</p></section>
        ) : <>
          {!!outreach.length && <h2 className={styles.sectionTitle}>Рабочие ящики</h2>}
          <div className={styles.warmGrid}>{outreach.map((c) => <MailCard key={c.id} card={c} busy={busy} act={act} />)}</div>
          {!!helpers.length && <h2 className={styles.sectionTitle}>Помощники</h2>}
          <div className={styles.warmGrid}>{helpers.map((c) => <MailCard key={c.id} card={c} busy={busy} act={act} />)}</div>
        </>}
        <p className={styles.muted}>Столбики — прогревочные письма за 14 дней: <span className={styles.legendInbox}>■</span> во «Входящих», <span className={styles.legendSpam}>■</span> в «Спаме» (портал перенёс во «Входящие»), <span className={styles.legendMissing}>■</span> не дошли, <span className={styles.legendPending}>■</span> ещё проверяем.</p>
      </>}
    </div>
  );
}
