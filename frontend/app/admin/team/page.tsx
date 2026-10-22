"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import styles from "../leadgen/leadgen.module.css";

type Member = {
  id: number; username: string; display_name: string | null; active: boolean; is_me: boolean;
  telegram_linked: boolean; telegram_username: string | null; created_at: string | null; created_by: string | null;
  last_login_at: string | null; disabled_at: string | null; sessions: number;
};
type Log = { id: number; actor: string; action: string; action_name: string; target: string | null; created_at: string | null };
type Team = { members: Member[]; log: Log[]; password?: string };

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
const send = (path: string, body?: unknown, method = "POST") =>
  api<Team>(`/admin/team${path}`, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });

export default function TeamPage() {
  const [data, setData] = useState<Team | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [secret, setSecret] = useState<{ login: string; password: string } | null>(null);
  const [confirm, setConfirm] = useState<{ member: Member; action: "disable" | "reset" } | null>(null);
  const [pw, setPw] = useState({ current: "", next: "", again: "" });

  const load = useCallback(() => api<Team>("/admin/team").then(setData).catch((e) => setError((e as Error).message)), []);
  useEffect(() => { void load(); }, [load]);

  async function run(fn: () => Promise<Team>, done: string) {
    setBusy(true); setError(""); setNotice("");
    try { const r = await fn(); setData(r); setNotice(done); return r; }
    catch (e) { setError((e as Error).message); return null; }
    finally { setBusy(false); }
  }
  async function add(e: FormEvent) {
    e.preventDefault();
    const login = username.trim().toLowerCase();
    const r = await run(() => send("", { username: login, display_name: name.trim() || null }), "Сотрудник добавлен");
    if (r?.password) { setSecret({ login, password: r.password }); setAdding(false); setUsername(""); setName(""); }
  }
  async function confirmed() {
    if (!confirm) return;
    const { member, action } = confirm;
    setConfirm(null);
    if (action === "disable") await run(() => send(`/${member.id}/disable`), `Доступ ${member.display_name || member.username} отключён`);
    else {
      const r = await run(() => send(`/${member.id}/reset-password`), "Пароль сброшен");
      if (r?.password) setSecret({ login: member.username, password: r.password });
    }
  }
  async function changePassword(e: FormEvent) {
    e.preventDefault();
    if (pw.next.length < 12) return setError("Новый пароль — не короче 12 символов.");
    if (pw.next !== pw.again) return setError("Новые пароли не совпадают.");
    setBusy(true); setError(""); setNotice("");
    try {
      await api("/admin/team/me/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current: pw.current, new: pw.next }) });
      setPw({ current: "", next: "", again: "" }); setNotice("Пароль изменён. Другие ваши сеансы завершены."); void load();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  const active = data?.members.filter((m) => m.active).length ?? 0;

  return (
    <div className={`page ${styles.page}`}>
      <header className="topbar"><div className="crumbs"><span>StepToLead</span><b>/</b><strong>Команда агентства</strong></div><span className="userAvatar">ЕД</span></header>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Система</p>
          <h1>Команда агентства</h1>
          <p className="subtitle">Кто входит в админку. Каждый добавленный получает полный доступ — такой же, как у вас.</p>
        </div>
        <div className={styles.actions}><button type="button" className={styles.primary} onClick={() => setAdding(true)}>＋ Добавить сотрудника</button></div>
      </section>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {notice && <p className={styles.notice} role="status">{notice}</p>}

      <section className={styles.card}>
        <h2>Сотрудники {data && <span className={styles.muted}>· активных {active}</span>}</h2>
        {!data ? <p className={styles.muted} role="status">Загружаем…</p> : (
          <div className={styles.scroll}>
            <table className={styles.table}>
              <thead><tr><th>Сотрудник</th><th>Статус</th><th>Последний вход</th><th>Telegram-уведомления</th><th>Добавлен</th><th /></tr></thead>
              <tbody>
                {data.members.map((m) => (
                  <tr key={m.id} style={m.active ? undefined : { opacity: 0.6 }}>
                    <td><b>{m.display_name || m.username}{m.is_me ? " (вы)" : ""}</b><small>{m.username}</small></td>
                    <td>
                      <span className={`${styles.badge} ${m.active ? styles.fitYes : styles.fitNo}`}>{m.active ? "Активен" : "Отключён"}</span>
                      {m.active && <small>{m.sessions ? `открыт на ${m.sessions} устр.` : "не в сети"}</small>}
                      {!m.active && m.disabled_at && <small>с {when(m.disabled_at)}</small>}
                    </td>
                    <td>{when(m.last_login_at)}</td>
                    <td>{m.telegram_linked ? <>Подключён{m.telegram_username ? ` · @${m.telegram_username}` : ""}</>
                      : <span className={styles.muted}>{m.is_me ? <Link href="/admin/monitor">Подключить →</Link> : "Не подключён — сотрудник подключает сам в «Мониторинге»"}</span>}</td>
                    <td>{when(m.created_at)}<small>{m.created_by ? `добавил ${m.created_by}` : ""}</small></td>
                    <td>
                      <div className={styles.rowActions}>
                        {m.active && m.sessions > 0 && (
                          <button type="button" className={styles.secondary} disabled={busy}
                            onClick={() => run(() => send(`/${m.id}/logout-all`), m.is_me ? "Другие ваши сеансы завершены" : "Все сеансы завершены")}>
                            {m.is_me ? "Выйти на других устройствах" : "Завершить сеансы"}
                          </button>
                        )}
                        {!m.is_me && m.active && <button type="button" className={styles.secondary} disabled={busy} onClick={() => setConfirm({ member: m, action: "reset" })}>Сбросить пароль</button>}
                        {!m.is_me && m.active && <button type="button" className={`${styles.secondary} ${styles.danger}`} disabled={busy || active <= 1} onClick={() => setConfirm({ member: m, action: "disable" })}>Отключить</button>}
                        {!m.active && <button type="button" className={styles.secondary} disabled={busy} onClick={() => run(() => send(`/${m.id}/enable`), "Доступ возвращён")}>Вернуть доступ</button>}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className={styles.grid}>
        <section className={styles.card}>
          <h2>Журнал</h2>
          {!data?.log.length ? <p className={styles.muted}>Пока пусто.</p> : (
            <ul className={styles.history}>
              {data.log.map((r) => <li key={r.id}><b>{r.actor}</b> {r.action_name}{r.target ? ` · ${r.target}` : ""} <small className={styles.muted}>{when(r.created_at)}</small></li>)}
            </ul>
          )}
        </section>
        <section className={styles.card}>
          <h2>Мой пароль</h2>
          <form className={styles.fields} onSubmit={changePassword}>
            <label className={styles.field}>Текущий пароль<input type="password" autoComplete="current-password" required value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} /></label>
            <label className={styles.field}>Новый пароль<input type="password" autoComplete="new-password" minLength={12} required value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} /><small>Не короче 12 символов.</small></label>
            <label className={styles.field}>Ещё раз<input type="password" autoComplete="new-password" minLength={12} required value={pw.again} onChange={(e) => setPw({ ...pw, again: e.target.value })} /></label>
            <div className={styles.actions}><button type="submit" className={styles.primary} disabled={busy}>Сменить пароль</button></div>
          </form>
        </section>
      </div>

      {adding && <>
        <button type="button" className={styles.overlay} aria-label="Закрыть" tabIndex={-1} onClick={() => setAdding(false)} />
        <form role="dialog" aria-modal="true" aria-labelledby="add-member" className={`${styles.card} ${styles.modal}`} onSubmit={add}>
          <h2 id="add-member">Новый сотрудник</h2>
          <p className={styles.muted}>Получит полный доступ к админке. Пароль портал придумает сам и покажет один раз.</p>
          <div className={styles.fields}>
            <label className={styles.field}>Логин<input autoFocus required minLength={3} maxLength={254} placeholder="anna@steptolead.ru" value={username} onChange={(e) => setUsername(e.target.value)} /><small>Удобнее всего — рабочая почта.</small></label>
            <label className={styles.field}>Имя<input maxLength={120} placeholder="Анна, аккаунт-менеджер" value={name} onChange={(e) => setName(e.target.value)} /></label>
          </div>
          <div className={styles.actions}>
            <button type="submit" className={styles.primary} disabled={busy || username.trim().length < 3}>{busy ? "Добавляем…" : "Добавить"}</button>
            <button type="button" className={styles.secondary} onClick={() => setAdding(false)}>Отмена</button>
          </div>
        </form>
      </>}

      {confirm && <>
        <button type="button" className={styles.overlay} aria-label="Закрыть" tabIndex={-1} onClick={() => setConfirm(null)} />
        <div role="dialog" aria-modal="true" aria-labelledby="confirm-member" className={`${styles.card} ${styles.modal}`}>
          <h2 id="confirm-member">{confirm.action === "disable" ? "Отключить доступ?" : "Сбросить пароль?"}</h2>
          <p className={styles.muted}>
            {confirm.action === "disable"
              ? `${confirm.member.display_name || confirm.member.username} сразу выйдет из портала и не сможет войти. Вернуть доступ можно в любой момент.`
              : `Старый пароль перестанет работать, ${confirm.member.display_name || confirm.member.username} выйдет со всех устройств. Новый пароль покажем один раз.`}
          </p>
          <div className={styles.actions}>
            <button type="button" className={`${styles.primary}`} onClick={confirmed}>{confirm.action === "disable" ? "Отключить" : "Сбросить"}</button>
            <button type="button" className={styles.secondary} onClick={() => setConfirm(null)}>Отмена</button>
          </div>
        </div>
      </>}

      {secret && <>
        <div className={styles.overlay} />
        <div role="dialog" aria-modal="true" aria-labelledby="secret" className={`${styles.card} ${styles.modal}`}>
          <h2 id="secret">Данные для входа</h2>
          <p className={styles.muted}>Пароль показываем один раз — передайте его сотруднику лично или в защищённом мессенджере. Потом он может сменить его в «Команде».</p>
          <pre className={styles.payload}>{`Адрес: ${typeof window !== "undefined" ? window.location.origin : ""}/login\nЛогин: ${secret.login}\nПароль: ${secret.password}`}</pre>
          <div className={styles.actions}>
            <button type="button" className={styles.secondary} onClick={() => {
              void navigator.clipboard?.writeText(`Адрес: ${window.location.origin}/login\nЛогин: ${secret.login}\nПароль: ${secret.password}`).then(() => setNotice("Скопировано"));
            }}>Скопировать</button>
            <button type="button" className={styles.primary} onClick={() => setSecret(null)}>Готово, передал</button>
          </div>
        </div>
      </>}
    </div>
  );
}
