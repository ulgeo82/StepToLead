"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { base, date, message, post } from "../../leadgen/shared";
import styles from "../../leadgen/leadgen.module.css";
import {
  channelNames,
  emptyStep,
  emptyWindow,
  Enrollment,
  fieldForError,
  Mailbox,
  Sequence,
  statusNames,
  Step,
  stopReasons,
  timezones,
  Window,
} from "./types";

function EmailPage() {
  const params = useSearchParams(),
    router = useRouter();
  const tab = params.get("tab") === "sequences" ? "sequences" : "mailboxes";
  const [mailboxes, setMailboxes] = useState<Mailbox[]>([]),
    [sequences, setSequences] = useState<Sequence[]>([]),
    [variables, setVariables] = useState<string[]>([]);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true),
    [revision, setRevision] = useState(0);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [id, setId] = useState<number | null>(null),
    [name, setName] = useState(""),
    [window, setWindow] = useState<Window>(emptyWindow),
    [steps, setSteps] = useState<Step[]>([emptyStep()]),
    [active, setActive] = useState(true);
  const [enrollments, setEnrollments] = useState<Enrollment[]>([]),
    [participantsLoading, setParticipantsLoading] = useState(false);
  const [companyQuery, setCompanyQuery] = useState(""),
    [companies, setCompanies] = useState<{ id: number; display_name: string }[]>([]),
    [companyId, setCompanyId] = useState("");
  const [preview, setPreview] = useState<{ steps: Step[] } | null>(null);
  const editorRef = useRef<HTMLDivElement>(null),
    dragIndex = useRef<number | null>(null),
    target = useRef<{ index: number; field: "subject" | "body"; start: number; end: number } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    const path = tab === "mailboxes" ? "mailboxes" : "sequences";
    api<{ items: Mailbox[] | Sequence[]; variables?: string[] }>(`${base}/${path}`, {
      signal: controller.signal,
    })
      .then((data) => {
        if (controller.signal.aborted) return;
        if (tab === "mailboxes") setMailboxes(data.items as Mailbox[]);
        else {
          setSequences(data.items as Sequence[]);
          setVariables(data.variables || []);
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(message(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [tab, revision]);
  useEffect(() => {
    setEnrollments([]);
    if (!id || tab !== "sequences") return;
    const controller = new AbortController();
    setParticipantsLoading(true);
    api<{ items: Enrollment[] }>(`${base}/sequences/${id}/enrollments?limit=500`, {
      signal: controller.signal,
    })
      .then((data) => {
        if (!controller.signal.aborted) setEnrollments(data.items);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(message(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setParticipantsLoading(false);
      });
    return () => controller.abort();
  }, [id, tab, revision]);
  useEffect(() => {
    if (tab !== "sequences") return;
    const controller = new AbortController();
    const timer = setTimeout(
      () =>
        api<{ items: { id: number; display_name: string }[] }>(
          `${base}/companies?limit=50&q=${encodeURIComponent(companyQuery)}`,
          { signal: controller.signal },
        )
          .then((data) => {
            if (!controller.signal.aborted) setCompanies(data.items);
          })
          .catch((e) => {
            if (!controller.signal.aborted) setError(message(e));
          }),
      300,
    );
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [companyQuery, tab]);
  function fail(e: unknown) {
    const text = message(e);
    setError(text);
    setFields(Object.fromEntries(text.split("; ").map((part) => [fieldForError(part), part])));
  }
  async function mailboxAction(mailbox: Mailbox, action: "check" | "toggle" | "delete") {
    if (busy) return;
    if (action === "delete" && !globalThis.confirm(`Удалить ящик ${mailbox.email}?`)) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (action === "check") {
        const result = await post<{ ok: boolean; error?: string }>(`/mailboxes/${mailbox.id}/check`, {});
        if (result.ok) setNotice(`Ящик ${mailbox.email}: подключение работает.`);
        else setError(result.error || "Проверка ящика не прошла.");
      } else
        await api(`${base}/mailboxes/${mailbox.id}`, {
          method: action === "delete" ? "DELETE" : "PATCH",
          headers: { "Content-Type": "application/json" },
          ...(action === "toggle" ? { body: JSON.stringify({ is_active: !mailbox.is_active }) } : {}),
        });
      setRevision((n) => n + 1);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function addMailbox(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy) return;
    const form = e.currentTarget,
      data = new FormData(form);
    setBusy(true);
    setError("");
    setFields({});
    try {
      await post("/mailboxes", {
        email: data.get("email"),
        sender_name: data.get("sender_name") || null,
        smtp_host: data.get("smtp_host"),
        smtp_port: Number(data.get("smtp_port")),
        imap_host: data.get("imap_host"),
        imap_port: Number(data.get("imap_port")),
        login: data.get("login") || data.get("email"),
        password: data.get("password"),
        daily_limit: Number(data.get("daily_limit")),
        warmup: data.has("warmup"),
      });
      form.reset();
      setNotice("Ящик добавлен. Нажмите «Проверить» для проверки подключения.");
      setRevision((n) => n + 1);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  function loadSequence(sequence?: Sequence) {
    setId(sequence?.id || null);
    setName(sequence?.name || "");
    setSteps(sequence?.steps.map((step) => ({ ...step })) || [emptyStep()]);
    setWindow(sequence ? { ...emptyWindow(), ...sequence.window } : emptyWindow());
    setActive(sequence?.is_active ?? true);
    setFields({});
    setPreview(null);
    setError("");
    target.current = null;
  }
  function updateStep(index: number, key: keyof Step, value: string | number | boolean) {
    setSteps((list) => list.map((step, i) => (i === index ? { ...step, [key]: value } : step)));
    setPreview(null);
    setFields({});
  }
  function move(from: number, to: number) {
    if (to < 0 || to >= steps.length || from === to) return;
    setSteps((list) => {
      const next = [...list];
      const [step] = next.splice(from, 1);
      next.splice(to, 0, step);
      return next;
    });
    target.current = null;
    setPreview(null);
    setFields({});
  }
  function insert(variable: string) {
    const position = target.current;
    if (!position) {
      setNotice("Сначала поставьте курсор в тему или текст шага.");
      return;
    }
    setNotice("");
    const text = steps[position.index]?.[position.field] || "",
      token = `{{${variable}}}`;
    updateStep(
      position.index,
      position.field,
      text.slice(0, position.start) + token + text.slice(position.end),
    );
    const cursor = position.start + token.length;
    target.current = { ...position, start: cursor, end: cursor };
    requestAnimationFrame(() => {
      const input = editorRef.current?.querySelector<HTMLInputElement | HTMLTextAreaElement>(
        `[data-step="${position.index}"][data-field="${position.field}"]`,
      );
      input?.focus();
      input?.setSelectionRange(cursor, cursor);
    });
  }
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setFields({});
    setError("");
    setNotice("");
    if (window.start_hour >= window.end_hour || !window.weekdays.length) {
      setFields({ window: "Выберите дни и окно: начало должно быть раньше окончания." });
      return;
    }
    setBusy(true);
    try {
      const saved = await api<Sequence>(`${base}/sequences${id ? `/${id}` : ""}`, {
        method: id ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, steps, window, is_active: active }),
      });
      loadSequence(saved);
      setNotice("Цепочка сохранена.");
      setRevision((n) => n + 1);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function showPreview() {
    if (busy || !companyId) return;
    setBusy(true);
    setError("");
    setFields({});
    setPreview(null);
    try {
      setPreview(
        await post<{ steps: Step[] }>("/sequences/preview", { company_id: Number(companyId), steps }),
      );
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function participant(enrollment: Enrollment, action: string) {
    if (busy) return;
    if (action === "stop" && !globalThis.confirm(`Остановить цепочку для «${enrollment.company}»?`)) return;
    setBusy(true);
    setError("");
    try {
      await post(`/enrollments/${enrollment.id}`, { action });
      setRevision((n) => n + 1);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  const fieldError = (key: string) =>
    fields[key] ? (
      <small className={styles.error} role="alert">
        {fields[key]}
      </small>
    ) : null;
  return (
    <div className={`page ${styles.page}`}>
      <header className="topbar">
        <div className="crumbs">
          <span>StepToLead</span>
          <b>/</b>
          <strong>Аутрич</strong>
        </div>
      </header>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">Рост агентства</p>
          <h1>Email-кампании</h1>
          <p className="subtitle">Ящики, цепочки писем и быстрых действий, участники и ответы.</p>
        </div>
        <Link className={styles.secondary} href="/admin/leadgen/companies">
          База компаний →
        </Link>
      </section>
      <div className={styles.tabs}>
        {[
          ["mailboxes", "Ящики"],
          ["sequences", "Цепочки"],
        ].map(([value, label]) => (
          <button
            type="button"
            key={value}
            className={styles.tab}
            aria-pressed={tab === value}
            onClick={() => {
              setError("");
              setFields({});
              router.replace(`/admin/outreach/email?tab=${value}`, { scroll: false });
            }}
          >
            {label}
          </button>
        ))}
        <button type="button" className={styles.secondary} onClick={() => setRevision((n) => n + 1)}>
          Обновить
        </button>
      </div>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className={styles.notice} role="status">
          {notice}
        </p>
      )}
      {loading && <p role="status">Загружаем…</p>}
      {tab === "mailboxes" ? (
        <>
          <section className={styles.card}>
            <h2>Почтовые ящики</h2>
            {!mailboxes.length && !loading ? (
              <p className={styles.empty}>Ящиков пока нет. Добавьте отдельный ящик для аутрича.</p>
            ) : (
              <div className={styles.scroll}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      {[
                        "Email",
                        "Отправитель",
                        "Лимит",
                        "Сегодня / осталось",
                        "Прогрев с",
                        "Статус / ошибка",
                        "Действия",
                      ].map((title) => (
                        <th scope="col" key={title}>
                          {title}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {mailboxes.map((mailbox) => (
                      <tr key={mailbox.id}>
                        <td>{mailbox.email}</td>
                        <td>{mailbox.sender_name || "—"}</td>
                        <td>{mailbox.daily_limit}</td>
                        <td>
                          {mailbox.sent_today} / {mailbox.left_today}
                        </td>
                        <td>{mailbox.warmup_started_on || "Выключен"}</td>
                        <td>
                          {mailbox.is_active ? "Включён" : "Выключен"}
                          {mailbox.last_error && <p className={styles.error}>{mailbox.last_error}</p>}
                        </td>
                        <td>
                          <div className={styles.actions}>
                            <button
                              type="button"
                              className={styles.secondary}
                              disabled={busy}
                              onClick={() => mailboxAction(mailbox, "check")}
                            >
                              Проверить
                            </button>
                            <button
                              type="button"
                              className={styles.secondary}
                              disabled={busy}
                              onClick={() => mailboxAction(mailbox, "toggle")}
                            >
                              {mailbox.is_active ? "Выключить" : "Включить"}
                            </button>
                            <button
                              type="button"
                              className={styles.secondary}
                              disabled={busy}
                              onClick={() => mailboxAction(mailbox, "delete")}
                            >
                              Удалить
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <form className={styles.card} onSubmit={addMailbox} autoComplete="off">
            <h2>Добавить ящик</h2>
            <div className={styles.fields}>
              {[
                ["email", "Email", "email", ""],
                ["sender_name", "Имя отправителя", "text", ""],
                ["smtp_host", "SMTP host", "text", "smtp.yandex.ru"],
                ["smtp_port", "SMTP port", "number", "465"],
                ["imap_host", "IMAP host", "text", "imap.yandex.ru"],
                ["imap_port", "IMAP port", "number", "993"],
                ["login", "Логин (по умолчанию email)", "text", ""],
                ["password", "Пароль приложения", "password", ""],
                ["daily_limit", "Дневной лимит", "number", "30"],
              ].map(([key, label, type, value]) => (
                <label className={styles.field} key={key}>
                  {label}
                  <input
                    name={key}
                    type={type}
                    defaultValue={value}
                    placeholder={key === "login" ? "Будет использован email" : ""}
                    required={!["login", "sender_name"].includes(key)}
                    min={type === "number" ? 1 : undefined}
                    max={key === "daily_limit" ? 200 : type === "number" ? 65535 : undefined}
                    autoComplete={key === "password" ? "new-password" : "off"}
                    aria-invalid={!!fields[key]}
                  />
                  {fieldError(key)}
                </label>
              ))}
            </div>
            <label className={styles.check}>
              <input type="checkbox" name="warmup" defaultChecked />
              Прогрев
            </label>
            <p className={styles.muted}>Яндекс 360: smtp.yandex.ru / imap.yandex.ru, пароль приложения</p>
            <button type="submit" className={styles.primary} disabled={busy}>
              {busy ? "Сохраняем…" : "Добавить ящик"}
            </button>
          </form>
        </>
      ) : (
        <>
          <section className={styles.card}>
            <div className={styles.row}>
              <h2>Цепочки</h2>
              <button type="button" className={styles.primary} disabled={busy} onClick={() => loadSequence()}>
                Новая цепочка
              </button>
            </div>
            {!sequences.length && !loading && (
              <p className={styles.empty}>Цепочек пока нет. Создайте первую.</p>
            )}
            {sequences.map((sequence) => (
              <button
                type="button"
                key={sequence.id}
                className={styles.run}
                aria-pressed={id === sequence.id}
                disabled={busy}
                onClick={() => loadSequence(sequence)}
              >
                <strong>
                  {sequence.name} · {sequence.steps.length} шагов{!sequence.is_active ? " · Выключена" : ""}
                </strong>
                <span>
                  {Object.entries(sequence.counts)
                    .map(([status, count]) => `${statusNames[status] || status}: ${count}`)
                    .join(" · ") || "Участников пока нет"}
                </span>
              </button>
            ))}
          </section>
          <form className={styles.card} onSubmit={save}>
            <h2>{id ? "Редактор цепочки" : "Новая цепочка"}</h2>
            <label className={styles.field}>
              Название
              <input
                value={name}
                required
                maxLength={180}
                onChange={(e) => setName(e.target.value)}
                aria-invalid={!!fields.name}
              />
              {fieldError("name")}
            </label>
            <label className={styles.check}>
              <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
              Цепочка включена
            </label>
            <h3>Окно отправки</h3>
            <div className={styles.fields}>
              <label className={styles.field}>
                Часовой пояс
                <select value={window.tz} onChange={(e) => setWindow({ ...window, tz: e.target.value })}>
                  {!timezones.some(([tz]) => tz === window.tz) && (
                    <option value={window.tz}>{window.tz}</option>
                  )}
                  {timezones.map(([tz, label]) => (
                    <option key={tz} value={tz}>
                      {label}
                    </option>
                  ))}
                </select>
                {fieldError("window")}
              </label>
              {[
                ["start_hour", "С часа"],
                ["end_hour", "До часа"],
              ].map(([key, label]) => (
                <label className={styles.field} key={key}>
                  {label}
                  <input
                    type="number"
                    min={0}
                    max={key === "end_hour" ? 24 : 23}
                    required
                    value={window[key as "start_hour"]}
                    onChange={(e) => setWindow({ ...window, [key]: Number(e.target.value) })}
                  />
                </label>
              ))}
            </div>
            <div className={styles.row}>
              {["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((day, index) => (
                <label key={day} className={styles.check}>
                  <input
                    type="checkbox"
                    checked={window.weekdays.includes(index)}
                    onChange={(e) =>
                      setWindow({
                        ...window,
                        weekdays: e.target.checked
                          ? [...window.weekdays, index]
                          : window.weekdays.filter((d) => d !== index),
                      })
                    }
                  />
                  {day}
                </label>
              ))}
            </div>
            <h3>Переменные</h3>
            <p className={styles.muted}>Поставьте курсор в теме или тексте, затем нажмите переменную.</p>
            <div className={styles.actions}>
              {variables.map((variable) => (
                <button
                  key={variable}
                  type="button"
                  className={styles.secondary}
                  onClick={() => insert(variable)}
                >{`{{${variable}}}`}</button>
              ))}
            </div>
            <div ref={editorRef}>
              {steps.map((step, index) => (
                <section
                  key={index}
                  className={styles.card}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => {
                    e.preventDefault();
                    if (dragIndex.current !== null) move(dragIndex.current, index);
                    dragIndex.current = null;
                  }}
                >
                  <div className={styles.row}>
                    <strong
                      draggable
                      onDragStart={(e) => {
                        dragIndex.current = index;
                        e.dataTransfer.effectAllowed = "move";
                        e.dataTransfer.setData("text/plain", String(index));
                      }}
                      onDragEnd={() => {
                        dragIndex.current = null;
                      }}
                      style={{ cursor: "grab" }}
                    >
                      ⠿ Шаг {index + 1}
                    </strong>
                    <button
                      type="button"
                      className={styles.secondary}
                      aria-label={`Шаг ${index + 1} вверх`}
                      disabled={index === 0}
                      onClick={() => move(index, index - 1)}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className={styles.secondary}
                      aria-label={`Шаг ${index + 1} вниз`}
                      disabled={index === steps.length - 1}
                      onClick={() => move(index, index + 1)}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      className={styles.secondary}
                      disabled={steps.length === 1}
                      onClick={() => {
                        setSteps((list) => list.filter((_, i) => i !== index));
                        target.current = null;
                        setPreview(null);
                        setFields({});
                      }}
                    >
                      Удалить шаг
                    </button>
                  </div>
                  <div className={styles.fields}>
                    <label className={styles.field}>
                      Канал
                      <select
                        value={step.channel}
                        onChange={(e) => updateStep(index, "channel", e.target.value)}
                      >
                        {Object.entries(channelNames).map(([key, label]) => (
                          <option key={key} value={key}>
                            {label}
                          </option>
                        ))}
                      </select>
                      {fieldError(`steps.${index}.channel`)}
                    </label>
                    <label className={styles.field}>
                      Через N дней
                      <input
                        type="number"
                        min={0}
                        max={60}
                        required
                        value={step.delay_days}
                        onChange={(e) => updateStep(index, "delay_days", Number(e.target.value))}
                      />
                      {fieldError(`steps.${index}.delay_days`)}
                    </label>
                    {step.channel === "email" && (
                      <label className={`${styles.field} ${styles.wide}`}>
                        Тема
                        <input
                          data-step={index}
                          data-field="subject"
                          value={step.subject || ""}
                          placeholder={index > 0 ? "Без темы — ответ Re: …" : "Тема первого письма"}
                          onFocus={(e) => {
                            const el = e.currentTarget;
                            target.current = {
                              index,
                              field: "subject",
                              start: el.selectionStart || 0,
                              end: el.selectionEnd || 0,
                            };
                          }}
                          onSelect={(e) => {
                            const el = e.currentTarget;
                            target.current = {
                              index,
                              field: "subject",
                              start: el.selectionStart || 0,
                              end: el.selectionEnd || 0,
                            };
                          }}
                          onChange={(e) => {
                            target.current = {
                              index,
                              field: "subject",
                              start: e.target.selectionStart || 0,
                              end: e.target.selectionEnd || 0,
                            };
                            updateStep(index, "subject", e.target.value);
                          }}
                          aria-invalid={!!fields[`steps.${index}.subject`]}
                        />
                        {fieldError(`steps.${index}.subject`)}
                      </label>
                    )}
                    <label className={`${styles.field} ${styles.wide}`}>
                      {step.channel === "email" ? "Текст письма" : "Текст действия"}
                      <textarea
                        data-step={index}
                        data-field="body"
                        required={step.channel === "email"}
                        value={step.body}
                        onFocus={(e) => {
                          const el = e.currentTarget;
                          target.current = {
                            index,
                            field: "body",
                            start: el.selectionStart || 0,
                            end: el.selectionEnd || 0,
                          };
                        }}
                        onSelect={(e) => {
                          const el = e.currentTarget;
                          target.current = {
                            index,
                            field: "body",
                            start: el.selectionStart || 0,
                            end: el.selectionEnd || 0,
                          };
                        }}
                        onChange={(e) => {
                          target.current = {
                            index,
                            field: "body",
                            start: e.target.selectionStart || 0,
                            end: e.target.selectionEnd || 0,
                          };
                          updateStep(index, "body", e.target.value);
                        }}
                        aria-invalid={!!fields[`steps.${index}.body`]}
                      />
                      {fieldError(`steps.${index}.body`)}
                    </label>
                  </div>
                  {step.channel === "email" && index > 0 && (
                    <label className={styles.check}>
                      <input
                        type="checkbox"
                        checked={step.new_thread}
                        onChange={(e) => updateStep(index, "new_thread", e.target.checked)}
                      />
                      Начать новую ветку (тема обязательна)
                    </label>
                  )}
                </section>
              ))}
            </div>
            {fieldError("steps")}
            <div className={styles.actions}>
              <button
                type="button"
                className={styles.secondary}
                disabled={steps.length >= 10}
                onClick={() => {
                  setSteps((list) => [...list, emptyStep()]);
                  setPreview(null);
                }}
              >
                ＋ Шаг
              </button>
              <button type="submit" className={styles.primary} disabled={busy}>
                {busy ? "Сохраняем…" : "Сохранить цепочку"}
              </button>
            </div>
          </form>
          <section className={styles.card}>
            <h2>Предпросмотр</h2>
            <div className={styles.fields}>
              <label className={styles.field}>
                Найти компанию
                <input value={companyQuery} onChange={(e) => setCompanyQuery(e.target.value)} />
              </label>
              <label className={styles.field}>
                Компания
                <select
                  value={companyId}
                  onChange={(e) => {
                    setCompanyId(e.target.value);
                    setPreview(null);
                  }}
                >
                  <option value="">Выберите из базы</option>
                  {companies.map((company) => (
                    <option key={company.id} value={company.id}>
                      {company.display_name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <button
              type="button"
              className={styles.secondary}
              disabled={busy || !companyId}
              onClick={showPreview}
            >
              Предпросмотр
            </button>
            {preview?.steps.map((step, index) => (
              <article className={styles.ad} key={index}>
                <h3>
                  Шаг {index + 1} · {channelNames[step.channel]}
                </h3>
                {step.channel === "email" && <strong>{step.subject || "Re: тема предыдущего письма"}</strong>}
                <p style={{ whiteSpace: "pre-wrap" }}>{step.body}</p>
              </article>
            ))}
          </section>
          {id && (
            <section className={styles.card}>
              <h2>Участники цепочки</h2>
              {participantsLoading ? (
                <p role="status">Загружаем участников…</p>
              ) : !enrollments.length ? (
                <p className={styles.empty}>Участников пока нет. Запустите цепочку из базы компаний.</p>
              ) : (
                <div className={styles.scroll}>
                  <table className={styles.table}>
                    <thead>
                      <tr>
                        {["Компания", "Шаг", "Следующее касание", "Статус", "Действия"].map((title) => (
                          <th scope="col" key={title}>
                            {title}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {enrollments.map((enrollment) => (
                        <tr key={enrollment.id}>
                          <td>
                            <Link href={`/admin/leadgen/companies?company=${enrollment.company_id}`}>
                              {enrollment.company}
                            </Link>
                          </td>
                          <td>
                            {Math.min(enrollment.current_step + 1, enrollment.steps_total)} из{" "}
                            {enrollment.steps_total}
                          </td>
                          <td>{date(enrollment.next_at)}</td>
                          <td>
                            {statusNames[enrollment.status] || enrollment.status}
                            {enrollment.stop_reason && (
                              <small>
                                {" "}
                                · {stopReasons[enrollment.stop_reason] || enrollment.stop_reason}
                              </small>
                            )}
                          </td>
                          <td>
                            <div className={styles.actions}>
                              {enrollment.status === "active" && (
                                <button
                                  type="button"
                                  className={styles.secondary}
                                  disabled={busy}
                                  onClick={() => participant(enrollment, "pause")}
                                >
                                  Пауза
                                </button>
                              )}
                              {enrollment.status === "paused" && (
                                <button
                                  type="button"
                                  className={styles.secondary}
                                  disabled={busy}
                                  onClick={() => participant(enrollment, "resume")}
                                >
                                  Продолжить
                                </button>
                              )}
                              {["active", "paused"].includes(enrollment.status) && (
                                <button
                                  type="button"
                                  className={styles.secondary}
                                  disabled={busy}
                                  onClick={() => participant(enrollment, "stop")}
                                >
                                  Остановить
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
export default function Page() {
  return (
    <Suspense fallback={<p role="status">Загружаем Email-кампании…</p>}>
      <EmailPage />
    </Suspense>
  );
}
